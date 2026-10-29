import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from itr.domain.computation import Computation
from itr.domain.inputs import TaxInput
from itr.domain.rules import Regime, TaxRules
from itr.domain.tax import compute, round_288a, round_288b

ROOT = Path(__file__).parents[2]


def _load(relative_path: str) -> Any:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    return TaxRules.model_validate(_load("rules/fy2025_26.json"))


# Something of everything: every deduction and exemption is claimed.
KITCHEN_SINK = {
    "salary": {
        "gross": "2400000",
        "basic": "1200000",
        "dearness_allowance": "100000",
        "hra_received": "500000",
        "other_allowances": "600000",
    },
    "rent_paid": "600000",
    "city": "Delhi",
    "professional_tax": "2500",
    "self_occupied_home_loan_interest": "350000",
    "savings_interest": "18000",
    "other_interest": "50000",
    "deductions": {
        "section_80c": "200000",
        "section_80d_self_and_family": "40000",
        "section_80d_parents": "60000",
    },
    "parents_include_senior_citizen": True,
}

# Inputs chosen to hit the edges: a loss from house property bigger than the
# salary, deductions bigger than income, and a surcharge-sized salary.
EDGE_CASES = {
    "kitchen-sink": KITCHEN_SINK,
    "no-income": {"salary": {"gross": "0"}},
    "house-loss-exceeds-salary": {
        "salary": {"gross": "150000"},
        "self_occupied_home_loan_interest": "200000",
    },
    "deductions-exceed-income": {
        "salary": {"gross": "120000"},
        "deductions": {"section_80c": "150000"},
    },
    "five-crore": {"salary": {"gross": "50000000"}},
}

SCENARIOS = _load("tests/fixtures/scenarios.json")["scenarios"]
ALL_INPUTS = {s["id"]: s["inputs"] for s in SCENARIOS} | EDGE_CASES
CASES = [(name, regime) for name in ALL_INPUTS for regime in ("new", "old")]
CASE_IDS = [f"{name}-{regime}" for name, regime in CASES]


def _sheet(name: str, rules: TaxRules, regime: Regime) -> Computation:
    return compute(TaxInput.model_validate(ALL_INPUTS[name]), rules, regime)


@pytest.mark.parametrize(("name", "regime"), CASES, ids=CASE_IDS)
def test_tax_is_never_negative(rules: TaxRules, name: str, regime: Regime) -> None:
    tax = _sheet(name, rules, regime).tax
    for line in (
        "tax_on_total_income",
        "rebate_87a",
        "tax_after_rebate",
        "surcharge",
        "cess",
        "total_tax",
        "tax_payable",
    ):
        assert getattr(tax, line) >= 0, line


# Lines the FY 2025-26 rules allow only in the old regime. This checks the
# data: the allowed_in tests already show the code follows whatever it says.
OLD_REGIME_ONLY = [
    "hra_exemption",
    "professional_tax",
    "house_property_income",
    "deduction_80c",
    "deduction_80d",
    "deduction_80tta",
]


@pytest.mark.parametrize("line", OLD_REGIME_ONLY)
def test_new_regime_never_applies_old_regime_deductions(
    rules: TaxRules, line: str
) -> None:
    income = _sheet("kitchen-sink", rules, "new").income
    assert getattr(income, line) == 0


@pytest.mark.parametrize("line", OLD_REGIME_ONLY)
def test_kitchen_sink_does_claim_them_in_old_regime(rules: TaxRules, line: str) -> None:
    """Without this, the test above would also pass if the kitchen sink
    simply claimed nothing."""
    income = _sheet("kitchen-sink", rules, "old").income
    assert getattr(income, line) != 0


@pytest.mark.parametrize(("name", "regime"), CASES, ids=CASE_IDS)
def test_every_line_adds_up_to_the_next(
    rules: TaxRules, name: str, regime: Regime
) -> None:
    sheet = _sheet(name, rules, regime)
    i, t = sheet.income, sheet.tax
    zero = Decimal(0)

    checks = {
        "income from salary": (
            i.income_from_salary,
            max(
                zero,
                i.gross_salary
                - i.hra_exemption
                - i.standard_deduction
                - i.professional_tax,
            ),
        ),
        "gross total income": (
            i.gross_total_income,
            i.income_from_salary + i.house_property_income + i.interest_income,
        ),
        "deductions total (80A(2) cap)": (
            i.deductions_total,
            min(
                i.deduction_80c + i.deduction_80d + i.deduction_80tta,
                max(zero, i.gross_total_income),
            ),
        ),
        "total income": (
            i.total_income,
            max(zero, i.gross_total_income - i.deductions_total),
        ),
        "income passed to tax": (t.total_income, i.total_income),
        "288A rounding": (t.total_income_rounded, round_288a(t.total_income)),
        "tax after rebate": (
            t.tax_after_rebate,
            t.tax_on_total_income - t.rebate_87a,
        ),
        "cess (after rebate, on tax plus surcharge)": (
            t.cess,
            (t.tax_after_rebate + t.surcharge) * rules.cess.rate,
        ),
        "total tax": (t.total_tax, t.tax_after_rebate + t.surcharge + t.cess),
        "288B rounding": (t.tax_payable, round_288b(t.total_tax)),
    }
    for line, (actual, expected) in checks.items():
        assert actual == expected, f"{line}: {actual} != {expected}"
