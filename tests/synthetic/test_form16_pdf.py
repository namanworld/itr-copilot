"""The rendered PDFs have a text layer that holds every truth value.

Day 4 sends the extracted text to the model. If a value is missing from the
text, or the PDF has no text layer at all, a perfect extraction is impossible.
"""

import json
import re
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from itr.domain.form16 import Form16
from itr.domain.rules import TaxRules
from itr.infrastructure.pdf.form16_renderer import (
    WATERMARK,
    NumberFormat,
    format_amount,
    render_form16,
)
from itr.infrastructure.pdf.form16_statement import render_form16_statement
from itr.synthetic.dataset import _with_capital_gains
from itr.synthetic.form16_generator import generate

ROOT = Path(__file__).parents[2]
SEEDS = range(20)


@pytest.fixture(scope="module")
def rules() -> TaxRules:
    text = (ROOT / "rules/fy2025_26.json").read_text(encoding="utf-8")
    return TaxRules.model_validate(json.loads(text, parse_float=Decimal))


def extract_text(pdf: bytes) -> str:
    reader = PdfReader(BytesIO(pdf))
    return "\n".join(page.extract_text() for page in reader.pages)


def normalise(text: str) -> str:
    """Reduce every number format to plain digits and collapse whitespace, so
    ₹15,00,000, Rs. 15,00,000, 1500000.00 and a value wrapped onto two lines
    all match 1500000."""
    text = text.replace("₹", "").replace("Rs.", "")
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"(?<=\d)\.00(?!\d)", "", text)
    return re.sub(r"\s+", " ", text)


# Every way a Form 16 can be drawn: (layout name, render function).
FORMATS: list[NumberFormat] = [
    "indian_grouping",
    "rupee_symbol",
    "plain_decimal",
    "rs_prefix",
]
RENDERERS = [("standard", render_form16)] + [
    (f"statement-{fmt}", lambda f, fmt=fmt: render_form16_statement(f, fmt))
    for fmt in FORMATS
]


def truth_values(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Every leaf of the truth JSON as (path, the text it should print as)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from truth_values(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from truth_values(value, f"{path}[{index}]")
    elif node is None:
        return  # an empty column prints nothing
    elif isinstance(node, bool):
        yield path, "YES" if node else "NO"
    elif re.fullmatch(r"-?\d+(\.\d+)?", str(node)):
        amount = Decimal(node)
        whole = amount == amount.to_integral_value()
        yield path, f"{amount:.0f}" if whole else f"{amount:.2f}"
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(node)):
        yield path, date.fromisoformat(node).strftime("%d-%m-%Y")
    else:
        yield path, str(node)


def appears(expected: str, text: str) -> bool:
    """A number must appear as a whole number, so 2400 doesn't count as found
    inside 12400; other values just need to appear."""
    expected = normalise(expected)
    if re.fullmatch(r"-?\d+(\.\d+)?", expected):
        pattern = rf"(?<![\d.]){re.escape(expected)}(?![\d.]\d|\d)"
        return re.search(pattern, text) is not None
    return expected in text


def test_pdf_has_a_text_layer(rules: TaxRules) -> None:
    form16, _ = generate(0, rules)
    text = extract_text(render_form16(form16))
    assert "FORM NO. 16" in text
    assert "PART B" in text
    assert len(text) > 1000


@pytest.mark.parametrize(("layout", "render"), RENDERERS, ids=[n for n, _ in RENDERERS])
@pytest.mark.parametrize("seed", SEEDS)
def test_every_truth_value_is_in_the_text(
    rules: TaxRules, seed: int, layout: str, render: Callable[[Form16], bytes]
) -> None:
    form16, _ = generate(seed, rules)
    if seed % 5 == 0:  # some cases carry an unrecognised line too
        form16 = _with_capital_gains(form16, seed)
    text = normalise(extract_text(render(form16)))
    truth = json.loads(form16.model_dump_json())

    missing = [
        (path, expected)
        for path, expected in truth_values(truth)
        if not appears(expected, text)
    ]
    assert missing == []


@pytest.mark.parametrize(("layout", "render"), RENDERERS, ids=[n for n, _ in RENDERERS])
def test_rendering_is_reproducible(
    rules: TaxRules, layout: str, render: Callable[[Form16], bytes]
) -> None:
    form16, _ = generate(7, rules)
    assert render(form16) == render(form16)


@pytest.mark.parametrize(("layout", "render"), RENDERERS, ids=[n for n, _ in RENDERERS])
def test_pdf_is_marked_synthetic(
    rules: TaxRules, layout: str, render: Callable[[Form16], bytes]
) -> None:
    form16, _ = generate(3, rules)
    text = extract_text(render(form16))
    assert "SYNTHETIC SPECIMEN" in text


@pytest.mark.parametrize(
    ("number_format", "expected"),
    [
        ("indian_grouping", "12,34,567"),
        ("rupee_symbol", "₹12,34,567"),
        ("plain_decimal", "1234567.00"),
        ("rs_prefix", "Rs. 12,34,567"),
    ],
)
def test_number_formats(number_format: NumberFormat, expected: str) -> None:
    assert format_amount(Decimal(1234567), number_format) == expected


def test_number_formats_keep_paise_and_sign() -> None:
    amount = Decimal("-200000.50")
    assert format_amount(amount) == "-2,00,000.50"
    assert format_amount(amount, "rupee_symbol") == "-₹2,00,000.50"
    assert format_amount(amount, "plain_decimal") == "-200000.50"
    assert format_amount(amount, "rs_prefix") == "Rs. -2,00,000.50"
    assert format_amount(None, "rupee_symbol") == ""


def test_rupee_sign_is_in_the_text_layer(rules: TaxRules) -> None:
    """Helvetica has no ₹, so this checks the bundled font is used: with a
    font that lacks the glyph, the sign is lost from the text."""
    form16, _ = generate(1, rules)
    text = extract_text(render_form16_statement(form16, "rupee_symbol"))
    assert f"₹{format_amount(form16.part_b.gross_salary.total)}" in text


# --- Each value is on the right line ------------------------------------------
# Presence alone isn't enough: 2(e) and 2(i) often hold the same figure, so a
# PDF that printed 0 on 2(e) would still "contain" the HRA amount. These tables
# say which field each printed line must show, in the order the PDF prints them.

AMOUNT_LINES = [
    ("1(a)", "gross_salary.section_17_1"),
    ("1(b)", "gross_salary.section_17_2"),
    ("1(c)", "gross_salary.section_17_3"),
    ("1(d)", "gross_salary.total"),
    ("1(e)", "gross_salary.other_employers"),
    ("2(a)", "section_10.travel_concession_10_5"),
    ("2(b)", "section_10.gratuity_10_10"),
    ("2(c)", "section_10.commuted_pension_10_10a"),
    ("2(d)", "section_10.leave_encashment_10_10aa"),
    ("2(e)", "section_10.house_rent_allowance_10_13a"),
    ("2(f)", "section_10.special_allowances_10_14"),
    ("2(h)", "section_10.other_total"),
    ("2(i)", "section_10.total"),
    ("3", "salary_from_current_employer"),
    ("4(a)", "section_16.standard_deduction_16_ia"),
    ("4(b)", "section_16.entertainment_allowance_16_ii"),
    ("4(c)", "section_16.tax_on_employment_16_iii"),
    ("5", "section_16.total"),
    ("6", "income_under_salaries"),
    ("7(a)", "other_income.house_property"),
    ("7(b)", "other_income.other_sources"),
    ("8", "other_income.total"),
    ("9", "gross_total_income"),
]
DEDUCTION_LINES = [
    ("10(a)", "chapter_via.section_80c"),
    ("10(b)", "chapter_via.section_80ccc"),
    ("10(c)", "chapter_via.section_80ccd_1"),
    ("10(d)", "chapter_via.total_80c_80ccc_80ccd_1"),
    ("10(e)", "chapter_via.section_80ccd_1b"),
    ("10(f)", "chapter_via.section_80ccd_2"),
    ("10(g)", "chapter_via.section_80d"),
    ("10(h)", "chapter_via.section_80e"),
    ("10(i)", "chapter_via.section_80cch_employee"),
    ("10(j)", "chapter_via.section_80cch_government"),
    ("10(k)", "chapter_via.section_80g"),
    ("10(l)", "chapter_via.section_80tta"),
]
TAX_LINES = [
    ("10(n)", "chapter_via.other_total"),
    ("11", "chapter_via.aggregate"),
    ("12", "tax.total_taxable_income"),
    ("13", "tax.tax_on_total_income"),
    ("14", "tax.rebate_87a"),
    ("15", "tax.surcharge"),
    ("16", "tax.health_and_education_cess"),
    ("17", "tax.tax_payable"),
    ("18", "tax.relief_section_89"),
    ("19", "tax.tds_form_12baa"),
    ("20", "tax.tcs_form_12baa"),
    ("21", "tax.net_tax_payable"),
]
BREAKUP_ROWS = [
    ("Basic salary", "basic"),
    ("Dearness allowance", "dearness_allowance"),
    ("House rent allowance received", "house_rent_allowance"),
    ("Other allowances", "other_allowances"),
]
NUMBER = re.compile(r"-?\d[\d,]*(\.\d+)?")


def _field(obj: Any, path: str) -> Any:
    for name in path.split("."):
        obj = getattr(obj, name)
    return obj


def row_amounts(text: str, row_ids: list[str]) -> dict[str, list[Decimal]]:
    """The amounts printed in each row, found by the row's line number or label.

    Each table cell is extracted on its own line: the row's id, then its label,
    then its amounts. Rows are looked for in order, and a row ends at the next
    row's id, so a line number such as "3" is never read as an amount.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rows: dict[str, list[Decimal]] = {}
    position = 0
    for index, row_id in enumerate(row_ids):
        position = lines.index(row_id, position) + 1  # ValueError if missing
        next_id = row_ids[index + 1] if index + 1 < len(row_ids) else None
        amounts: list[Decimal] = []
        for line in lines[position:]:
            if line == next_id:  # a line number like "3" is not an amount
                break
            if NUMBER.fullmatch(line):
                amounts.append(Decimal(line.replace(",", "")))
            elif amounts:
                break
        rows[row_id] = amounts
    return rows


@pytest.mark.parametrize("seed", SEEDS)
def test_each_amount_is_on_its_own_line(rules: TaxRules, seed: int) -> None:
    form16, _ = generate(seed, rules)
    text = extract_text(render_form16(form16))
    b = form16.part_b
    ids = [line for line, _ in AMOUNT_LINES + DEDUCTION_LINES + TAX_LINES]
    printed = row_amounts(text, ids)

    for line, path in AMOUNT_LINES + TAX_LINES:
        assert printed[line] == [_field(b, path)], (line, path)
    for line, path in DEDUCTION_LINES:
        entry = _field(b, path)
        columns = [entry.gross, entry.qualifying, entry.deductible]
        assert printed[line] == [c for c in columns if c is not None], (line, path)

    breakup = form16.salary_breakup
    assert breakup is not None
    in_annexure = text[text.index("ANNEXURE") :]
    labels = [label for label, _ in BREAKUP_ROWS]
    printed = row_amounts(in_annexure, labels)
    for label, field in BREAKUP_ROWS:
        assert printed[label] == [getattr(breakup, field)], label


# --- Statement layout: each value is on the right line ---------------------


def statement_amounts(text: str, row_ids: list[str]) -> dict[str, list[Decimal]]:
    """The amounts after the last colon on each "[line] label: amounts" row
    (labels such as "Less: Relief ..." have a colon of their own). A row ends
    where the next row's "[line]" starts; the last ends at the annexures."""
    # The diagonal watermark can land inside a line; its "16" is not an amount.
    flat = normalise(text.split("Salary breakup")[0].replace(WATERMARK, " "))
    rows: dict[str, list[Decimal]] = {}
    position = 0
    for index, row_id in enumerate(row_ids):
        start = flat.index(f"[{row_id}] ", position)  # ValueError if missing
        if index + 1 < len(row_ids):
            end = flat.index(f"[{row_ids[index + 1]}] ", start)
        else:
            end = len(flat)
        after_colon = flat[start:end].rsplit(": ", 1)[1]
        rows[row_id] = [
            Decimal(number) for number in re.findall(r"-?\d+(?:\.\d+)?", after_colon)
        ]
        position = end
    return rows


@pytest.mark.parametrize("number_format", FORMATS)
@pytest.mark.parametrize("seed", SEEDS)
def test_statement_amounts_are_on_their_own_lines(
    rules: TaxRules, seed: int, number_format: NumberFormat
) -> None:
    form16, _ = generate(seed, rules)
    text = extract_text(render_form16_statement(form16, number_format))
    b = form16.part_b
    ids = [line for line, _ in AMOUNT_LINES + DEDUCTION_LINES + TAX_LINES]
    printed = statement_amounts(text, ids)

    for line, path in AMOUNT_LINES + TAX_LINES:
        assert printed[line] == [_field(b, path)], (line, path)
    for line, path in DEDUCTION_LINES:
        entry = _field(b, path)
        columns = [entry.gross, entry.qualifying, entry.deductible]
        assert printed[line] == [c for c in columns if c is not None], (line, path)
