"""Use case: the text of a Form 16 becomes a validated domain Form16.

One structured-output call returns ExtractedForm16: identity fields plus one
row per printed amount. to_form16 turns the rows into the domain Form16, so
the domain's own validators (PAN and TAN formats, the assessment year, no
negative salary, ...) run on what the model returned. Anything wrong, in the
rows or in the domain, raises Form16ValidationError with every problem and
the raw extraction attached, never a half-valid Form16.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from itr.application.form16_extraction import ExtractedForm16, Row
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

# Chapter VI-A totals with a single amount. Their labels say "deductible", so
# the model may label the amount that way; it is the same single value.
DEDUCTIBLE_TOTALS = ("10(n)", "11")

# Where each single-amount line goes in the domain Form16.
AMOUNT_LINES: dict[str, tuple[str, ...]] = {
    "A:total_paid": ("part_a", "total_amount_paid_or_credited"),
    "A:total_deducted": ("part_a", "total_tax_deducted"),
    "A:total_deposited": ("part_a", "total_tax_deposited"),
    "1(a)": ("part_b", "gross_salary", "section_17_1"),
    "1(b)": ("part_b", "gross_salary", "section_17_2"),
    "1(c)": ("part_b", "gross_salary", "section_17_3"),
    "1(d)": ("part_b", "gross_salary", "total"),
    "1(e)": ("part_b", "gross_salary", "other_employers"),
    "2(a)": ("part_b", "section_10", "travel_concession_10_5"),
    "2(b)": ("part_b", "section_10", "gratuity_10_10"),
    "2(c)": ("part_b", "section_10", "commuted_pension_10_10a"),
    "2(d)": ("part_b", "section_10", "leave_encashment_10_10aa"),
    "2(e)": ("part_b", "section_10", "house_rent_allowance_10_13a"),
    "2(f)": ("part_b", "section_10", "special_allowances_10_14"),
    "2(h)": ("part_b", "section_10", "other_total"),
    "2(i)": ("part_b", "section_10", "total"),
    "3": ("part_b", "salary_from_current_employer"),
    "4(a)": ("part_b", "section_16", "standard_deduction_16_ia"),
    "4(b)": ("part_b", "section_16", "entertainment_allowance_16_ii"),
    "4(c)": ("part_b", "section_16", "tax_on_employment_16_iii"),
    "5": ("part_b", "section_16", "total"),
    "6": ("part_b", "income_under_salaries"),
    "7(a)": ("part_b", "other_income", "house_property"),
    "7(b)": ("part_b", "other_income", "other_sources"),
    "8": ("part_b", "other_income", "total"),
    "9": ("part_b", "gross_total_income"),
    "10(n)": ("part_b", "chapter_via", "other_total"),
    "11": ("part_b", "chapter_via", "aggregate"),
    "12": ("part_b", "tax", "total_taxable_income"),
    "13": ("part_b", "tax", "tax_on_total_income"),
    "14": ("part_b", "tax", "rebate_87a"),
    "15": ("part_b", "tax", "surcharge"),
    "16": ("part_b", "tax", "health_and_education_cess"),
    "17": ("part_b", "tax", "tax_payable"),
    "18": ("part_b", "tax", "relief_section_89"),
    "19": ("part_b", "tax", "tds_form_12baa"),
    "20": ("part_b", "tax", "tcs_form_12baa"),
    "21": ("part_b", "tax", "net_tax_payable"),
}
# Chapter VI-A lines, which have gross, qualifying and deductible columns.
DEDUCTION_LINES: dict[str, tuple[str, ...]] = {
    "10(a)": ("part_b", "chapter_via", "section_80c"),
    "10(b)": ("part_b", "chapter_via", "section_80ccc"),
    "10(c)": ("part_b", "chapter_via", "section_80ccd_1"),
    "10(d)": ("part_b", "chapter_via", "total_80c_80ccc_80ccd_1"),
    "10(e)": ("part_b", "chapter_via", "section_80ccd_1b"),
    "10(f)": ("part_b", "chapter_via", "section_80ccd_2"),
    "10(g)": ("part_b", "chapter_via", "section_80d"),
    "10(h)": ("part_b", "chapter_via", "section_80e"),
    "10(i)": ("part_b", "chapter_via", "section_80cch_employee"),
    "10(j)": ("part_b", "chapter_via", "section_80cch_government"),
    "10(k)": ("part_b", "chapter_via", "section_80g"),
    "10(l)": ("part_b", "chapter_via", "section_80tta"),
}
BREAKUP_LINES: dict[str, str] = {
    "breakup:basic": "basic",
    "breakup:da": "dearness_allowance",
    "breakup:hra": "house_rent_allowance",
    "breakup:other": "other_allowances",
}
# Lines every Form 16 prints. A missing row here is a misread, not a zero.
REQUIRED_LINES = ("1(d)", "6", "9", "12", "17")


class Form16ValidationError(Exception):
    """The model's reply fits the extraction schema, but isn't a valid Form16."""

    def __init__(
        self,
        extracted: ExtractedForm16,
        problems: list[str],
        usage: LLMUsage | None = None,
    ) -> None:
        super().__init__("the extracted Form 16 is invalid: " + "; ".join(problems))
        self.extracted = extracted
        self.problems = problems
        self.usage = usage


@dataclass(frozen=True)
class Form16Extraction:
    form16: Form16
    extracted: ExtractedForm16  # what the model returned, before mapping
    usage: LLMUsage


def extract_form16(text: str, llm: LLMClient, system_prompt: str) -> Form16Extraction:
    prompt = (
        f"Extract the figures from this Form 16.\n\n<document>\n{text}\n</document>"
    )
    result = llm.structured(system_prompt, prompt, ExtractedForm16)
    try:
        form16 = to_form16(result.output)
    except Form16ValidationError as error:
        raise Form16ValidationError(
            error.extracted, error.problems, usage=result.usage
        ) from error
    return Form16Extraction(form16=form16, extracted=result.output, usage=result.usage)


# --- Rows to domain model -------------------------------------------------------


def _column(row: Row) -> str:
    """The row's column, with "deductible" on 10(n) and 11 read as "amount"."""
    if row.line in DEDUCTIBLE_TOTALS and row.column == "deductible":
        return "amount"
    return row.column


def _row_problems(extracted: ExtractedForm16) -> list[str]:
    problems = [f"flagged for review: {note}" for note in extracted.needs_review]
    seen: dict[tuple[str, str], str] = {}
    for row in extracted.rows:
        column = _column(row)
        key = (row.line, column)
        if key in seen and Decimal(seen[key]) != Decimal(row.amount):
            problems.append(
                f"line {row.line} {column} is given twice: {seen[key]} and {row.amount}"
            )
        seen[key] = row.amount
        if row.line in DEDUCTION_LINES:
            if column == "amount":
                problems.append(
                    f"line {row.line} needs a gross, qualifying or deductible column"
                )
        elif column != "amount":
            problems.append(f"line {row.line} has no {column} column")
    lines = {row.line for row in extracted.rows}
    problems += [
        f"line {line} is missing; every Form 16 prints it"
        for line in REQUIRED_LINES
        if line not in lines
    ]
    if not extracted.salary_breakup_printed and lines & set(BREAKUP_LINES):
        problems.append("salary breakup rows given, but no breakup is printed")
    return problems


def _put(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value


def to_form16(extracted: ExtractedForm16) -> Form16:
    """Build the domain Form16 from the rows. Raises Form16ValidationError
    listing every problem with the rows or with the resulting values."""
    problems = _row_problems(extracted)
    if problems:
        raise Form16ValidationError(extracted, problems)

    amounts: dict[tuple[str, str], Decimal] = {
        (row.line, _column(row)): Decimal(row.amount) for row in extracted.rows
    }
    data: dict[str, Any] = {
        "part_a": {**extracted.identity.model_dump(), "quarters": ()},
        "part_b": {
            "opted_out_of_new_regime": extracted.opted_out_of_new_regime == "YES"
        },
    }
    for line, path in AMOUNT_LINES.items():
        _put(data, path, amounts.get((line, "amount"), Decimal(0)))  # absent is 0
    for line, path in DEDUCTION_LINES.items():
        _put(
            data,
            path,
            {
                # A blank gross or qualifying cell has no row: None, not 0.
                "gross": amounts.get((line, "gross")),
                "qualifying": amounts.get((line, "qualifying")),
                "deductible": amounts.get((line, "deductible"), Decimal(0)),
            },
        )
    if extracted.salary_breakup_printed:
        counts = extracted.dearness_allowance_counts_for_retirement
        data["salary_breakup"] = {
            field: amounts.get((line, "amount"), Decimal(0))
            for line, field in BREAKUP_LINES.items()
        } | {
            "dearness_allowance_counts_for_retirement": (
                None if counts == "NOT_STATED" else counts == "YES"
            )
        }

    try:
        return Form16.model_validate(data)
    except ValidationError as error:
        raise Form16ValidationError(
            extracted,
            [
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in error.errors()
            ],
        ) from error
