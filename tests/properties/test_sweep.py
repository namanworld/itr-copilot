import json
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

import pytest

from itr.domain.rules import Regime, TaxRules
from itr.domain.tax import compute_tax, regime_rules

ROOT = Path(__file__).parents[2]
STEP = 1_000


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


# 288B rounding can move tax payable by up to Rs 5 either way, at each end.
ROUNDING_SLACK = Decimal(10)


def _cliffs(rules: TaxRules, regime: Regime) -> list[int]:
    """Thresholds where the rules give no marginal relief, so tax may jump.

    In FY 2025-26 that is the old regime's 87A limit: Rs 5,00,000 pays nothing,
    Rs 5,01,000 pays the full slab tax. Read from the rules, not hard-coded.
    """
    regime_rule = regime_rules(rules, regime)
    points = []
    if not regime_rule.rebate_87a.marginal_relief:
        points.append(regime_rule.rebate_87a.taxable_income_limit)
    points += [
        b.min_income for b in regime_rule.surcharge_bands if not b.marginal_relief
    ]
    return [int(point) for point in points]


def _assert_never_falls(
    incomes: Iterable[int], rules: TaxRules, regime: Regime
) -> None:
    """Tax never falls, and never rises faster than income.

    Marginal relief caps tax *before cess* at the income above a threshold,
    so with cess on top, tax can rise by at most (1 + cess rate) times the
    rise in income. A missing relief shows up as a bigger jump than that.
    The one exception is a step across a threshold the rules give no relief.
    """
    max_rate = 1 + rules.cess.rate
    cliffs = _cliffs(rules, regime)
    previous_income, previous_tax = None, Decimal(0)
    for income in incomes:
        tax = compute_tax(Decimal(income), rules, regime).tax_payable
        assert tax >= 0, f"negative tax {tax} at {income}"
        if previous_income is not None:
            where = f"{regime} regime, {previous_income} to {income}"
            assert tax >= previous_tax, (
                f"{where}: tax fell from {previous_tax} to {tax}"
            )
            crosses_cliff = any(previous_income <= c < income for c in cliffs)
            allowed = max_rate * (income - previous_income) + ROUNDING_SLACK
            assert crosses_cliff or tax - previous_tax <= allowed, (
                f"{where}: tax rose by {tax - previous_tax}, more than {allowed}"
            )
        previous_income, previous_tax = income, tax


@pytest.mark.parametrize("regime", ["new", "old"])
def test_tax_never_falls_up_to_60_lakh(rules: TaxRules, regime: Regime) -> None:
    """Covers the 87A limit and the Rs 50 lakh surcharge threshold."""
    _assert_never_falls(range(0, 6_000_001, STEP), rules, regime)


def _thresholds(rules: TaxRules, regime: Regime) -> list[int]:
    """Every point where the rules change the tax: the 87A limit and each
    surcharge threshold. Read from the rules, so a new band is swept too."""
    regime_rule = regime_rules(rules, regime)
    points = [regime_rule.rebate_87a.taxable_income_limit]
    points += [band.min_income for band in regime_rule.surcharge_bands]
    return [int(point) for point in points]


@pytest.mark.parametrize("regime", ["new", "old"])
def test_tax_never_falls_around_every_threshold(
    rules: TaxRules, regime: Regime
) -> None:
    """Rs 2 lakh either side of each threshold, including Rs 1, 2 and 5 crore,
    which the first sweep doesn't reach."""
    for threshold in _thresholds(rules, regime):
        start = max(0, threshold - 200_000)
        _assert_never_falls(range(start, threshold + 200_001, STEP), rules, regime)


def test_old_regime_87a_cliff_is_intended(rules: TaxRules) -> None:
    """The old regime's 87A rebate has no marginal relief, so Rs 1,000 more
    income past Rs 5 lakh costs the whole slab tax. This pins that down, so
    the sweep's exception for it can't hide a real bug."""
    assert _cliffs(rules, "old") == [500000]
    assert _cliffs(rules, "new") == []
    at_limit = compute_tax(Decimal("500000"), rules, "old").tax_payable
    just_above = compute_tax(Decimal("501000"), rules, "old").tax_payable
    assert at_limit == 0
    assert just_above == Decimal("13210")  # 12,700 slab tax + 4% cess, rounded
