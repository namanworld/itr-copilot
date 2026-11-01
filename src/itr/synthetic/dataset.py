"""The evaluation dataset: which documents to make, and how each is tagged.

Every case gets a tag saying what kind of document it is and what a correct
system should do with it: extract it, or refuse it. The tag and the truth go
together in the case's truth JSON, so Day 10 can score extraction on the cases
that should be extracted, and refusal on the ones that should be refused.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from itr.domain.form16 import Form16, UnrecognisedLine
from itr.domain.form16_mapping import UserExtras
from itr.domain.rules import TaxRules
from itr.infrastructure.pdf.form16_renderer import NumberFormat, render_form16
from itr.infrastructure.pdf.form16_statement import render_form16_statement
from itr.infrastructure.pdf.payslip_renderer import render_payslip
from itr.synthetic.form16_generator import generate

Category = Literal["normal", "hard", "out_of_scope"]
Layout = Literal["standard", "statement", "payslip"]
Expected = Literal["extract", "refuse"]


class CaseTag(BaseModel):
    """What kind of document a case is, and what should happen to it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str  # the file name, e.g. "F001"
    seed: int
    category: Category
    layout: Layout
    number_format: NumberFormat
    expected: Expected
    reason: str | None = None  # why it should be refused


class TruthFile(BaseModel):
    """The contents of a case's .truth.json."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: CaseTag
    # What a perfect extraction returns; None when the document isn't a
    # Form 16 at all, since there is nothing to extract.
    form16: Form16 | None


@dataclass(frozen=True)
class Case:
    tag: CaseTag
    document: Form16  # what the renderer draws from
    truth: TruthFile
    extras: UserExtras


# The evaluation mix: (category, layout, number format), in file order.
EVAL_MIX: tuple[tuple[Category, Layout, NumberFormat], ...] = (
    *(("normal", "standard", "indian_grouping"),) * 15,
    ("hard", "statement", "rupee_symbol"),
    ("hard", "statement", "plain_decimal"),
    ("hard", "statement", "rs_prefix"),
    ("out_of_scope", "standard", "indian_grouping"),  # capital gains
    ("out_of_scope", "payslip", "indian_grouping"),  # not a Form 16
)


def _with_capital_gains(form16: Form16, seed: int) -> Form16:
    """The same Form 16 with long-term capital gains declared by the employee
    in an annexure. Part B is unchanged: the employer didn't use it for TDS."""
    amount = Decimal(150_000 + (seed % 7) * 25_000)
    line = UnrecognisedLine(
        where="Annexure: other income declared by the employee u/s 192(2B)",
        description="Long-term capital gains on listed equity shares (section 112A)",
        amount=amount,
    )
    return form16.model_copy(update={"unrecognised_lines": (line,)})


def make_case(
    number: int,
    seed: int,
    category: Category,
    layout: Layout,
    number_format: NumberFormat,
    rules: TaxRules,
) -> Case:
    form16, extras = generate(seed, rules)
    case_id = f"F{number:03d}"

    if layout == "payslip":
        tag = CaseTag(
            case_id=case_id,
            seed=seed,
            category=category,
            layout=layout,
            number_format=number_format,
            expected="refuse",
            reason="not_a_form16",
        )
        return Case(tag, form16, TruthFile(tag=tag, form16=None), extras)

    if category == "out_of_scope":
        form16 = _with_capital_gains(form16, seed)
        expected: Expected = "refuse"
        reason: str | None = "capital_gains"
    else:
        expected, reason = "extract", None

    tag = CaseTag(
        case_id=case_id,
        seed=seed,
        category=category,
        layout=layout,
        number_format=number_format,
        expected=expected,
        reason=reason,
    )
    return Case(tag, form16, TruthFile(tag=tag, form16=form16), extras)


def build_eval_dataset(first_seed: int, rules: TaxRules) -> list[Case]:
    """The 20-case mix: 15 normal, 3 hard, 2 out of scope."""
    return [
        make_case(index + 1, first_seed + index, category, layout, fmt, rules)
        for index, (category, layout, fmt) in enumerate(EVAL_MIX)
    ]


def build_normal_dataset(n: int, first_seed: int, rules: TaxRules) -> list[Case]:
    """n normal cases in the standard layout."""
    return [
        make_case(
            index + 1,
            first_seed + index,
            "normal",
            "standard",
            "indian_grouping",
            rules,
        )
        for index in range(n)
    ]


def render_case(case: Case) -> bytes:
    """The PDF for a case, in its layout and number format."""
    if case.tag.layout == "payslip":
        return render_payslip(case.document)
    if case.tag.layout == "statement":
        return render_form16_statement(case.document, case.tag.number_format)
    return render_form16(case.document)
