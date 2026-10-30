from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Amount = Annotated[Decimal, Field(ge=0)]


class InputModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SalaryInput(InputModel):
    gross: Amount
    basic: Amount = Decimal(0)
    dearness_allowance: Amount = Decimal(0)
    hra_received: Amount = Decimal(0)
    other_allowances: Amount = Decimal(0)

    @model_validator(mode="after")
    def check_parts_fit_in_gross(self) -> Self:
        parts = (
            self.basic
            + self.dearness_allowance
            + self.hra_received
            + self.other_allowances
        )
        if parts > self.gross:
            raise ValueError(
                f"salary parts add up to {parts}, more than gross {self.gross}"
            )
        return self


class DeductionClaims(InputModel):
    section_80c: Amount = Decimal(0)
    section_80d_self_and_family: Amount = Decimal(0)
    section_80d_parents: Amount = Decimal(0)


class TaxInput(InputModel):
    salary: SalaryInput
    rent_paid: Amount = Decimal(0)
    city: str | None = None
    professional_tax: Amount = Decimal(0)
    self_occupied_home_loan_interest: Amount = Decimal(0)
    savings_interest: Amount = Decimal(0)
    other_interest: Amount = Decimal(0)
    deductions: DeductionClaims = DeductionClaims()
    self_or_family_has_senior_citizen: bool = False
    parents_include_senior_citizen: bool = False
