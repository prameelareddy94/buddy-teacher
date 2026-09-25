"""Download NCERT chapter PDFs."""
from pathlib import Path

import httpx

from buddy.books import Book
from buddy.config import get_settings

NCERT_PDF = "https://ncert.nic.in/textbook/pdf/{code}{chapter:02d}.pdf"


def chapter_pdf_path(subject: str, chapter: int) -> Path:
    return get_settings().raw_dir / subject / f"ch{chapter:02d}.pdf"


def download_chapter(book: Book, chapter: int, force: bool = False) -> Path:
    dest = chapter_pdf_path(book.subject, chapter)
    if dest.exists() and not force:
        return dest
    if not book.ncert_code:
        raise SystemExit(
            f"{book.label} has no download source. Put the chapter PDF at {dest} "
            "(photos can be combined into a PDF, or use the parent upload page)."
        )
    url = NCERT_PDF.format(code=book.ncert_code, chapter=chapter)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # NCERT's server rejects some default client user agents.
    headers = {"User-Agent": "Mozilla/5.0 (BuddyTeacher personal study helper)"}
    with httpx.stream("GET", url, headers=headers, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    if not tmp.read_bytes()[:5] == b"%PDF-":
        tmp.unlink()
        raise SystemExit(f"{url} did not return a PDF")
    tmp.rename(dest)
    return dest


def available_chapters(book: Book) -> list[int]:
    if book.chapters:
        return list(range(1, book.chapters + 1))
    folder = get_settings().raw_dir / book.subject
    return sorted(int(p.stem[2:]) for p in folder.glob("ch[0-9][0-9].pdf"))
