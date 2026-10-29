from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonNegativeAmount = Annotated[Decimal, Field(ge=0)]
Rate = Annotated[Decimal, Field(ge=0, le=1)]
SourceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Regime = Literal["old", "new"]


class RuleModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Slab(RuleModel):
    min_income: NonNegativeAmount
    max_income: NonNegativeAmount | None
    rate: Rate
    source_ids: tuple[SourceId, ...] = Field(min_length=1)


class StandardDeduction(RuleModel):
    salary_max: NonNegativeAmount
    source_ids: tuple[SourceId, ...] = Field(min_length=1)


class Rebate87A(RuleModel):
    taxable_income_limit: NonNegativeAmount
    max_rebate: NonNegativeAmount
    resident_individual_only: bool
    marginal_relief: bool
    source_ids: tuple[SourceId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rebate(self) -> Self:
        if self.max_rebate > self.taxable_income_limit:
            raise ValueError("87A rebate cannot exceed its taxable-income limit")
        return self


class SurchargeBand(RuleModel):
    min_income: NonNegativeAmount
    max_income: NonNegativeAmount | None
    rate: Rate
    marginal_relief: bool
    source_ids: tuple[SourceId, ...] = Field(min_length=1)


class RegimeRules(RuleModel):
    slabs: tuple[Slab, ...]
    standard_deduction: StandardDeduction
    rebate_87a: Rebate87A
    surcharge_bands: tuple[SurchargeBand, ...]
    source_ids: tuple[SourceId, ...] = Field(min_length=1)


class AgeBasedLimit(RuleModel):
    regular_limit: NonNegativeAmount
    senior_citizen_limit: NonNegativeAmount


class HealthInsuranceRule(RuleModel):
    senior_citizen_age: Annotated[int, Field(ge=0)]
    self_and_family: AgeBasedLimit
    parents: AgeBasedLimit
    preventive_checkup_limit: NonNegativeAmount
    preventive_checkup_within_category_limits: bool

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if (
            self.self_and_family.senior_citizen_limit
            < self.self_and_family.regular_limit
            or self.parents.senior_citizen_limit < self.parents.regular_limit
        ):
            raise ValueError(
                "80D senior-citizen limits cannot be lower than regular limits"
            )
        if self.preventive_checkup_within_category_limits and (
            self.preventive_checkup_limit
            > min(
                self.self_and_family.regular_limit,
                self.parents.regular_limit,
            )
        ):
            raise ValueError("preventive check-up limit cannot exceed category limits")
        return self


class HraRule(RuleModel):
    exemption_is_minimum_of: tuple[
        Literal["hra_received", "rent_paid_less_salary", "salary_percentage"], ...
    ]
    salary_components: tuple[Literal["basic", "dearness_allowance"], ...]
    include_dearness_allowance_only_if_part_of_retirement_benefits: bool
    rent_less_salary_rate: Rate
    metro_salary_rate: Rate
    non_metro_salary_rate: Rate
    metro_cities: tuple[str, ...]

    @model_validator(mode="after")
    def validate_formula(self) -> Self:
        if set(self.exemption_is_minimum_of) != {
            "hra_received",
            "rent_paid_less_salary",
            "salary_percentage",
        }:
            raise ValueError("HRA exemption must use all three statutory limits")
        if "basic" not in self.salary_components:
            raise ValueError("HRA salary must include basic salary")
        if not self.metro_cities:
            raise ValueError("HRA must specify at least one metro city")
        if self.metro_salary_rate < self.non_metro_salary_rate:
            raise ValueError("HRA metro rate must be at least the non-metro rate")
        return self


class DeductionRule(RuleModel):
    code: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    name: str
    max_limit: NonNegativeAmount | None = None
    health_insurance: HealthInsuranceRule | None = None
    hra: HraRule | None = None
    allowed_in: Regime
    source_ids: tuple[SourceId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_hra_percentages(self) -> Self:
        if self.code == "HRA" and self.hra is None:
            raise ValueError("HRA deduction must specify its calculation rules")
        if self.code == "80D" and self.health_insurance is None:
            raise ValueError("80D deduction must specify its category limits")
        return self


class Cess(RuleModel):
    rate: Rate
    source_ids: tuple[SourceId, ...] = Field(min_length=1)


def validate_sequential_brackets(
    items: Sequence[Slab | SurchargeBand],
    name: str,
    *,
    require_zero_start: bool = True,
) -> None:
    if not items:
        raise ValueError(f"{name}s cannot be empty")
    if require_zero_start and items[0].min_income != 0:
        raise ValueError(f"the first {name} must start at 0")

    previous_rate = Decimal("-1")
    previous_min_income = Decimal("-1")
    for item in items:
        if item.min_income <= previous_min_income:
            raise ValueError(f"{name} minimum incomes must be strictly ascending")
        if item.rate < previous_rate:
            raise ValueError(f"{name} rates cannot decrease")
        if item.max_income is not None and item.max_income <= item.min_income:
            raise ValueError(f"{name} upper limit must exceed its lower limit")
        previous_min_income = item.min_income
        previous_rate = item.rate

    for i in range(len(items) - 1):
        if items[i].max_income is None:
            raise ValueError(f"only the last {name} can have no upper limit")
        if items[i + 1].min_income != items[i].max_income:
            raise ValueError(f"{name} {i + 1} does not start where {name} {i} ends")

    if items[-1].max_income is not None:
        raise ValueError(f"the last {name} must have no upper limit")


class TaxRules(RuleModel):
    financial_year: str
    assessment_year: str
    source_ids: tuple[SourceId, ...] = Field(min_length=1)
    new_regime: RegimeRules
    old_regime: RegimeRules
    deductions: tuple[DeductionRule, ...]
    cess: Cess

    @model_validator(mode="before")
    @classmethod
    def reject_duplicate_deductions(cls, data: object) -> object:
        if isinstance(data, dict):
            deductions = data.get("deductions")
            if isinstance(deductions, list):
                codes = [
                    code.strip().casefold()
                    for item in deductions
                    if isinstance(item, dict)
                    and isinstance(code := item.get("code"), str)
                ]
                if len(codes) != len(set(codes)):
                    raise ValueError("deduction codes must be unique")
        return data

    @model_validator(mode="after")
    def check_all_structures(self) -> Self:
        validate_sequential_brackets(self.new_regime.slabs, "new regime slab")
        validate_sequential_brackets(self.old_regime.slabs, "old regime slab")
        validate_sequential_brackets(
            self.new_regime.surcharge_bands,
            "new-regime surcharge band",
            require_zero_start=False,
        )
        validate_sequential_brackets(
            self.old_regime.surcharge_bands,
            "old-regime surcharge band",
            require_zero_start=False,
        )
        return self
