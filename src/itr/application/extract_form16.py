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

from itr.application.form16_extraction import DeductionRow, ExtractedForm16
from itr.application.ports import LLMClient, LLMUsage
from itr.domain.form16 import Form16

# Fields of the domain Form16 that the extraction doesn't read, or reads in
# its own words, so a diff against truth skips them. "[*]" matches any index:
# an unsupported item's amount is scored, its wording isn't.
NOT_EXTRACTED = (
    "part_a.quarters",
    "part_a.employer_address",  # nothing downstream reads addresses
    "part_a.employee_address",
    "part_b.section_10.other",
    "part_b.chapter_via.other",
    "unrecognised_lines[*].where",
    "unrecognised_lines[*].description",
)

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
# Deduction lines that normally print a gross amount next to a non-zero
# deductible. 10(d) is a total and often prints only the deductible.
GROSS_EXPECTED_LINES = tuple(line for line in DEDUCTION_LINES if line != "10(d)")


class Form16ValidationError(Exception):
    """The model's reply fits the extraction schema, but isn't a valid Form16."""

    def __init__(
        self,
        extracted: ExtractedForm16,
        problems: list[str],
        usage: LLMUsage | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__("the extracted Form 16 is invalid: " + "; ".join(problems))
        self.extracted = extracted
        self.problems = problems
        self.usage = usage  # every call made, retries included
        self.attempts = attempts


class NeedsReviewError(Form16ValidationError):
    """The model flagged values it couldn't read. Not retried: asking again
    would only invite a guess."""


class NotAForm16Error(Exception):
    """The model says the document isn't a Form 16, e.g. a payslip."""

    def __init__(
        self,
        extracted: ExtractedForm16,
        usage: LLMUsage | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__("the document is not a Form 16")
        self.extracted = extracted
        self.usage = usage
        self.attempts = attempts


@dataclass(frozen=True)
class Form16Extraction:
    form16: Form16
    extracted: ExtractedForm16  # what the model returned, before mapping
    usage: LLMUsage  # every call made, retries included
    attempts: int = 1  # 2 when the first reply failed validation or had doubts


def _document_prompt(text: str) -> str:
    return f"Extract the figures from this Form 16.\n\n<document>\n{text}\n</document>"


def _retry_prompt(text: str, problems: list[str], doubts: list[str]) -> str:
    parts = [_document_prompt(text)]
    if problems:
        listed = "\n".join(f"- {problem}" for problem in problems)
        parts.append(
            "Your previous answer for this document was rejected by the "
            f"application's checks:\n{listed}"
        )
    if doubts:
        listed = "\n".join(f"- {doubt}" for doubt in doubts)
        parts.append(
            "In your previous answer these cells look like they may have been "
            f"missed:\n{listed}\n"
            "Check each one in the document. If the cell prints an amount, copy "
            'it. If the cell really is blank, keep it as "".'
        )
    parts.append(
        "Read the document again and answer again from the start. These problems "
        "usually mean a value was misread, or put on the wrong line or column. "
        "Copy every figure exactly as printed: never change a printed figure to "
        "satisfy a check. If a value the document prints is itself the cause, "
        "keep it as printed and add a needs_review entry naming it."
    )
    return "\n\n".join(parts)


def doubtful_cells(extracted: ExtractedForm16) -> list[str]:
    """Cells that are allowed but probably misread: a blank gross next to a
    non-zero deductible. Seen on F008, where the form printed 1,11,000 in
    both columns and the reply left gross blank; sampling makes it random.
    A doubt only earns one more look. A form can leave gross blank, so the
    second answer is accepted either way."""
    return [
        f"line {row.line}: deductible {row.deductible} is given but gross is "
        "blank; check the Gross column"
        for row in extracted.deductions
        if row.line in GROSS_EXPECTED_LINES
        and not row.gross
        and Decimal(row.deductible) != 0
    ]


def extract_form16(text: str, llm: LLMClient, system_prompt: str) -> Form16Extraction:
    """Extract and validate, retrying once if the first reply fails the row or
    domain checks, or has doubtful cells (doubtful_cells). The retry shows the
    model what was found. Not retried: a document the model says isn't a
    Form 16, and values it flagged for review. Doubts in the retry's answer
    are accepted; it never gets a third call.
    """
    first = llm.structured(system_prompt, _document_prompt(text), ExtractedForm16)
    problems: list[str] = []
    try:
        form16 = to_form16(first.output)
    except NotAForm16Error as error:
        raise NotAForm16Error(error.extracted, usage=first.usage) from error
    except NeedsReviewError as error:
        raise NeedsReviewError(
            error.extracted, error.problems, usage=first.usage
        ) from error
    except Form16ValidationError as error:
        problems = error.problems
    doubts = doubtful_cells(first.output)
    if not problems and not doubts:
        return Form16Extraction(form16, first.output, first.usage)

    retry = llm.structured(
        system_prompt, _retry_prompt(text, problems, doubts), ExtractedForm16
    )
    usage = first.usage + retry.usage
    try:
        form16 = to_form16(retry.output)
    except NotAForm16Error as error:
        raise NotAForm16Error(error.extracted, usage=usage, attempts=2) from error
    except Form16ValidationError as error:
        raise type(error)(
            error.extracted, error.problems, usage=usage, attempts=2
        ) from error
    return Form16Extraction(form16, retry.output, usage, attempts=2)


# --- Rows to domain model -------------------------------------------------------


def _cell(text: str) -> Decimal | None:
    """A printed amount, or None for a blank cell."""
    return Decimal(text) if text else None


def _cells(cells: tuple[str, ...]) -> tuple[Decimal | None, ...]:
    return tuple(_cell(text) for text in cells)


def _row_problems(extracted: ExtractedForm16) -> list[str]:
    problems: list[str] = []
    seen: dict[str, tuple[str, ...]] = {}
    printed: list[tuple[str, tuple[str, ...]]] = [
        (row.line, (row.amount,)) for row in extracted.rows
    ] + [
        (row.line, (row.gross, row.qualifying, row.deductible))
        for row in extracted.deductions
    ]
    for line, cells in printed:
        if line in seen and _cells(seen[line]) != _cells(cells):
            problems.append(
                f"line {line} is given twice: {' / '.join(seen[line])} "
                f"and {' / '.join(cells)}"
            )
        seen[line] = cells
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
    listing every problem with the rows or with the resulting values, or
    NotAForm16Error when the model says the document isn't a Form 16.

    Unsupported items become the Form16's unrecognised_lines, which make
    to_tax_input refuse to compute (ADR 0004).
    """
    if extracted.document_type != "form16":
        raise NotAForm16Error(extracted)
    if extracted.needs_review:
        raise NeedsReviewError(
            extracted,
            [f"flagged for review: {note}" for note in extracted.needs_review],
        )
    problems = _row_problems(extracted)
    if problems:
        raise Form16ValidationError(extracted, problems)

    amounts: dict[str, Decimal] = {
        row.line: Decimal(row.amount) for row in extracted.rows
    }
    deductions: dict[str, DeductionRow] = {
        row.line: row for row in extracted.deductions
    }
    data: dict[str, Any] = {
        "part_a": {
            **extracted.identity.model_dump(),
            "employer_address": "",  # not extracted
            "employee_address": "",
            "quarters": (),
        },
        "part_b": {
            "opted_out_of_new_regime": extracted.opted_out_of_new_regime == "YES"
        },
    }
    for line, path in AMOUNT_LINES.items():
        _put(data, path, amounts.get(line, Decimal(0)))  # absent is 0
    for line, path in DEDUCTION_LINES.items():
        row = deductions.get(line)
        _put(
            data,
            path,
            # A blank gross or qualifying cell is None, not 0.
            {
                "gross": _cell(row.gross) if row else None,
                "qualifying": _cell(row.qualifying) if row else None,
                "deductible": Decimal(row.deductible) if row else Decimal(0),
            },
        )
    if extracted.salary_breakup_printed:
        counts = extracted.dearness_allowance_counts_for_retirement
        data["salary_breakup"] = {
            field: amounts.get(line, Decimal(0))
            for line, field in BREAKUP_LINES.items()
        } | {
            "dearness_allowance_counts_for_retirement": (
                None if counts == "NOT_STATED" else counts == "YES"
            )
        }

    data["unrecognised_lines"] = [
        {"where": item.where, "description": item.description, "amount": item.amount}
        for item in extracted.unsupported_items
    ]

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
