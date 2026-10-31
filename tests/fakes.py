"""Test doubles for the ports in itr.application.ports, so unit tests never
touch the network."""

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from itr.application.form16_extraction_v2 import (
    Column,
    ExtractedForm16,
    ExtractedIdentity,
    LineId,
    Row,
)
from itr.application.ports import LLMResult, LLMUsage
from itr.domain.form16 import DeductionLine, Form16

FAKE_USAGE = LLMUsage(
    input_tokens=0,
    output_tokens=0,
    cache_write_tokens=0,
    cache_read_tokens=0,
    cost_usd=Decimal(0),
)


@dataclass(frozen=True)
class FakeCall:
    system: str
    prompt: str
    output_model: type[BaseModel]


@dataclass
class FakeLLM:
    """Returns canned objects, one per call, in order, and records each call.

    FakeLLM([extraction]) answers the first call with `extraction`. Running out
    of answers, or an answer of the wrong type, fails the test loudly.
    """

    answers: list[BaseModel]
    calls: list[FakeCall] = field(default_factory=list)

    def structured[T: BaseModel](
        self, system: str, prompt: str, output_model: type[T]
    ) -> LLMResult[T]:
        self.calls.append(FakeCall(system, prompt, output_model))
        if not self.answers:
            raise AssertionError("FakeLLM has no answer left for this call")
        answer = self.answers.pop(0)
        if not isinstance(answer, output_model):
            raise AssertionError(
                f"FakeLLM answer is {type(answer).__name__}, "
                f"but the call asked for {output_model.__name__}"
            )
        return LLMResult(output=answer, usage=FAKE_USAGE)


def _amount(value: Decimal) -> str:
    """The amount as the extraction schema wants it: digits, paise if any."""
    return f"{value.quantize(Decimal('0.01')):f}".removesuffix(".00")


def perfect_extraction(form16: Form16) -> ExtractedForm16:
    """What a flawless model would return for this Form 16: the canned answer
    for FakeLLM in extraction tests. One row per printed value, in the order
    the form prints them; a blank gross or qualifying cell gets no row.

    Written from the Form16 side, separately from the mapping tables in
    extract_form16, so a round-trip test catches a mistake in either."""
    a, b = form16.part_a, form16.part_b
    g, s10, s16 = b.gross_salary, b.section_10, b.section_16
    other, via, t = b.other_income, b.chapter_via, b.tax

    amounts: list[tuple[LineId, Decimal]] = [
        ("A:total_paid", a.total_amount_paid_or_credited),
        ("A:total_deducted", a.total_tax_deducted),
        ("A:total_deposited", a.total_tax_deposited),
        ("1(a)", g.section_17_1),
        ("1(b)", g.section_17_2),
        ("1(c)", g.section_17_3),
        ("1(d)", g.total),
        ("1(e)", g.other_employers),
        ("2(a)", s10.travel_concession_10_5),
        ("2(b)", s10.gratuity_10_10),
        ("2(c)", s10.commuted_pension_10_10a),
        ("2(d)", s10.leave_encashment_10_10aa),
        ("2(e)", s10.house_rent_allowance_10_13a),
        ("2(f)", s10.special_allowances_10_14),
        ("2(h)", s10.other_total),
        ("2(i)", s10.total),
        ("3", b.salary_from_current_employer),
        ("4(a)", s16.standard_deduction_16_ia),
        ("4(b)", s16.entertainment_allowance_16_ii),
        ("4(c)", s16.tax_on_employment_16_iii),
        ("5", s16.total),
        ("6", b.income_under_salaries),
        ("7(a)", other.house_property),
        ("7(b)", other.other_sources),
        ("8", other.total),
        ("9", b.gross_total_income),
    ]
    deductions: list[tuple[LineId, DeductionLine]] = [
        ("10(a)", via.section_80c),
        ("10(b)", via.section_80ccc),
        ("10(c)", via.section_80ccd_1),
        ("10(d)", via.total_80c_80ccc_80ccd_1),
        ("10(e)", via.section_80ccd_1b),
        ("10(f)", via.section_80ccd_2),
        ("10(g)", via.section_80d),
        ("10(h)", via.section_80e),
        ("10(i)", via.section_80cch_employee),
        ("10(j)", via.section_80cch_government),
        ("10(k)", via.section_80g),
        ("10(l)", via.section_80tta),
    ]
    totals: list[tuple[LineId, Decimal]] = [
        ("10(n)", via.other_total),
        ("11", via.aggregate),
        ("12", t.total_taxable_income),
        ("13", t.tax_on_total_income),
        ("14", t.rebate_87a),
        ("15", t.surcharge),
        ("16", t.health_and_education_cess),
        ("17", t.tax_payable),
        ("18", t.relief_section_89),
        ("19", t.tds_form_12baa),
        ("20", t.tcs_form_12baa),
        ("21", t.net_tax_payable),
    ]
    breakup = form16.salary_breakup
    if breakup is not None:
        totals += [
            ("breakup:basic", breakup.basic),
            ("breakup:da", breakup.dearness_allowance),
            ("breakup:hra", breakup.house_rent_allowance),
            ("breakup:other", breakup.other_allowances),
        ]

    rows = [Row(line=line, column="amount", amount=_amount(v)) for line, v in amounts]
    for line, entry in deductions:
        columns: list[tuple[Column, Decimal | None]] = [
            ("gross", entry.gross),
            ("qualifying", entry.qualifying),
            ("deductible", entry.deductible),
        ]
        rows += [
            Row(line=line, column=column, amount=_amount(value))
            for column, value in columns
            if value is not None
        ]
    rows += [Row(line=line, column="amount", amount=_amount(v)) for line, v in totals]

    counts = (
        None if breakup is None else breakup.dearness_allowance_counts_for_retirement
    )
    return ExtractedForm16(
        identity=ExtractedIdentity(
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
        ),
        opted_out_of_new_regime="YES" if b.opted_out_of_new_regime else "NO",
        salary_breakup_printed=breakup is not None,
        dearness_allowance_counts_for_retirement=(
            "NOT_STATED" if counts is None else ("YES" if counts else "NO")
        ),
        rows=rows,
        needs_review=[],
    )
