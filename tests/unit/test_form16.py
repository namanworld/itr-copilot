import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from itr.domain.form16 import Form16


def _sample_form16_data() -> dict[str, Any]:
    return {
        "part_a": {
            "certificate_number": "ABCD123456789",
            "employer_name": "Example Technologies Ltd",
            "employer_address": "10 Example Road, Mumbai",
            "employee_name": "Asha Sharma",
            "employee_address": "20 Sample Street, Mumbai",
            "employer_pan": "AAACE1234F",
            "employer_tan": "MUMC12345B",
            "employee_pan": "ABCDE1234F",
            "assessment_year": "2026-27",
            "period_from": "2025-04-01",
            "period_to": "2026-03-31",
            "quarters": [
                {
                    "quarter": "Q1",
                    "receipt_number": "Q1-001",
                    "amount_paid_or_credited": "300000",
                    "tax_deducted": "15000",
                    "tax_deposited": "15000",
                },
                {
                    "quarter": "Q2",
                    "receipt_number": "Q2-001",
                    "amount_paid_or_credited": "300000",
                    "tax_deducted": "15000",
                    "tax_deposited": "15000",
                },
                {
                    "quarter": "Q3",
                    "receipt_number": "Q3-001",
                    "amount_paid_or_credited": "300000",
                    "tax_deducted": "15000",
                    "tax_deposited": "15000",
                },
                {
                    "quarter": "Q4",
                    "receipt_number": "Q4-001",
                    "amount_paid_or_credited": "300000",
                    "tax_deducted": "15000",
                    "tax_deposited": "15000",
                },
            ],
            "total_amount_paid_or_credited": "1200000",
            "total_tax_deducted": "60000",
            "total_tax_deposited": "60000",
        },
        "part_b": {
            "opted_out_of_new_regime": False,
            "gross_salary": {
                "section_17_1": "1200000",
                "section_17_2": "0",
                "section_17_3": "0",
                "total": "1200000",
                "other_employers": "0",
            },
            "section_10": {
                "travel_concession_10_5": "0",
                "gratuity_10_10": "0",
                "commuted_pension_10_10a": "0",
                "leave_encashment_10_10aa": "0",
                "house_rent_allowance_10_13a": "0",
                "special_allowances_10_14": "0",
                "other_total": "0",
                "total": "0",
            },
            "salary_from_current_employer": "1200000",
            "section_16": {
                "standard_deduction_16_ia": "75000",
                "entertainment_allowance_16_ii": "0",
                "tax_on_employment_16_iii": "0",
                "total": "75000",
            },
            "income_under_salaries": "1125000",
            "other_income": {
                "house_property": "-12000",
                "other_sources": "1000",
                "total": "-11000",
            },
            "gross_total_income": "1114000",
            "chapter_via": {
                "section_80c": {"gross": "0", "qualifying": "0", "deductible": "0"},
                "section_80ccc": {
                    "gross": "0",
                    "qualifying": "0",
                    "deductible": "0",
                },
                "section_80ccd_1": {
                    "gross": "0",
                    "qualifying": "0",
                    "deductible": "0",
                },
                "total_80c_80ccc_80ccd_1": {
                    "gross": "0",
                    "qualifying": "0",
                    "deductible": "0",
                },
                "section_80ccd_1b": {
                    "gross": "0",
                    "qualifying": "0",
                    "deductible": "0",
                },
                "section_80ccd_2": {
                    "gross": "0",
                    "qualifying": "0",
                    "deductible": "0",
                },
                "section_80d": {"gross": "0", "qualifying": "0", "deductible": "0"},
                "section_80e": {"gross": "0", "qualifying": "0", "deductible": "0"},
                "section_80cch_employee": {
                    "gross": "0",
                    "qualifying": "0",
                    "deductible": "0",
                },
                "section_80cch_government": {
                    "gross": "0",
                    "qualifying": "0",
                    "deductible": "0",
                },
                "section_80g": {"gross": "0", "qualifying": "0", "deductible": "0"},
                "section_80tta": {"gross": "0", "qualifying": "0", "deductible": "0"},
                "other_total": "0",
                "aggregate": "0",
            },
            "tax": {
                "total_taxable_income": "1114000",
                "tax_on_total_income": "0",
                "rebate_87a": "0",
                "surcharge": "0",
                "health_and_education_cess": "0",
                "tax_payable": "0",
                "relief_section_89": "0",
                "tds_form_12baa": "0",
                "tcs_form_12baa": "0",
                "net_tax_payable": "-60000",
            },
        },
        "salary_breakup": {
            "basic": "600000",
            "dearness_allowance": "0",
            "dearness_allowance_counts_for_retirement": None,
            "house_rent_allowance": "300000",
            "other_allowances": "300000",
        },
    }


def test_complete_form16_loads_and_round_trips_through_json() -> None:
    form = Form16.model_validate(_sample_form16_data())

    assert Form16.model_validate_json(form.model_dump_json()) == form


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("part_a", "employee_pan"), "INVALID"),
        (("part_a", "employer_tan"), "INVALID"),
        (("part_a", "assessment_year"), "2026-28"),
    ],
)
def test_form16_rejects_invalid_pan_tan_and_assessment_year(
    path: tuple[str, str], value: str
) -> None:
    data = deepcopy(_sample_form16_data())
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError):
        Form16.model_validate(data)


def test_form16_accepts_pannotreqd_as_employer_pan() -> None:
    data = _sample_form16_data()
    data["part_a"]["employer_pan"] = "PANNOTREQD"

    form = Form16.model_validate(data)

    assert form.part_a.employer_pan == "PANNOTREQD"


def test_form16_accepts_negative_7a_but_rejects_negative_1d() -> None:
    data = _sample_form16_data()
    data["part_b"]["other_income"]["house_property"] = "-50000"

    form = Form16.model_validate(data)

    assert form.part_b.other_income.house_property == -50000

    data["part_b"]["gross_salary"]["total"] = "-1"
    with pytest.raises(ValidationError):
        Form16.model_validate(data)


def test_form16_regime_used_property_is_not_serialized() -> None:
    form = Form16.model_validate(_sample_form16_data())

    serialized = json.loads(form.model_dump_json())

    assert form.part_b.regime_used == "new"
    assert "regime_used" not in serialized["part_b"]
