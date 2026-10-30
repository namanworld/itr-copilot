"""Random but sensible synthetic Form 16s, reproducible from a seed.

generate(seed, rules) draws one fictional employee: their salary, claims,
rent and the regime their employer used. The Form 16's tax lines come from
compute(), never from arithmetic here, so every document is consistent.

All randomness comes from one random.Random(seed), so the same seed always
gives the same employee and the whole dataset can be rebuilt from its seeds.
"""

import random
import string
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from itr.domain.form16 import Form16
from itr.domain.form16_mapping import UserExtras
from itr.domain.inputs import DeductionClaims, SalaryInput, TaxInput
from itr.domain.rules import Regime, TaxRules
from itr.synthetic.form16_builder import (
    Identity,
    extras_from_inputs,
    form16_from_inputs,
)

# Clearly fictional names. Every surname and employer marks itself as made up.
FIRST_NAMES = (
    "Asha",
    "Ravi",
    "Meera",
    "Arjun",
    "Kavya",
    "Rohan",
    "Nisha",
    "Vikram",
    "Priya",
    "Sameer",
    "Ananya",
    "Karan",
    "Divya",
    "Imran",
    "Lakshmi",
    "Tarun",
)
SURNAMES = (
    "Sampleton",
    "Fakeworth",
    "Dummykar",
    "Placeholder",
    "Testwala",
    "Mockrajan",
    "Specimenda",
    "Fictionpur",
)
EMPLOYERS = (
    "Specimen Kestrel Analytics Private Limited",
    "Specimen Northwind Ledger Systems Private Limited",
    "Specimen Bluegill Software Private Limited",
    "Specimen Tamarind Logistics Private Limited",
    "Specimen Monsoon Payments Private Limited",
    "Specimen Banyan Health Services Private Limited",
)
# (city, TAN prefix). Metro and non-metro cities, so both HRA rates appear.
CITIES = (
    ("Mumbai", "MUM"),
    ("Delhi", "DEL"),
    ("Kolkata", "CAL"),
    ("Chennai", "CHE"),
    ("Pune", "PNE"),
    ("Bengaluru", "BLR"),
    ("Hyderabad", "HYD"),
    ("Jaipur", "JPR"),
)

# Gross salary bands (lowest, highest) in rupees, with how often each appears.
SALARY_BANDS = (
    ((300_000, 800_000), 3),
    ((800_000, 1_500_000), 4),
    ((1_500_000, 3_000_000), 3),
    ((3_000_000, 6_000_000), 1),
    ((6_000_000, 12_000_000), 1),
)


def _round_to(amount: Decimal, step: int) -> Decimal:
    """Round to the nearest multiple of `step` rupees."""
    return (amount / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def _rupees(rng: random.Random, low: int, high: int, step: int = 100) -> Decimal:
    """A whole-rupee amount between low and high, in multiples of `step`."""
    return Decimal(rng.randrange(low // step, high // step + 1) * step)


def _chance(rng: random.Random, probability: float) -> bool:
    return rng.random() < probability


def _pan(rng: random.Random, holder_type: str, name_initial: str) -> str:
    """Real PAN format AAAAA9999A: the 4th letter is the holder type (P for a
    person, C for a company) and the 5th is the name's initial."""
    letters = "".join(rng.choice(string.ascii_uppercase) for _ in range(3))
    digits = f"{rng.randrange(10_000):04d}"
    check_letter = rng.choice(string.ascii_uppercase)
    return f"{letters}{holder_type}{name_initial}{digits}{check_letter}"


def _tan(rng: random.Random, city_code: str, employer_initial: str) -> str:
    """Real TAN format AAAA99999A: a 3-letter city code, then the deductor's
    initial."""
    digits = f"{rng.randrange(100_000):05d}"
    return f"{city_code}{employer_initial}{digits}{rng.choice(string.ascii_uppercase)}"


def _identity(rng: random.Random, seed: int) -> tuple[Identity, str]:
    """Part A's people and IDs, plus the city the employee lives in."""
    first, surname = rng.choice(FIRST_NAMES), rng.choice(SURNAMES)
    employer = rng.choice(EMPLOYERS)
    office_city, tan_code = rng.choice(CITIES)
    home_city = office_city if _chance(rng, 0.85) else rng.choice(CITIES)[0]
    # The employer's name starts with "Specimen ", so use the word after it.
    employer_initial = employer.split()[1][0]
    identity = Identity(
        certificate_number=f"SYN{seed:06d}",
        employer_name=employer,
        employer_address=office_city,
        employer_pan=_pan(rng, "C", employer_initial),
        employer_tan=_tan(rng, tan_code, employer_initial),
        employee_name=f"{first} {surname}",
        employee_address=home_city,
        employee_pan=_pan(rng, "P", surname[0]),
        period_from=date(2025, 4, 1),
        period_to=date(2026, 3, 31),
    )
    return identity, home_city


def _salary(rng: random.Random) -> SalaryInput:
    """Gross salary and its breakup; the parts add up to exactly gross."""
    bands = [band for band, _ in SALARY_BANDS]
    weights = [weight for _, weight in SALARY_BANDS]
    low, high = rng.choices(bands, weights=weights)[0]
    gross = _rupees(rng, low, high)

    basic = _round_to(gross * Decimal(rng.randrange(40, 51)) / 100, 100)
    dearness_allowance = Decimal(0)
    if _chance(rng, 0.15):
        share = Decimal(rng.randrange(5, 16)) / 100
        dearness_allowance = _round_to(basic * share, 100)
    hra_received = Decimal(0)
    if _chance(rng, 0.75):
        share = Decimal(rng.randrange(40, 51)) / 100
        hra_received = _round_to(basic * share, 100)
    # Other allowances are whatever is left, so the parts add up exactly.
    other_allowances = gross - basic - dearness_allowance - hra_received
    return SalaryInput(
        gross=gross,
        basic=basic,
        dearness_allowance=dearness_allowance,
        hra_received=hra_received,
        other_allowances=other_allowances,
    )


def _tax_input(rng: random.Random, home_city: str) -> TaxInput:
    salary = _salary(rng)

    rent_paid = Decimal(0)
    if salary.hra_received > 0 and _chance(rng, 0.8):
        share = Decimal(rng.randrange(60, 141)) / 100
        rent_paid = _round_to(salary.hra_received * share, 1000)

    claims_80d_parents = _chance(rng, 0.2)
    return TaxInput(
        salary=salary,
        rent_paid=rent_paid,
        city=home_city if rent_paid > 0 else None,
        professional_tax=Decimal(rng.choice((0, 2400, 2500))),
        self_occupied_home_loan_interest=(
            _rupees(rng, 50_000, 350_000, 1000) if _chance(rng, 0.2) else Decimal(0)
        ),
        savings_interest=(
            _rupees(rng, 1_000, 25_000) if _chance(rng, 0.8) else Decimal(0)
        ),
        other_interest=(
            _rupees(rng, 5_000, 80_000) if _chance(rng, 0.3) else Decimal(0)
        ),
        deductions=DeductionClaims(
            # Up to Rs 2 lakh, so some claims go over the Rs 1.5 lakh cap.
            section_80c=(
                _rupees(rng, 20_000, 200_000, 1000) if _chance(rng, 0.7) else Decimal(0)
            ),
            section_80d_self_and_family=(
                _rupees(rng, 10_000, 60_000) if _chance(rng, 0.4) else Decimal(0)
            ),
            section_80d_parents=(
                _rupees(rng, 10_000, 60_000) if claims_80d_parents else Decimal(0)
            ),
        ),
        parents_include_senior_citizen=claims_80d_parents and _chance(rng, 0.5),
    )


def generate(seed: int, rules: TaxRules) -> tuple[Form16, UserExtras]:
    """One fictional employee: their Form 16 and what they'd tell the tool."""
    rng = random.Random(seed)
    identity, home_city = _identity(rng, seed)
    inputs = _tax_input(rng, home_city)
    # Most salaried people now stay in the default new regime.
    regime: Regime = "new" if _chance(rng, 0.65) else "old"
    form16 = form16_from_inputs(inputs, rules, regime, identity)
    return form16, extras_from_inputs(inputs)
