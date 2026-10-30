"""Render a Form16 as a PDF with a real text layer.

Day 4 sends the PDF's extracted text to the model, so everything is drawn as
text in a standard font: no images of text, no outlines. Every value in the
Form16 appears on the page, so a perfect extraction can recover the truth JSON.

The output is reproducible: the same Form16 always gives the same bytes.
"""

from datetime import date
from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from itr.domain.form16 import DeductionLine, Form16

WATERMARK = "SYNTHETIC SPECIMEN - NOT A REAL FORM-16"

_styles = getSampleStyleSheet()
TITLE = ParagraphStyle("Title16", parent=_styles["Title"], fontSize=14, spaceAfter=2)
HEADING = ParagraphStyle(
    "Heading16", parent=_styles["Heading2"], fontSize=11, spaceBefore=8
)
BODY = ParagraphStyle("Body16", parent=_styles["BodyText"], fontSize=8, leading=10)
# Amounts line up on the right, as on real statements.
AMOUNT = ParagraphStyle("Amount16", parent=BODY, alignment=TA_RIGHT)


# --- Formatting --------------------------------------------------------------


def format_amount(amount: Decimal | None) -> str:
    """Indian digit grouping, paise only when there are any: 15,00,000 or
    6,300.12. Blank for an amount the form leaves empty."""
    if amount is None:
        return ""
    sign = "-" if amount < 0 else ""
    text = f"{abs(amount).quantize(Decimal('0.01')):f}"
    whole, paise = text.split(".")
    head, last_three = whole[:-3], whole[-3:]
    groups: list[str] = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    grouped = ",".join([*groups, last_three])
    return f"{sign}{grouped}" if paise == "00" else f"{sign}{grouped}.{paise}"


def format_date(value: date) -> str:
    """dd-mm-yyyy, as Form 16 prints dates."""
    return value.strftime("%d-%m-%Y")


# --- Building blocks ---------------------------------------------------------


def _table(
    rows: list[list[str]],
    widths: list[float],
    header: bool,
    amount_columns: tuple[int, ...] = (),
) -> Table:
    cells = [
        [
            Paragraph(cell, AMOUNT if column in amount_columns else BODY)
            for column, cell in enumerate(row)
        ]
        for row in rows
    ]
    table = Table(cells, colWidths=widths, repeatRows=1 if header else 0)
    style = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
    )
    if header:
        style.add("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8ecf2"))
    table.setStyle(style)
    return table


def _pairs(rows: list[tuple[str, str]]) -> Table:
    """Label and value side by side."""
    return _table([[label, value] for label, value in rows], [70 * mm, 110 * mm], False)


def _amount_rows(rows: list[tuple[str, str, Decimal]]) -> Table:
    """Part B lines: line number, description, amount in rupees."""
    body = [["Line", "Particulars", "Amount (Rs.)"]]
    body += [[line, label, format_amount(value)] for line, label, value in rows]
    return _table(body, [16 * mm, 124 * mm, 40 * mm], True, amount_columns=(2,))


def _deduction_rows(rows: list[tuple[str, str, DeductionLine]]) -> Table:
    """Chapter VI-A: gross, qualifying and deductible amounts."""
    body = [["Line", "Deduction", "Gross", "Qualifying", "Deductible"]]
    body += [
        [
            line,
            label,
            format_amount(entry.gross),
            format_amount(entry.qualifying),
            format_amount(entry.deductible),
        ]
        for line, label, entry in rows
    ]
    return _table(
        body,
        [14 * mm, 76 * mm, 30 * mm, 30 * mm, 30 * mm],
        True,
        amount_columns=(2, 3, 4),
    )


# --- The document ------------------------------------------------------------


def _part_a(form16: Form16) -> list[Flowable]:
    a = form16.part_a
    quarters = [
        [
            "Quarter",
            "Receipt number",
            "Amount paid/credited",
            "Tax deducted",
            "Tax deposited",
        ]
    ]
    quarters += [
        [
            q.quarter,
            q.receipt_number,
            format_amount(q.amount_paid_or_credited),
            format_amount(q.tax_deducted),
            format_amount(q.tax_deposited),
        ]
        for q in a.quarters
    ]
    quarters.append(
        [
            "Total",
            "",
            format_amount(a.total_amount_paid_or_credited),
            format_amount(a.total_tax_deducted),
            format_amount(a.total_tax_deposited),
        ]
    )
    return [
        Paragraph("PART A", HEADING),
        Paragraph(
            "Certificate under section 203 of the Income-tax Act, 1961 for tax "
            "deducted at source on salary paid to an employee under section 192",
            BODY,
        ),
        Spacer(1, 3 * mm),
        _pairs(
            [
                ("Certificate No.", a.certificate_number),
                (
                    "Name and address of the Employer",
                    f"{a.employer_name}, {a.employer_address}",
                ),
                (
                    "Name and address of the Employee",
                    f"{a.employee_name}, {a.employee_address}",
                ),
                ("PAN of the Deductor", a.employer_pan),
                ("TAN of the Deductor", a.employer_tan),
                ("PAN of the Employee", a.employee_pan),
                ("Assessment Year", a.assessment_year),
                ("Period with the Employer: From", format_date(a.period_from)),
                ("Period with the Employer: To", format_date(a.period_to)),
            ]
        ),
        Spacer(1, 3 * mm),
        Paragraph(
            "Summary of amount paid/credited and tax deducted at source thereon",
            BODY,
        ),
        _table(
            quarters,
            [20 * mm, 40 * mm, 40 * mm, 40 * mm, 40 * mm],
            True,
            amount_columns=(2, 3, 4),
        ),
    ]


def _part_b(form16: Form16) -> list[Flowable]:
    b = form16.part_b
    g, s10, s16 = b.gross_salary, b.section_10, b.section_16
    other, via, t = b.other_income, b.chapter_via, b.tax

    salary_rows: list[tuple[str, str, Decimal]] = [
        ("1(a)", "Salary as per provisions contained in section 17(1)", g.section_17_1),
        ("1(b)", "Value of perquisites under section 17(2)", g.section_17_2),
        ("1(c)", "Profits in lieu of salary under section 17(3)", g.section_17_3),
        ("1(d)", "Total", g.total),
        (
            "1(e)",
            "Reported total amount of salary received from other employer(s)",
            g.other_employers,
        ),
        (
            "2(a)",
            "Travel concession or assistance under section 10(5)",
            s10.travel_concession_10_5,
        ),
        (
            "2(b)",
            "Death-cum-retirement gratuity under section 10(10)",
            s10.gratuity_10_10,
        ),
        (
            "2(c)",
            "Commuted value of pension under section 10(10A)",
            s10.commuted_pension_10_10a,
        ),
        (
            "2(d)",
            "Cash equivalent of leave salary encashment under section 10(10AA)",
            s10.leave_encashment_10_10aa,
        ),
        (
            "2(e)",
            "House rent allowance under section 10(13A)",
            s10.house_rent_allowance_10_13a,
        ),
        (
            "2(f)",
            "Other special allowances under section 10(14)",
            s10.special_allowances_10_14,
        ),
    ]
    salary_rows += [
        ("2(g)", f"Other exemption under section 10, clause {c.clause}", c.amount)
        for c in s10.other
    ]
    salary_rows += [
        (
            "2(h)",
            "Total amount of any other exemption under section 10",
            s10.other_total,
        ),
        ("2(i)", "Total amount of exemption claimed under section 10", s10.total),
        (
            "3",
            "Total amount of salary received from current employer",
            b.salary_from_current_employer,
        ),
        (
            "4(a)",
            "Standard deduction under section 16(ia)",
            s16.standard_deduction_16_ia,
        ),
        (
            "4(b)",
            "Entertainment allowance under section 16(ii)",
            s16.entertainment_allowance_16_ii,
        ),
        (
            "4(c)",
            "Tax on employment under section 16(iii)",
            s16.tax_on_employment_16_iii,
        ),
        ("5", "Total amount of deductions under section 16", s16.total),
        ("6", 'Income chargeable under the head "Salaries"', b.income_under_salaries),
        (
            "7(a)",
            "Income (or admissible loss) from house property reported by "
            "employee offered for TDS",
            other.house_property,
        ),
        (
            "7(b)",
            "Income under the head Other Sources offered for TDS",
            other.other_sources,
        ),
        ("8", "Total amount of other income reported by the employee", other.total),
        ("9", "Gross total income", b.gross_total_income),
    ]

    deduction_rows: list[tuple[str, str, DeductionLine]] = [
        ("10(a)", "Section 80C", via.section_80c),
        ("10(b)", "Section 80CCC", via.section_80ccc),
        ("10(c)", "Section 80CCD(1)", via.section_80ccd_1),
        (
            "10(d)",
            "Total under sections 80C, 80CCC and 80CCD(1)",
            via.total_80c_80ccc_80ccd_1,
        ),
        ("10(e)", "Section 80CCD(1B)", via.section_80ccd_1b),
        ("10(f)", "Section 80CCD(2), contribution by employer", via.section_80ccd_2),
        ("10(g)", "Section 80D", via.section_80d),
        ("10(h)", "Section 80E", via.section_80e),
        (
            "10(i)",
            "Section 80CCH, contribution by employee",
            via.section_80cch_employee,
        ),
        (
            "10(j)",
            "Section 80CCH, contribution by Central Government",
            via.section_80cch_government,
        ),
        ("10(k)", "Section 80G", via.section_80g),
        ("10(l)", "Section 80TTA", via.section_80tta),
    ]
    deduction_rows += [
        ("10(m)", f"Section {c.clause}", DeductionLine(deductible=c.amount))
        for c in via.other
    ]

    tax_rows: list[tuple[str, str, Decimal]] = [
        (
            "10(n)",
            "Total of amount deductible under any other provision(s) of Chapter VI-A",
            via.other_total,
        ),
        ("11", "Aggregate of deductible amount under Chapter VI-A", via.aggregate),
        ("12", "Total taxable income", t.total_taxable_income),
        ("13", "Tax on total income", t.tax_on_total_income),
        ("14", "Rebate under section 87A, if applicable", t.rebate_87a),
        ("15", "Surcharge, wherever applicable", t.surcharge),
        ("16", "Health and education cess", t.health_and_education_cess),
        ("17", "Tax payable", t.tax_payable),
        ("18", "Less: Relief under section 89", t.relief_section_89),
        ("19", "Less: Tax deducted at source as per Form No. 12BAA", t.tds_form_12baa),
        ("20", "Less: Tax collected at source as per Form No. 12BAA", t.tcs_form_12baa),
        ("21", "Net tax payable", t.net_tax_payable),
    ]

    return [
        Paragraph("PART B (Annexure-I)", HEADING),
        Paragraph("Details of salary paid and any other income and tax deducted", BODY),
        Spacer(1, 3 * mm),
        _pairs(
            [
                (
                    "A. Whether opting out of taxation u/s 115BAC(1A)?",
                    "YES" if b.opted_out_of_new_regime else "NO",
                )
            ]
        ),
        Spacer(1, 2 * mm),
        _amount_rows(salary_rows),
        Spacer(1, 2 * mm),
        Paragraph("10. Deductions under Chapter VI-A", BODY),
        _deduction_rows(deduction_rows),
        Spacer(1, 2 * mm),
        _amount_rows(tax_rows),
    ]


def _annexure(form16: Form16) -> list[Flowable]:
    breakup = form16.salary_breakup
    if breakup is None:
        return []
    counts = breakup.dearness_allowance_counts_for_retirement
    counts_text = "Not stated" if counts is None else ("YES" if counts else "NO")
    return [
        PageBreak(),
        Paragraph("ANNEXURE: SALARY BREAKUP (attached by the employer)", HEADING),
        _amount_rows(
            [
                ("", "Basic salary", breakup.basic),
                ("", "Dearness allowance", breakup.dearness_allowance),
                ("", "House rent allowance received", breakup.house_rent_allowance),
                ("", "Other allowances", breakup.other_allowances),
            ]
        ),
        Spacer(1, 2 * mm),
        _pairs(
            [
                (
                    "Dearness allowance forms part of salary for retirement benefits",
                    counts_text,
                )
            ]
        ),
    ]


def _draw_watermark(canvas: Canvas, _document: BaseDocTemplate) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica-Bold", 30)
    canvas.setFillColor(colors.Color(0.85, 0.2, 0.2, alpha=0.18))
    canvas.translate(A4[0] / 2, A4[1] / 2)
    canvas.rotate(45)
    canvas.drawCentredString(0, 0, WATERMARK)
    canvas.restoreState()


def render_form16(form16: Form16) -> bytes:
    """The PDF for one Form 16, as bytes."""
    buffer = BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Form 16 {form16.part_a.certificate_number} (synthetic)",
        author="itr synthetic dataset",
        invariant=1,  # no timestamps or random IDs, so output is reproducible
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
    story: list[Flowable] = [
        Paragraph("FORM NO. 16", TITLE),
        Paragraph("[See rule 31(1)(a)]", BODY),
        Spacer(1, 2 * mm),
        *_part_a(form16),
        PageBreak(),
        *_part_b(form16),
        *_annexure(form16),
    ]
    document.build(story)
    return buffer.getvalue()
