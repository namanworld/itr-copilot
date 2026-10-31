"""What Claude fills in when it reads a Form 16: a flat, all-strings model.

This is not the domain Form16. It is shaped for structured outputs, which
can't take the domain model as it is (ADR 0004 keeps the two apart anyway):

- Every field is required. Nothing is Optional, so the schema stays under the
  limit on optional fields.
- Amounts are strings matching AMOUNT, not Decimals. Pydantic would turn each
  Decimal into anyOf(number, string), and there's a limit on those too.
- A line that isn't printed is "0". The two optional Chapter VI-A columns
  (gross, qualifying) are "" when the column is blank, because the form
  distinguishes a blank cell from a printed 0.
- Part A's quarter-by-quarter table and the "other clause" lists are left
  out. Only the Part A totals are read.

The pattern on amounts is checked by Pydantic after the reply arrives, not
while Claude writes it (the SDK moves it into the field description), so the
prompt states the format as well.

It lives in the application layer, not infrastructure/llm, because the
extract use case must import it and the application layer can't import
infrastructure. It has no vendor code in it.
"""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AmountText = Annotated[str, Field(pattern=r"^-?\d+(\.\d{1,2})?$")]
ColumnText = Annotated[str, Field(pattern=r"^(-?\d+(\.\d{1,2})?)?$")]
YesNo = Literal["YES", "NO"]


def line(number: str) -> Any:
    return Field(description=f"Part B line {number}")


class Extracted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExtractedPartA(Extracted):
    certificate_number: str
    employer_name: str
    employer_address: str
    employee_name: str
    employee_address: str
    employer_pan: str
    employer_tan: str
    employee_pan: str
    assessment_year: str = Field(description="as printed, e.g. 2026-27")
    period_from: date
    period_to: date
    total_amount_paid_or_credited: AmountText
    total_tax_deducted: AmountText
    total_tax_deposited: AmountText


class ExtractedDeduction(Extracted):
    gross: ColumnText
    qualifying: ColumnText
    deductible: AmountText


class ExtractedPartB(Extracted):
    opted_out_of_new_regime: YesNo = Field(
        description="Answer to 'Whether opting out of taxation u/s 115BAC(1A)?'"
    )
    section_17_1: AmountText = line("1(a)")
    section_17_2: AmountText = line("1(b)")
    section_17_3: AmountText = line("1(c)")
    gross_salary_total: AmountText = line("1(d)")
    other_employers: AmountText = line("1(e)")
    travel_concession_10_5: AmountText = line("2(a)")
    gratuity_10_10: AmountText = line("2(b)")
    commuted_pension_10_10a: AmountText = line("2(c)")
    leave_encashment_10_10aa: AmountText = line("2(d)")
    house_rent_allowance_10_13a: AmountText = line("2(e)")
    special_allowances_10_14: AmountText = line("2(f)")
    section_10_other_total: AmountText = line("2(h)")
    section_10_total: AmountText = line("2(i)")
    salary_from_current_employer: AmountText = line("3")
    standard_deduction_16_ia: AmountText = line("4(a)")
    entertainment_allowance_16_ii: AmountText = line("4(b)")
    tax_on_employment_16_iii: AmountText = line("4(c)")
    section_16_total: AmountText = line("5")
    income_under_salaries: AmountText = line("6")
    house_property: AmountText = line("7(a)")
    other_sources: AmountText = line("7(b)")
    other_income_total: AmountText = line("8")
    gross_total_income: AmountText = line("9")
    section_80c: ExtractedDeduction = line("10(a)")
    section_80ccc: ExtractedDeduction = line("10(b)")
    section_80ccd_1: ExtractedDeduction = line("10(c)")
    total_80c_80ccc_80ccd_1: ExtractedDeduction = line("10(d)")
    section_80ccd_1b: ExtractedDeduction = line("10(e)")
    section_80ccd_2: ExtractedDeduction = line("10(f)")
    section_80d: ExtractedDeduction = line("10(g)")
    section_80e: ExtractedDeduction = line("10(h)")
    section_80cch_employee: ExtractedDeduction = line("10(i)")
    section_80cch_government: ExtractedDeduction = line("10(j)")
    section_80g: ExtractedDeduction = line("10(k)")
    section_80tta: ExtractedDeduction = line("10(l)")
    chapter_via_other_total: AmountText = line("10(n)")
    chapter_via_aggregate: AmountText = line("11")
    total_taxable_income: AmountText = line("12")
    tax_on_total_income: AmountText = line("13")
    rebate_87a: AmountText = line("14")
    surcharge: AmountText = line("15")
    health_and_education_cess: AmountText = line("16")
    tax_payable: AmountText = line("17")
    relief_section_89: AmountText = line("18")
    tds_form_12baa: AmountText = line("19")
    tcs_form_12baa: AmountText = line("20")
    net_tax_payable: AmountText = line("21")


class ExtractedSalaryBreakup(Extracted):
    basic: AmountText
    dearness_allowance: AmountText
    house_rent_allowance: AmountText
    other_allowances: AmountText
    dearness_allowance_counts_for_retirement: Literal["YES", "NO", "NOT_STATED"]


class ExtractedForm16(Extracted):
    part_a: ExtractedPartA
    part_b: ExtractedPartB
    salary_breakup_printed: bool = Field(
        description="true only if the employer attached a salary breakup"
    )
    salary_breakup: ExtractedSalaryBreakup = Field(
        description='all "0" and NOT_STATED when salary_breakup_printed is false'
    )
