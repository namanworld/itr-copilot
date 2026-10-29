import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from itr.domain.computation import TaxComputation
from itr.domain.rules import Regime, TaxRules
from itr.domain.tax import (
    compute_tax,
    rebate_87a,
    regime_rules,
    round_288a,
    round_288b,
    slab_tax,
    surcharge,
)

ROOT = Path(__file__).parents[2]


def _load(relative_path: str) -> Any:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    return TaxRules.model_validate(_load("rules/fy2025_26.json"))


def _slab_tax(income: str, rules: TaxRules, regime: Regime) -> Decimal:
    return slab_tax(Decimal(income), regime_rules(rules, regime).slabs)


# --- Step 1: section 288A -----------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "rounded"),
    [
        ("0", "0"),
        ("1240", "1240"),
        ("1244", "1240"),
        ("1245", "1250"),  # a last digit of 5 goes up
        ("1249", "1250"),
        ("1244.99", "1240"),  # paise are ignored first, so 1244, then down
        ("1234.50", "1230"),
        ("1210000", "1210000"),
    ],
)
def test_288a_rounds_to_nearest_ten(amount: str, rounded: str) -> None:
    assert round_288a(Decimal(amount)) == Decimal(rounded)


# --- Step 3: 87A rebate -------------------------------------------------------


def test_new_regime_full_rebate_at_12_lakh(rules: TaxRules) -> None:
    tax = _slab_tax("1200000", rules, "new")  # 60,000
    assert rebate_87a(Decimal("1200000"), tax, rules, "new") == Decimal("60000")


def test_new_regime_marginal_relief_just_above_12_lakh(rules: TaxRules) -> None:
    # Rs 10,000 above the limit: slab tax 61,500, but tax may not exceed 10,000.
    tax = _slab_tax("1210000", rules, "new")
    rebate = rebate_87a(Decimal("1210000"), tax, rules, "new")
    assert rebate == Decimal("51500")
    assert tax - rebate == Decimal("10000")


def test_new_regime_marginal_relief_runs_out(rules: TaxRules) -> None:
    # At 13 lakh, slab tax 75,000 is less than the 1,00,000 above the limit.
    tax = _slab_tax("1300000", rules, "new")
    assert rebate_87a(Decimal("1300000"), tax, rules, "new") == 0


def test_old_regime_full_rebate_at_5_lakh(rules: TaxRules) -> None:
    tax = _slab_tax("500000", rules, "old")  # 12,500
    assert rebate_87a(Decimal("500000"), tax, rules, "old") == Decimal("12500")


def test_old_regime_has_no_marginal_relief(rules: TaxRules) -> None:
    tax = _slab_tax("500010", rules, "old")
    assert rebate_87a(Decimal("500010"), tax, rules, "old") == 0


# --- Step 4: surcharge --------------------------------------------------------


def test_no_surcharge_at_exactly_50_lakh(rules: TaxRules) -> None:
    tax = _slab_tax("5000000", rules, "new")
    assert surcharge(Decimal("5000000"), tax, rules, "new") == 0


def test_surcharge_marginal_relief_just_above_50_lakh(rules: TaxRules) -> None:
    # Tax 10,83,000; full 10% would be 1,08,300. Tax plus surcharge may be at
    # most tax at 50 lakh (10,80,000) plus the 10,000 above it.
    tax = _slab_tax("5010000", rules, "new")
    assert surcharge(Decimal("5010000"), tax, rules, "new") == Decimal("7000")


def test_surcharge_relief_compares_with_total_at_threshold(rules: TaxRules) -> None:
    """Just above Rs 1 crore, the ceiling is tax plus the 10% surcharge at
    Rs 1 crore, not slab tax alone.

    Tax at 1 crore: 25,80,000; with 10% surcharge: 28,38,000.
    At 1,00,10,000: tax 25,83,000; full 15% surcharge would be 3,87,450.
    Ceiling: 28,38,000 + 10,000 = 28,48,000, so surcharge is 2,65,000.
    Comparing with slab tax alone would wrongly give 7,000.
    """
    tax = _slab_tax("10010000", rules, "new")
    assert surcharge(Decimal("10010000"), tax, rules, "new") == Decimal("265000")


def test_full_surcharge_well_above_threshold(rules: TaxRules) -> None:
    tax = _slab_tax("6000000", rules, "new")  # 13,80,000
    assert surcharge(Decimal("6000000"), tax, rules, "new") == Decimal("138000")


def test_old_regime_37_percent_band(rules: TaxRules) -> None:
    tax = _slab_tax("60000000", rules, "old")  # 1,78,12,500
    assert surcharge(Decimal("60000000"), tax, rules, "old") == Decimal("6590625")


# --- Step 5: rebate comes before cess -----------------------------------------


def test_cess_is_charged_on_tax_after_rebate(rules: TaxRules) -> None:
    sheet = compute_tax(Decimal("1210000"), rules, "new")
    # 4% of the 10,000 left after the rebate, not of the 61,500 slab tax.
    assert sheet.cess == Decimal("400")
    assert sheet.total_tax == Decimal("10400")


# --- Step 6: section 288B -----------------------------------------------------


def test_288b_rounds_tax_payable(rules: TaxRules) -> None:
    # Rs 12,00,010 just above the limit: tax after relief is Rs 10, cess 0.40.
    sheet = compute_tax(Decimal("1200010"), rules, "new")
    assert sheet.total_tax == Decimal("10.40")
    assert sheet.tax_payable == Decimal("10")


def test_288b_keeps_the_sign_of_a_refund() -> None:
    assert round_288b(Decimal("-1245")) == Decimal("-1250")


def test_total_income_is_rounded_before_tax(rules: TaxRules) -> None:
    sheet = compute_tax(Decimal("1200004.99"), rules, "new")
    assert sheet.total_income_rounded == Decimal("1200000")
    assert sheet.tax_payable == 0  # full rebate on the rounded figure


def test_every_line_is_decimal(rules: TaxRules) -> None:
    sheet = compute_tax(Decimal("5010000"), rules, "old")
    for name in TaxComputation.model_fields:
        if name != "regime":
            assert type(getattr(sheet, name)) is Decimal, name


# --- Golden scenarios: the official calculator's figures ----------------------

SCENARIOS: list[dict[str, Any]] = _load("tests/fixtures/scenarios.json")["scenarios"]
CASES = [(s, r) for s in SCENARIOS for r in ("new", "old")]
CASE_IDS = [f"{s['id']}-{r}" for s, r in CASES]

# scenarios.json key -> TaxComputation field
LINES = {
    "tax_before_rebate": "tax_on_total_income",
    "rebate_87a": "rebate_87a",
    "surcharge": "surcharge",
    "cess": "cess",
    "total_tax": "tax_payable",
}


@pytest.mark.parametrize(("scenario", "regime"), CASES, ids=CASE_IDS)
def test_golden_scenario_tax(
    rules: TaxRules, scenario: dict[str, Any], regime: Regime
) -> None:
    expected = scenario["expected"][regime]
    sheet = compute_tax(Decimal(expected["total_income"]), rules, regime)

    for key, field in LINES.items():
        assert getattr(sheet, field) == Decimal(expected[key]), key
