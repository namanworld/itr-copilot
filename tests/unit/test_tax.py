import json
from decimal import Decimal
from pathlib import Path

import pytest

from itr.domain.rules import Slab, TaxRules
from itr.domain.tax import slab_tax

RULES_FILE = Path(__file__).parents[2] / "rules/fy2025_26.json"


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    data = json.loads(RULES_FILE.read_text(encoding="utf-8"), parse_float=Decimal)
    return TaxRules.model_validate(data)


@pytest.fixture(scope="module")
def new_slabs(rules: TaxRules) -> tuple[Slab, ...]:
    return rules.new_regime.slabs


@pytest.fixture(scope="module")
def old_slabs(rules: TaxRules) -> tuple[Slab, ...]:
    return rules.old_regime.slabs


# The two anchors named in the slice brief.


def test_new_regime_tax_on_12_lakh_is_60000(new_slabs: tuple[Slab, ...]) -> None:
    assert slab_tax(Decimal("1200000"), new_slabs) == Decimal("60000")


def test_old_regime_tax_on_10_lakh_is_112500(old_slabs: tuple[Slab, ...]) -> None:
    assert slab_tax(Decimal("1000000"), old_slabs) == Decimal("112500")


# Zero, every slab boundary, and one rupee above each boundary.

NEW_REGIME = [
    ("0", "0"),
    ("400000", "0"),
    ("400001", "0.05"),
    ("800000", "20000"),
    ("800001", "20000.10"),
    ("1200000", "60000"),
    ("1200001", "60000.15"),
    ("1600000", "120000"),
    ("1600001", "120000.20"),
    ("2000000", "200000"),
    ("2000001", "200000.25"),
    ("2400000", "300000"),
    ("2400001", "300000.30"),
    # Large income: Rs 3,00,000 + 30% of Rs 76,00,000.
    ("10000000", "2580000"),
]

OLD_REGIME_BELOW_60 = [
    ("0", "0"),
    ("250000", "0"),
    ("250001", "0.05"),
    ("500000", "12500"),
    ("500001", "12500.20"),
    ("1000000", "112500"),
    ("1000001", "112500.30"),
    # Large income: Rs 1,12,500 + 30% of Rs 90,00,000.
    ("10000000", "2812500"),
]


@pytest.mark.parametrize(
    ("income", "expected"), NEW_REGIME, ids=[i for i, _ in NEW_REGIME]
)
def test_new_regime_slab_tax(
    new_slabs: tuple[Slab, ...], income: str, expected: str
) -> None:
    assert slab_tax(Decimal(income), new_slabs) == Decimal(expected)


@pytest.mark.parametrize(
    ("income", "expected"), OLD_REGIME_BELOW_60, ids=[i for i, _ in OLD_REGIME_BELOW_60]
)
def test_old_regime_slab_tax(
    old_slabs: tuple[Slab, ...], income: str, expected: str
) -> None:
    assert slab_tax(Decimal(income), old_slabs) == Decimal(expected)


# Money ADR: Decimal in, Decimal out, and no rounding inside the helper.


def test_result_is_decimal(new_slabs: tuple[Slab, ...]) -> None:
    assert type(slab_tax(Decimal("1234567"), new_slabs)) is Decimal
    assert type(slab_tax(Decimal("0"), new_slabs)) is Decimal


def test_paise_are_kept_exactly(new_slabs: tuple[Slab, ...]) -> None:
    # 50 paise above Rs 4 lakh at 5% is 2.5 paise. Nothing is rounded here;
    # rounding happens only where the rounding ADR says.
    assert slab_tax(Decimal("400000.50"), new_slabs) == Decimal("0.025")


def test_negative_income_is_rejected(new_slabs: tuple[Slab, ...]) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        slab_tax(Decimal("-1"), new_slabs)
