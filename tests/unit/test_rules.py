from decimal import Decimal

from itr.infrastructure.rules.json_loader import load_rules


def test_load_rules_accepts_surcharge_bands_starting_at_threshold() -> None:
    rules = load_rules("2025-26")

    assert rules.surcharge_bands[0].min_income == Decimal("5000000")
