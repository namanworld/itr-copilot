from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator


class Slab(BaseModel):
    model_config = ConfigDict(frozen=True)
    min_income: Decimal
    max_income: Decimal | None
    rate: Decimal
    source_ids: tuple[str, ...]


class StandardDeduction(BaseModel):
    model_config = ConfigDict(frozen=True)
    salary_max: Decimal
    source_ids: tuple[str, ...]


class Rebate87A(BaseModel):
    model_config = ConfigDict(frozen=True)
    taxable_income_limit: Decimal
    max_rebate: Decimal
    resident_individual_only: bool
    source_ids: tuple[str, ...]


class RegimeRules(BaseModel):
    model_config = ConfigDict(frozen=True)
    slabs: tuple[Slab, ...]
    standard_deduction: StandardDeduction
    rebate_87a: Rebate87A
    source_ids: tuple[str, ...]


class SurchargeBand(BaseModel):
    model_config = ConfigDict(frozen=True)
    min_income: Decimal
    max_income: Decimal | None
    rate: Decimal
    marginal_relief: bool
    source_ids: tuple[str, ...]


class DeductionRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: str
    name: str
    max_limit: Decimal | None = None
    metro_percentage: Decimal | None = None
    non_metro_percentage: Decimal | None = None
    allowed_in: Literal["old", "new"]
    source_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_hra_percentages(self) -> Self:
        if self.code == "HRA":
            if self.metro_percentage is None or self.non_metro_percentage is None:
                raise ValueError(
                    "HRA deduction must specify metro and non_metro percentages"
                )
            if self.metro_percentage < self.non_metro_percentage:
                raise ValueError(
                    "HRA metro percentage must be greater than or equal to "
                    "non-metro percentage"
                )
        return self


class Cess(BaseModel):
    model_config = ConfigDict(frozen=True)
    rate: Decimal
    source_ids: tuple[str, ...]


def validate_sequential_brackets(
    items: Sequence[Slab | SurchargeBand],
    name: str,
    *,
    require_zero_start: bool = True,
) -> None:
    if not items:
        return
    if require_zero_start and items[0].min_income != 0:
        raise ValueError(f"the first {name} must start at 0")

    for i in range(len(items) - 1):
        if items[i].max_income is None:
            raise ValueError(f"only the last {name} can have no upper limit")
        if items[i + 1].min_income != items[i].max_income:
            raise ValueError(f"{name} {i + 1} does not start where {name} {i} ends")

    if items[-1].max_income is not None:
        raise ValueError(f"the last {name} must have no upper limit")

    for item in items:
        if not (0 <= item.rate <= 1):
            raise ValueError(f"rate {item.rate} must be between 0 and 1")


class TaxRules(BaseModel):
    model_config = ConfigDict(frozen=True)

    financial_year: str
    assessment_year: str
    source_ids: tuple[str, ...]
    new_regime: RegimeRules
    old_regime: RegimeRules
    surcharge_bands: tuple[SurchargeBand, ...]
    deductions: tuple[DeductionRule, ...]
    cess: Cess

    @model_validator(mode="after")
    def check_all_structures(self) -> Self:
        validate_sequential_brackets(self.new_regime.slabs, "new regime slab")
        validate_sequential_brackets(self.old_regime.slabs, "old regime slab")
        validate_sequential_brackets(
            self.surcharge_bands, "surcharge band", require_zero_start=False
        )
        return self
