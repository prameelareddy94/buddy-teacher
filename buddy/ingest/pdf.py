"""Turn a PDF (or photos) into per-page text + page images."""
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# Longest image side in pixels. ~1200px keeps a page around 1.5k input tokens
# while small print stays readable.
MAX_SIDE = 1200


@dataclass
class Page:
    index: int          # 1-based position in the PDF
    text: str           # PDF text layer ("" when unusable)
    jpeg: bytes         # rendered page image


def _render(page: pymupdf.Page) -> bytes:
    zoom = MAX_SIDE / max(page.rect.width, page.rect.height)
    # JPEG keeps a 20-page chapter request a few MB; token cost depends on pixels only.
    return page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("jpg", jpg_quality=85)


def load_pages(path: Path, use_text: bool = True) -> list[Page]:
    pages = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text").strip() if use_text else ""
            pages.append(Page(i, text, _render(page)))
    return pages


def image_to_page(data: bytes, index: int) -> Page:
    """Photo upload -> Page, resized the same way as PDF pages."""
    with pymupdf.open(stream=data) as doc:
        pdf_bytes = doc.convert_to_pdf()
    with pymupdf.open("pdf", pdf_bytes) as pdf:
        return Page(index, "", _render(pdf[0]))


def load_upload(path: Path) -> list[Page]:
    if path.suffix.lower() == ".pdf":
        return load_pages(path, use_text=True)
    return [image_to_page(path.read_bytes(), 1)]


def files_to_pdf(files: list[Path], dest: Path) -> int:
    """Combine photos (JPEG/PNG/WebP) and PDFs, in the given order, into one PDF.
    Used for chapters of her school books photographed on a phone. Returns page count."""
    out = pymupdf.open()
    for f in files:
        if f.suffix.lower() == ".pdf":
            with pymupdf.open(f) as src:
                out.insert_pdf(src)
        else:
            with pymupdf.open(f) as img:
                pdf_bytes = img.convert_to_pdf()
            with pymupdf.open("pdf", pdf_bytes) as page_pdf:
                out.insert_pdf(page_pdf)
    if out.page_count == 0:
        raise ValueError("no pages found in the uploaded files")
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = out.page_count
    out.save(dest, garbage=3, deflate=True)
    out.close()
    return n
