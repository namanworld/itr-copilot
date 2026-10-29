import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from itr.domain.inputs import TaxInput

SCENARIOS_FILE = Path(__file__).parents[2] / "tests/fixtures/scenarios.json"


def _load_scenarios() -> list[dict[str, Any]]:
    data = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
    scenarios: list[dict[str, Any]] = data["scenarios"]
    return scenarios


SCENARIOS = _load_scenarios()


def _scenario(scenario_id: str) -> dict[str, Any]:
    return next(s for s in SCENARIOS if s["id"] == scenario_id)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_every_scenario_input_loads(scenario: dict[str, Any]) -> None:
    TaxInput.model_validate(scenario["inputs"])


def test_values_arrive_as_decimal() -> None:
    inputs = TaxInput.model_validate(_scenario("03-metro-hra-old-wins")["inputs"])

    assert inputs.rent_paid == Decimal("540000")
    assert type(inputs.rent_paid) is Decimal
    assert inputs.deductions.section_80d_self_and_family == Decimal("60000")
    assert inputs.city == "Mumbai"


def test_missing_fields_default_to_zero() -> None:
    inputs = TaxInput.model_validate(_scenario("01-full-87a-rebate")["inputs"])

    assert inputs.rent_paid == 0
    assert inputs.deductions.section_80c == 0
    assert inputs.city is None


def test_misspelt_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaxInput.model_validate({"salary": {"gross": "1000000"}, "rent_payed": "5"})


def test_negative_amount_is_rejected() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        TaxInput.model_validate({"salary": {"gross": "-1"}})


def test_salary_parts_cannot_exceed_gross() -> None:
    with pytest.raises(ValidationError, match="more than gross"):
        TaxInput.model_validate({"salary": {"gross": "100", "basic": "200"}})


def test_inputs_cannot_be_changed_after_loading() -> None:
    inputs = TaxInput.model_validate(_scenario("01-full-87a-rebate")["inputs"])

    with pytest.raises(ValidationError):
        inputs.rent_paid = Decimal("1")  # type: ignore[misc]
