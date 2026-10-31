"""The synthetic Form 16 generator: reproducible, consistent, realistic IDs."""

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from itr.domain.form16 import Form16
from itr.domain.form16_mapping import to_tax_input
from itr.domain.rules import TaxRules
from itr.domain.tax import compute
from itr.synthetic.form16_generator import generate

ROOT = Path(__file__).parents[2]
SEEDS = range(300)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


def test_same_seed_gives_same_output(rules: TaxRules) -> None:
    for seed in SEEDS:
        assert generate(seed, rules) == generate(seed, rules)


def test_different_seeds_give_different_people(rules: TaxRules) -> None:
    pans = {generate(seed, rules)[0].part_a.employee_pan for seed in SEEDS}
    assert len(pans) > len(SEEDS) * 0.95


def test_tax_lines_equal_compute(rules: TaxRules) -> None:
    """The employer's tax lines are compute() on what the employer knows:
    everything except the employee's bank interest."""
    for seed in SEEDS:
        form16, extras = generate(seed, rules)
        result = to_tax_input(form16, extras, rules)
        assert result.tax_input is not None, (seed, result.findings)
        employer_view = result.tax_input.model_copy(
            update={"savings_interest": Decimal(0), "other_interest": Decimal(0)}
        )
        lines = form16.part_b.tax
        tax = compute(employer_view, rules, form16.part_b.regime_used).tax

        assert lines.total_taxable_income == tax.total_income_rounded, seed
        assert lines.tax_on_total_income == tax.tax_on_total_income, seed
        assert lines.rebate_87a == tax.rebate_87a, seed
        assert lines.surcharge == tax.surcharge, seed
        assert lines.health_and_education_cess == (
            tax.cess.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        ), seed
        assert lines.tax_payable == tax.tax_payable, seed


def test_generated_cases_map_back_without_findings(rules: TaxRules) -> None:
    """A consistent document plus the true user facts raises no alarms."""
    for seed in SEEDS:
        form16, extras = generate(seed, rules)
        findings = to_tax_input(form16, extras, rules).findings
        assert findings == (), (seed, [(f.line, f.message) for f in findings])


def test_salary_parts_add_up_to_gross(rules: TaxRules) -> None:
    for seed in SEEDS:
        form16, _ = generate(seed, rules)
        breakup = form16.salary_breakup
        assert breakup is not None, seed
        parts = (
            breakup.basic
            + breakup.dearness_allowance
            + breakup.house_rent_allowance
            + breakup.other_allowances
        )
        assert parts == form16.part_b.gross_salary.total, seed


def test_amounts_are_whole_rupees(rules: TaxRules) -> None:
    for seed in SEEDS:
        form16, _ = generate(seed, rules)
        breakup = form16.salary_breakup
        assert breakup is not None
        for amount in (
            form16.part_b.gross_salary.total,
            breakup.basic,
            breakup.house_rent_allowance,
        ):
            assert amount == amount.to_integral_value(), (seed, amount)


PAN = re.compile(r"[A-Z]{3}[PC][A-Z][0-9]{4}[A-Z]")
TAN = re.compile(r"[A-Z]{4}[0-9]{5}[A-Z]")


def test_pan_and_tan_formats_are_valid(rules: TaxRules) -> None:
    for seed in SEEDS:
        part_a = generate(seed, rules)[0].part_a
        assert PAN.fullmatch(part_a.employee_pan), part_a.employee_pan
        assert PAN.fullmatch(part_a.employer_pan), part_a.employer_pan
        assert TAN.fullmatch(part_a.employer_tan), part_a.employer_tan
        # 4th letter: P for a person, C for a company. 5th: the name's initial.
        assert part_a.employee_pan[3] == "P"
        assert part_a.employer_pan[3] == "C"
        surname = part_a.employee_name.split()[-1]
        assert part_a.employee_pan[4] == surname[0]


def test_names_are_clearly_fictional(rules: TaxRules) -> None:
    for seed in SEEDS:
        part_a = generate(seed, rules)[0].part_a
        assert part_a.employer_name.startswith("Specimen ")
        assert part_a.certificate_number.startswith("SYN")


def test_dataset_covers_the_interesting_cases(rules: TaxRules) -> None:
    """Over 300 seeds, both regimes, both HRA rates, capped claims and
    surcharge all appear, so the dataset exercises the whole mapping."""
    cases = [generate(seed, rules) for seed in SEEDS]
    regimes = {f.part_b.regime_used for f, _ in cases}
    cities = {e.city for _, e in cases if e.rent_paid > 0}
    assert regimes == {"new", "old"}
    assert {"Mumbai", "Pune"} <= cities  # a metro and a non-metro rate
    assert any((e.section_80c or 0) > 150_000 for _, e in cases)  # over the cap
    assert any(f.part_b.tax.surcharge > 0 for f, _ in cases)
    assert any(e.self_occupied_home_loan_interest for _, e in cases)
    assert any(f.part_b.tax.rebate_87a > 0 for f, _ in cases)


def test_truth_json_round_trips(rules: TaxRules) -> None:
    for seed in range(20):
        form16, _ = generate(seed, rules)
        assert Form16.model_validate_json(form16.model_dump_json()) == form16
