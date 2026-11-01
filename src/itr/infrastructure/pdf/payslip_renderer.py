"""A monthly payslip: a salary document that is not a Form 16.

It names the same employer and employee and shows salary figures, so it looks
plausible to a careless extractor. But it isn't a TDS certificate, and a
correct system must refuse it rather than read it as one.
"""

from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
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
    BODY,
    HEADING,
    TITLE,
    AmountLine,
    amount_table,
    draw_watermark,
    format_amount,
    pairs_table,
)


def _monthly(annual: Decimal) -> Decimal:
    return (annual / 12).quantize(Decimal(1), rounding=ROUND_HALF_UP)


def render_payslip(form16: Form16) -> bytes:
    """The March payslip of the employee on this Form 16, as bytes."""
    a, b = form16.part_a, form16.part_b
    breakup = form16.salary_breakup
    if breakup is None:
        earnings: list[AmountLine] = [
            ("", "Gross salary", _monthly(b.gross_salary.total))
        ]
    else:
        earnings = [
            ("", "Basic salary", _monthly(breakup.basic)),
            ("", "Dearness allowance", _monthly(breakup.dearness_allowance)),
            ("", "House rent allowance", _monthly(breakup.house_rent_allowance)),
            ("", "Other allowances", _monthly(breakup.other_allowances)),
        ]
    gross = sum((amount for _, _, amount in earnings), Decimal(0))
    march_tds = a.quarters[-1].tax_deducted / 3 if a.quarters else Decimal(0)
    deductions: list[AmountLine] = [
        ("", "Professional tax", _monthly(b.section_16.tax_on_employment_16_iii)),
        ("", "Income tax (TDS)", march_tds.quantize(Decimal(1))),
    ]
    total_deductions = sum((amount for _, _, amount in deductions), Decimal(0))

    buffer = BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Payslip for March 2026 (synthetic)",
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
        [PageTemplate(id="page", frames=[frame], onPage=draw_watermark)]
    )
    story: list[Flowable] = [
        Paragraph(a.employer_name, TITLE),
        Paragraph(a.employer_address, BODY),
        Paragraph("PAYSLIP FOR THE MONTH OF MARCH 2026", HEADING),
        pairs_table(
            [
                ("Employee name", a.employee_name),
                ("PAN", a.employee_pan),
                ("Pay period", "01-03-2026 to 31-03-2026"),
                ("Paid days", "31"),
            ]
        ),
        Spacer(1, 3 * mm),
        Paragraph("Earnings", HEADING),
        amount_table([*earnings, ("", "Gross earnings", gross)]),
        Paragraph("Deductions", HEADING),
        amount_table([*deductions, ("", "Total deductions", total_deductions)]),
        Spacer(1, 3 * mm),
        Paragraph(f"Net pay: Rs. {format_amount(gross - total_deductions)}", HEADING),
        Paragraph(
            "This is a computer-generated payslip and does not need a signature.",
            BODY,
        ),
    ]
    document.build(story)
    return buffer.getvalue()
