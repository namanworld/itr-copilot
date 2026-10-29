"""Computation results: every line, not just the final number."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from itr.domain.rules import Regime


class IncomeComputation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: Regime

    # Salaries
    gross_salary: Decimal
    hra_exemption: Decimal
    standard_deduction: Decimal
    professional_tax: Decimal
    income_from_salary: Decimal

    # Other heads
    house_property_income: Decimal  # negative: self-occupied loan interest
    interest_income: Decimal
    gross_total_income: Decimal

    # Chapter VI-A
    deduction_80c: Decimal
    deduction_80d: Decimal
    deduction_80tta: Decimal
    deductions_total: Decimal

    total_income: Decimal


class TaxComputation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: Regime
    total_income: Decimal  # before section 288A rounding
    total_income_rounded: Decimal  # section 288A
    tax_on_total_income: Decimal  # slab tax
    rebate_87a: Decimal
    tax_after_rebate: Decimal
    surcharge: Decimal
    cess: Decimal
    total_tax: Decimal  # tax after rebate + surcharge + cess, unrounded
    tax_payable: Decimal  # section 288B


class Computation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regime: Regime
    income: IncomeComputation  # gross salary ... total income
    tax: TaxComputation  # total income ... tax payable


class Comparison(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    new: Computation
    old: Computation
    cheaper: Regime | None  # None when both cost the same
    saving: Decimal  # how much the cheaper regime saves; 0 on a tie
