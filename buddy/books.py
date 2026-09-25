"""Catalog of textbooks: Class 4 (current) plus Classes 1-3 for basics.

NCERT PDFs live at https://ncert.nic.in/textbook/pdf/<code><NN>.pdf (one PDF per
chapter; <code>dd.zip is the whole book). `text_mode` says how ingestion reads a page:
  "text"  - use the PDF text layer plus the page image (for diagrams)
  "image" - trust only the page image (Hindi/Kannada PDFs often use legacy
            font encodings whose text layer is garbage)
Book keys: the Class 4 book uses the bare subject ("evs"); older classes add the
class ("evs-c3", "english-c1"). Classes 1-2 have no EVS book in the NEP syllabus.
"""
from dataclasses import dataclass

CURRENT_GRADE = 4

SUBJECTS = {"evs": "EVS", "english": "English", "maths": "Maths", "hindi": "Hindi",
            "kannada": "Kannada"}


@dataclass(frozen=True)
class Book:
    subject: str            # evs, english, maths, hindi, kannada
    grade: int              # class 1-4
    title: str
    language: str
    text_mode: str
    ncert_code: str | None  # None = PDFs supplied by hand
    chapters: int | None    # None = unknown; found from files / by probing NCERT

    @property
    def key(self) -> str:
        return self.subject if self.grade == CURRENT_GRADE else f"{self.subject}-c{self.grade}"

    @property
    def label(self) -> str:
        """Shown to the child in citations."""
        name = SUBJECTS[self.subject]
        return name if self.grade == CURRENT_GRADE else f"{name} (Class {self.grade})"


_ALL = [
    # Class 4 (current)
    Book("evs", 4, "Our Wondrous World", "English", "text", "deev1", 10),
    Book("english", 4, "Santoor", "English", "text", "desa1", 12),
    Book("maths", 4, "Maths Mela", "English", "text", "demm1", 14),
    Book("hindi", 4, "Veena", "Hindi", "image", "dhve1", 13),
    # Kannada (2nd language): book not decided yet. Put chapter PDFs or page
    # photos at data/raw/kannada/ch01.pdf, ch02.pdf ... and ingest as usual.
    Book("kannada", 4, "Kannada (Karnataka Textbook Society)", "Kannada", "image", None, None),
    # Class 3
    Book("evs", 3, "Our Wondrous World", "English", "text", "ceev1", None),
    Book("english", 3, "Santoor", "English", "text", "cesa1", None),
    Book("maths", 3, "Maths Mela", "English", "text", "cemm1", None),
    Book("hindi", 3, "Veena", "Hindi", "image", "chve1", None),
    # Class 2
    Book("english", 2, "Mridang", "English", "text", "bemr1", None),
    Book("maths", 2, "Joyful Mathematics", "English", "text", "bejm1", None),
    Book("hindi", 2, "Sarangi", "Hindi", "image", "bhsr1", None),
    # Class 1
    Book("english", 1, "Mridang", "English", "text", "aemr1", None),
    Book("maths", 1, "Joyful Mathematics", "English", "text", "aejm1", 13),
    Book("hindi", 1, "Sarangi", "Hindi", "image", "ahsr1", 19),
]

BOOKS: dict[str, Book] = {b.key: b for b in _ALL}


def get_book(key: str) -> Book:
    try:
        return BOOKS[key]
    except KeyError:
        raise SystemExit(f"Unknown book {key!r}. Choose from: {', '.join(BOOKS)}")


def citation(book_key: str, chapter: int | str | None, page: int | str | None) -> str:
    label = BOOKS[book_key].label if book_key in BOOKS else SUBJECTS.get(book_key, book_key.title())
    if chapter in (None, "", 0, "0"):
        return f"{label}, page {page}" if page else label
    return f"{label}, Chapter {chapter}, page {page}" if page else f"{label}, Chapter {chapter}"
