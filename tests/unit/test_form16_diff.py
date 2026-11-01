"""Field-by-field diff of an extracted Form16 against its truth."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from itr.application.form16_diff import diff_form16, field_paths
from itr.domain.rules import TaxRules
from itr.synthetic.form16_generator import generate

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


def test_identical_forms_have_no_mismatches(rules: TaxRules) -> None:
    form16, _ = generate(1, rules)
    diff = diff_form16(form16, form16)
    assert diff.mismatches == ()
    assert diff.matched == diff.compared > 100


def test_a_changed_amount_is_reported_with_its_path(rules: TaxRules) -> None:
    truth, _ = generate(1, rules)
    tax = truth.part_b.tax.model_copy(update={"surcharge": Decimal(123)})
    wrong = truth.model_copy(
        update={"part_b": truth.part_b.model_copy(update={"tax": tax})}
    )
    diff = diff_form16(truth, wrong)

    [mismatch] = diff.mismatches
    assert mismatch.path == "part_b.tax.surcharge"
    assert mismatch.expected == truth.part_b.tax.surcharge
    assert mismatch.actual == 123
    assert diff.matched == diff.compared - 1


def test_skipped_paths_are_not_compared(rules: TaxRules) -> None:
    truth, _ = generate(1, rules)
    no_quarters = truth.model_copy(
        update={"part_a": truth.part_a.model_copy(update={"quarters": ()})}
    )
    assert diff_form16(truth, no_quarters).mismatches != ()
    assert diff_form16(truth, no_quarters, skip=("part_a.quarters",)).mismatches == ()


def test_a_star_skips_that_field_at_every_index(rules: TaxRules) -> None:
    truth, _ = generate(1, rules)
    paths = field_paths(truth, skip=("part_a.quarters[*].receipt_number",))
    assert "part_a.quarters[0].receipt_number" not in paths
    assert "part_a.quarters[3].receipt_number" not in paths
    assert "part_a.quarters[0].tax_deducted" in paths
