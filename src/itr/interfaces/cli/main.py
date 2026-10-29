import argparse
import sys
from decimal import Decimal

from itr.domain.rules import TaxRules
from itr.infrastructure.rules.json_loader import load_rules


def format_inr(amount: Decimal) -> str:
    text = f"{amount:f}"
    whole, _, paise = text.partition(".")
    last_three = whole[-3:]
    rest = whole[:-3]
    groups: list[str] = []
    while rest:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    groups.append(last_three)
    result = "₹" + ",".join(groups)
    if paise:
        result += "." + paise
    return result


def format_percent(rate: Decimal) -> str:
    text = f"{rate * 100:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text + "%"


def print_rules(rules: TaxRules) -> None:
    print(
        f"Tax Rules for Financial Year {rules.financial_year} "
        f"(Assessment Year {rules.assessment_year})"
    )
    print(f"Sources: {', '.join(rules.source_ids)}\n")

    # Side-by-Side Slabs Header
    print(f"{'NEW REGIME':<45} | {'OLD REGIME':<45}")
    print(
        f"{'Min Income':<15} {'Max Income':<18} {'Rate':<8} | "
        f"{'Min Income':<15} {'Max Income':<18} {'Rate':<8}"
    )
    print("-" * 93)

    new_slabs = rules.new_regime.slabs
    old_slabs = rules.old_regime.slabs
    max_len = max(len(new_slabs), len(old_slabs))

    for i in range(max_len):
        if i < len(new_slabs):
            ns = new_slabs[i]
            n_min, n_max = (
                format_inr(ns.min_income),
                (format_inr(ns.max_income) if ns.max_income is not None else "Above"),
            )
            new_str = f"{n_min:<15} {n_max:<18} {format_percent(ns.rate):<8}"
        else:
            new_str = f"{'':<43}"

        if i < len(old_slabs):
            os = old_slabs[i]
            o_min, o_max = (
                format_inr(os.min_income),
                (format_inr(os.max_income) if os.max_income is not None else "Above"),
            )
            old_str = f"{o_min:<15} {o_max:<18} {format_percent(os.rate):<8}"
        else:
            old_str = f"{'':<43}"

        print(f"{new_str} | {old_str}")

    print("-" * 93)
    new_deduction = format_inr(rules.new_regime.standard_deduction.salary_max)
    old_deduction = format_inr(rules.old_regime.standard_deduction.salary_max)
    print(f"Standard Deduction:  New: {new_deduction}  |  Old: {old_deduction}")
    print(f"Health & Education Cess: {format_percent(rules.cess.rate)}")
    print()

    # Deductions section split by regime
    print("DEDUCTIONS & EXEMPTIONS APPLICABILITY:")
    new_deductions = [d for d in rules.deductions if d.allowed_in == "new"]
    old_deductions = [d for d in rules.deductions if d.allowed_in == "old"]

    new_allowed = (
        ", ".join(d.code for d in new_deductions)
        if new_deductions
        else "None (other than Standard Deduction)"
    )
    old_allowed = ", ".join(d.code for d in old_deductions)
    print(f"  • New Regime Allowed: {new_allowed}")
    print(f"  • Old Regime Allowed: {old_allowed}")
    for d in rules.deductions:
        if d.max_limit is not None:
            limit_str = f"Max: {format_inr(d.max_limit)}"
        else:
            if d.metro_percentage is None or d.non_metro_percentage is None:
                raise ValueError(
                    f"deduction {d.code} must specify both HRA percentages"
                )
            limit_str = (
                f"Metro: {format_percent(d.metro_percentage)}, "
                f"Non-Metro: {format_percent(d.non_metro_percentage)}"
            )
        print(
            f"    - [{d.code}] {d.name} ({limit_str}) "
            f"[Allowed in: {d.allowed_in.upper()} regime]"
        )
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="itr")
    commands = parser.add_subparsers(dest="command", required=True)
    rules_cmd = commands.add_parser(
        "rules", help="show the tax rules for a financial year"
    )
    rules_cmd.add_argument("--fy", required=True, help="financial year, e.g. 2025-26")

    args = parser.parse_args(argv)

    if args.command == "rules":
        try:
            rules = load_rules(args.fy)
        except FileNotFoundError:
            print(f"itr: no rules file for FY {args.fy}", file=sys.stderr)
            return 1
        except ValueError as error:
            print(
                f"itr: rules file for FY {args.fy} is invalid:\n{error}",
                file=sys.stderr,
            )
            return 1
        print_rules(rules)

    return 0


if __name__ == "__main__":
    sys.exit(main())
