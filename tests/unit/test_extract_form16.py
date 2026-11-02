"""The extract use case, with FakeLLM standing in for Claude."""

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter, ValidationError

from itr.application.extract_form16 import (
    AMOUNT_LINES,
    BREAKUP_LINES,
    DEDUCTION_LINES,
    NOT_EXTRACTED,
    Form16ValidationError,
    NeedsReviewError,
    NotAForm16Error,
    doubtful_cells,
    extract_form16,
    to_form16,
)
from itr.application.form16_diff import diff_form16
from itr.application.form16_extraction import (
    DeductionLineId,
    DeductionRow,
    ExtractedForm16,
    LineId,
    Row,
    UnsupportedItem,
)
from itr.application.ports import LLMUsage
from itr.domain.form16 import Form16
from itr.domain.form16_mapping import to_tax_input
from itr.domain.rules import TaxRules
from itr.synthetic.dataset import _with_capital_gains
from itr.synthetic.form16_generator import generate
from tests.fakes import FakeLLM, perfect_extraction

ROOT = Path(__file__).parents[2]
PROMPT = (ROOT / "prompts/extract_form16.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


@pytest.fixture(scope="module")
def truth(rules: TaxRules) -> Form16:
    return generate(3, rules)[0]


def without_unextracted(form16: Form16) -> Form16:
    """The truth, minus what the extraction doesn't read."""
    b = form16.part_b
    return form16.model_copy(
        update={
            "part_a": form16.part_a.model_copy(
                update={"quarters": (), "employer_address": "", "employee_address": ""}
            ),
            "part_b": b.model_copy(
                update={
                    "section_10": b.section_10.model_copy(update={"other": ()}),
                    "chapter_via": b.chapter_via.model_copy(update={"other": ()}),
                }
            ),
            "unrecognised_lines": (),
        }
    )


def with_rows(extracted: ExtractedForm16, rows: list[Row]) -> ExtractedForm16:
    return extracted.model_copy(update={"rows": rows})


def set_row(extracted: ExtractedForm16, line: str, amount: str) -> ExtractedForm16:
    """A copy with one row's amount changed."""
    rows = [
        r.model_copy(update={"amount": amount}) if r.line == line else r
        for r in extracted.rows
    ]
    return with_rows(extracted, rows)


def drop_row(extracted: ExtractedForm16, line: str) -> ExtractedForm16:
    return with_rows(extracted, [r for r in extracted.rows if r.line != line])


def set_deduction(
    extracted: ExtractedForm16, line: str, **cells: str
) -> ExtractedForm16:
    """A copy with some cells of one deduction row changed."""
    deductions = [
        r.model_copy(update=cells) if r.line == line else r
        for r in extracted.deductions
    ]
    return extracted.model_copy(update={"deductions": deductions})


def set_identity(extracted: ExtractedForm16, **changes: Any) -> ExtractedForm16:
    identity = extracted.identity.model_copy(update=changes)
    return extracted.model_copy(update={"identity": identity})


def problems_of(extracted: ExtractedForm16) -> list[str]:
    with pytest.raises(Form16ValidationError) as caught:
        to_form16(extracted)
    assert caught.value.extracted == extracted
    return caught.value.problems


# --- The schema fits structured outputs -----------------------------------------


def _objects(schema: dict[str, Any]) -> list[dict[str, Any]]:
    found = [schema] if "properties" in schema else []
    for definition in schema.get("$defs", {}).values():
        found += _objects(definition)
    return found


def test_schema_has_no_optional_fields_and_no_anyof() -> None:
    schema = TypeAdapter(ExtractedForm16).json_schema()
    assert "anyOf" not in json.dumps(schema)
    for obj in _objects(schema):
        assert set(obj["required"]) == set(obj["properties"]), obj["title"]
        assert obj["additionalProperties"] is False, obj["title"]


def test_schema_stays_small() -> None:
    """The named-field schema (~110 fields) was rejected as too large to
    compile. Keep the row schema to a handful of objects and fields."""
    schema = TypeAdapter(ExtractedForm16).json_schema()
    objects = _objects(schema)
    assert len(objects) <= 5  # root, identity, row, deduction row, unsupported item
    assert sum(len(obj["properties"]) for obj in objects) <= 30


def test_every_line_id_has_one_place_in_the_form16() -> None:
    tables = [set(AMOUNT_LINES), set(BREAKUP_LINES)]
    assert set().union(*tables) == set(get_args(LineId))
    assert sum(len(t) for t in tables) == len(get_args(LineId))  # no overlaps
    assert set(DEDUCTION_LINES) == set(get_args(DeductionLineId))
    paths = list(AMOUNT_LINES.values()) + list(DEDUCTION_LINES.values())
    assert len(paths) == len(set(paths))  # no two lines fill the same field


def test_prompt_has_the_rules_and_is_long_enough_to_cache() -> None:
    lowered = PROMPT.lower()
    assert "never calculate" in lowered  # copy numbers, never compute them
    assert "115BAC(1A)" in PROMPT  # the regime flag comes from the form
    assert "extract only" in lowered  # nothing beyond what is printed
    assert '{"line":' in PROMPT  # the prompt shows the row output
    assert "needs_review" in PROMPT  # and where to put anything unclear
    assert "document_type" in PROMPT  # how to say it isn't a Form 16
    assert "unsupported_items" in PROMPT  # where income it can't handle goes
    assert "Addresses are not extracted" in PROMPT
    # Haiku 5.5 caches a prefix of at least 512 tokens. English runs at about
    # 1.3 tokens a word, so 450 words is safely over.
    assert len(PROMPT.split()) > 450


def test_amount_pattern_rejects_printed_formats() -> None:
    """The SDK checks replies against this pattern, so "12,00,000" never
    reaches the mapping; it surfaces as LLMOutputError instead."""
    schema = TypeAdapter(ExtractedForm16).json_schema()
    pattern = schema["$defs"]["Row"]["properties"]["amount"]["pattern"]

    for good in ["1200000", "0", "-200000.50", "6300.5"]:
        assert re.fullmatch(pattern, good), good
    for bad in ["12,00,000", "₹1200000", "Rs. 1200000", "1200000.000", "", "abc"]:
        assert not re.fullmatch(pattern, bad), bad


# --- Rows to the domain model ---------------------------------------------------


def test_a_perfect_extraction_gives_back_the_truth(rules: TaxRules) -> None:
    for seed in range(50):
        truth, _ = generate(seed, rules)
        form16 = to_form16(perfect_extraction(truth))
        assert form16 == without_unextracted(truth), seed
        assert diff_form16(truth, form16, skip=NOT_EXTRACTED).mismatches == ()


def test_extract_calls_the_llm_once_and_returns_the_form16(truth: Form16) -> None:
    llm = FakeLLM([perfect_extraction(truth)])

    extraction = extract_form16("the PDF text", llm, PROMPT)

    assert extraction.form16 == without_unextracted(truth)
    [call] = llm.calls
    assert call.system == PROMPT
    start, end = call.prompt.index("<document>"), call.prompt.index("</document>")
    assert "the PDF text" in call.prompt[start:end]
    assert call.output_model is ExtractedForm16


def test_a_missing_amount_line_is_zero(truth: Form16) -> None:
    extracted = drop_row(perfect_extraction(truth), "2(a)")
    assert to_form16(extracted).part_b.section_10.travel_concession_10_5 == 0


def test_rows_can_come_in_any_order(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    shuffled = with_rows(extracted, list(reversed(extracted.rows)))
    assert to_form16(shuffled) == to_form16(extracted)


def test_blank_cell_is_none_and_printed_zero_is_zero(truth: Form16) -> None:
    extracted = set_deduction(perfect_extraction(truth), "10(a)", gross="")
    extracted = set_deduction(extracted, "10(h)", gross="0")
    via = to_form16(extracted).part_b.chapter_via
    assert via.section_80c.gross is None
    assert via.section_80e.gross == 0


def test_gross_equal_to_deductible_is_kept(truth: Form16) -> None:
    """F004 and F008: with one row per column, the gross row went missing
    when it printed the same amount as the deductible."""
    extracted = set_deduction(
        perfect_extraction(truth), "10(a)", gross="84000", deductible="84000"
    )
    section_80c = to_form16(extracted).part_b.chapter_via.section_80c
    assert section_80c.gross == section_80c.deductible == 84000


def test_a_missing_deduction_line_is_blank_with_nothing_deductible(
    truth: Form16,
) -> None:
    extracted = perfect_extraction(truth)
    deductions = [r for r in extracted.deductions if r.line != "10(k)"]
    extracted = extracted.model_copy(update={"deductions": deductions})
    section_80g = to_form16(extracted).part_b.chapter_via.section_80g
    assert (section_80g.gross, section_80g.qualifying) == (None, None)
    assert section_80g.deductible == 0


@pytest.mark.parametrize("cell", ["gross", "qualifying", "deductible"])
def test_a_deduction_row_needs_every_cell(cell: str) -> None:
    cells = {"line": "10(a)", "gross": "1", "qualifying": "", "deductible": "1"}
    del cells[cell]
    with pytest.raises(ValidationError):
        DeductionRow.model_validate(cells)


def test_a_blank_deductible_cell_is_not_allowed() -> None:
    with pytest.raises(ValidationError):
        DeductionRow(line="10(a)", gross="", qualifying="", deductible="")


def test_regime_flag_is_copied_from_the_answer(truth: Form16) -> None:
    for answer, opted_out in [("YES", True), ("NO", False)]:
        extracted = perfect_extraction(truth).model_copy(
            update={"opted_out_of_new_regime": answer}
        )
        assert to_form16(extracted).part_b.opted_out_of_new_regime is opted_out


def test_no_breakup_printed_means_no_salary_breakup(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    rows = [r for r in extracted.rows if not r.line.startswith("breakup:")]
    extracted = with_rows(extracted, rows).model_copy(
        update={"salary_breakup_printed": False}
    )
    assert to_form16(extracted).salary_breakup is None


def test_trailing_zeros_compare_equal(truth: Form16) -> None:
    amount = truth.part_b.gross_salary.total
    extracted = set_row(perfect_extraction(truth), "1(d)", f"{amount}.00")
    form16 = to_form16(extracted)
    assert form16.part_b.gross_salary.total == amount
    assert diff_form16(truth, form16, skip=NOT_EXTRACTED).mismatches == ()


# --- Problems raise Form16ValidationError -----------------------------------------


def test_a_duplicate_row_with_another_amount_is_a_problem(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    extra = Row(line="1(a)", amount="1")
    [problem] = problems_of(with_rows(extracted, [*extracted.rows, extra]))
    assert "line 1(a) is given twice" in problem


def test_a_duplicate_deduction_row_with_other_cells_is_a_problem(
    truth: Form16,
) -> None:
    extracted = perfect_extraction(truth)
    extra = DeductionRow(line="10(a)", gross="1", qualifying="", deductible="1")
    extracted = extracted.model_copy(
        update={"deductions": [*extracted.deductions, extra]}
    )
    [problem] = problems_of(extracted)
    assert "line 10(a) is given twice" in problem


def test_a_repeated_identical_deduction_row_is_fine(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    first = extracted.deductions[0]
    extracted = extracted.model_copy(
        update={"deductions": [*extracted.deductions, first]}
    )
    assert to_form16(extracted)


def test_a_repeated_identical_row_is_fine(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    first = extracted.rows[0]
    assert to_form16(with_rows(extracted, [*extracted.rows, first]))


@pytest.mark.parametrize("line", ["1(d)", "6", "9", "12", "17"])
def test_a_missing_core_line_is_a_problem(truth: Form16, line: str) -> None:
    extracted = drop_row(perfect_extraction(truth), line)
    assert f"line {line} is missing; every Form 16 prints it" in problems_of(extracted)


def test_breakup_rows_without_a_printed_breakup_are_a_problem(truth: Form16) -> None:
    extracted = perfect_extraction(truth).model_copy(
        update={"salary_breakup_printed": False}
    )
    assert "salary breakup rows given, but no breakup is printed" in problems_of(
        extracted
    )


def test_anything_flagged_for_review_stops_the_extraction(truth: Form16) -> None:
    extracted = perfect_extraction(truth).model_copy(
        update={"needs_review": ["line 16: digits cut off at the page edge"]}
    )
    assert problems_of(extracted) == [
        "flagged for review: line 16: digits cut off at the page edge"
    ]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"employee_pan": "ABCD1234"}, "part_a.employee_pan"),
        ({"employer_tan": "MUM12345A"}, "part_a.employer_tan"),
        ({"assessment_year": "2026-28"}, "part_a.assessment_year"),
    ],
)
def test_identity_failing_domain_validation_is_a_problem(
    truth: Form16, change: dict[str, str], problem: str
) -> None:
    extracted = set_identity(perfect_extraction(truth), **change)
    assert any(p.startswith(problem) for p in problems_of(extracted))


def test_a_negative_salary_fails_domain_validation(truth: Form16) -> None:
    extracted = set_row(perfect_extraction(truth), "1(a)", "-5000")
    problems = problems_of(extracted)
    assert any(p.startswith("part_b.gross_salary.section_17_1") for p in problems)


def test_extract_raises_the_typed_error(truth: Form16) -> None:
    bad = set_identity(perfect_extraction(truth), employee_pan="NOTAPAN")
    with pytest.raises(Form16ValidationError):
        extract_form16("text", FakeLLM([bad, bad]), PROMPT)


# --- Documents the engine can't handle ------------------------------------------

CAPITAL_GAINS = UnsupportedItem(
    where="Annexure: other income declared by the employee u/s 192(2B)",
    description="Long-term capital gains on listed equity shares (section 112A)",
    amount="250000",
)


def test_a_document_that_is_not_a_form16_is_refused(truth: Form16) -> None:
    extracted = perfect_extraction(truth).model_copy(update={"document_type": "other"})
    with pytest.raises(NotAForm16Error) as caught:
        to_form16(extracted)
    assert caught.value.extracted == extracted


def test_not_a_form16_is_checked_before_the_rows(truth: Form16) -> None:
    """A payslip has none of the core lines; that must not be reported as
    missing lines, only as not a Form 16."""
    extracted = perfect_extraction(truth).model_copy(
        update={"document_type": "other", "rows": []}
    )
    with pytest.raises(NotAForm16Error):
        to_form16(extracted)


def test_extract_reports_the_cost_of_a_refused_document(truth: Form16) -> None:
    extracted = perfect_extraction(truth).model_copy(update={"document_type": "other"})
    with pytest.raises(NotAForm16Error) as caught:
        extract_form16("payslip text", FakeLLM([extracted]), PROMPT)
    assert caught.value.usage is not None


def test_unsupported_items_become_unrecognised_lines(truth: Form16) -> None:
    extracted = perfect_extraction(truth).model_copy(
        update={"unsupported_items": [CAPITAL_GAINS]}
    )
    [line] = to_form16(extracted).unrecognised_lines
    assert line.where == CAPITAL_GAINS.where
    assert line.description == CAPITAL_GAINS.description
    assert line.amount == Decimal(250000)


def test_unsupported_items_make_to_tax_input_refuse(
    truth: Form16, rules: TaxRules
) -> None:
    """The point of reporting them: the engine must not compute tax while
    ignoring income it can't model (ADR 0004)."""
    form16, extras = generate(3, rules)
    extracted = perfect_extraction(form16).model_copy(
        update={"unsupported_items": [CAPITAL_GAINS]}
    )
    result = to_tax_input(to_form16(extracted), extras, rules)
    assert result.blocked
    assert any("capital gains" in f.message.lower() for f in result.findings)


def test_unsupported_item_wording_is_not_scored_but_its_amount_is(
    rules: TaxRules,
) -> None:
    truth_cg = _with_capital_gains(generate(5, rules)[0], 5)
    reworded = CAPITAL_GAINS.model_copy(
        update={
            "where": "Other income annexure",
            "description": "LTCG",
            "amount": f"{truth_cg.unrecognised_lines[0].amount}",
        }
    )
    extracted = perfect_extraction(truth_cg).model_copy(
        update={"unsupported_items": [reworded]}
    )
    diff = diff_form16(truth_cg, to_form16(extracted), skip=NOT_EXTRACTED)
    assert diff.mismatches == ()

    wrong_amount = extracted.model_copy(
        update={"unsupported_items": [reworded.model_copy(update={"amount": "1"})]}
    )
    diff = diff_form16(truth_cg, to_form16(wrong_amount), skip=NOT_EXTRACTED)
    assert [m.path for m in diff.mismatches] == ["unrecognised_lines[0].amount"]


# --- One retry on validation failure ----------------------------------------------


def _usage(cost: str, from_disk_cache: bool = False) -> LLMUsage:
    return LLMUsage(
        input_tokens=2000,
        output_tokens=2500,
        cache_write_tokens=0,
        cache_read_tokens=7800,
        cost_usd=Decimal(cost),
        from_disk_cache=from_disk_cache,
    )


def test_a_bad_reply_then_a_good_one_succeeds_after_one_retry(truth: Form16) -> None:
    bad = set_identity(perfect_extraction(truth), employee_pan="NOTAPAN")
    good = perfect_extraction(truth)
    llm = FakeLLM([bad, good], usage=_usage("0.0016"))

    extraction = extract_form16("the PDF text", llm, PROMPT)

    assert extraction.form16 == without_unextracted(truth)
    assert extraction.attempts == 2
    assert len(llm.calls) == 2
    assert extraction.usage.cost_usd == Decimal("0.0032")  # both calls counted
    assert extraction.usage.output_tokens == 5000


def test_the_retry_shows_the_problems_and_the_same_document(truth: Form16) -> None:
    bad = set_identity(perfect_extraction(truth), employee_pan="NOTAPAN")
    llm = FakeLLM([bad, perfect_extraction(truth)])

    extract_form16("the PDF text", llm, PROMPT)

    first, retry = llm.calls
    assert retry.system == first.system == PROMPT
    assert "<document>\nthe PDF text\n</document>" in retry.prompt
    assert "part_a.employee_pan" in retry.prompt
    assert "rejected" not in first.prompt
    # The retry must not push the model to change printed figures.
    assert "never change a printed figure" in retry.prompt


def test_two_bad_replies_raise_the_typed_error(truth: Form16) -> None:
    bad = set_identity(perfect_extraction(truth), employee_pan="NOTAPAN")
    llm = FakeLLM([bad, bad], usage=_usage("0.0016"))

    with pytest.raises(Form16ValidationError) as caught:
        extract_form16("text", llm, PROMPT)

    assert caught.value.attempts == 2
    assert len(llm.calls) == 2
    assert any(p.startswith("part_a.employee_pan") for p in caught.value.problems)
    assert caught.value.usage is not None
    assert caught.value.usage.cost_usd == Decimal("0.0032")


def test_a_good_first_reply_makes_one_call(truth: Form16) -> None:
    llm = FakeLLM([perfect_extraction(truth)])
    assert extract_form16("text", llm, PROMPT).attempts == 1
    assert len(llm.calls) == 1


def test_values_flagged_for_review_are_not_retried(truth: Form16) -> None:
    flagged = perfect_extraction(truth).model_copy(
        update={"needs_review": ["line 16: digits cut off"]}
    )
    llm = FakeLLM([flagged, perfect_extraction(truth)])

    with pytest.raises(NeedsReviewError) as caught:
        extract_form16("text", llm, PROMPT)

    assert len(llm.calls) == 1  # asking again would only invite a guess
    assert caught.value.attempts == 1


def test_not_a_form16_is_not_retried(truth: Form16) -> None:
    other = perfect_extraction(truth).model_copy(update={"document_type": "other"})
    llm = FakeLLM([other, perfect_extraction(truth)])

    with pytest.raises(NotAForm16Error):
        extract_form16("payslip", llm, PROMPT)
    assert len(llm.calls) == 1


def test_usage_adds_up_and_is_cached_only_if_both_calls_were() -> None:
    assert (_usage("1", True) + _usage("2", True)).from_disk_cache
    assert not (_usage("1", True) + _usage("2", False)).from_disk_cache
    assert (_usage("1") + _usage("2")).cost_usd == Decimal(3)


# --- Fewer output tokens: no addresses ------------------------


def test_identity_has_no_addresses() -> None:
    schema = TypeAdapter(ExtractedForm16).json_schema()
    identity = schema["$defs"]["ExtractedIdentity"]["properties"]
    assert "employer_address" not in identity
    assert "employee_address" not in identity


# --- Doubtful cells get one more look --------------------------------------------


def _blank_80c_gross(truth: Form16) -> ExtractedForm16:
    return set_deduction(
        perfect_extraction(truth), "10(a)", gross="", deductible="111000"
    )


def test_a_blank_gross_next_to_a_deductible_is_doubtful(truth: Form16) -> None:
    [doubt] = doubtful_cells(_blank_80c_gross(truth))
    assert doubt.startswith("line 10(a): deductible 111000 is given but gross")


@pytest.mark.parametrize(
    ("line", "cells"),
    [
        ("10(a)", {"gross": "", "deductible": "0"}),  # nothing claimed
        ("10(a)", {"gross": "0", "deductible": "50000"}),  # printed zero, not blank
        ("10(d)", {"gross": "", "deductible": "111000"}),  # a total
    ],
)
def test_cells_that_are_not_doubtful(
    truth: Form16, line: str, cells: dict[str, str]
) -> None:
    extracted = set_deduction(perfect_extraction(truth), line, **cells)
    assert doubtful_cells(extracted) == []


def test_synthetic_forms_have_no_doubtful_cells(rules: TaxRules) -> None:
    """A correct reading of the eval forms must never cost a retry."""
    for seed in range(1, 101):
        form16, _ = generate(seed, rules)
        assert doubtful_cells(perfect_extraction(form16)) == [], seed


def test_a_doubtful_cell_is_retried_once_and_the_fix_is_kept(truth: Form16) -> None:
    llm = FakeLLM([_blank_80c_gross(truth), perfect_extraction(truth)])

    extraction = extract_form16("the PDF text", llm, PROMPT)

    assert extraction.form16 == without_unextracted(truth)
    assert extraction.attempts == 2
    first, retry = llm.calls
    assert "line 10(a): deductible 111000" in retry.prompt
    assert 'really is blank, keep it as ""' in retry.prompt
    assert "rejected" not in retry.prompt  # a doubt is not a failed check


def test_a_doubtful_cell_kept_blank_on_the_retry_is_accepted(truth: Form16) -> None:
    blank = _blank_80c_gross(truth)
    llm = FakeLLM([blank, blank])

    extraction = extract_form16("text", llm, PROMPT)

    assert extraction.attempts == 2
    assert len(llm.calls) == 2  # never a third call
    assert extraction.form16.part_b.chapter_via.section_80c.gross is None
    assert extraction.form16.part_b.chapter_via.section_80c.deductible == 111000


def test_a_retry_for_a_failed_check_also_lists_doubts(truth: Form16) -> None:
    bad = set_identity(_blank_80c_gross(truth), employee_pan="NOTAPAN")
    llm = FakeLLM([bad, perfect_extraction(truth)])

    extract_form16("text", llm, PROMPT)

    retry = llm.calls[1]
    assert "part_a.employee_pan" in retry.prompt
    assert "line 10(a): deductible 111000" in retry.prompt
