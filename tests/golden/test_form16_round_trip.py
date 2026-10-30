"""Build a Form 16 from each golden scenario, map it back, and get the same tax.

This proves to_tax_input loses nothing: every fact ends up in the right
TaxInput field, from the Form 16 or from the user. It does not prove the
engine is right; the golden scenario tests do that against the calculator.
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from itr.domain.form16 import Form16
from itr.domain.form16_mapping import to_tax_input
from itr.domain.inputs import TaxInput
from itr.domain.rules import Regime, TaxRules
from itr.domain.tax import compute
from itr.synthetic.form16_builder import extras_from_inputs, form16_from_inputs

ROOT = Path(__file__).parents[2]


def _load(relative_path: str) -> Any:
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    return json.loads(text, parse_float=Decimal)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    return TaxRules.model_validate(_load("rules/fy2025_26.json"))


SCENARIOS: list[dict[str, Any]] = _load("tests/fixtures/scenarios.json")["scenarios"]
# (scenario, regime the Form 16 was printed under, regime being computed)
CASES = [
    (s, printed, computed)
    for s in SCENARIOS
    for printed in ("new", "old")
    for computed in ("new", "old")
]


@pytest.mark.parametrize(
    ("scenario", "printed", "computed"),
    CASES,
    ids=[f"{s['id']}-{p}-form16-{c}-tax" for s, p, c in CASES],
)
def test_round_trip_gives_the_scenario_tax(
    rules: TaxRules,
    scenario: dict[str, Any],
    printed: Regime,
    computed: Regime,
) -> None:
    inputs = TaxInput.model_validate(scenario["inputs"])
    form16 = form16_from_inputs(inputs, rules, printed)

    result = to_tax_input(form16, extras_from_inputs(inputs), rules)

    assert result.findings == (), [f.message for f in result.findings]
    assert result.tax_input is not None
    tax = compute(result.tax_input, rules, computed).tax
    expected = scenario["expected"][computed]
    assert tax.tax_payable == Decimal(expected["total_tax"])
    assert tax.total_income == Decimal(expected["total_income"])


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_form16_survives_json(rules: TaxRules, scenario: dict[str, Any]) -> None:
    """The truth JSON of a synthetic PDF is a serialized Form16."""
    form16 = form16_from_inputs(
        TaxInput.model_validate(scenario["inputs"]), rules, "old"
    )
    assert Form16.model_validate_json(form16.model_dump_json()) == form16
