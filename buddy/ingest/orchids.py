"""Import Orchids e-books from the school portal's e-book listing (JSON).

Save the listing the portal shows for a subject (the JSON with "results": [...]) to a
file, then:

  python -m buddy.ingest import-orchids english.json            # plan only
  python -m buddy.ingest import-orchids english.json --go       # download + split
  python -m buddy.ingest submit-all                             # read them (batch)

The same book is often listed several times (re-uploads per school zone/volume), so
each title is kept once: the newest upload. Every title becomes its own school book
(e.g. orchids-eng-gv-t1, "English Grammar (Term 1)") so citations name the right book.
Only for books the child is enrolled for; keep the result private.
"""
import json
import re
from pathlib import Path

import httpx
import pymupdf

from buddy.books import BOOKS, add_school_book, load_custom_books
from buddy.config import get_settings
from buddy.ingest.ebook import detect_chapters, pdf_thumbs, split_ranges, write_pdf_chapters

SUBJECT_NAMES = {
    "english": "english", "eng": "english",
    "hindi": "hindi", "hin": "hindi",
    "kannada": "kannada", "kan": "kannada",
    "mathematics": "maths", "maths": "maths", "math": "maths", "mat": "maths",
    "evs": "evs", "environmental studies": "evs", "environmental science": "evs",
    # Natural Science, Social Studies, … are their own subjects at her school.
}
SUBJECT_LABEL = {"english": "English", "hindi": "Hindi", "kannada": "Kannada",
                 "maths": "Maths", "evs": "EVS"}
# Codes seen in Orchids book names.
PART_NAMES = {"CS": "Coursebook", "RC": "Reading", "GV": "Grammar", "WS": "Writing",
              "LIT": "Literature", "LS": "Listening & Speaking"}
# Codes that are subjects, not parts.
SUBJECT_CODES = {"ENG", "HIN", "KAN", "MAT", "MATHS", "EVS", "SCI", "SST"}


def subject_of(entry: dict) -> tuple[str, str] | None:
    """(subject key, the school's name for it). Known subjects map onto Buddy's
    (Science -> evs); others (e.g. Horticulture) become new subjects."""
    raw = str(entry.get("subject_name", "")).strip()
    if not raw:
        return None
    if raw.isupper() and len(raw) <= 4:
        label = raw                      # an acronym like IDP or EVS
    else:
        label = raw.title() if raw.isupper() or raw.islower() else raw
    from buddy.books import canonical_subject

    key = SUBJECT_NAMES.get(raw.lower()) or canonical_subject(raw.lower(), raw)
    if key:
        return key, label
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:30]
    return (slug, label) if slug and slug[0].isalpha() else None


def grade_of(entry: dict) -> int:
    m = re.search(r"\d+", str(entry.get("grade_name", "")))
    return int(m.group()) if m else 4


def latest_per_title(results: list[dict]) -> list[dict]:
    """One entry per book title: the most recent upload that has a PDF."""
    best: dict[str, dict] = {}
    for e in results:
        if not e.get("file_url") or e.get("pages_ready") is False:
            continue
        name = (e.get("book_name") or e.get("title") or "").strip()
        cur = best.get(name)
        if cur is None or (e.get("created_at", ""), e.get("id", 0)) > \
                (cur.get("created_at", ""), cur.get("id", 0)):
            best[name] = e
    return sorted(best.values(), key=lambda e: e.get("book_name", ""))


SUBJECT_KEY = {"english": "eng", "hindi": "hin", "kannada": "kan", "maths": "maths",
               "evs": "evs"}


def describe(book_name: str, subject: str, label: str | None = None) -> tuple[str, str]:
    """('orchids-eng-gv-t1', 'English Grammar (Term 1)') from e.g.
    'Textbook_Eng_GV_G4_T1_26-27' (word order in the names varies)."""
    kind, part, part_code, other = "Textbook", "", "", []
    term = vol = None
    annual = False
    subject_words = {w for w in re.split(r"[^a-z0-9]+", f"{subject} {label or ''}".lower()) if w}
    pending = None  # "VOL"/"TERM" waiting for its number
    for p in re.split(r"[_\s]+", book_name.strip()):
        u = p.upper()
        if pending and p.isdigit():
            if pending == "T":
                term = p
            else:
                vol = p
            pending = None
            continue
        pending = None
        if u in ("VOL", "VOLUME"):
            pending = "V"
        elif u == "TERM":
            pending = "T"
        elif u in ("AY", "SESSION") or re.fullmatch(r"\(?\d{1,2}\)?|\(\d+\)", p) or not p:
            continue  # "AY 26-27", "(1)" copy markers
        elif p.lower() in subject_words or p.lower() == subject_code(subject):
            continue  # "Financial", "Literacy" in a Financial Literacy book
        elif u == "WORKBOOK":
            kind = "Workbook"
        elif u == "TEXTBOOK" or u == "BOOK" or u in SUBJECT_CODES:
            continue
        elif len(p) >= 3 and (p.lower() == subject or
                              (label or "").lower().replace(" ", "").startswith(p.lower())):
            continue  # the subject's own code, e.g. "Hort" in a Horticulture book
        elif u in PART_NAMES:
            part, part_code = PART_NAMES[u], u.lower()
        elif re.fullmatch(r"G\d+", u) or re.fullmatch(r"\d{2}-\d{2}", p):
            continue  # grade and session year: the same for all her books
        elif m := re.fullmatch(r"T(\d)", u):
            term = m.group(1)
        elif m := re.fullmatch(r"V(\d+)", u):
            vol = m.group(1)
        elif u == "ANNUAL":
            annual = True
        elif p:
            other.append(re.sub(r"[^a-z0-9]", "", p.lower()))
    label = label or SUBJECT_LABEL.get(subject, subject.title())
    if kind == "Workbook":
        name = f"{label} Workbook" + (f" Vol {vol}" if vol else "")
        bits = ["wb", f"v{vol}" if vol else ""]
    else:
        name = f"{label} {part or 'Textbook'}"
        bits = [part_code or "tb"]
    extra = ([f"Term {term}"] if term else []) + (["Annual"] if annual else [])
    if kind != "Workbook" and vol:
        extra.append(f"Vol {vol}")
    if extra:
        name += f" ({', '.join(extra)})"
    bits += [f"t{term}" if term else "", "annual" if annual else "",
             f"v{vol}" if vol and kind != "Workbook" else "", *other]
    key = "-".join(["orchids", subject_code(subject), *[b for b in bits if b]])
    return key[:41].rstrip("-"), name


def subject_code(subject: str) -> str:
    """Short subject part of book keys: eng, hin, maths, fl (financial-literacy), hort…"""
    if subject in SUBJECT_KEY:
        return SUBJECT_KEY[subject]
    words = subject.split("-")
    if len(words) > 1:
        return "".join(w[0] for w in words if w)
    return subject[:12]


def plan(listing: dict, used: dict | None = None) -> list[dict]:
    """One row per book title. `used` (title -> key) keeps keys unique across listings."""
    load_custom_books()
    existing = {b.title: b for b in BOOKS.values() if b.is_school}
    used = used if used is not None else {}
    rows = []
    for e in latest_per_title(listing.get("results", [])):
        found = subject_of(e)
        if not found:
            rows.append({"entry": e, "skip": f"no subject name for {e.get('book_name')!r}"})
            continue
        subject, label = found
        title = e.get("book_name") or e.get("title", "")
        key, name = describe(title, subject, label)
        if title in existing:  # imported before (maybe under an older key): keep it
            key, name = existing[title].key, existing[title].label
        elif key in used.values():
            n = 2
            while f"{key[:38]}-{n}" in used.values():
                n += 1
            key = f"{key[:38]}-{n}"
        used[title] = key
        rows.append({"entry": e, "subject": subject, "label": label, "grade": grade_of(e),
                     "key": key, "name": name, "skip": None})
    return rows


def pdf_path(key: str, ebook_id) -> Path:
    return get_settings().raw_dir / key / f"ebook-{ebook_id}.pdf"


def download(entry: dict, dest: Path, get=None, log=print) -> Path:
    size = entry.get("file_size") or 0
    if dest.exists():  # only complete downloads are renamed from .part
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    url = entry["file_url"]
    log(f"  downloading {entry.get('file_name', url)} ({size / 1e6:.1f} MB)…")
    if get is not None:  # tests
        tmp.write_bytes(get(url))
    else:
        headers = {"User-Agent": "BuddyTeacher (personal study helper; parent account)"}
        with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                          timeout=httpx.Timeout(300, connect=30)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
    if tmp.read_bytes()[:5] != b"%PDF-":
        tmp.unlink()
        raise SystemExit(f"{url} did not return a PDF (is this computer logged in / allowed?)")
    tmp.replace(dest)
    return dest


def import_one(row: dict, client=None, get=None, log=print) -> list[dict]:
    """Download, register as a school book, split into chapters."""
    e = row["entry"]
    load_custom_books()
    if row["key"] not in BOOKS:
        add_school_book(row["key"], row["subject"], row["name"], grade=row["grade"],
                        title=e.get("book_name", ""), subject_label=row.get("label"))
    pdf = download(e, pdf_path(row["key"], e["id"]), get=get, log=log)
    with pymupdf.open(pdf) as doc:
        n = doc.page_count
    split_file = pdf.with_suffix(".chapters.json")
    if split_file.exists():
        rows = json.loads(split_file.read_text())
    else:
        log(f"  {n} pages; finding chapters…")
        chapters, cost = detect_chapters(pdf_thumbs(pdf), client=client, log=log)
        if not chapters:  # no chapter headings found: keep the whole book as one
            chapters = [{"number": 1, "title": e.get("book_name", ""), "start_page": 1}]
        rows = split_ranges(chapters, n)
        split_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        log(f"  chapter detection ${cost:.3f}")
    write_pdf_chapters(row["key"], pdf, rows)
    return rows


def load_listing(path: Path) -> dict:
    data = json.loads(Path(path).expanduser().read_text())
    return data if isinstance(data, dict) else {"results": data}
