"""What Claude fills in when it reads a Form 16: identity fields plus rows.

This is not the domain Form16. It is shaped for structured outputs, whose
compiled grammar has a size limit: a schema with a named field for each of
the ~110 values was rejected as "too large". So the amounts come back as a
list of small rows instead, one per printed value:

    {"line": "10(a)", "column": "deductible", "amount": "150000"}

The grammar only has to describe one row, so its size doesn't grow with the
form. Code (extract_form16.to_form16) turns the rows into the domain Form16
and checks what the schema no longer can: no duplicates, the right columns,
and the core lines present.

- A line that isn't printed has no row; the mapping treats it as 0.
- A blank gross or qualifying cell on line 10 has no row either; the mapping
  treats it as None, so a blank cell and a printed 0 stay different.
- Amounts are strings matching AMOUNT. The pattern is checked by Pydantic when
  the reply arrives, not while Claude writes it, so the prompt states it too.
- needs_review lists anything Claude couldn't read reliably. Any entry stops
  the extraction: an unreadable amount must never quietly become 0.

It lives in the application layer, not infrastructure/llm, because the
extract use case imports it and the application layer can't import
infrastructure. It has no vendor code in it.
"""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

AMOUNT = r"^-?\d+(\.\d{1,2})?$"
AmountText = Annotated[str, Field(pattern=AMOUNT)]
YesNo = Literal["YES", "NO"]

# Every value a row can hold, named as the form prints it. "A:" is the Total
# row of Part A's quarterly table; "breakup:" is the salary breakup annexure.
LineId = Literal[
    "A:total_paid",
    "A:total_deducted",
    "A:total_deposited",
    "1(a)",
    "1(b)",
    "1(c)",
    "1(d)",
    "1(e)",
    "2(a)",
    "2(b)",
    "2(c)",
    "2(d)",
    "2(e)",
    "2(f)",
    "2(h)",
    "2(i)",
    "3",
    "4(a)",
    "4(b)",
    "4(c)",
    "5",
    "6",
    "7(a)",
    "7(b)",
    "8",
    "9",
    "10(a)",
    "10(b)",
    "10(c)",
    "10(d)",
    "10(e)",
    "10(f)",
    "10(g)",
    "10(h)",
    "10(i)",
    "10(j)",
    "10(k)",
    "10(l)",
    "10(n)",
    "11",
    "12",
    "13",
    "14",
    "15",
    "16",
    "17",
    "18",
    "19",
    "20",
    "21",
    "breakup:basic",
    "breakup:da",
    "breakup:hra",
    "breakup:other",
]
# "amount" for single-amount lines; line 10(a)-10(l) has three columns.
Column = Literal["amount", "gross", "qualifying", "deductible"]


class Extracted(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExtractedIdentity(Extracted):
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


class Row(Extracted):
    line: LineId
    column: Column
    amount: AmountText


class UnsupportedItem(Extracted):
    """An amount printed on the document that no row line covers, such as
    capital gains the employee declared to the employer."""

    where: str = Field(description="where it is printed, e.g. the annexure title")
    description: str
    amount: AmountText


class ExtractedForm16(Extracted):
    document_type: Literal["form16", "other"] = Field(
        description="form16 only for a certificate of TDS on salary (Form No. 16); "
        "other for anything else, such as a payslip or a bank statement"
    )
    identity: ExtractedIdentity
    opted_out_of_new_regime: YesNo = Field(
        description="Answer to 'Whether opting out of taxation u/s 115BAC(1A)?'"
    )
    salary_breakup_printed: bool
    dearness_allowance_counts_for_retirement: Literal["YES", "NO", "NOT_STATED"]
    rows: list[Row]
    unsupported_items: list[UnsupportedItem] = Field(
        description="income or amounts printed that no row line covers"
    )
    needs_review: list[str] = Field(
        description="each field or line that couldn't be read reliably, and why"
    )
