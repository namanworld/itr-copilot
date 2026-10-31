"""Print a Form 16 from a TaxInput, the way an employer would.

Shared by the dataset generator and the round-trip test, so the two can never
drift apart.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from itr.domain.form16 import (
    ChapterVIA,
    DeductionLine,
    Form16,
    GrossSalary,
    OtherIncomeReported,
    PartA,
    PartB,
    QuarterlyTds,
    SalaryBreakup,
    Section10Exemptions,
    Section16Deductions,
    TaxLines,
)
from itr.domain.form16_mapping import UserExtras
from itr.domain.inputs import TaxInput
from itr.domain.rules import Regime, TaxRules
from itr.domain.tax import compute

ZERO = Decimal(0)


@dataclass(frozen=True)
class Identity:
    """Who the certificate is for and from: Part A's non-money fields."""

    certificate_number: str
    employer_name: str
    employer_address: str
    employer_pan: str
    employer_tan: str
    employee_name: str
    employee_address: str
    employee_pan: str
    period_from: date
    period_to: date


SAMPLE_IDENTITY = Identity(
    certificate_number="SYN-0001",
    employer_name="Specimen Kestrel Analytics Private Limited",
    employer_address="Pune",
    employer_pan="AAACK1234F",
    employer_tan="PNEK12345B",
    employee_name="Asha Sampleton",
    employee_address="Pune",
    employee_pan="ABCPS1234K",
    period_from=date(2025, 4, 1),
    period_to=date(2026, 3, 31),
)


def _quarters(gross: Decimal, tax: Decimal) -> tuple[QuarterlyTds, ...]:
    """Spread salary and tax over four quarters; any remainder goes in Q4."""
    salary_part, tax_part = gross // 4, tax // 4
    rows = []
    for quarter in ("Q1", "Q2", "Q3", "Q4"):
        last = quarter == "Q4"
        paid = gross - 3 * salary_part if last else salary_part
        deducted = tax - 3 * tax_part if last else tax_part
        rows.append(
            QuarterlyTds(
                quarter=quarter,
                receipt_number=f"SYN{quarter}",
                amount_paid_or_credited=paid,
                tax_deducted=deducted,
                tax_deposited=deducted,
            )
        )
    return tuple(rows)


def _line(deductible: Decimal, gross: Decimal | None = None) -> DeductionLine:
    return DeductionLine(gross=gross, deductible=deductible)


def form16_from_inputs(
    inputs: TaxInput,
    rules: TaxRules,
    regime: Regime,
    identity: Identity = SAMPLE_IDENTITY,
) -> Form16:
    """Print a Form 16 the way an employer computing TDS under `regime` would.

    Every figure comes from compute(), so the document is internally
    consistent. Lines the engine can't model are left at zero.
    """
    # The employer computes TDS without the employee's bank interest, which it
    # never sees; line 7(b) is printed as zero to match.
    employer_view = inputs.model_copy(
        update={"savings_interest": ZERO, "other_interest": ZERO}
    )
    sheet = compute(employer_view, rules, regime)
    income, tax = sheet.income, sheet.tax
    salary = inputs.salary
    claims = inputs.deductions
    old = regime == "old"

    part_a = PartA(
        certificate_number=identity.certificate_number,
        employer_name=identity.employer_name,
        employer_address=identity.employer_address,
        employee_name=identity.employee_name,
        employee_address=identity.employee_address,
        employer_pan=identity.employer_pan,
        employer_tan=identity.employer_tan,
        employee_pan=identity.employee_pan,
        assessment_year=rules.assessment_year,
        period_from=identity.period_from,
        period_to=identity.period_to,
        quarters=_quarters(salary.gross, tax.tax_payable),
        total_amount_paid_or_credited=salary.gross,
        total_tax_deducted=tax.tax_payable,
        total_tax_deposited=tax.tax_payable,
    )

    exempt_total = income.hra_exemption
    section_16_total = income.standard_deduction + income.professional_tax
    # Section 80A(2) caps the deductions at gross total income, so the
    # deductible columns are cut back to add up to the engine's total.
    allowed = income.deductions_total
    deductible_80c = min(income.deduction_80c, allowed)
    deductible_80d = min(income.deduction_80d, allowed - deductible_80c)
    deduction_80c = _line(deductible_80c, claims.section_80c if old else ZERO)
    claimed_80d = claims.section_80d_self_and_family + claims.section_80d_parents
    deduction_80d = _line(deductible_80d, claimed_80d if old else ZERO)
    part_b = PartB(
        opted_out_of_new_regime=old,
        gross_salary=GrossSalary(
            section_17_1=salary.gross,
            section_17_2=ZERO,
            section_17_3=ZERO,
            total=salary.gross,
            other_employers=ZERO,
        ),
        section_10=Section10Exemptions(
            travel_concession_10_5=ZERO,
            gratuity_10_10=ZERO,
            commuted_pension_10_10a=ZERO,
            leave_encashment_10_10aa=ZERO,
            house_rent_allowance_10_13a=income.hra_exemption,
            special_allowances_10_14=ZERO,
            other_total=ZERO,
            total=exempt_total,
        ),
        salary_from_current_employer=salary.gross - exempt_total,
        section_16=Section16Deductions(
            standard_deduction_16_ia=income.standard_deduction,
            entertainment_allowance_16_ii=ZERO,
            tax_on_employment_16_iii=income.professional_tax,
            total=section_16_total,
        ),
        income_under_salaries=income.income_from_salary,
        other_income=OtherIncomeReported(
            house_property=income.house_property_income,
            other_sources=ZERO,  # interest comes from banks, not the employer
            total=income.house_property_income,
        ),
        gross_total_income=income.income_from_salary + income.house_property_income,
        chapter_via=ChapterVIA(
            section_80c=deduction_80c,
            section_80ccc=_line(ZERO),
            section_80ccd_1=_line(ZERO),
            total_80c_80ccc_80ccd_1=_line(deductible_80c),
            section_80ccd_1b=_line(ZERO),
            section_80ccd_2=_line(ZERO),
            section_80d=deduction_80d,
            section_80e=_line(ZERO),
            section_80cch_employee=_line(ZERO),
            section_80cch_government=_line(ZERO),
            section_80g=_line(ZERO),
            section_80tta=_line(ZERO),
            other_total=ZERO,
            aggregate=allowed,
        ),
        tax=TaxLines(
            total_taxable_income=tax.total_income_rounded,
            tax_on_total_income=tax.tax_on_total_income,
            rebate_87a=tax.rebate_87a,
            surcharge=tax.surcharge,
            health_and_education_cess=tax.cess.quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            tax_payable=tax.tax_payable,
            relief_section_89=ZERO,
            tds_form_12baa=ZERO,
            tcs_form_12baa=ZERO,
            net_tax_payable=tax.tax_payable,
        ),
    )

    has_breakup = any((salary.basic, salary.dearness_allowance, salary.hra_received))
    breakup = (
        SalaryBreakup(
            basic=salary.basic,
            dearness_allowance=salary.dearness_allowance,
            dearness_allowance_counts_for_retirement=True,
            house_rent_allowance=salary.hra_received,
            other_allowances=salary.other_allowances,
        )
        if has_breakup
        else None
    )
    return Form16(part_a=part_a, part_b=part_b, salary_breakup=breakup)


def extras_from_inputs(inputs: TaxInput) -> UserExtras:
    """Everything the user would tell us. Old-regime facts are always stated,
    so the new-regime Form 16 can still produce a full old-regime result."""
    claims = inputs.deductions
    return UserExtras(
        rent_paid=inputs.rent_paid,
        city=inputs.city,
        savings_interest=inputs.savings_interest,
        other_interest=inputs.other_interest,
        self_or_family_has_senior_citizen=inputs.self_or_family_has_senior_citizen,
        parents_include_senior_citizen=inputs.parents_include_senior_citizen,
        professional_tax=inputs.professional_tax,
        section_80c=claims.section_80c,
        section_80d_self_and_family=claims.section_80d_self_and_family,
        section_80d_parents=claims.section_80d_parents,
        self_occupied_home_loan_interest=inputs.self_occupied_home_loan_interest,
    )
