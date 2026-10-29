from collections.abc import Sequence
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from itr.domain.computation import (
    Comparison,
    Computation,
    IncomeComputation,
    TaxComputation,
)
from itr.domain.inputs import TaxInput
from itr.domain.rules import (
    DeductionRule,
    Regime,
    RegimeRules,
    Slab,
    SurchargeBand,
    TaxRules,
)


def slab_tax(income: Decimal, slabs: Sequence[Slab]) -> Decimal:
    if income < 0:
        raise ValueError(f"income cannot be negative: {income}")

    tax = Decimal(0)
    for slab in slabs:
        if income <= slab.min_income:
            break
        top = income if slab.max_income is None else min(income, slab.max_income)
        tax += (top - slab.min_income) * slab.rate
    return tax


def regime_rules(rules: TaxRules, regime: Regime) -> RegimeRules:
    by_regime = {"old": rules.old_regime, "new": rules.new_regime}
    return by_regime[regime]


def standard_deduction(salary: Decimal, rules: TaxRules, regime: Regime) -> Decimal:
    return min(salary, regime_rules(rules, regime).standard_deduction.salary_max)


def find_rule(rules: TaxRules, code: str) -> DeductionRule:
    for deduction in rules.deductions:
        if deduction.code == code:
            return deduction
    raise LookupError(
        f"no deduction rule with code {code!r} for FY {rules.financial_year}"
    )


def is_allowed(rule: DeductionRule, regime: Regime) -> bool:
    return rule.allowed_in == regime


def professional_tax(inputs: TaxInput, rules: TaxRules, regime: Regime) -> Decimal:
    rule = find_rule(rules, "16(iii)")
    if not is_allowed(rule, regime):
        return Decimal(0)
    return inputs.professional_tax


def house_property_income(inputs: TaxInput, rules: TaxRules, regime: Regime) -> Decimal:
    rule = find_rule(rules, "24(b)")
    if not is_allowed(rule, regime):
        return Decimal(0)
    interest = inputs.self_occupied_home_loan_interest
    if rule.max_limit is not None:
        interest = min(interest, rule.max_limit)
    return -interest if interest else Decimal(0)


def interest_income(inputs: TaxInput) -> Decimal:
    return inputs.savings_interest + inputs.other_interest


def hra_exemption(inputs: TaxInput, rules: TaxRules, regime: Regime) -> Decimal:
    rule = find_rule(rules, "HRA")
    hra = rule.hra
    if not is_allowed(rule, regime) or hra is None:
        return Decimal(0)
    pay = inputs.salary
    if pay.hra_received == 0 or inputs.rent_paid == 0:
        return Decimal(0)

    components = {"basic": pay.basic, "dearness_allowance": pay.dearness_allowance}
    salary = sum((components[name] for name in hra.salary_components), Decimal(0))

    metro_cities = {city.casefold() for city in hra.metro_cities}
    city = (inputs.city or "").strip().casefold()
    salary_rate = (
        hra.metro_salary_rate if city in metro_cities else hra.non_metro_salary_rate
    )

    limits = {
        "hra_received": pay.hra_received,
        "rent_paid_less_salary": max(
            Decimal(0), inputs.rent_paid - hra.rent_less_salary_rate * salary
        ),
        "salary_percentage": salary_rate * salary,
    }
    return min(limits[name] for name in hra.exemption_is_minimum_of)


def _capped(amount: Decimal, rule: DeductionRule) -> Decimal:
    return amount if rule.max_limit is None else min(amount, rule.max_limit)


def deduction_80c(inputs: TaxInput, rules: TaxRules, regime: Regime) -> Decimal:
    rule = find_rule(rules, "80C")
    if not is_allowed(rule, regime):
        return Decimal(0)
    return _capped(inputs.deductions.section_80c, rule)


def deduction_80d(inputs: TaxInput, rules: TaxRules, regime: Regime) -> Decimal:
    rule = find_rule(rules, "80D")
    limits = rule.health_insurance
    if not is_allowed(rule, regime) or limits is None:
        return Decimal(0)
    claims = inputs.deductions

    own = limits.self_and_family
    own_cap = (
        own.senior_citizen_limit
        if inputs.self_or_family_has_senior_citizen
        else own.regular_limit
    )
    parents = limits.parents
    parents_cap = (
        parents.senior_citizen_limit
        if inputs.parents_include_senior_citizen
        else parents.regular_limit
    )
    return min(claims.section_80d_self_and_family, own_cap) + min(
        claims.section_80d_parents, parents_cap
    )


def deduction_80tta(inputs: TaxInput, rules: TaxRules, regime: Regime) -> Decimal:
    rule = find_rule(rules, "80TTA")
    if not is_allowed(rule, regime):
        return Decimal(0)
    return _capped(inputs.savings_interest, rule)


def compute_income(
    inputs: TaxInput, rules: TaxRules, regime: Regime
) -> IncomeComputation:
    gross_salary = inputs.salary.gross
    hra = hra_exemption(inputs, rules, regime)
    std = standard_deduction(gross_salary - hra, rules, regime)
    ptax = professional_tax(inputs, rules, regime)
    # Professional tax could exceed a very small salary; income from salary
    # is never negative.
    income_from_salary = max(Decimal(0), gross_salary - hra - std - ptax)

    house_property = house_property_income(inputs, rules, regime)
    interest = interest_income(inputs)
    gross_total_income = income_from_salary + house_property + interest

    d80c = deduction_80c(inputs, rules, regime)
    d80d = deduction_80d(inputs, rules, regime)
    d80tta = deduction_80tta(inputs, rules, regime)
    # Section 80A(2): Chapter VI-A deductions can't exceed gross total income.
    deductions_total = min(d80c + d80d + d80tta, max(Decimal(0), gross_total_income))

    return IncomeComputation(
        regime=regime,
        gross_salary=gross_salary,
        hra_exemption=hra,
        standard_deduction=std,
        professional_tax=ptax,
        income_from_salary=income_from_salary,
        house_property_income=house_property,
        interest_income=interest,
        gross_total_income=gross_total_income,
        deduction_80c=d80c,
        deduction_80d=d80d,
        deduction_80tta=d80tta,
        deductions_total=deductions_total,
        total_income=max(Decimal(0), gross_total_income - deductions_total),
    )


def _nearest_ten(amount: Decimal) -> Decimal:
    rupees = abs(amount).quantize(Decimal(1), rounding=ROUND_DOWN)
    tens = (rupees / 10).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 10
    return tens if amount >= 0 else -tens


def round_288a(total_income: Decimal) -> Decimal:
    return _nearest_ten(total_income)


def round_288b(amount: Decimal) -> Decimal:
    return _nearest_ten(amount)


def rebate_87a(
    total_income: Decimal, tax: Decimal, rules: TaxRules, regime: Regime
) -> Decimal:
    rule = regime_rules(rules, regime).rebate_87a
    if total_income <= rule.taxable_income_limit:
        return min(tax, rule.max_rebate)
    if not rule.marginal_relief:
        return Decimal(0)
    income_above_limit = total_income - rule.taxable_income_limit
    return max(Decimal(0), tax - income_above_limit)


def _surcharge_band(
    total_income: Decimal, bands: Sequence[SurchargeBand]
) -> SurchargeBand | None:
    applicable = None
    for band in bands:
        if total_income > band.min_income:
            applicable = band
    return applicable


def _tax_and_surcharge(
    total_income: Decimal, rules: TaxRules, regime: Regime
) -> Decimal:
    slabs = regime_rules(rules, regime).slabs
    tax = slab_tax(total_income, slabs)
    tax -= rebate_87a(total_income, tax, rules, regime)
    return tax + surcharge(total_income, tax, rules, regime)


def surcharge(
    total_income: Decimal, tax: Decimal, rules: TaxRules, regime: Regime
) -> Decimal:
    band = _surcharge_band(total_income, regime_rules(rules, regime).surcharge_bands)
    if band is None:
        return Decimal(0)
    full = tax * band.rate
    if not band.marginal_relief:
        return full
    at_threshold = _tax_and_surcharge(band.min_income, rules, regime)
    ceiling = at_threshold + (total_income - band.min_income)
    return max(Decimal(0), min(full, ceiling - tax))


def cess(tax_and_surcharge: Decimal, rules: TaxRules) -> Decimal:
    return tax_and_surcharge * rules.cess.rate


def compute_tax(
    total_income: Decimal, rules: TaxRules, regime: Regime
) -> TaxComputation:
    rounded = round_288a(total_income)
    tax = slab_tax(rounded, regime_rules(rules, regime).slabs)
    rebate = rebate_87a(rounded, tax, rules, regime)
    after_rebate = tax - rebate
    extra = surcharge(rounded, after_rebate, rules, regime)
    health_cess = cess(after_rebate + extra, rules)
    total = after_rebate + extra + health_cess
    return TaxComputation(
        regime=regime,
        total_income=total_income,
        total_income_rounded=rounded,
        tax_on_total_income=tax,
        rebate_87a=rebate,
        tax_after_rebate=after_rebate,
        surcharge=extra,
        cess=health_cess,
        total_tax=total,
        tax_payable=round_288b(total),
    )


def compute(inputs: TaxInput, rules: TaxRules, regime: Regime) -> Computation:
    income = compute_income(inputs, rules, regime)
    return Computation(
        regime=regime,
        income=income,
        tax=compute_tax(income.total_income, rules, regime),
    )


def compare(inputs: TaxInput, rules: TaxRules) -> Comparison:
    new = compute(inputs, rules, "new")
    old = compute(inputs, rules, "old")
    difference = old.tax.tax_payable - new.tax.tax_payable

    cheaper: Regime | None
    if difference > 0:
        cheaper = "new"
    elif difference < 0:
        cheaper = "old"
    else:
        cheaper = None
    return Comparison(new=new, old=old, cheaper=cheaper, saving=abs(difference))
