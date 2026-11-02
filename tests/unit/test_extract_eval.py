"""Scoring extraction against truth, with FakeLLM standing in for Claude."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from itr.application.extract_eval import build_report, evaluate_document
from itr.application.form16_extraction import ExtractedForm16, LineId, Row
from itr.application.ports import LLMClient, LLMOutputError, LLMResult, LLMUsage
from itr.domain.form16 import Form16
from itr.domain.rules import TaxRules
from itr.infrastructure.pdf.form16_renderer import render_form16
from itr.interfaces.cli import main as cli
from itr.settings import Settings, get_settings
from itr.synthetic.dataset import _with_capital_gains
from itr.synthetic.form16_generator import generate
from tests.fakes import FakeLLM, perfect_extraction

ROOT = Path(__file__).parents[2]
PROMPT = "system prompt"


def usage(cost: str, from_disk_cache: bool = False) -> LLMUsage:
    return LLMUsage(
        input_tokens=2000,
        output_tokens=2500,
        cache_write_tokens=0,
        cache_read_tokens=7800,
        cost_usd=Decimal(cost),
        from_disk_cache=from_disk_cache,
    )


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


@pytest.fixture(scope="module")
def truth(rules: TaxRules) -> Form16:
    return generate(3, rules)[0]


def with_amount(
    extracted: ExtractedForm16, line: LineId, amount: str
) -> ExtractedForm16:
    """A copy with one single-amount line set to `amount`."""
    rows = [r for r in extracted.rows if r.line != line]
    rows.append(Row(line=line, amount=amount))
    return extracted.model_copy(update={"rows": rows})


@dataclass
class FailingLLM:
    """A client whose reply never fits the schema."""

    def structured[T: BaseModel](
        self, system: str, prompt: str, output_model: type[T]
    ) -> LLMResult[T]:
        raise LLMOutputError("reply didn't match ExtractedForm16: EOF while parsing")


# --- One document ---------------------------------------------------------------


def test_a_perfect_extraction_has_no_mismatches(truth: Form16) -> None:
    result = evaluate_document(
        "F001", "text", truth, FakeLLM([perfect_extraction(truth)]), PROMPT
    )
    assert result.extracted
    assert result.mismatches == ()
    assert result.wrong_fields == set()
    assert len(result.fields) > 80
    assert not any(f.startswith("part_a.quarters") for f in result.fields)


def test_a_wrong_amount_is_one_mismatch(truth: Form16) -> None:
    answer = with_amount(perfect_extraction(truth), "15", "999")
    result = evaluate_document("F001", "text", truth, FakeLLM([answer]), PROMPT)

    [mismatch] = result.mismatches
    assert mismatch.path == "part_b.tax.surcharge"
    assert mismatch.actual == Decimal(999)
    assert result.wrong_fields == {"part_b.tax.surcharge"}


def test_a_rejected_extraction_counts_every_field_wrong_and_keeps_its_cost(
    truth: Form16,
) -> None:
    answer = perfect_extraction(truth).model_copy(
        update={"needs_review": ["line 16: unreadable"]}
    )
    llm = FakeLLM([answer], usage=usage("0.0016"))
    result = evaluate_document("F001", "text", truth, llm, PROMPT)

    assert not result.extracted
    assert result.wrong_fields == set(result.fields)
    assert result.problems == ("flagged for review: line 16: unreadable",)
    assert result.usage is not None and result.usage.cost_usd == Decimal("0.0016")


def test_a_reply_that_does_not_fit_counts_every_field_wrong(truth: Form16) -> None:
    llm: LLMClient = FailingLLM()
    result = evaluate_document("F001", "text", truth, llm, PROMPT)

    assert not result.extracted
    assert "EOF while parsing" in (result.error or "")
    assert result.usage is None
    assert result.wrong_fields == set(result.fields)


# --- The report -----------------------------------------------------------------


def test_report_scores_fields_documents_and_cost(truth: Form16) -> None:
    perfect = perfect_extraction(truth)
    results = [
        evaluate_document(
            "F001", "t", truth, FakeLLM([perfect], usage=usage("0.0026")), PROMPT
        ),
        evaluate_document(
            "F002",
            "t",
            truth,
            FakeLLM([with_amount(perfect, "15", "999")], usage=usage("0.0016")),
            PROMPT,
        ),
        evaluate_document(
            "F003",
            "t",
            truth,
            FakeLLM([perfect], usage=usage("0.0018", from_disk_cache=True)),
            PROMPT,
        ),
        evaluate_document("F004", "t", truth, FailingLLM(), PROMPT),
    ]
    report = build_report(results, {"date": "2026-10-31", "model": "m"})

    assert report["date"] == "2026-10-31"
    assert report["documents"] == 4
    assert report["documents_extracted"] == 3
    assert report["documents_fully_correct"] == 2
    assert report["fully_correct_share"] == 0.5

    surcharge = report["field_accuracy"]["part_b.tax.surcharge"]
    assert surcharge == {"correct": 2, "documents": 4, "accuracy": 0.5}
    pan = report["field_accuracy"]["part_a.employee_pan"]
    assert pan == {"correct": 3, "documents": 4, "accuracy": 0.75}  # F004 failed
    assert "part_b.tax.surcharge" in report["fields_below_100_percent"]

    assert report["failures"] == [
        {
            "document": "F002",
            "field": "part_b.tax.surcharge",
            "expected": str(truth.part_b.tax.surcharge),
            "extracted": "999",
        }
    ]
    [failed] = report["failed_extractions"]
    assert failed["document"] == "F004"

    cost = report["cost"]
    assert cost["total_usd"] == "0.006000"  # 0.0026 + 0.0016 + 0.0018
    assert cost["average_usd_per_document"] == "0.002000"  # over the 3 with a reply
    assert cost["charged_this_run_usd"] == "0.004200"  # F003 came from disk
    assert cost["documents_from_disk_cache"] == 1
    assert cost["tokens"]["output"] == 7500


def test_report_is_json_serialisable(truth: Form16) -> None:
    result = evaluate_document(
        "F001", "t", truth, FakeLLM([perfect_extraction(truth)]), PROMPT
    )
    json.dumps(build_report([result], {}))


# --- The command ----------------------------------------------------------------


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_eval_extract_writes_a_dated_report(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api_key: None,
) -> None:
    data, out = tmp_path / "data", tmp_path / "reports"
    data.mkdir()
    answers = []
    for number, seed in [(1, 42), (2, 43)]:
        form16, _ = generate(seed, rules)
        (data / f"F{number:03d}.pdf").write_bytes(render_form16(form16))
        (data / f"F{number:03d}.truth.json").write_text(form16.model_dump_json())
        answers.append(perfect_extraction(form16))
    answers[1] = with_amount(answers[1], "15", "999")
    # A PDF without a truth file is listed as skipped, not scored.
    (data / "F003.pdf").write_bytes(render_form16(generate(44, rules)[0]))

    llm = FakeLLM(answers)

    def make_llm(settings: Settings, use_disk_cache: bool) -> LLMClient:
        return llm

    monkeypatch.setattr(cli, "make_llm", make_llm)

    assert cli.main(["eval-extract", str(data), "--out", str(out)]) == 0

    [path] = out.glob("extract-*.json")
    assert path.name.startswith("extract-20") and path.suffix == ".json"
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["documents"] == 2
    assert report["documents_fully_correct"] == 1
    assert report["skipped"] == ["F003.pdf"]
    assert [f["document"] for f in report["failures"]] == ["F002"]
    assert len(llm.calls) == 2

    printed = capsys.readouterr().out
    assert "F001: ok" in printed
    assert "F002: FAIL" in printed
    assert "1/2 documents fully correct" in printed
    assert f"Report: {path}" in printed


def test_eval_extract_needs_pdfs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], api_key: None
) -> None:
    assert cli.main(["eval-extract", str(tmp_path)]) == 1
    assert "no PDFs" in capsys.readouterr().err


# --- Refusals -------------------------------------------------------------------


def test_a_payslip_refused_as_not_a_form16_is_correct(truth: Form16) -> None:
    answer = perfect_extraction(truth).model_copy(update={"document_type": "other"})
    result = evaluate_document(
        "F020", "payslip", None, FakeLLM([answer]), PROMPT, "not_a_form16"
    )
    assert result.extracted
    assert result.refusal == "not_a_form16"
    assert result.fully_correct
    assert result.fields == ()


def test_a_form16_invented_from_a_payslip_is_a_missed_refusal(truth: Form16) -> None:
    result = evaluate_document(
        "F020",
        "payslip",
        None,
        FakeLLM([perfect_extraction(truth)]),
        PROMPT,
        "not_a_form16",
    )
    assert result.refusal is None
    assert not result.refusal_correct
    assert not result.fully_correct


def test_capital_gains_reported_as_unsupported_is_correct(rules: TaxRules) -> None:
    truth_cg = _with_capital_gains(generate(5, rules)[0], 5)
    result = evaluate_document(
        "F019",
        "text",
        truth_cg,
        FakeLLM([perfect_extraction(truth_cg)]),
        PROMPT,
        "unsupported_items",
    )
    assert result.refusal == "unsupported_items"
    assert result.mismatches == ()
    assert result.fully_correct


def test_capital_gains_left_out_is_a_missed_refusal(rules: TaxRules) -> None:
    truth_cg = _with_capital_gains(generate(5, rules)[0], 5)
    answer = perfect_extraction(truth_cg).model_copy(update={"unsupported_items": []})
    result = evaluate_document(
        "F019", "text", truth_cg, FakeLLM([answer]), PROMPT, "unsupported_items"
    )
    assert result.refusal is None
    assert not result.refusal_correct
    assert "unrecognised_lines[0].amount" in result.wrong_fields


def test_refusing_a_real_form16_is_a_false_refusal(truth: Form16) -> None:
    answer = perfect_extraction(truth).model_copy(update={"document_type": "other"})
    result = evaluate_document("F001", "text", truth, FakeLLM([answer]), PROMPT)

    assert not result.extracted  # a real Form 16 came back with nothing
    assert result.refusal == "not_a_form16"
    assert result.wrong_fields == set(result.fields)


def test_report_counts_refusals(truth: Form16, rules: TaxRules) -> None:
    truth_cg = _with_capital_gains(generate(5, rules)[0], 5)
    other = perfect_extraction(truth).model_copy(update={"document_type": "other"})
    results = [
        evaluate_document(  # correct refusal
            "F020", "p", None, FakeLLM([other]), PROMPT, "not_a_form16"
        ),
        evaluate_document(  # missed: capital gains not reported
            "F019",
            "t",
            truth_cg,
            FakeLLM(
                [
                    perfect_extraction(truth_cg).model_copy(
                        update={"unsupported_items": []}
                    )
                ]
            ),
            PROMPT,
            "unsupported_items",
        ),
        evaluate_document(  # false: a real Form 16 refused
            "F001", "t", truth, FakeLLM([other]), PROMPT
        ),
    ]
    report = build_report(results, {})

    assert report["refusals"] == {
        "expected": 2,
        "correct": 1,
        "missed": [{"document": "F019", "expected": "unsupported_items", "got": None}],
        "false": [{"document": "F001", "got": "not_a_form16"}],
    }
    assert report["documents_fully_correct"] == 1  # only F020


def test_eval_extract_scores_the_tagged_eval_set(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api_key: None,
) -> None:
    """synth-eval's tagged truth files drive refusal scoring end to end."""
    data, out = tmp_path / "data", tmp_path / "reports"
    assert cli.main(["synth-eval", "--out", str(data)]) == 0
    capsys.readouterr()

    answers: list[BaseModel] = []
    for path in sorted(data.glob("*.truth.json")):
        truth_file = json.loads(path.read_text(encoding="utf-8"))
        if truth_file["form16"] is None:  # the payslip: say it isn't a Form 16
            stand_in = perfect_extraction(generate(1, rules)[0])
            answers.append(stand_in.model_copy(update={"document_type": "other"}))
        else:
            answers.append(
                perfect_extraction(Form16.model_validate(truth_file["form16"]))
            )

    llm = FakeLLM(answers)

    def make_llm(settings: Settings, use_disk_cache: bool) -> LLMClient:
        return llm

    monkeypatch.setattr(cli, "make_llm", make_llm)
    assert cli.main(["eval-extract", str(data), "--out", str(out)]) == 0

    [path] = out.glob("extract-*.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["documents"] == 20
    assert report["documents_fully_correct"] == 20
    assert report["refusals"]["expected"] == 2
    assert report["refusals"]["correct"] == 2
    printed = capsys.readouterr().out
    assert "F019: ok (refused: unsupported_items)" in printed
    assert "F020: ok (refused: not_a_form16)" in printed
    assert "refusals 2/2 correct, 0 false" in printed


# --- Retries ----------------------------------------------------------------------


def test_report_records_retries(truth: Form16) -> None:
    perfect = perfect_extraction(truth)
    bad = perfect.model_copy(
        update={"identity": perfect.identity.model_copy(update={"employee_pan": "X"})}
    )
    results = [
        evaluate_document("F001", "t", truth, FakeLLM([perfect]), PROMPT),
        evaluate_document("F002", "t", truth, FakeLLM([bad, perfect]), PROMPT),
        evaluate_document("F003", "t", truth, FakeLLM([bad, bad]), PROMPT),
    ]
    assert [r.attempts for r in results] == [1, 2, 2]

    report = build_report(results, {})
    assert report["retries"] == {
        "documents_retried": 2,
        "succeeded_after_retry": 1,
        "documents": [
            {"document": "F002", "extracted": True},
            {"document": "F003", "extracted": False},
        ],
    }
    assert report["documents_fully_correct"] == 2  # F002 counts once it succeeds


# --- Token averages and report names ------------------------------------------


def test_report_gives_average_tokens_per_document(truth: Form16) -> None:
    results = [
        evaluate_document(
            name,
            "t",
            truth,
            FakeLLM([perfect_extraction(truth)], usage=usage("0.001")),
            PROMPT,
        )
        for name in ("F001", "F002")
    ]
    tokens = build_report(results, {})["cost"]["average_tokens_per_document"]
    assert tokens == {"input": 2000.0, "output": 2500.0}


def test_label_goes_into_the_report_name(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api_key: None,
) -> None:
    data, out = tmp_path / "data", tmp_path / "reports"
    data.mkdir()
    form16, _ = generate(42, rules)
    (data / "F001.pdf").write_bytes(render_form16(form16))
    (data / "F001.truth.json").write_text(form16.model_dump_json())
    llm = FakeLLM([perfect_extraction(form16)])
    monkeypatch.setattr(cli, "make_llm", lambda settings, use_disk_cache: llm)

    assert (
        cli.main(["eval-extract", str(data), "--out", str(out), "--label", "after"])
        == 0
    )

    [path] = out.glob("extract-*-after.json")
    assert "Tokens per document:" in capsys.readouterr().out
    assert json.loads(path.read_text())["documents"] == 1
