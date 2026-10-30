import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from itr.domain.rules import Regime

Amount = Annotated[Decimal, Field(ge=0)]
SignedAmount = Decimal

PAN_PATTERN = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
TAN_PATTERN = re.compile(r"[A-Z]{4}[0-9]{5}[A-Z]")
ASSESSMENT_YEAR_PATTERN = re.compile(r"(\d{4})-(\d{2})")


class Form16Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class QuarterlyTds(Form16Model):
    quarter: Literal["Q1", "Q2", "Q3", "Q4"]
    receipt_number: str
    amount_paid_or_credited: Amount
    tax_deducted: Amount
    tax_deposited: Amount


class PartA(Form16Model):
    certificate_number: str
    employer_name: str
    employer_address: str
    employee_name: str
    employee_address: str
    employer_pan: str
    employer_tan: str
    employee_pan: str
    assessment_year: str
    period_from: date
    period_to: date
    quarters: tuple[QuarterlyTds, ...]
    total_amount_paid_or_credited: Amount
    total_tax_deducted: Amount
    total_tax_deposited: Amount

    @field_validator("employee_pan")
    @classmethod
    def check_employee_pan(cls, value: str) -> str:
        if not PAN_PATTERN.fullmatch(value):
            raise ValueError(f"employee PAN {value!r} is not in the format AAAAA9999A")
        return value

    @field_validator("employer_pan")
    @classmethod
    def check_employer_pan(cls, value: str) -> str:
        if value != "PANNOTREQD" and not PAN_PATTERN.fullmatch(value):
            raise ValueError(f"employer PAN {value!r} is not in the format AAAAA9999A")
        return value

    @field_validator("employer_tan")
    @classmethod
    def check_employer_tan(cls, value: str) -> str:
        if not TAN_PATTERN.fullmatch(value):
            raise ValueError(f"TAN {value!r} is not in the format AAAA99999A")
        return value

    @field_validator("assessment_year")
    @classmethod
    def check_assessment_year(cls, value: str) -> str:
        match = ASSESSMENT_YEAR_PATTERN.fullmatch(value)
        if match is None or (int(match.group(1)) + 1) % 100 != int(match.group(2)):
            raise ValueError(f"assessment year {value!r} must look like 2026-27")
        return value

    @model_validator(mode="after")
    def check_period(self) -> Self:
        if self.period_from > self.period_to:
            raise ValueError("period with the employer ends before it starts")
        return self


class GrossSalary(Form16Model):
    section_17_1: Amount
    section_17_2: Amount
    section_17_3: Amount
    total: Amount
    other_employers: Amount


class ClauseAmount(Form16Model):
    clause: str
    amount: Amount


class Section10Exemptions(Form16Model):
    travel_concession_10_5: Amount
    gratuity_10_10: Amount
    commuted_pension_10_10a: Amount
    leave_encashment_10_10aa: Amount
    house_rent_allowance_10_13a: Amount
    special_allowances_10_14: Amount
    other: tuple[ClauseAmount, ...] = ()
    other_total: Amount
    total: Amount


class Section16Deductions(Form16Model):
    standard_deduction_16_ia: Amount
    entertainment_allowance_16_ii: Amount
    tax_on_employment_16_iii: Amount
    total: Amount


class OtherIncomeReported(Form16Model):
    house_property: SignedAmount
    other_sources: Amount
    total: SignedAmount


class DeductionLine(Form16Model):
    gross: Amount | None = None
    qualifying: Amount | None = None
    deductible: Amount


class ChapterVIA(Form16Model):
    section_80c: DeductionLine
    section_80ccc: DeductionLine
    section_80ccd_1: DeductionLine
    total_80c_80ccc_80ccd_1: DeductionLine
    section_80ccd_1b: DeductionLine
    section_80ccd_2: DeductionLine
    section_80d: DeductionLine
    section_80e: DeductionLine
    section_80cch_employee: DeductionLine
    section_80cch_government: DeductionLine
    section_80g: DeductionLine
    section_80tta: DeductionLine
    other: tuple[ClauseAmount, ...] = ()
    other_total: Amount
    aggregate: Amount


class TaxLines(Form16Model):
    total_taxable_income: Amount
    tax_on_total_income: Amount
    rebate_87a: Amount
    surcharge: Amount
    health_and_education_cess: Amount
    tax_payable: Amount
    relief_section_89: Amount
    tds_form_12baa: Amount
    tcs_form_12baa: Amount
    net_tax_payable: SignedAmount


class PartB(Form16Model):
    opted_out_of_new_regime: bool
    gross_salary: GrossSalary
    section_10: Section10Exemptions
    salary_from_current_employer: Amount
    section_16: Section16Deductions
    income_under_salaries: Amount
    other_income: OtherIncomeReported
    gross_total_income: SignedAmount
    chapter_via: ChapterVIA
    tax: TaxLines

    @property
    def regime_used(self) -> Regime:
        return "old" if self.opted_out_of_new_regime else "new"


class SalaryBreakup(Form16Model):
    basic: Amount
    dearness_allowance: Amount
    dearness_allowance_counts_for_retirement: bool | None = None
    house_rent_allowance: Amount
    other_allowances: Amount


class UnrecognisedLine(Form16Model):
    """An amount printed on the document that is not a line of the official
    form, such as capital gains in an other-income annexure. The engine can't
    model these, so to_tax_input refuses to compute (ADR 0004)."""

    where: str  # where it is printed, e.g. "Annexure: other income"
    description: str
    amount: SignedAmount


class Form16(Form16Model):
    part_a: PartA
    part_b: PartB
    salary_breakup: SalaryBreakup | None = None
    unrecognised_lines: tuple[UnrecognisedLine, ...] = ()
