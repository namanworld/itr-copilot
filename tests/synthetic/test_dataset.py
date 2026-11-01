"""The evaluation dataset: the right mix, tagged, and the refusals refuse."""

import json
from collections import Counter
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader

from itr.domain.form16_mapping import to_tax_input
from itr.domain.rules import TaxRules
from itr.synthetic.dataset import (
    Case,
    TruthFile,
    build_eval_dataset,
    build_normal_dataset,
    render_case,
)

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


@pytest.fixture(scope="module")
def cases(rules: TaxRules) -> list[Case]:
    return build_eval_dataset(42, rules)


def text_of(case: Case) -> str:
    reader = PdfReader(BytesIO(render_case(case)))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_the_mix_is_15_normal_3_hard_2_out_of_scope(cases: list[Case]) -> None:
    assert Counter(c.tag.category for c in cases) == {
        "normal": 15,
        "hard": 3,
        "out_of_scope": 2,
    }
    assert [c.tag.case_id for c in cases] == [f"F{n:03d}" for n in range(1, 21)]
    assert [c.tag.seed for c in cases] == list(range(42, 62))


def test_normal_cases_mix_regimes_and_claims(cases: list[Case]) -> None:
    normal = [c for c in cases if c.tag.category == "normal"]
    assert {c.document.part_b.regime_used for c in normal} == {"new", "old"}
    assert any(c.document.part_b.chapter_via.aggregate > 0 for c in normal)
    assert any(
        c.document.part_b.section_10.house_rent_allowance_10_13a > 0 for c in normal
    )
    assert all(c.tag.layout == "standard" for c in normal)


def test_hard_cases_use_the_second_layout_and_other_formats(cases: list[Case]) -> None:
    hard = [c for c in cases if c.tag.category == "hard"]
    assert {c.tag.layout for c in hard} == {"statement"}
    assert {c.tag.number_format for c in hard} == {
        "rupee_symbol",
        "plain_decimal",
        "rs_prefix",
    }


def test_cases_that_should_be_extracted_map_cleanly(
    cases: list[Case], rules: TaxRules
) -> None:
    for case in cases:
        if case.tag.expected != "extract":
            continue
        assert case.truth.form16 == case.document
        result = to_tax_input(case.document, case.extras, rules)
        assert result.findings == (), case.tag.case_id


def test_capital_gains_case_is_refused(cases: list[Case], rules: TaxRules) -> None:
    [case] = [c for c in cases if c.tag.reason == "capital_gains"]
    assert case.tag.expected == "refuse"
    assert case.truth.form16 == case.document  # the truth still records it
    assert case.document.unrecognised_lines

    result = to_tax_input(case.document, case.extras, rules)
    assert result.blocked
    assert result.tax_input is None
    assert "capital gains" in text_of(case).lower()


def test_payslip_is_refused_and_has_no_form16_truth(cases: list[Case]) -> None:
    [case] = [c for c in cases if c.tag.reason == "not_a_form16"]
    assert case.tag.expected == "refuse"
    assert case.tag.layout == "payslip"
    assert case.truth.form16 is None

    text = text_of(case)
    assert "PAYSLIP" in text
    assert "Form No. 16" not in text
    assert "FORM NO. 16" not in text


def test_every_case_has_a_reason_exactly_when_it_is_refused(
    cases: list[Case],
) -> None:
    for case in cases:
        assert (case.tag.expected == "refuse") == (case.tag.reason is not None)
        assert (case.tag.category == "out_of_scope") == (case.tag.expected == "refuse")


def test_truth_files_round_trip(cases: list[Case]) -> None:
    for case in cases:
        text = case.truth.model_dump_json()
        assert TruthFile.model_validate_json(text) == case.truth


def test_dataset_is_reproducible(rules: TaxRules, cases: list[Case]) -> None:
    again = build_eval_dataset(42, rules)
    assert [c.truth for c in again] == [c.truth for c in cases]
    assert render_case(again[16]) == render_case(cases[16])


def test_normal_dataset(rules: TaxRules) -> None:
    cases = build_normal_dataset(3, 7, rules)
    assert [c.tag.seed for c in cases] == [7, 8, 9]
    assert {c.tag.category for c in cases} == {"normal"}
    assert all(c.tag.expected == "extract" for c in cases)
