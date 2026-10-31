"""The extract use case, with FakeLLM standing in for Claude."""

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import TypeAdapter

from itr.application.extract_form16_v2 import (
    AMOUNT_LINES,
    BREAKUP_LINES,
    DEDUCTION_LINES,
    NOT_EXTRACTED,
    Form16ValidationError,
    extract_form16,
    to_form16,
)
from itr.application.form16_diff import diff_form16
from itr.application.form16_extraction_v2 import ExtractedForm16, LineId, Row
from itr.domain.form16 import Form16
from itr.domain.rules import TaxRules
from itr.synthetic.form16_generator import generate
from tests.fakes import FakeLLM, perfect_extraction

ROOT = Path(__file__).parents[2]
PROMPT = (ROOT / "prompts/extract_form16_v2.md").read_text(encoding="utf-8")


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
            "part_a": form16.part_a.model_copy(update={"quarters": ()}),
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


def set_row(
    extracted: ExtractedForm16, line: str, column: str, amount: str
) -> ExtractedForm16:
    """A copy with one row's amount changed."""
    rows = [
        r.model_copy(update={"amount": amount})
        if (r.line, r.column) == (line, column)
        else r
        for r in extracted.rows
    ]
    return with_rows(extracted, rows)


def drop_row(extracted: ExtractedForm16, line: str, column: str) -> ExtractedForm16:
    rows = [r for r in extracted.rows if (r.line, r.column) != (line, column)]
    return with_rows(extracted, rows)


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
    assert len(objects) <= 3
    assert sum(len(obj["properties"]) for obj in objects) <= 25


def test_every_line_id_has_one_place_in_the_form16() -> None:
    tables = [set(AMOUNT_LINES), set(DEDUCTION_LINES), set(BREAKUP_LINES)]
    assert set().union(*tables) == set(get_args(LineId))
    assert sum(len(t) for t in tables) == len(get_args(LineId))  # no overlaps
    paths = list(AMOUNT_LINES.values()) + list(DEDUCTION_LINES.values())
    assert len(paths) == len(set(paths))  # no two lines fill the same field


def test_prompt_has_the_rules_and_is_long_enough_to_cache() -> None:
    lowered = PROMPT.lower()
    assert "never calculate" in lowered  # copy numbers, never compute them
    assert "115BAC(1A)" in PROMPT  # the regime flag comes from the form
    assert "extract only" in lowered  # nothing beyond what is printed
    assert '{"line":' in PROMPT  # the prompt shows the row output
    assert "needs_review" in PROMPT  # and where to put anything unclear
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
    extracted = drop_row(perfect_extraction(truth), "2(a)", "amount")
    assert to_form16(extracted).part_b.section_10.travel_concession_10_5 == 0


def test_rows_can_come_in_any_order(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    shuffled = with_rows(extracted, list(reversed(extracted.rows)))
    assert to_form16(shuffled) == to_form16(extracted)


def test_blank_column_is_none_and_printed_zero_is_zero(truth: Form16) -> None:
    extracted = drop_row(perfect_extraction(truth), "10(a)", "gross")
    extracted = with_rows(
        extracted, [*extracted.rows, Row(line="10(h)", column="gross", amount="0")]
    )
    via = to_form16(extracted).part_b.chapter_via
    assert via.section_80c.gross is None
    assert via.section_80e.gross == 0


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
    extracted = set_row(perfect_extraction(truth), "1(d)", "amount", f"{amount}.00")
    form16 = to_form16(extracted)
    assert form16.part_b.gross_salary.total == amount
    assert diff_form16(truth, form16, skip=NOT_EXTRACTED).mismatches == ()


# --- Problems raise Form16ValidationError -----------------------------------------


def test_a_duplicate_row_with_another_amount_is_a_problem(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    extra = Row(line="1(a)", column="amount", amount="1")
    [problem] = problems_of(with_rows(extracted, [*extracted.rows, extra]))
    assert "line 1(a) amount is given twice" in problem


def test_a_repeated_identical_row_is_fine(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    first = extracted.rows[0]
    assert to_form16(with_rows(extracted, [*extracted.rows, first]))


def test_a_column_the_line_does_not_have_is_a_problem(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    wrong = [
        Row(line="1(a)", column="gross", amount="5"),
        Row(line="10(a)", column="amount", amount="5"),
    ]
    problems = problems_of(with_rows(extracted, [*extracted.rows, *wrong]))
    assert "line 1(a) has no gross column" in problems
    assert any(p.startswith("line 10(a) needs a gross") for p in problems)


@pytest.mark.parametrize("line", ["1(d)", "6", "9", "12", "17"])
def test_a_missing_core_line_is_a_problem(truth: Form16, line: str) -> None:
    extracted = drop_row(perfect_extraction(truth), line, "amount")
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
    extracted = set_row(perfect_extraction(truth), "1(a)", "amount", "-5000")
    problems = problems_of(extracted)
    assert any(p.startswith("part_b.gross_salary.section_17_1") for p in problems)


def test_extract_raises_the_typed_error(truth: Form16) -> None:
    bad = set_identity(perfect_extraction(truth), employee_pan="NOTAPAN")
    with pytest.raises(Form16ValidationError):
        extract_form16("text", FakeLLM([bad]), PROMPT)


@pytest.mark.parametrize("line", ["10(n)", "11"])
def test_chapter_via_totals_accept_the_deductible_label(
    truth: Form16, line: str
) -> None:
    """10(n) and 11 have one amount, labelled "deductible" on the form; seen
    from the model on F004."""
    extracted = perfect_extraction(truth)
    rows = [
        r.model_copy(update={"column": "deductible"}) if r.line == line else r
        for r in extracted.rows
    ]
    assert to_form16(with_rows(extracted, rows)) == to_form16(extracted)


def test_chapter_via_total_given_twice_with_different_amounts(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    extra = Row(line="11", column="deductible", amount="999999")
    [problem] = problems_of(with_rows(extracted, [*extracted.rows, extra]))
    assert "line 11 amount is given twice" in problem


def test_other_single_amount_lines_still_reject_deductible(truth: Form16) -> None:
    extracted = perfect_extraction(truth)
    extra = Row(line="12", column="deductible", amount="0")
    assert "line 12 has no deductible column" in problems_of(
        with_rows(extracted, [*extracted.rows, extra])
    )
