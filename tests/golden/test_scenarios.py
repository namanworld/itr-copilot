import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from itr.domain.inputs import TaxInput
from itr.domain.rules import Regime, TaxRules
from itr.domain.tax import compare, compute

ROOT = Path(__file__).parents[2]


def _load(relative_path: str) -> Any:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    return TaxRules.model_validate(_load("rules/fy2025_26.json"))


SCENARIOS: list[dict[str, Any]] = _load("tests/fixtures/scenarios.json")["scenarios"]
CASES = [(s, r) for s in SCENARIOS for r in ("new", "old")]


@pytest.mark.parametrize(
    ("scenario", "regime"), CASES, ids=[f"{s['id']}-{r}" for s, r in CASES]
)
def test_total_income_and_tax_match_calculator(
    rules: TaxRules, scenario: dict[str, Any], regime: Regime
) -> None:
    expected = scenario["expected"][regime]
    sheet = compute(TaxInput.model_validate(scenario["inputs"]), rules, regime)

    assert sheet.income.total_income == Decimal(expected["total_income"])
    assert sheet.tax.tax_payable == Decimal(expected["total_tax"])


def _expected_cheaper(new_tax: Decimal, old_tax: Decimal) -> Regime | None:
    """Worked out from the calculator's two totals, independently of compare()."""
    if new_tax < old_tax:
        return "new"
    if old_tax < new_tax:
        return "old"
    return None


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_cheaper_regime_matches_calculator(
    rules: TaxRules, scenario: dict[str, Any]
) -> None:
    new_tax = Decimal(scenario["expected"]["new"]["total_tax"])
    old_tax = Decimal(scenario["expected"]["old"]["total_tax"])

    result = compare(TaxInput.model_validate(scenario["inputs"]), rules)

    assert result.cheaper == _expected_cheaper(new_tax, old_tax)
    assert result.saving == abs(new_tax - old_tax)
