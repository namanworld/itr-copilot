"""PDF to text: every page's text, and a clear error for a scan."""

from io import BytesIO
from pathlib import Path

import pytest
from reportlab.pdfgen.canvas import Canvas

from itr.infrastructure.pdf.pdf_to_text import NoTextLayerError, pdf_to_text


def make_pdf(pages: list[str]) -> bytes:
    buffer = BytesIO()
    canvas = Canvas(buffer, invariant=1)
    for text in pages:
        if text:
            canvas.drawString(72, 720, text)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def test_reads_every_page_in_order() -> None:
    text = pdf_to_text(make_pdf(["FORM NO. 16", "PART B"]))
    assert text.index("--- page 1 ---") < text.index("FORM NO. 16")
    assert text.index("FORM NO. 16") < text.index("--- page 2 ---")
    assert text.index("--- page 2 ---") < text.index("PART B")


def test_reads_from_a_path(tmp_path: Path) -> None:
    path = tmp_path / "form.pdf"
    path.write_bytes(make_pdf(["FORM NO. 16"]))
    assert "FORM NO. 16" in pdf_to_text(path)


def test_a_pdf_without_text_is_rejected() -> None:
    with pytest.raises(NoTextLayerError):
        pdf_to_text(make_pdf(["", ""]))
