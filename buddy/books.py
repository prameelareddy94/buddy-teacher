"""Catalog of textbooks: Class 4 (current) plus Classes 1-3 for basics.

NCERT PDFs live at https://ncert.nic.in/textbook/pdf/<code><NN>.pdf (one PDF per
chapter; <code>dd.zip is the whole book). `text_mode` says how ingestion reads a page:
  "text"  - use the PDF text layer plus the page image (for diagrams)
  "image" - trust only the page image (Hindi/Kannada PDFs often use legacy
            font encodings whose text layer is garbage)
Book keys: the Class 4 book uses the bare subject ("evs"); older classes add the
class ("evs-c3", "english-c1"). Classes 1-2 have no EVS book in the NEP syllabus.

School books (e.g. Orchids' own books) are added at runtime from photos or scans and
kept in data/books.json. They are "school" books: search ranks them above NCERT.
"""
import json
import re
from dataclasses import asdict, dataclass

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
    custom_key: str | None = None   # school books only
    name: str | None = None         # school books: label shown in citations

    @property
    def key(self) -> str:
        if self.custom_key:
            return self.custom_key
        return self.subject if self.grade == CURRENT_GRADE else f"{self.subject}-c{self.grade}"

    @property
    def is_school(self) -> bool:
        return self.custom_key is not None

    @property
    def label(self) -> str:
        """Shown to the child in citations."""
        if self.name:
            return self.name
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
BUILTIN_KEYS = frozenset(BOOKS)


def _custom_file():
    from buddy.config import get_settings
    return get_settings().data_dir / "books.json"


def load_custom_books() -> None:
    """(Re)load school books from data/books.json into BOOKS."""
    for k in [k for k in BOOKS if k not in BUILTIN_KEYS]:
        del BOOKS[k]
    f = _custom_file()
    if f.exists():
        for d in json.loads(f.read_text()):
            b = Book(**d)
            BOOKS[b.key] = b


def add_school_book(key: str, subject: str, name: str, grade: int = CURRENT_GRADE,
                    title: str = "", language: str | None = None) -> Book:
    """Register one of her school's own books (read from photos/scans)."""
    load_custom_books()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", key):
        raise ValueError("Book key: lowercase letters, digits and dashes, e.g. orchids-evs")
    if key in BUILTIN_KEYS:
        raise ValueError(f"{key} is an NCERT book key; pick another, e.g. orchids-{subject}")
    if subject not in SUBJECTS:
        raise ValueError(f"subject must be one of {', '.join(SUBJECTS)}")
    language = language or {"hindi": "Hindi", "kannada": "Kannada"}.get(subject, "English")
    book = Book(subject, grade, title or name, language, "image", None, None,
                custom_key=key, name=name)
    BOOKS[key] = book
    books = [asdict(b) for b in BOOKS.values() if b.is_school]
    f = _custom_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(books, ensure_ascii=False, indent=2))
    return book


def school_book_keys() -> set[str]:
    return {k for k, b in BOOKS.items() if b.is_school}


def get_book(key: str) -> Book:
    if key not in BOOKS:
        load_custom_books()
    try:
        return BOOKS[key]
    except KeyError:
        raise SystemExit(f"Unknown book {key!r}. Choose from: {', '.join(BOOKS)}")


def citation(book_key: str, chapter: int | str | None, page: int | str | None) -> str:
    label = BOOKS[book_key].label if book_key in BOOKS else SUBJECTS.get(book_key, book_key.title())
    if chapter in (None, "", 0, "0"):
        return f"{label}, page {page}" if page else label
    return f"{label}, Chapter {chapter}, page {page}" if page else f"{label}, Chapter {chapter}"
