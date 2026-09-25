"""Catalog of Class 4 textbooks.

NCERT PDFs live at https://ncert.nic.in/textbook/pdf/<code><NN>.pdf (one PDF per
chapter). `text_mode` says how ingestion reads a page:
  "text"  - use the PDF text layer plus the page image (for diagrams)
  "image" - trust only the page image (Hindi/Kannada PDFs often use legacy
            font encodings whose text layer is garbage)
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Book:
    subject: str          # short key used everywhere (evs, english, ...)
    label: str            # shown to the child in citations
    title: str
    language: str
    text_mode: str
    ncert_code: str | None  # None = PDFs supplied by hand
    chapters: int | None    # None = unknown, discovered from files present


BOOKS: dict[str, Book] = {
    b.subject: b
    for b in [
        Book("evs", "EVS", "Our Wondrous World", "English", "text", "deev1", 10),
        Book("english", "English", "Santoor", "English", "text", "desa1", 12),
        Book("maths", "Maths", "Maths Mela", "English", "text", "demm1", 14),
        Book("hindi", "Hindi", "Veena", "Hindi", "image", "dhve1", 13),
        # Kannada (2nd language): book not decided yet. Put chapter PDFs or page
        # photos at data/raw/kannada/ch01.pdf, ch02.pdf ... and ingest as usual.
        Book("kannada", "Kannada", "Kannada (Karnataka Textbook Society)", "Kannada", "image", None, None),
    ]
}


def get_book(subject: str) -> Book:
    try:
        return BOOKS[subject]
    except KeyError:
        raise SystemExit(f"Unknown subject {subject!r}. Choose from: {', '.join(BOOKS)}")


def citation(subject: str, chapter: int | str, page: int | str | None) -> str:
    label = BOOKS[subject].label if subject in BOOKS else subject.title()
    if subject == "school" or chapter in (None, "", 0, "0"):
        return f"{label}, page {page}" if page else label
    return f"{label}, Chapter {chapter}, page {page}" if page else f"{label}, Chapter {chapter}"
