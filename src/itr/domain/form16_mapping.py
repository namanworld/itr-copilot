from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from itr.domain.form16 import DeductionLine, Form16
from itr.domain.inputs import DeductionClaims, SalaryInput, TaxInput
from itr.domain.rules import TaxRules
from itr.domain.tax import compute, find_rule, round_288b

Amount = Annotated[Decimal, Field(ge=0)]  # keeps amount: Decimal as non-negative
Severity = Literal["block", "warn"]


class MappingModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Finding(MappingModel):
    """Something the user must know about. A block stops the computation."""

    severity: Severity
    line: str  # where to look on Form 16, e.g. "Part B 2(e)"
    message: str
    form16_value: Decimal | None = None
    engine_value: Decimal | None = None


class SalaryParts(MappingModel):
    """The salary breakup from a payslip, for a Form 16 with no annexure."""

    basic: Amount
    dearness_allowance: Amount = Decimal(0)
    house_rent_allowance: Amount = Decimal(0)
    other_allowances: Amount = Decimal(0)


class UserExtras(MappingModel):
    """What the user tells us. Form 16 never carries these (form16-vs-tax-input.md)."""

    rent_paid: Amount = Decimal(0)
    city: str | None = None  # of the rented home, never Part A's address
    rent_city_and_salary_unchanged_all_year: bool = True
    savings_interest: Amount = Decimal(0)
    other_interest: Amount = Decimal(0)
    resident: bool = True
    aged_60_or_over: bool = False
    self_or_family_has_senior_citizen: bool = False
    parents_include_senior_citizen: bool = False
    # Only used when the Form 16 has no salary breakup annexure.
    salary_parts: SalaryParts | None = None
    # Answers the question when the breakup doesn't say.
    dearness_allowance_counts_for_retirement: bool | None = None

    # For each of these, None means "use the Form 16 figure", and a value is
    # what the user states. On a new-regime Form 16 the printed figures are not
    # evidence of what was paid, so None there means the user didn't say.
    professional_tax: Amount | None = None
    section_80c: Amount | None = None
    section_80d_self_and_family: Amount | None = None
    section_80d_parents: Amount | None = None
    self_occupied_home_loan_interest: Amount | None = None


class MappingResult(MappingModel):
    tax_input: TaxInput | None  # None when any finding blocks
    findings: tuple[Finding, ...]

    @property
    def blocked(self) -> bool:
        return any(finding.severity == "block" for finding in self.findings)


def _rupees(amount: Decimal) -> Decimal:
    """Whole rupees, for comparing figures only. Never changes a result."""
    return amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def _largest(line: DeductionLine) -> Decimal:
    """The biggest amount in any column: non-zero anywhere means present."""
    columns = [line.gross, line.qualifying, line.deductible]
    return max(column for column in columns if column is not None)


def _claimed(line: DeductionLine) -> Decimal:
    """What was claimed: the gross column, or the deductible one if blank.
    The engine applies the caps itself."""
    return line.gross if line.gross is not None else line.deductible


def _block(line: str, message: str, value: Decimal | None = None) -> Finding:
    return Finding(severity="block", line=line, message=message, form16_value=value)


def _warn(
    line: str,
    message: str,
    form16_value: Decimal | None = None,
    engine_value: Decimal | None = None,
) -> Finding:
    return Finding(
        severity="warn",
        line=line,
        message=message,
        form16_value=form16_value,
        engine_value=engine_value,
    )


def _unsupported_lines(form16: Form16) -> list[tuple[str, str, Decimal]]:
    """(line, what it is, amount) for every line the engine cannot represent."""
    b = form16.part_b
    s10 = b.section_10
    via = b.chapter_via
    other_s10 = max(s10.other_total, sum((c.amount for c in s10.other), Decimal(0)))
    other_via = max(via.other_total, sum((c.amount for c in via.other), Decimal(0)))
    return [
        ("Part B 1(e)", "salary from other employers", b.gross_salary.other_employers),
        ("Part B 2(a)", "travel concession, 10(5)", s10.travel_concession_10_5),
        ("Part B 2(b)", "gratuity, 10(10)", s10.gratuity_10_10),
        ("Part B 2(c)", "commuted pension, 10(10A)", s10.commuted_pension_10_10a),
        ("Part B 2(d)", "leave encashment, 10(10AA)", s10.leave_encashment_10_10aa),
        ("Part B 2(f)", "special allowances, 10(14)", s10.special_allowances_10_14),
        ("Part B 2(h)", "other section 10 exemptions", other_s10),
        (
            "Part B 4(b)",
            "entertainment allowance, 16(ii)",
            b.section_16.entertainment_allowance_16_ii,
        ),
        ("Part B 10(b)", "80CCC", _largest(via.section_80ccc)),
        ("Part B 10(c)", "80CCD(1)", _largest(via.section_80ccd_1)),
        ("Part B 10(e)", "80CCD(1B)", _largest(via.section_80ccd_1b)),
        (
            "Part B 10(f)",
            "80CCD(2), employer's contribution",
            _largest(via.section_80ccd_2),
        ),
        ("Part B 10(h)", "80E", _largest(via.section_80e)),
        ("Part B 10(i)", "80CCH, employee", _largest(via.section_80cch_employee)),
        (
            "Part B 10(j)",
            "80CCH, Central Government",
            _largest(via.section_80cch_government),
        ),
        ("Part B 10(k)", "80G", _largest(via.section_80g)),
        ("Part B 10(n)", "other Chapter VI-A deductions", other_via),
        ("Part B 18", "relief under section 89", b.tax.relief_section_89),
    ]


def _blocking_findings(
    form16: Form16, user: UserExtras, rules: TaxRules
) -> list[Finding]:
    findings = [
        _block(line, f"{what} is not supported yet, so tax can't be computed", amount)
        for line, what, amount in _unsupported_lines(form16)
        if amount > 0
    ]

    for extra in form16.unrecognised_lines:
        findings.append(
            _block(
                extra.where,
                f"{extra.description} is not a Form 16 line the engine can "
                "model, so tax can't be computed",
                extra.amount,
            )
        )

    house_property = form16.part_b.other_income.house_property
    if house_property > 0:
        findings.append(
            _block(
                "Part B 7(a)",
                "income from a let-out property is not supported yet",
                house_property,
            )
        )
    if form16.part_a.assessment_year != rules.assessment_year:
        findings.append(
            _block(
                "Part A",
                f"the Form 16 is for AY {form16.part_a.assessment_year}, but the "
                f"rules loaded are for AY {rules.assessment_year}",
            )
        )
    if not user.resident:
        findings.append(_block("User", "tax for non-residents is not supported yet"))
    if user.aged_60_or_over:
        findings.append(
            _block(
                "User",
                "senior-citizen slabs and 80TTB are not supported yet",
            )
        )
    return findings


def _arithmetic_findings(form16: Form16) -> list[Finding]:
    a = form16.part_a
    b = form16.part_b
    g = b.gross_salary
    s10 = b.section_10
    s16 = b.section_16
    other = b.other_income
    via = b.chapter_via
    t = b.tax

    checks: list[tuple[str, Decimal, Decimal]] = [
        ("Part B 1(d)", g.total, g.section_17_1 + g.section_17_2 + g.section_17_3),
        (
            "Part B 2(i)",
            s10.total,
            s10.travel_concession_10_5
            + s10.gratuity_10_10
            + s10.commuted_pension_10_10a
            + s10.leave_encashment_10_10aa
            + s10.house_rent_allowance_10_13a
            + s10.special_allowances_10_14
            + s10.other_total,
        ),
        ("Part B 3", b.salary_from_current_employer, g.total - s10.total),
        (
            "Part B 5",
            s16.total,
            s16.standard_deduction_16_ia
            + s16.entertainment_allowance_16_ii
            + s16.tax_on_employment_16_iii,
        ),
        (
            "Part B 6",
            b.income_under_salaries,
            b.salary_from_current_employer + g.other_employers - s16.total,
        ),
        ("Part B 8", other.total, other.house_property + other.other_sources),
        ("Part B 9", b.gross_total_income, b.income_under_salaries + other.total),
        (
            "Part B 10(d)",
            via.total_80c_80ccc_80ccd_1.deductible,
            via.section_80c.deductible
            + via.section_80ccc.deductible
            + via.section_80ccd_1.deductible,
        ),
        (
            "Part B 11",
            via.aggregate,
            via.total_80c_80ccc_80ccd_1.deductible
            + via.section_80ccd_1b.deductible
            + via.section_80ccd_2.deductible
            + via.section_80d.deductible
            + via.section_80e.deductible
            + via.section_80cch_employee.deductible
            + via.section_80cch_government.deductible
            + via.section_80g.deductible
            + via.section_80tta.deductible
            + via.other_total,
        ),
        (
            "Part B 12",
            t.total_taxable_income,
            max(Decimal(0), b.gross_total_income - via.aggregate),
        ),
        (
            "Part B 21",
            t.net_tax_payable,
            t.tax_payable - t.relief_section_89 - t.tds_form_12baa - t.tcs_form_12baa,
        ),
        (
            "Part A total paid or credited",
            a.total_amount_paid_or_credited,
            sum((q.amount_paid_or_credited for q in a.quarters), Decimal(0)),
        ),
        (
            "Part A total tax deducted",
            a.total_tax_deducted,
            sum((q.tax_deducted for q in a.quarters), Decimal(0)),
        ),
        (
            "Part A total tax deposited",
            a.total_tax_deposited,
            sum((q.tax_deposited for q in a.quarters), Decimal(0)),
        ),
    ]
    if s10.other:
        checks.append(
            (
                "Part B 2(h)",
                s10.other_total,
                sum((c.amount for c in s10.other), Decimal(0)),
            )
        )
    if via.other:
        checks.append(
            (
                "Part B 10(n)",
                via.other_total,
                sum((c.amount for c in via.other), Decimal(0)),
            )
        )

    findings = [
        _warn(
            line,
            "the printed figure doesn't match the lines it adds up",
            printed,
            expected,
        )
        for line, printed, expected in checks
        if _rupees(printed) != _rupees(expected)
    ]

    # Line 17 may or may not be rounded to Rs 10 under section 288B; accept both.
    expected_17 = (
        t.tax_on_total_income - t.rebate_87a + t.surcharge + t.health_and_education_cess
    )
    if _rupees(t.tax_payable) not in {_rupees(expected_17), round_288b(expected_17)}:
        findings.append(
            _warn(
                "Part B 17",
                "the printed figure doesn't match the lines it adds up",
                t.tax_payable,
                expected_17,
            )
        )
    return findings


# --- 3. Build the TaxInput (ADR 0004, sections 3 to 6) -----------------------


def _build_tax_input(
    form16: Form16, user: UserExtras, rules: TaxRules
) -> tuple[TaxInput | None, list[Finding]]:
    findings: list[Finding] = []
    b = form16.part_b
    old_regime_form = b.regime_used == "old"
    gross = b.gross_salary.total

    # Salary breakup: the annexure if there is one, else the user's payslip.
    breakup = form16.salary_breakup
    if breakup is not None:
        basic, da = breakup.basic, breakup.dearness_allowance
        hra_received, other_allowances = (
            breakup.house_rent_allowance,
            breakup.other_allowances,
        )
        da_counts = breakup.dearness_allowance_counts_for_retirement
    elif user.salary_parts is not None:
        parts = user.salary_parts
        basic, da = parts.basic, parts.dearness_allowance
        hra_received, other_allowances = (
            parts.house_rent_allowance,
            parts.other_allowances,
        )
        da_counts = None
    else:
        basic = da = hra_received = other_allowances = Decimal(0)
        da_counts = None
    if da_counts is None:
        da_counts = user.dearness_allowance_counts_for_retirement

    has_breakup = breakup is not None or user.salary_parts is not None
    employer_hra = b.section_10.house_rent_allowance_10_13a
    if not has_breakup and (user.rent_paid > 0 or employer_hra > 0):
        findings.append(
            _block(
                "Part B 2(e)",
                "HRA needs basic pay and HRA received: attach the salary breakup or "
                "enter them from a payslip",
                employer_hra,
            )
        )

    claims_hra = hra_received > 0 and user.rent_paid > 0
    if claims_hra:
        if not (user.city or "").strip():
            findings.append(_block("User", "HRA needs the city of the rented home"))
        if not user.rent_city_and_salary_unchanged_all_year:
            findings.append(
                _block(
                    "User",
                    "rent, city or salary changed during the year; period-wise HRA "
                    "is not supported yet",
                )
            )
        if da > 0 and da_counts is None:
            findings.append(
                _block(
                    "Salary breakup",
                    "say whether dearness allowance counts for retirement benefits; "
                    "only that part counts as salary for HRA",
                    da,
                )
            )
    if has_breakup and user.rent_paid > 0 and hra_received == 0:
        findings.append(
            _warn(
                "User",
                "rent is paid but no HRA is received; the 80GG deduction for this "
                "case is not supported, so old-regime tax may be overstated",
            )
        )

    if basic + da + hra_received + other_allowances > gross:
        findings.append(
            _block(
                "Salary breakup",
                "the breakup adds up to more than gross salary on line 1(d)",
                gross,
            )
        )

    # DA that doesn't count for retirement is still salary, so it moves to
    # other allowances; only qualifying DA counts as salary for HRA.
    qualifying_da = da if da_counts else Decimal(0)
    other_allowances += da - qualifying_da

    # Facts a new-regime Form 16 does not evidence (ADR 0004, section 6).
    missing_old_regime_facts: list[str] = []

    if user.professional_tax is not None:
        professional_tax = user.professional_tax
        printed = b.section_16.tax_on_employment_16_iii
        if old_regime_form and professional_tax != printed:
            findings.append(
                _warn(
                    "Part B 4(c)",
                    "professional tax differs from the Form 16",
                    printed,
                    professional_tax,
                )
            )
    elif old_regime_form:
        professional_tax = b.section_16.tax_on_employment_16_iii
    else:
        professional_tax = Decimal(0)
        missing_old_regime_facts.append("professional tax")

    printed_80c = _claimed(b.chapter_via.section_80c)
    if user.section_80c is not None:
        section_80c = user.section_80c
        if old_regime_form and section_80c > printed_80c:
            findings.append(
                _warn(
                    "Part B 10(a)",
                    "80C claimed is more than the Form 16 shows; keep the proof",
                    printed_80c,
                    section_80c,
                )
            )
    elif old_regime_form:
        section_80c = printed_80c
    else:
        section_80c = Decimal(0)
        missing_old_regime_facts.append("80C")

    printed_80d = _claimed(b.chapter_via.section_80d)
    if (
        user.section_80d_self_and_family is not None
        or user.section_80d_parents is not None
    ):
        own_80d = user.section_80d_self_and_family or Decimal(0)
        parents_80d = user.section_80d_parents or Decimal(0)
        if old_regime_form and own_80d + parents_80d != printed_80d:
            findings.append(
                _warn(
                    "Part B 10(g)",
                    "the 80D split doesn't add up to the Form 16 figure",
                    printed_80d,
                    own_80d + parents_80d,
                )
            )
    elif old_regime_form:
        own_80d, parents_80d = printed_80d, Decimal(0)
        if printed_80d > 0:
            findings.append(
                _warn(
                    "Part B 10(g)",
                    "80D is all counted as self and family; split out any premium "
                    "for parents, which has its own limit",
                    printed_80d,
                )
            )
    else:
        own_80d = parents_80d = Decimal(0)
        missing_old_regime_facts.append("80D")

    house_property = b.other_income.house_property
    if user.self_occupied_home_loan_interest is not None:
        loan_interest = user.self_occupied_home_loan_interest
        if old_regime_form and house_property < 0:
            cap = find_rule(rules, "24(b)").max_limit
            allowed = loan_interest if cap is None else min(loan_interest, cap)
            if allowed != -house_property:
                findings.append(
                    _warn(
                        "Part B 7(a)",
                        "home-loan interest differs from the loss the employer allowed",
                        house_property,
                        -allowed,
                    )
                )
    elif house_property < 0:
        loan_interest = -house_property
        findings.append(
            _warn(
                "Part B 7(a)",
                "home-loan interest was taken from the loss on line 7(a); confirm the "
                "full interest paid, since the employer may have capped it",
                house_property,
            )
        )
    else:
        loan_interest = Decimal(0)
        if not old_regime_form:
            missing_old_regime_facts.append("home-loan interest")

    if missing_old_regime_facts:
        findings.append(
            _warn(
                "Part B line A",
                "this Form 16 uses the new regime, so it doesn't show old-regime "
                "deductions; the old-regime result leaves out "
                + ", ".join(missing_old_regime_facts)
                + " unless you enter them",
            )
        )

    interest_total = user.savings_interest + user.other_interest
    if interest_total < b.other_income.other_sources:
        findings.append(
            _warn(
                "Part B 7(b)",
                "your interest is less than the other income on the Form 16",
                b.other_income.other_sources,
                interest_total,
            )
        )

    if any(finding.severity == "block" for finding in findings):
        return None, findings

    tax_input = TaxInput(
        salary=SalaryInput(
            gross=gross,
            basic=basic,
            dearness_allowance=qualifying_da,
            hra_received=hra_received,
            other_allowances=other_allowances,
        ),
        rent_paid=user.rent_paid,
        city=user.city,
        professional_tax=professional_tax,
        self_occupied_home_loan_interest=loan_interest,
        savings_interest=user.savings_interest,
        other_interest=user.other_interest,
        deductions=DeductionClaims(
            section_80c=section_80c,
            section_80d_self_and_family=own_80d,
            section_80d_parents=parents_80d,
        ),
        self_or_family_has_senior_citizen=user.self_or_family_has_senior_citizen,
        parents_include_senior_citizen=user.parents_include_senior_citizen,
    )
    return tax_input, findings


# --- 4. Compare with the employer's own figures (sections 4 and 6) --


def _cross_check_findings(
    form16: Form16, tax_input: TaxInput, rules: TaxRules
) -> list[Finding]:
    b = form16.part_b
    regime = b.regime_used

    # The employer only saw the interest reported on line 7(b), not the user's
    # bank interest. Compare its figures with a computation on what it saw:
    # 7(b) as interest, with enough of it treated as savings interest to give
    # the employer's own 80TTA on line 10(l).
    reported = b.other_income.other_sources
    savings = min(b.chapter_via.section_80tta.deductible, reported)
    employer_view = tax_input.model_copy(
        update={"savings_interest": savings, "other_interest": reported - savings}
    )
    sheet = compute(employer_view, rules, regime)
    income, tax = sheet.income, sheet.tax

    checks: list[tuple[str, str, Decimal, Decimal]] = [
        (
            "Part B 4(a)",
            "standard deduction (check line A was read right)",
            b.section_16.standard_deduction_16_ia,
            income.standard_deduction,
        ),
        (
            "Part B 6",
            "income from salary",
            b.income_under_salaries,
            income.income_from_salary,
        ),
        (
            "Part B 9",
            "gross total income",
            b.gross_total_income,
            income.gross_total_income,
        ),
        (
            "Part B 11",
            "Chapter VI-A deductions",
            b.chapter_via.aggregate,
            income.deductions_total,
        ),
        (
            "Part B 12",
            "total income",
            b.tax.total_taxable_income,
            tax.total_income_rounded,
        ),
        (
            "Part B 13",
            "tax on total income",
            b.tax.tax_on_total_income,
            tax.tax_on_total_income,
        ),
        ("Part B 14", "87A rebate", b.tax.rebate_87a, tax.rebate_87a),
        ("Part B 15", "surcharge", b.tax.surcharge, tax.surcharge),
        ("Part B 16", "cess", b.tax.health_and_education_cess, tax.cess),
        ("Part B 17", "tax payable", b.tax.tax_payable, tax.tax_payable),
    ]
    if regime == "old":
        checks.append(
            (
                "Part B 10(l)",
                "80TTA",
                b.chapter_via.section_80tta.deductible,
                income.deduction_80tta,
            )
        )

    findings = [
        _warn(
            line, f"the employer's {what} differs from the engine's", printed, computed
        )
        for line, what, printed, computed in checks
        if _rupees(printed) != _rupees(computed)
    ]

    employer_hra = b.section_10.house_rent_allowance_10_13a
    if regime == "old" and _rupees(employer_hra) != _rupees(income.hra_exemption):
        message = "the employer's HRA exemption differs from the engine's"
        if income.hra_exemption > employer_hra:
            message += (
                "; the employer allowed less, so keep rent receipts to support "
                "the higher figure"
            )
        findings.append(
            _warn("Part B 2(e)", message, employer_hra, income.hra_exemption)
        )
    return findings


# --- The mapping ----------------------------------------------------------------


def to_tax_input(form16: Form16, user: UserExtras, rules: TaxRules) -> MappingResult:
    """Combine a Form 16 and the user's facts into a TaxInput, with findings.

    The TaxInput is None whenever any finding blocks. Cross-checks against the
    employer's figures run only when there is a TaxInput to compute.
    """
    findings: list[Finding] = []
    findings += _blocking_findings(form16, user, rules)
    findings += _arithmetic_findings(form16)
    tax_input, build_findings = _build_tax_input(form16, user, rules)
    findings += build_findings

    if tax_input is None or any(f.severity == "block" for f in findings):
        return MappingResult(tax_input=None, findings=tuple(findings))
    findings += _cross_check_findings(form16, tax_input, rules)
    return MappingResult(tax_input=tax_input, findings=tuple(findings))
