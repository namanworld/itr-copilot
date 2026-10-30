from copy import deepcopy
from typing import Any

import pytest
from test_form16 import _sample_form16_data

from itr.domain.form16 import Form16
from itr.domain.form16_mapping import UserExtras, to_tax_input
from itr.domain.rules import TaxRules
from itr.infrastructure.rules.json_loader import load_rules


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    return load_rules("2025-26")


def _form16(data: dict[str, Any] | None = None) -> Form16:
    return Form16.model_validate(data if data is not None else _sample_form16_data())


def _finding(result: Any, *, line: str, severity: str) -> Any:
    return next(
        finding
        for finding in result.findings
        if finding.line == line and finding.severity == severity
    )


def test_nonzero_80ccd_2_blocks_mapping(rules: TaxRules) -> None:
    data = deepcopy(_sample_form16_data())
    line = data["part_b"]["chapter_via"]["section_80ccd_2"]
    line.update(gross="50000", qualifying="50000", deductible="50000")

    result = to_tax_input(_form16(data), UserExtras(), rules)

    assert result.blocked
    assert result.tax_input is None
    assert _finding(result, line="Part B 10(f)", severity="block")


def test_wrong_assessment_year_blocks_mapping(rules: TaxRules) -> None:
    data = deepcopy(_sample_form16_data())
    data["part_a"]["assessment_year"] = "2025-26"

    result = to_tax_input(_form16(data), UserExtras(), rules)

    assert result.blocked
    assert _finding(result, line="Part A", severity="block")


def test_nonresident_taxpayer_blocks_mapping(rules: TaxRules) -> None:
    result = to_tax_input(_form16(), UserExtras(resident=False), rules)

    assert result.blocked
    assert _finding(result, line="User", severity="block")


def test_claimed_hra_without_rent_city_blocks_mapping(rules: TaxRules) -> None:
    result = to_tax_input(
        _form16(),
        UserExtras(rent_paid="600000", city=None),
        rules,
    )

    assert result.blocked
    assert _finding(result, line="User", severity="block")


def test_claimed_hra_without_salary_breakup_blocks_mapping(rules: TaxRules) -> None:
    data = deepcopy(_sample_form16_data())
    data.pop("salary_breakup")

    result = to_tax_input(
        _form16(data),
        UserExtras(rent_paid="600000", city="Mumbai"),
        rules,
    )

    assert result.blocked
    assert _finding(result, line="Part B 2(e)", severity="block")


def test_new_regime_form_without_old_regime_facts_warns(rules: TaxRules) -> None:
    result = to_tax_input(_form16(), UserExtras(), rules)

    assert not result.blocked
    assert result.tax_input is not None
    finding = _finding(result, line="Part B line A", severity="warn")
    assert "professional tax" in finding.message
    assert "80C" in finding.message
    assert "80D" in finding.message


def test_unsplit_80d_amount_warns_that_parent_split_is_needed(
    rules: TaxRules,
) -> None:
    data = deepcopy(_sample_form16_data())
    data["part_b"]["opted_out_of_new_regime"] = True
    data["part_b"]["chapter_via"]["section_80d"].update(
        gross="30000", qualifying="30000", deductible="30000"
    )

    result = to_tax_input(_form16(data), UserExtras(), rules)

    assert not result.blocked
    finding = _finding(result, line="Part B 10(g)", severity="warn")
    assert "split out any premium for parents" in finding.message


def test_negative_7a_prefills_home_loan_interest_and_warns_to_confirm(
    rules: TaxRules,
) -> None:
    data = deepcopy(_sample_form16_data())
    data["part_b"]["opted_out_of_new_regime"] = True
    data["part_b"]["other_income"]["house_property"] = "-100000"
    data["part_b"]["other_income"]["total"] = "-99000"
    data["part_b"]["gross_total_income"] = "1026000"

    result = to_tax_input(_form16(data), UserExtras(), rules)

    assert not result.blocked
    assert result.tax_input is not None
    assert result.tax_input.self_occupied_home_loan_interest == 100000
    finding = _finding(result, line="Part B 7(a)", severity="warn")
    assert "confirm the full interest paid" in finding.message
