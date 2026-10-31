from io import BytesIO
from pathlib import Path

from pypdf import PdfReader


class NoTextLayerError(Exception):
    """The PDF has no extractable text, e.g. a scanned image. Reading it would
    need OCR, which the engine doesn't do."""


def pdf_to_text(pdf: Path | bytes) -> str:
    source = BytesIO(pdf) if isinstance(pdf, bytes) else pdf
    reader = PdfReader(source)
    pages = [page.extract_text(extraction_mode="layout") for page in reader.pages]

    if not any(text.strip() for text in pages):
        raise NoTextLayerError("the PDF has no text layer; it may be a scan")
    return "\n\n".join(
        f"--- page {number} ---\n"
        + "\n".join(line.rstrip() for line in text.splitlines())
        for number, text in enumerate(pages, start=1)
    )
