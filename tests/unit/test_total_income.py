import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from itr.domain.computation import IncomeComputation
from itr.domain.inputs import TaxInput
from itr.domain.rules import Regime, TaxRules
from itr.domain.tax import (
    compute_income,
    deduction_80c,
    deduction_80d,
    deduction_80tta,
    find_rule,
    house_property_income,
    hra_exemption,
    interest_income,
    professional_tax,
    standard_deduction,
)

ROOT = Path(__file__).parents[2]


def _load(relative_path: str) -> Any:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    return TaxRules.model_validate(_load("rules/fy2025_26.json"))


SCENARIOS: list[dict[str, Any]] = _load("tests/fixtures/scenarios.json")["scenarios"]
CASES = [(s, r) for s in SCENARIOS for r in ("new", "old")]
CASE_IDS = [f"{s['id']}-{r}" for s, r in CASES]


# --- Step 3: standard deduction ------------------------------------------------


@pytest.mark.parametrize(("regime", "amount"), [("old", "50000"), ("new", "75000")])
def test_standard_deduction_per_regime(
    rules: TaxRules, regime: Regime, amount: str
) -> None:
    assert standard_deduction(Decimal("1000000"), rules, regime) == Decimal(amount)


def test_standard_deduction_cannot_exceed_salary(rules: TaxRules) -> None:
    assert standard_deduction(Decimal("30000"), rules, "new") == Decimal("30000")


def test_no_salary_means_no_standard_deduction(rules: TaxRules) -> None:
    assert standard_deduction(Decimal("0"), rules, "old") == 0


def test_standard_deduction_is_decimal(rules: TaxRules) -> None:
    assert type(standard_deduction(Decimal("1000000"), rules, "new")) is Decimal


@pytest.mark.parametrize(("scenario", "regime"), CASES, ids=CASE_IDS)
def test_standard_deduction_matches_calculator(
    rules: TaxRules, scenario: dict[str, Any], regime: Regime
) -> None:
    inputs = TaxInput.model_validate(scenario["inputs"])
    expected = scenario["expected"][regime]["standard_deduction"]

    assert standard_deduction(inputs.salary.gross, rules, regime) == Decimal(expected)


def _with_professional_tax(amount: str) -> TaxInput:
    return TaxInput.model_validate(
        {"salary": {"gross": "1000000"}, "professional_tax": amount}
    )


def test_find_rule_raises_for_unknown_code(rules: TaxRules) -> None:
    with pytest.raises(LookupError, match="'nope'"):
        find_rule(rules, "nope")


def test_professional_tax_is_deducted_in_old_regime(rules: TaxRules) -> None:
    inputs = _with_professional_tax("2500")
    assert professional_tax(inputs, rules, "old") == Decimal("2500")


def test_professional_tax_is_not_deducted_in_new_regime(rules: TaxRules) -> None:
    inputs = _with_professional_tax("2500")
    assert professional_tax(inputs, rules, "new") == 0


def test_professional_tax_is_decimal_in_both_regimes(rules: TaxRules) -> None:
    inputs = _with_professional_tax("2500")
    for regime in ("old", "new"):
        assert type(professional_tax(inputs, rules, regime)) is Decimal


def test_professional_tax_follows_allowed_in(rules: TaxRules) -> None:
    """Move the rule to the new regime in a copy of the rules: the deduction
    must move with it, which proves the helper has no regime hard-coded."""
    rule = find_rule(rules, "16(iii)")
    moved = rule.model_copy(update={"allowed_in": "new"})
    flipped = rules.model_copy(
        update={
            "deductions": tuple(
                moved if r.code == "16(iii)" else r for r in rules.deductions
            )
        }
    )
    inputs = _with_professional_tax("2500")

    assert professional_tax(inputs, flipped, "new") == Decimal("2500")
    assert professional_tax(inputs, flipped, "old") == 0


def _move_rule(rules: TaxRules, code: str, to: Regime) -> TaxRules:
    """A copy of the rules with one rule's allowed_in changed."""
    moved = find_rule(rules, code).model_copy(update={"allowed_in": to})
    return rules.model_copy(
        update={
            "deductions": tuple(
                moved if r.code == code else r for r in rules.deductions
            )
        }
    )


# --- Step 5: house property (24(b)) -------------------------------------------


def _with_home_loan_interest(amount: str) -> TaxInput:
    return TaxInput.model_validate(
        {"salary": {"gross": "1000000"}, "self_occupied_home_loan_interest": amount}
    )


def test_home_loan_interest_is_capped_and_negative(rules: TaxRules) -> None:
    inputs = _with_home_loan_interest("300000")
    assert house_property_income(inputs, rules, "old") == Decimal("-200000")


def test_home_loan_interest_below_cap_is_taken_in_full(rules: TaxRules) -> None:
    inputs = _with_home_loan_interest("150000")
    assert house_property_income(inputs, rules, "old") == Decimal("-150000")


def test_home_loan_interest_not_allowed_in_new_regime(rules: TaxRules) -> None:
    inputs = _with_home_loan_interest("300000")
    assert house_property_income(inputs, rules, "new") == 0


def test_no_home_loan_gives_plain_zero(rules: TaxRules) -> None:
    inputs = _with_home_loan_interest("0")
    assert str(house_property_income(inputs, rules, "old")) == "0"


def test_house_property_follows_allowed_in(rules: TaxRules) -> None:
    flipped = _move_rule(rules, "24(b)", to="new")
    inputs = _with_home_loan_interest("300000")

    assert house_property_income(inputs, flipped, "new") == Decimal("-200000")
    assert house_property_income(inputs, flipped, "old") == 0


# Only the cases where the calculator's house-property figure was recorded.
HOUSE_PROPERTY_CASES = [
    (s, r) for s, r in CASES if "house_property_income" in s["expected"][r]
]


@pytest.mark.parametrize(
    ("scenario", "regime"),
    HOUSE_PROPERTY_CASES,
    ids=[f"{s['id']}-{r}" for s, r in HOUSE_PROPERTY_CASES],
)
def test_house_property_matches_calculator(
    rules: TaxRules, scenario: dict[str, Any], regime: Regime
) -> None:
    expected = scenario["expected"][regime]["house_property_income"]
    inputs = TaxInput.model_validate(scenario["inputs"])

    assert house_property_income(inputs, rules, regime) == Decimal(expected)


# --- Step 6: interest income --------------------------------------------------


def test_interest_income_adds_savings_and_other_interest() -> None:
    inputs = TaxInput.model_validate(
        {
            "salary": {"gross": "1000000"},
            "savings_interest": "12000",
            "other_interest": "30000",
        }
    )
    assert interest_income(inputs) == Decimal("42000")


def test_interest_income_is_zero_without_interest() -> None:
    inputs = TaxInput.model_validate({"salary": {"gross": "1000000"}})
    result = interest_income(inputs)
    assert result == 0
    assert type(result) is Decimal


# --- Step 7: Chapter VI-A (80C, 80D, 80TTA) -----------------------------------


def _claims(**claims: str) -> TaxInput:
    return TaxInput.model_validate(
        {"salary": {"gross": "1000000"}, "deductions": claims}
    )


def test_80c_is_capped(rules: TaxRules) -> None:
    assert deduction_80c(_claims(section_80c="200000"), rules, "old") == Decimal(
        "150000"
    )


def test_80c_below_cap_is_taken_in_full(rules: TaxRules) -> None:
    assert deduction_80c(_claims(section_80c="90000"), rules, "old") == Decimal("90000")


def test_80d_caps_each_category_separately(rules: TaxRules) -> None:
    inputs = _claims(section_80d_self_and_family="60000", section_80d_parents="30000")
    # 25,000 + 25,000: one category's unused room doesn't spill into the other.
    assert deduction_80d(inputs, rules, "old") == Decimal("50000")


def test_80d_senior_citizen_caps(rules: TaxRules) -> None:
    inputs = TaxInput.model_validate(
        {
            "salary": {"gross": "1000000"},
            "deductions": {
                "section_80d_self_and_family": "60000",
                "section_80d_parents": "60000",
            },
            "self_or_family_has_senior_citizen": True,
            "parents_include_senior_citizen": True,
        }
    )
    assert deduction_80d(inputs, rules, "old") == Decimal("100000")


def test_80tta_caps_savings_interest(rules: TaxRules) -> None:
    inputs = TaxInput.model_validate(
        {
            "salary": {"gross": "1000000"},
            "savings_interest": "15000",
            "other_interest": "40000",
        }
    )
    # Only savings interest counts, and only up to 10,000.
    assert deduction_80tta(inputs, rules, "old") == Decimal("10000")


@pytest.mark.parametrize("code", ["80C", "80D", "80TTA"])
def test_chapter_via_not_allowed_in_new_regime(rules: TaxRules, code: str) -> None:
    inputs = TaxInput.model_validate(
        {
            "salary": {"gross": "1000000"},
            "savings_interest": "15000",
            "deductions": {
                "section_80c": "150000",
                "section_80d_self_and_family": "25000",
            },
        }
    )
    helper = {"80C": deduction_80c, "80D": deduction_80d, "80TTA": deduction_80tta}
    assert helper[code](inputs, rules, "old") > 0
    assert helper[code](inputs, rules, "new") == 0
    # Moving the rule moves the deduction: nothing is hard-coded.
    flipped = _move_rule(rules, code, to="new")
    assert helper[code](inputs, flipped, "new") > 0
    assert helper[code](inputs, flipped, "old") == 0


# --- Step 2: HRA exemption ----------------------------------------------------


def _renting(city: str | None = "Mumbai", rent: str = "540000") -> TaxInput:
    """Scenario 3's salary: basic 8,00,000 and HRA received 4,50,000."""
    return TaxInput.model_validate(
        {
            "salary": {
                "gross": "1500000",
                "basic": "800000",
                "hra_received": "450000",
                "other_allowances": "250000",
            },
            "rent_paid": rent,
            "city": city,
        }
    )


def test_hra_metro_takes_least_of_three(rules: TaxRules) -> None:
    # HRA 4,50,000; rent minus 10% of basic 4,60,000; 50% of basic 4,00,000.
    assert hra_exemption(_renting(), rules, "old") == Decimal("400000")


def test_hra_non_metro_uses_lower_salary_share(rules: TaxRules) -> None:
    # 40% of basic, 3,20,000, is now the least.
    assert hra_exemption(_renting(city="Jaipur"), rules, "old") == Decimal("320000")


def test_hra_with_no_city_is_treated_as_non_metro(rules: TaxRules) -> None:
    assert hra_exemption(_renting(city=None), rules, "old") == Decimal("320000")


def test_hra_metro_match_ignores_case_and_spaces(rules: TaxRules) -> None:
    assert hra_exemption(_renting(city="  mumbai "), rules, "old") == Decimal("400000")


def test_hra_rent_limb_can_be_the_least(rules: TaxRules) -> None:
    # Rent 2,00,000 minus 80,000 = 1,20,000.
    assert hra_exemption(_renting(rent="200000"), rules, "old") == Decimal("120000")


def test_hra_is_zero_when_rent_is_below_ten_percent_of_salary(
    rules: TaxRules,
) -> None:
    assert hra_exemption(_renting(rent="50000"), rules, "old") == 0


def test_hra_is_zero_without_rent(rules: TaxRules) -> None:
    assert hra_exemption(_renting(rent="0"), rules, "old") == 0


def test_hra_follows_allowed_in(rules: TaxRules) -> None:
    assert hra_exemption(_renting(), rules, "new") == 0
    flipped = _move_rule(rules, "HRA", to="new")
    assert hra_exemption(_renting(), flipped, "new") == Decimal("400000")
    assert hra_exemption(_renting(), flipped, "old") == 0


# --- compute_income: all the steps together -----------------------------------


def test_deductions_cannot_exceed_gross_total_income(rules: TaxRules) -> None:
    inputs = TaxInput.model_validate(
        {"salary": {"gross": "100000"}, "deductions": {"section_80c": "150000"}}
    )
    sheet = compute_income(inputs, rules, "old")
    assert sheet.gross_total_income == Decimal("50000")
    assert sheet.deductions_total == Decimal("50000")
    assert sheet.total_income == 0


def test_every_income_line_is_decimal(rules: TaxRules) -> None:
    sheet = compute_income(_renting(), rules, "old")
    for name in IncomeComputation.model_fields:
        if name != "regime":
            assert type(getattr(sheet, name)) is Decimal, name


# scenarios.json key -> IncomeComputation field, for every line it records.
INCOME_LINES = {
    "standard_deduction": "standard_deduction",
    "house_property_income": "house_property_income",
    "section_80d_allowed": "deduction_80d",
    "deductions_total": "deductions_total",
    "total_income": "total_income",
}


@pytest.mark.parametrize(("scenario", "regime"), CASES, ids=CASE_IDS)
def test_golden_scenario_total_income(
    rules: TaxRules, scenario: dict[str, Any], regime: Regime
) -> None:
    inputs = TaxInput.model_validate(scenario["inputs"])
    expected = scenario["expected"][regime]
    sheet = compute_income(inputs, rules, regime)

    for key, field in INCOME_LINES.items():
        if key in expected:
            assert getattr(sheet, field) == Decimal(expected[key]), key
