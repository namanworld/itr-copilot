"""A second Form 16 layout: a plain statement instead of ruled tables.

Payroll vendors print Form 16 in different ways. This one has no grid, puts
each line on its own row as "[line] label: amount", writes the three Chapter
VI-A columns on one row, and prints amounts in any NumberFormat. It holds the
same values as the standard layout, so the same truth JSON applies.
"""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
)

from itr.domain.form16 import Form16
from itr.infrastructure.pdf.form16_renderer import (
    WATERMARK,
    AmountLine,
    DeductionRow,
    NumberFormat,
    breakup_lines,
    deduction_lines,
    format_amount,
    format_date,
    salary_lines,
    tax_lines,
)

TITLE = ParagraphStyle("StatementTitle", fontName="DejaVuSans-Bold", fontSize=12)
HEADING = ParagraphStyle(
    "StatementHeading",
    fontName="DejaVuSans-Bold",
    fontSize=9,
    spaceBefore=6,
    spaceAfter=2,
)
LINE = ParagraphStyle("StatementLine", fontName="DejaVuSans", fontSize=8, leading=11)


def _lines(rows: list[AmountLine], number_format: NumberFormat) -> list[Flowable]:
    return [
        Paragraph(f"[{line}] {label}: {format_amount(value, number_format)}", LINE)
        for line, label, value in rows
    ]


def _deductions(
    rows: list[DeductionRow], number_format: NumberFormat
) -> list[Flowable]:
    flowables: list[Flowable] = []
    for line, label, entry in rows:
        columns = [
            ("gross", entry.gross),
            ("qualifying", entry.qualifying),
            ("deductible", entry.deductible),
        ]
        printed = "; ".join(
            f"{name} {format_amount(value, number_format)}"
            for name, value in columns
            if value is not None
        )
        flowables.append(Paragraph(f"[{line}] {label}: {printed}", LINE))
    return flowables


def _details(form16: Form16, number_format: NumberFormat) -> list[Flowable]:
    a = form16.part_a
    pairs = [
        ("Certificate No.", a.certificate_number),
        ("Employer", f"{a.employer_name}, {a.employer_address}"),
        ("Employee", f"{a.employee_name}, {a.employee_address}"),
        ("PAN of the Deductor", a.employer_pan),
        ("TAN of the Deductor", a.employer_tan),
        ("PAN of the Employee", a.employee_pan),
        ("Assessment Year", a.assessment_year),
        (
            "Period with the Employer",
            f"{format_date(a.period_from)} to {format_date(a.period_to)}",
        ),
    ]
    flowables: list[Flowable] = [
        Paragraph(f"{label}: {value}", LINE) for label, value in pairs
    ]
    flowables.append(Paragraph("Tax deducted and deposited, by quarter", HEADING))
    for q in a.quarters:
        flowables.append(
            Paragraph(
                f"{q.quarter} (receipt {q.receipt_number}): paid/credited "
                f"{format_amount(q.amount_paid_or_credited, number_format)}; "
                f"deducted {format_amount(q.tax_deducted, number_format)}; "
                f"deposited {format_amount(q.tax_deposited, number_format)}",
                LINE,
            )
        )
    flowables.append(
        Paragraph(
            "Total: paid/credited "
            f"{format_amount(a.total_amount_paid_or_credited, number_format)}; "
            f"deducted {format_amount(a.total_tax_deducted, number_format)}; "
            f"deposited {format_amount(a.total_tax_deposited, number_format)}",
            LINE,
        )
    )
    return flowables


def _salary_breakup(form16: Form16, number_format: NumberFormat) -> list[Flowable]:
    breakup = form16.salary_breakup
    if breakup is None:
        return []
    counts = breakup.dearness_allowance_counts_for_retirement
    counts_text = "Not stated" if counts is None else ("YES" if counts else "NO")
    return [
        Paragraph("Salary breakup (attached by the employer)", HEADING),
        *[
            Paragraph(f"{label}: {format_amount(value, number_format)}", LINE)
            for _, label, value in breakup_lines(form16)
        ],
        Paragraph(
            "Dearness allowance forms part of salary for retirement benefits: "
            f"{counts_text}",
            LINE,
        ),
    ]


def _declared_income(form16: Form16, number_format: NumberFormat) -> list[Flowable]:
    if not form16.unrecognised_lines:
        return []
    return [
        Paragraph("Other income declared by the employee", HEADING),
        *[
            Paragraph(
                f"{extra.where} - {extra.description}: "
                f"{format_amount(extra.amount, number_format)}",
                LINE,
            )
            for extra in form16.unrecognised_lines
        ],
    ]


def _draw_watermark(canvas: Canvas, _document: BaseDocTemplate) -> None:
    canvas.saveState()
    canvas.setFont("DejaVuSans-Bold", 26)
    canvas.setFillColor(colors.Color(0.2, 0.3, 0.8, alpha=0.15))
    canvas.translate(A4[0] / 2, A4[1] / 2)
    canvas.rotate(35)
    canvas.drawCentredString(0, 0, WATERMARK)
    canvas.restoreState()


def render_form16_statement(form16: Form16, number_format: NumberFormat) -> bytes:
    """The statement-layout PDF for one Form 16, as bytes."""
    b = form16.part_b
    buffer = BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"TDS certificate {form16.part_a.certificate_number} (synthetic)",
        author="itr synthetic dataset",
        invariant=1,
    )
    frame = Frame(
        document.leftMargin,
        document.bottomMargin,
        document.width,
        document.height,
        id="body",
    )
    document.addPageTemplates(
        [PageTemplate(id="page", frames=[frame], onPage=_draw_watermark)]
    )
    regime = "YES" if b.opted_out_of_new_regime else "NO"
    story: list[Flowable] = [
        Paragraph("Certificate of Tax Deducted at Source on Salary", TITLE),
        Paragraph("Form No. 16, rule 31(1)(a), section 203", LINE),
        Spacer(1, 3 * mm),
        Paragraph("Employer and employee", HEADING),
        *_details(form16, number_format),
        Paragraph("Salary and other income", HEADING),
        Paragraph(f"Opted out of taxation u/s 115BAC(1A): {regime}", LINE),
        *_lines(salary_lines(form16), number_format),
        Paragraph("Deductions under Chapter VI-A", HEADING),
        *_deductions(deduction_lines(form16), number_format),
        Paragraph("Tax computation", HEADING),
        *_lines(tax_lines(form16), number_format),
        *_salary_breakup(form16, number_format),
        *_declared_income(form16, number_format),
    ]
    document.build(story)
    return buffer.getvalue()
