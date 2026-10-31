"""Use case: the text of a Form 16 becomes a validated domain Form16.

One structured-output call fills in ExtractedForm16; this module then builds
the domain Form16 from it, so the domain's own validators (PAN and TAN
formats, the assessment year, no negative salary, ...) run on what the model
returned. If they reject it, the caller gets Form16ValidationError with the
raw extraction attached, never a half-valid Form16.
"""

from dataclasses import dataclass
from decimal import Decimal
from textwrap import dedent

from pydantic import ValidationError

from itr.application.form16_extraction import (
    ExtractedDeduction,
    ExtractedForm16,
)
from itr.application.ports import LLMClient, LLMUsage
from itr.domain.form16 import Form16

# Fields of the domain Form16 that the extraction doesn't read. They are
# always empty in an extracted Form16, and a diff against truth skips them.
NOT_EXTRACTED = (
    "part_a.quarters",
    "part_b.section_10.other",
    "part_b.chapter_via.other",
    "unrecognised_lines",
)


class Form16ValidationError(Exception):
    """The model's reply fits the extraction schema, but isn't a valid Form16."""

    def __init__(self, extracted: ExtractedForm16, problems: list[str]) -> None:
        super().__init__("the extracted Form 16 is invalid: " + "; ".join(problems))
        self.extracted = extracted
        self.problems = problems


@dataclass(frozen=True)
class Form16Extraction:
    form16: Form16
    extracted: ExtractedForm16  # what the model returned, before mapping
    usage: LLMUsage


def extract_form16(text: str, llm: LLMClient, system_prompt: str) -> Form16Extraction:
    prompt = dedent(f"""
        Extract the figures from this Form 16.

        <document>
        {text}
        </document>""").strip()

    result = llm.structured(system_prompt, prompt, ExtractedForm16)
    try:
        form16 = to_form16(result.output)
    except ValidationError as error:
        problems = [
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        ]
        raise Form16ValidationError(result.output, problems) from error
    return Form16Extraction(form16=form16, extracted=result.output, usage=result.usage)


def _column(text: str) -> Decimal | None:
    """A Chapter VI-A gross or qualifying cell: "" means the cell is blank."""
    return None if text == "" else Decimal(text)


def _deduction(entry: ExtractedDeduction) -> dict[str, Decimal | None]:
    return dict(
        gross=_column(entry.gross),
        qualifying=_column(entry.qualifying),
        deductible=Decimal(entry.deductible),
    )


def to_form16(extracted: ExtractedForm16) -> Form16:
    """Build the domain Form16. Raises pydantic.ValidationError when the
    domain rejects a value.

    The parts are plain dicts validated in one go, so an error's location is
    its full path, such as part_a.employee_pan.
    """
    a, b = extracted.part_a, extracted.part_b
    part_a = dict(
        certificate_number=a.certificate_number,
        employer_name=a.employer_name,
        employer_address=a.employer_address,
        employee_name=a.employee_name,
        employee_address=a.employee_address,
        employer_pan=a.employer_pan,
        employer_tan=a.employer_tan,
        employee_pan=a.employee_pan,
        assessment_year=a.assessment_year,
        period_from=a.period_from,
        period_to=a.period_to,
        quarters=(),  # not extracted
        total_amount_paid_or_credited=Decimal(a.total_amount_paid_or_credited),
        total_tax_deducted=Decimal(a.total_tax_deducted),
        total_tax_deposited=Decimal(a.total_tax_deposited),
    )
    part_b = dict(
        opted_out_of_new_regime=b.opted_out_of_new_regime == "YES",
        gross_salary=dict(
            section_17_1=Decimal(b.section_17_1),
            section_17_2=Decimal(b.section_17_2),
            section_17_3=Decimal(b.section_17_3),
            total=Decimal(b.gross_salary_total),
            other_employers=Decimal(b.other_employers),
        ),
        section_10=dict(
            travel_concession_10_5=Decimal(b.travel_concession_10_5),
            gratuity_10_10=Decimal(b.gratuity_10_10),
            commuted_pension_10_10a=Decimal(b.commuted_pension_10_10a),
            leave_encashment_10_10aa=Decimal(b.leave_encashment_10_10aa),
            house_rent_allowance_10_13a=Decimal(b.house_rent_allowance_10_13a),
            special_allowances_10_14=Decimal(b.special_allowances_10_14),
            other_total=Decimal(b.section_10_other_total),
            total=Decimal(b.section_10_total),
        ),
        salary_from_current_employer=Decimal(b.salary_from_current_employer),
        section_16=dict(
            standard_deduction_16_ia=Decimal(b.standard_deduction_16_ia),
            entertainment_allowance_16_ii=Decimal(b.entertainment_allowance_16_ii),
            tax_on_employment_16_iii=Decimal(b.tax_on_employment_16_iii),
            total=Decimal(b.section_16_total),
        ),
        income_under_salaries=Decimal(b.income_under_salaries),
        other_income=dict(
            house_property=Decimal(b.house_property),
            other_sources=Decimal(b.other_sources),
            total=Decimal(b.other_income_total),
        ),
        gross_total_income=Decimal(b.gross_total_income),
        chapter_via=dict(
            section_80c=_deduction(b.section_80c),
            section_80ccc=_deduction(b.section_80ccc),
            section_80ccd_1=_deduction(b.section_80ccd_1),
            total_80c_80ccc_80ccd_1=_deduction(b.total_80c_80ccc_80ccd_1),
            section_80ccd_1b=_deduction(b.section_80ccd_1b),
            section_80ccd_2=_deduction(b.section_80ccd_2),
            section_80d=_deduction(b.section_80d),
            section_80e=_deduction(b.section_80e),
            section_80cch_employee=_deduction(b.section_80cch_employee),
            section_80cch_government=_deduction(b.section_80cch_government),
            section_80g=_deduction(b.section_80g),
            section_80tta=_deduction(b.section_80tta),
            other_total=Decimal(b.chapter_via_other_total),
            aggregate=Decimal(b.chapter_via_aggregate),
        ),
        tax=dict(
            total_taxable_income=Decimal(b.total_taxable_income),
            tax_on_total_income=Decimal(b.tax_on_total_income),
            rebate_87a=Decimal(b.rebate_87a),
            surcharge=Decimal(b.surcharge),
            health_and_education_cess=Decimal(b.health_and_education_cess),
            tax_payable=Decimal(b.tax_payable),
            relief_section_89=Decimal(b.relief_section_89),
            tds_form_12baa=Decimal(b.tds_form_12baa),
            tcs_form_12baa=Decimal(b.tcs_form_12baa),
            net_tax_payable=Decimal(b.net_tax_payable),
        ),
    )
    breakup = None
    if extracted.salary_breakup_printed:
        s = extracted.salary_breakup
        counts = s.dearness_allowance_counts_for_retirement
        breakup = dict(
            basic=Decimal(s.basic),
            dearness_allowance=Decimal(s.dearness_allowance),
            dearness_allowance_counts_for_retirement=(
                None if counts == "NOT_STATED" else counts == "YES"
            ),
            house_rent_allowance=Decimal(s.house_rent_allowance),
            other_allowances=Decimal(s.other_allowances),
        )
    return Form16.model_validate(
        {"part_a": part_a, "part_b": part_b, "salary_breakup": breakup}
    )
