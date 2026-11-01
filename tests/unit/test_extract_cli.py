"""itr extract, with the Claude client replaced by FakeLLM."""

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from itr.application.ports import LLMClient
from itr.domain.form16 import Form16
from itr.domain.rules import TaxRules
from itr.infrastructure.pdf.form16_renderer import render_form16
from itr.interfaces.cli import main as cli
from itr.settings import Settings, get_settings
from itr.synthetic.form16_generator import generate
from tests.fakes import FakeLLM, perfect_extraction

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def use_fake(monkeypatch: pytest.MonkeyPatch, llm: FakeLLM) -> None:
    def make_llm(settings: Settings, use_disk_cache: bool) -> LLMClient:
        return llm

    monkeypatch.setattr(cli, "make_llm", make_llm)


def write_case(
    tmp_path: Path, rules: TaxRules, with_truth: bool
) -> tuple[Path, Form16]:
    form16, _ = generate(5, rules)
    pdf = tmp_path / "F001.pdf"
    pdf.write_bytes(render_form16(form16))
    if with_truth:
        (tmp_path / "F001.truth.json").write_text(form16.model_dump_json())
    return pdf, form16


def test_prints_json_cost_and_a_clean_diff(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf, form16 = write_case(tmp_path, rules, with_truth=True)
    llm = FakeLLM([perfect_extraction(form16)])
    use_fake(monkeypatch, llm)

    assert cli.main(["extract", str(pdf)]) == 0

    out = capsys.readouterr().out
    assert '"employee_pan"' in out
    assert "Cost: $0.000000 (input 0, output 0" in out
    assert "Truth: " in out and "fields match" in out
    matched, compared = out.split("Truth: ")[1].split(" fields")[0].split("/")
    assert matched == compared
    assert "FORM NO. 16" in llm.calls[0].prompt  # the PDF's text was sent


def test_lists_each_mismatch(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf, form16 = write_case(tmp_path, rules, with_truth=True)
    answer = perfect_extraction(form16)
    rows = [
        r.model_copy(update={"amount": "999"}) if r.line == "15" else r
        for r in answer.rows
    ]
    use_fake(monkeypatch, FakeLLM([answer.model_copy(update={"rows": rows})]))

    assert cli.main(["extract", str(pdf)]) == 0

    out = capsys.readouterr().out
    assert "part_b.tax.surcharge: expected" in out
    assert "got 999" in out


def test_reads_the_tagged_truth_envelope(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf, form16 = write_case(tmp_path, rules, with_truth=False)
    envelope = {
        "tag": {"case_id": "F001"},
        "form16": json.loads(form16.model_dump_json()),
    }
    (tmp_path / "F001.truth.json").write_text(json.dumps(envelope))
    use_fake(monkeypatch, FakeLLM([perfect_extraction(form16)]))

    assert cli.main(["extract", str(pdf)]) == 0
    assert "fields match" in capsys.readouterr().out


def test_no_truth_file_means_no_diff(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf, form16 = write_case(tmp_path, rules, with_truth=False)
    use_fake(monkeypatch, FakeLLM([perfect_extraction(form16)]))

    assert cli.main(["extract", str(pdf)]) == 0
    assert "Truth:" not in capsys.readouterr().out


def test_invalid_extraction_exits_1_with_the_problems(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf, form16 = write_case(tmp_path, rules, with_truth=False)
    answer = perfect_extraction(form16)
    identity = answer.identity.model_copy(update={"employee_pan": "NOTAPAN"})
    use_fake(monkeypatch, FakeLLM([answer.model_copy(update={"identity": identity})]))

    assert cli.main(["extract", str(pdf)]) == 1
    assert "part_a.employee_pan" in capsys.readouterr().err


def test_missing_file_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["extract", str(tmp_path / "nope.pdf")]) == 1
    assert "itr:" in capsys.readouterr().err


def test_a_document_that_is_not_a_form16_exits_1(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pdf, form16 = write_case(tmp_path, rules, with_truth=False)
    answer = perfect_extraction(form16).model_copy(update={"document_type": "other"})
    use_fake(monkeypatch, FakeLLM([answer]))

    assert cli.main(["extract", str(pdf)]) == 1
    assert "not a Form 16" in capsys.readouterr().err


def test_unsupported_items_are_reported(
    tmp_path: Path,
    rules: TaxRules,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from itr.application.form16_extraction import UnsupportedItem

    pdf, form16 = write_case(tmp_path, rules, with_truth=False)
    item = UnsupportedItem(
        where="Annexure", description="LTCG u/s 112A", amount="250000"
    )
    answer = perfect_extraction(form16).model_copy(update={"unsupported_items": [item]})
    use_fake(monkeypatch, FakeLLM([answer]))

    assert cli.main(["extract", str(pdf)]) == 0
    assert "Unsupported: LTCG u/s 112A (250000) in Annexure" in capsys.readouterr().out
