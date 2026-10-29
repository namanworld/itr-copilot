"""compute() joins the two halves; compare() runs both regimes."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from itr.domain.inputs import TaxInput
from itr.domain.rules import TaxRules
from itr.domain.tax import compare, compute, compute_income, compute_tax

ROOT = Path(__file__).parents[2]


def _load(relative_path: str) -> Any:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    return TaxRules.model_validate(_load("rules/fy2025_26.json"))


def _salary(gross: str) -> TaxInput:
    return TaxInput.model_validate({"salary": {"gross": gross}})


def test_compute_feeds_total_income_into_tax(rules: TaxRules) -> None:
    inputs = _salary("1285000")
    sheet = compute(inputs, rules, "new")

    assert sheet.regime == "new"
    assert sheet.income == compute_income(inputs, rules, "new")
    assert sheet.tax == compute_tax(sheet.income.total_income, rules, "new")


def test_new_regime_cheaper(rules: TaxRules) -> None:
    # Scenario 1: new regime pays nothing, old pays 1,87,200.
    result = compare(_salary("1275000"), rules)
    assert result.cheaper == "new"
    assert result.saving == Decimal("187200")


def test_old_regime_cheaper(rules: TaxRules) -> None:
    # Scenario 3: old 91,000 against new 97,500.
    inputs = TaxInput.model_validate(
        {
            "salary": {
                "gross": "1500000",
                "basic": "800000",
                "hra_received": "450000",
                "other_allowances": "250000",
            },
            "rent_paid": "540000",
            "city": "Mumbai",
            "deductions": {
                "section_80c": "150000",
                "section_80d_self_and_family": "60000",
            },
        }
    )
    result = compare(inputs, rules)
    assert result.cheaper == "old"
    assert result.saving == Decimal("6500")


def test_tie_names_no_regime(rules: TaxRules) -> None:
    # Rs 5 lakh salary: the 87A rebate takes both regimes to zero.
    result = compare(_salary("500000"), rules)
    assert result.new.tax.tax_payable == 0
    assert result.old.tax.tax_payable == 0
    assert result.cheaper is None
    assert result.saving == 0


def test_saving_is_never_negative(rules: TaxRules) -> None:
    for gross in ("0", "500000", "1275000", "5085000"):
        assert compare(_salary(gross), rules).saving >= 0
