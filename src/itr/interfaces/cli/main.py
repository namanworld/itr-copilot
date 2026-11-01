import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from itr.application.extract_eval import (
    DocumentResult,
    Refusal,
    build_report,
    evaluate_document,
)
from itr.application.extract_form16 import (
    NOT_EXTRACTED,
    Form16ValidationError,
    NotAForm16Error,
    extract_form16,
)
from itr.application.form16_diff import diff_form16
from itr.application.ports import LLMClient, LLMOutputError, LLMUsage
from itr.domain.computation import Comparison, Computation
from itr.domain.form16 import Form16
from itr.domain.inputs import TaxInput
from itr.domain.rules import TaxRules
from itr.domain.tax import compare
from itr.infrastructure.llm.anthropic_client import AnthropicLLM
from itr.infrastructure.pdf.form16_renderer import render_form16
from itr.infrastructure.pdf.pdf_to_text import NoTextLayerError, pdf_to_text
from itr.infrastructure.rules.json_loader import load_rules
from itr.settings import Settings, get_settings
from itr.synthetic.dataset import build_eval_dataset, render_case
from itr.synthetic.form16_generator import generate


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
    for label, regime in (
        ("New", rules.new_regime),
        ("Old", rules.old_regime),
    ):
        rebate = regime.rebate_87a
        relief = "yes" if rebate.marginal_relief else "no"
        print(
            f"{label} regime 87A rebate: up to {format_inr(rebate.max_rebate)} "
            f"through income {format_inr(rebate.taxable_income_limit)}; "
            f"marginal relief: {relief}"
        )
        print(
            f"{label} regime surcharge bands: "
            + ", ".join(
                f"{format_percent(band.rate)} from {format_inr(band.min_income)}"
                for band in regime.surcharge_bands
            )
        )
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
        elif d.health_insurance is not None:
            health_policy = d.health_insurance
            self_senior_limit = format_inr(
                health_policy.self_and_family.senior_citizen_limit
            )
            parent_senior_limit = format_inr(health_policy.parents.senior_citizen_limit)
            limit_str = (
                "Self/family: "
                f"{format_inr(health_policy.self_and_family.regular_limit)} "
                f"(senior {self_senior_limit}); "
                "parents: "
                f"{format_inr(health_policy.parents.regular_limit)} "
                f"(senior {parent_senior_limit}); "
                f"senior from age {health_policy.senior_citizen_age}; "
                "preventive check-up within category limit: "
                f"{format_inr(health_policy.preventive_checkup_limit)}"
            )
        elif d.hra is not None:
            hra_policy = d.hra
            limit_str = (
                f"lowest of HRA received, rent less "
                f"{format_percent(hra_policy.rent_less_salary_rate)} of salary, and "
                f"{format_percent(hra_policy.metro_salary_rate)} metro/"
                f"{format_percent(hra_policy.non_metro_salary_rate)} non-metro salary; "
                f"metro cities: {', '.join(hra_policy.metro_cities)}; "
                "salary: basic plus qualifying DA"
            )
        else:
            limit_str = "No specific cap configured"
        print(
            f"    - [{d.code}] {d.name} ({limit_str}) "
            f"[Allowed in: {d.allowed_in.upper()} regime]"
        )
    print()


# --- itr compute ---------------------------------------------------------------


def format_amount(amount: Decimal) -> str:
    """An amount for the computation sheet: Indian grouping, paise only when
    there are any, and a minus sign in front of the rupee symbol."""
    paise = amount.quantize(Decimal("0.01"))
    if paise == paise.to_integral_value():
        paise = paise.quantize(Decimal(1))
    text = format_inr(abs(paise))
    return f"-{text}" if paise < 0 else text


# --- itr extract ---------------------------------------------------------------


def make_llm(settings: Settings, use_disk_cache: bool) -> LLMClient:
    """The real Claude client. Tests replace this function with a fake."""
    cache_dir = settings.llm_cache_dir if use_disk_cache else None
    return AnthropicLLM(settings, cache_dir=cache_dir)


def format_usage(usage: LLMUsage) -> str:
    line = (
        f"Cost: ${usage.cost_usd:.6f} (input {usage.input_tokens}, "
        f"output {usage.output_tokens}, cache write {usage.cache_write_tokens}, "
        f"cache read {usage.cache_read_tokens} tokens)"
    )
    if usage.from_disk_cache:
        line += " - from the disk cache, not charged this time"
    return line


def load_truth(pdf: Path) -> Form16 | None:
    """F001.pdf's truth is F001.truth.json, if it exists. Reads both the plain
    Form16 format and the tagged {"tag": ..., "form16": ...} envelope."""
    path = pdf.with_name(f"{pdf.stem}.truth.json")
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if "tag" in data:
        data = data["form16"]
    return None if data is None else Form16.model_validate(data)


# How a truth file's refusal reason is scored: what the model must do.
REFUSALS: dict[str, Refusal] = {
    "not_a_form16": "not_a_form16",  # say document_type is "other"
    "capital_gains": "unsupported_items",  # report the capital gains
}


def load_case(pdf: Path) -> tuple[Form16 | None, Refusal | None] | None:
    """The truth for F001.pdf and how it should be refused, if at all; None
    when there is no truth file. A plain Form16 file should be extracted. A
    tagged {"tag": ..., "form16": ...} file says whether to refuse, and its
    form16 is null for a document that isn't a Form 16."""
    path = pdf.with_name(f"{pdf.stem}.truth.json")
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if "tag" not in data:
        return Form16.model_validate(data), None
    tag = data["tag"]
    form16 = None if data["form16"] is None else Form16.model_validate(data["form16"])
    if tag["expected"] != "refuse":
        return form16, None
    if tag["reason"] not in REFUSALS:
        raise ValueError(f"{path.name}: unknown refusal reason {tag['reason']!r}")
    return form16, REFUSALS[tag["reason"]]


def run_extract(args: argparse.Namespace) -> int:
    settings = get_settings()
    pdf = Path(args.pdf)
    try:
        text = pdf_to_text(pdf)
        system_prompt = (settings.prompts_dir / "extract_form16.md").read_text(
            encoding="utf-8"
        )
    except (OSError, NoTextLayerError) as error:
        print(f"itr: {error}", file=sys.stderr)
        return 1
    if not settings.anthropic_api_key:
        print("itr: set ANTHROPIC_API_KEY in .env", file=sys.stderr)
        return 1

    llm = make_llm(settings, use_disk_cache=not args.no_cache)
    try:
        extraction = extract_form16(text, llm, system_prompt)
    except NotAForm16Error:
        print("itr: this document is not a Form 16; nothing extracted", file=sys.stderr)
        return 1
    except Form16ValidationError as error:
        print("itr: the extracted Form 16 failed validation:", file=sys.stderr)
        for problem in error.problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    except LLMOutputError as error:
        print(f"itr: {error}", file=sys.stderr)
        return 1

    print(extraction.form16.model_dump_json(indent=2))
    print(format_usage(extraction.usage))
    for item in extraction.form16.unrecognised_lines:
        print(
            f"Unsupported: {item.description} ({item.amount}) in {item.where}; "
            "tax can't be computed from this Form 16"
        )

    truth = load_truth(pdf)
    if truth is not None:
        diff = diff_form16(truth, extraction.form16, skip=NOT_EXTRACTED)
        print(f"Truth: {diff.matched}/{diff.compared} fields match")
        for mismatch in diff.mismatches:
            print(
                f"  {mismatch.path}: expected {mismatch.expected}, "
                f"got {mismatch.actual}"
            )
    return 0


# One row per line of the sheet: (label, how to read it from a Computation).
# A row with no reader is a section heading.
Row = tuple[str, Callable[[Computation], Decimal] | None]

SHEET_ROWS: list[Row] = [
    ("Salary", None),
    ("Gross salary", lambda c: c.income.gross_salary),
    ("Less: HRA exemption, 10(13A)", lambda c: c.income.hra_exemption),
    ("Less: standard deduction, 16(ia)", lambda c: c.income.standard_deduction),
    ("Less: professional tax, 16(iii)", lambda c: c.income.professional_tax),
    ("Income from salary", lambda c: c.income.income_from_salary),
    ("Other heads", None),
    ("House property, 24(b)", lambda c: c.income.house_property_income),
    ("Interest income", lambda c: c.income.interest_income),
    ("Gross total income", lambda c: c.income.gross_total_income),
    ("Deductions, Chapter VI-A", None),
    ("80C", lambda c: c.income.deduction_80c),
    ("80D", lambda c: c.income.deduction_80d),
    ("80TTA", lambda c: c.income.deduction_80tta),
    ("Total deductions", lambda c: c.income.deductions_total),
    ("Total income", lambda c: c.income.total_income),
    ("Tax", None),
    ("Total income, rounded (288A)", lambda c: c.tax.total_income_rounded),
    ("Tax on total income", lambda c: c.tax.tax_on_total_income),
    ("Less: rebate, 87A", lambda c: c.tax.rebate_87a),
    ("Tax after rebate", lambda c: c.tax.tax_after_rebate),
    ("Add: surcharge", lambda c: c.tax.surcharge),
    ("Add: health and education cess", lambda c: c.tax.cess),
    ("Tax payable (288B)", lambda c: c.tax.tax_payable),
]


def print_comparison(result: Comparison, title: str) -> None:
    """The two computations side by side, then which regime is cheaper."""
    label_width = max(len(label) for label, _ in SHEET_ROWS)
    cells = [
        format_amount(read(sheet))
        for _, read in SHEET_ROWS
        if read is not None
        for sheet in (result.new, result.old)
    ]
    width = max(len("New regime"), *(len(cell) for cell in cells))

    print(title)
    print()
    print(f"{'':<{label_width}}  {'New regime':>{width}}  {'Old regime':>{width}}")
    for label, read in SHEET_ROWS:
        if read is None:
            print()
            print(label.upper())
            continue
        new, old = format_amount(read(result.new)), format_amount(read(result.old))
        print(f"{label:<{label_width}}  {new:>{width}}  {old:>{width}}")

    print()
    if result.cheaper is None:
        print("Both regimes cost the same.")
    else:
        regime = "New" if result.cheaper == "new" else "Old"
        print(f"{regime} regime is cheaper by {format_amount(result.saving)}.")


def scenarios_file() -> Path:
    """The golden scenarios, found next to the rules folder."""
    return get_settings().rules_dir.parent / "tests" / "fixtures" / "scenarios.json"


def find_scenario(wanted: str, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Accept a full id ("03-metro-hra-old-wins"), its number ("03" or "3"),
    or "S3"."""
    key = wanted.strip()
    if key[:1] in ("S", "s") and key[1:].isdigit():
        key = key[1:]
    if key.isdigit():
        key = f"{int(key):02d}-"
    matches = [s for s in scenarios if s["id"] == wanted or s["id"].startswith(key)]
    if len(matches) != 1:
        known = ", ".join(s["id"] for s in scenarios)
        raise LookupError(f"no single scenario matches {wanted!r}; known: {known}")
    return matches[0]


def run_compute(args: argparse.Namespace) -> int:
    try:
        if args.scenario is not None:
            data = json.loads(scenarios_file().read_text(encoding="utf-8"))
            scenario = find_scenario(args.scenario, data["scenarios"])
            raw_inputs, title = scenario["inputs"], f"Scenario {scenario['id']}"
        else:
            raw_inputs = json.loads(Path(args.input).read_text(encoding="utf-8"))
            title = f"Inputs from {args.input}"
        inputs = TaxInput.model_validate(raw_inputs)
        rules = load_rules(args.fy)
    except (OSError, LookupError, ValidationError, ValueError) as error:
        print(f"itr: {error}", file=sys.stderr)
        return 1

    print_comparison(compare(inputs, rules), f"{title}, FY {rules.financial_year}")
    return 0


# --- itr synth-form16 ---------------


def _positive(text: str) -> int:
    number = int(text)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def run_synth_form16(args: argparse.Namespace) -> int:
    """Write n synthetic Form 16s: a PDF, its truth JSON and the user's facts."""

    try:
        rules = load_rules(args.fy)
    except (OSError, ValueError) as error:
        print(f"itr: {error}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    width = max(3, len(str(args.n)))
    manifest = []

    for index in range(args.n):
        seed = args.seed + index
        form16, extras = generate(seed, rules)
        name = f"F{index + 1:0{width}d}"
        (out / f"{name}.pdf").write_bytes(render_form16(form16))
        (out / f"{name}.truth.json").write_text(
            form16.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (out / f"{name}.user.json").write_text(
            extras.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        manifest.append({"file": name, "seed": seed})

    (out / "manifest.json").write_text(
        json.dumps({"fy": args.fy, "cases": manifest}, indent=2) + "\n",
        encoding="utf-8",
    )
    last = args.seed + args.n - 1
    print(f"Wrote {args.n} synthetic Form 16s (seeds {args.seed} to {last}) to {out}")
    return 0


# ------- eval-extract ------------------------------------------------------------


def print_result(result: DocumentResult) -> None:
    """One line per document, with the reason when it isn't fully correct."""
    if result.fully_correct:
        refused = f" (refused: {result.refusal})" if result.refusal else ""
        print(f"{result.name}: ok{refused}")
        return
    reasons: list[str] = []
    if not result.refusal_correct:
        reasons.append(
            f"refusal expected {result.expected_refusal}, got {result.refusal}"
        )
    if not result.extracted and result.error:
        reasons.append(result.error.splitlines()[0])
        reasons += list(result.problems)
    elif result.mismatches:
        reasons += [
            f"{m.path}: expected {m.expected}, got {m.actual}"
            for m in result.mismatches
        ]
    print(f"{result.name}: FAIL")
    for reason in reasons:
        print(f"    {reason}")


def run_synth_eval(args: argparse.Namespace) -> int:
    """Write the evaluation set: normal, hard and out-of-scope documents, each
    with a PDF, a tagged truth file, the user's facts and a manifest line."""
    try:
        rules = load_rules(args.fy)
    except (OSError, ValueError) as error:
        print(f"itr: {error}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cases = build_eval_dataset(args.seed, rules)
    for case in cases:
        name = case.tag.case_id
        (out / f"{name}.pdf").write_bytes(render_case(case))
        (out / f"{name}.truth.json").write_text(
            case.truth.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        (out / f"{name}.user.json").write_text(
            case.extras.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    manifest = {"fy": args.fy, "cases": [c.tag.model_dump(mode="json") for c in cases]}
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    counts = Counter(case.tag.category for case in cases)
    summary = ", ".join(f"{count} {category}" for category, count in counts.items())
    print(f"Wrote {len(cases)} evaluation cases ({summary}) to {out}")
    return 0


def run_eval_extract(args: argparse.Namespace) -> int:
    """Extract every PDF in a folder, score it against its truth file and
    write a dated JSON report."""
    settings = get_settings()
    folder = Path(args.folder)
    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        print(f"itr: no PDFs in {folder}", file=sys.stderr)
        return 1
    try:
        system_prompt = (settings.prompts_dir / "extract_form16.md").read_text(
            encoding="utf-8"
        )
    except OSError as error:
        print(f"itr: {error}", file=sys.stderr)
        return 1
    if not settings.anthropic_api_key:
        print("itr: set ANTHROPIC_API_KEY in .env", file=sys.stderr)
        return 1

    llm = make_llm(settings, use_disk_cache=not args.no_cache)
    results: list[DocumentResult] = []
    skipped: list[str] = []
    for pdf in pdfs:
        case = load_case(pdf)
        if case is None:  # no truth file
            skipped.append(pdf.name)
            continue
        truth, expected_refusal = case
        try:
            text = pdf_to_text(pdf)
        except NoTextLayerError as error:
            print(f"itr: {pdf.name}: {error}", file=sys.stderr)
            skipped.append(pdf.name)
            continue
        result = evaluate_document(
            pdf.stem, text, truth, llm, system_prompt, expected_refusal
        )
        print_result(result)
        results.append(result)

    today = date.today().isoformat()
    report = build_report(
        results,
        {
            "date": today,
            "model": settings.claude_model,
            "prompt": system_prompt.splitlines()[0] if system_prompt else "",
            "folder": str(folder),
            "skipped": skipped,
        },
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"extract-{today}.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    refusals = report["refusals"]
    print(
        f"{report['documents_fully_correct']}/{report['documents']} documents "
        f"fully correct; {len(report['failures'])} field mismatches; "
        f"{len(report['failed_extractions'])} failed extractions; "
        f"refusals {refusals['correct']}/{refusals['expected']} correct, "
        f"{len(refusals['false'])} false"
    )
    cost = report["cost"]
    print(
        f"Cost: ${cost['total_usd']} total, ${cost['average_usd_per_document']} "
        f"per document; charged this run: ${cost['charged_this_run_usd']}"
    )
    print(f"Report: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="itr")
    commands = parser.add_subparsers(dest="command", required=True)
    rules_cmd = commands.add_parser(
        "rules", help="show the tax rules for a financial year"
    )
    rules_cmd.add_argument("--fy", required=True, help="financial year, e.g. 2025-26")

    compute_cmd = commands.add_parser(
        "compute", help="compute tax in both regimes and compare them"
    )
    source = compute_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--scenario", help="a golden scenario, e.g. 03 or S3")
    source.add_argument("--input", help="a JSON file shaped like a scenario's inputs")
    compute_cmd.add_argument("--fy", default="2025-26", help="financial year")

    # ---- itr synth-form16 ------
    synth_cmd = commands.add_parser(
        "synth-form16", help="write synthetic Form 16 PDFs with truth JSON"
    )
    synth_cmd.add_argument("--n", type=_positive, default=20, help="how many")
    synth_cmd.add_argument("--seed", type=int, default=42, help="first seed")
    synth_cmd.add_argument("--out", default="data/form16", help="output folder")
    synth_cmd.add_argument("--fy", default="2025-26", help="financial year")

    # ---- itr extract ------
    extract_cmd = commands.add_parser(
        "extract", help="read a Form 16 PDF with Claude and print it as JSON"
    )
    extract_cmd.add_argument("pdf", help="path to the Form 16 PDF")
    extract_cmd.add_argument(
        "--no-cache", action="store_true", help="ignore saved responses"
    )

    # ---- itr eval-extract ------
    synth_eval_cmd = commands.add_parser(
        "synth-eval",
        help="write the evaluation set: normal, hard and out-of-scope PDFs",
    )
    synth_eval_cmd.add_argument("--seed", type=int, default=42, help="first seed")
    synth_eval_cmd.add_argument(
        "--out", default="data/form16-eval", help="output folder"
    )
    synth_eval_cmd.add_argument("--fy", default="2025-26", help="financial year")

    eval_cmd = commands.add_parser(
        "eval-extract",
        help="""
        extract every Form 16 PDF in a folder and score it against its truth,
        score extraction on a folder of PDFs with truth files""",
    )
    eval_cmd.add_argument(
        "folder", help="path to a folder of Form 16 PDFs, e.g. data/form16"
    )
    eval_cmd.add_argument(
        "--out",
        default="evals/reports",
        help="folder to write the JSON report to",
    )
    eval_cmd.add_argument(
        "--no-cache", action="store_true", help="ignore saved responses"
    )

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
    elif args.command == "compute":
        return run_compute(args)
    elif args.command == "synth-form16":
        return run_synth_form16(args)
    elif args.command == "extract":
        return run_extract(args)
    elif args.command == "synth-eval":
        return run_synth_eval(args)
    elif args.command == "eval-extract":
        return run_eval_extract(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
