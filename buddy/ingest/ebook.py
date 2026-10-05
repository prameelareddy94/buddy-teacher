"""Import her school's e-books (page images on the school's content server).

  python -m buddy.ingest fetch-ebook orchids-evs 1749

1. Download page_1.png, page_2.png ... until the server says the page doesn't exist.
   Polite: one request at a time with a pause, resumable (pages already saved are kept).
2. Find where each chapter starts: Claude Haiku looks at small thumbnails of all pages
   (~$0.05 a book). Or give the split yourself with --split "1:5,2:17,3:30".
3. Write data/raw/<book>/chNN.pdf for each chapter, ready for `submit <book> all`.

Only for books the child is enrolled for; keep the result private.
"""
import base64
import json
import re
import time
from pathlib import Path

import httpx
import pymupdf

from buddy.books import get_book
from buddy.config import ESCALATION_MODEL, cost_usd, get_settings
from buddy.ingest.download import chapter_pdf_path
from buddy.ingest.pdf import files_to_pdf

PAGE_URL = "https://acad-cdn.letseduvate.com/dev/media/acms/ebooks/{ebook}/pages/page_{page}.png"
MAX_PAGES = 600
THUMB_SIDE = 520         # px; enough to read a chapter heading
PAGES_PER_CALL = 90      # the API takes at most 100 images per request


def ebook_dir(book_key: str, ebook_id: str) -> Path:
    return get_settings().raw_dir / book_key / "ebooks" / str(ebook_id)


def _page_path(folder: Path, n: int) -> Path:
    return folder / f"page_{n:03d}.png"


def fetch_pages(book_key: str, ebook_id: str, url_template: str = PAGE_URL,
                pause: float = 0.3, get=None, log=print) -> list[Path]:
    """Download every page of one e-book. Stops at the first page that doesn't exist."""
    folder = ebook_dir(book_key, ebook_id)
    folder.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "BuddyTeacher (personal study helper; parent account)"}
    client = None
    if get is None:
        client = httpx.Client(headers=headers, timeout=httpx.Timeout(60, connect=30),
                              follow_redirects=True)
        get = client.get
    pages: list[Path] = []
    try:
        for n in range(1, MAX_PAGES + 1):
            dest = _page_path(folder, n)
            if dest.exists() and dest.stat().st_size > 0:
                pages.append(dest)
                continue
            url = url_template.format(ebook=ebook_id, page=n)
            resp = None
            for attempt in range(3):
                try:
                    resp = get(url)
                    break
                except httpx.TransportError as e:
                    log(f"  page {n}: {type(e).__name__}, retrying…")
                    time.sleep(3 * (attempt + 1))
            if resp is None:
                raise SystemExit(f"Network keeps failing at page {n}; run the same command "
                                 "again later to continue where it stopped.")
            ctype = resp.headers.get("content-type", "")
            if resp.status_code in (403, 404) or (resp.status_code == 200
                                                  and not ctype.startswith("image")):
                break  # past the last page
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            pages.append(dest)
            if n % 10 == 0:
                log(f"  {n} pages…")
            time.sleep(pause)
    finally:
        if client:
            client.close()
    if not pages:
        raise SystemExit(f"E-book {ebook_id}: page 1 not found. Check the id, and that this "
                         "computer can open the page link in a browser.")
    return pages


def _thumb_b64(path: Path) -> str:
    pix = pymupdf.Pixmap(str(path))
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    scale = THUMB_SIDE / max(pix.width, pix.height)
    if scale < 1:
        pix = pymupdf.Pixmap(pix, int(pix.width * scale), int(pix.height * scale), None)
    return base64.standard_b64encode(pix.tobytes("jpg", jpg_quality=70)).decode()


SPLIT_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "title": {"type": "string"},
                    "start_page": {"type": "integer"},
                },
                "required": ["number", "title", "start_page"],
                "additionalProperties": False,
            },
        },
        "subject": {"type": "string"},
    },
    "required": ["chapters", "subject"],
    "additionalProperties": False,
}


def detect_chapters(pages: list[Path], client=None, log=print) -> tuple[list[dict], float]:
    """Ask Claude Haiku where each chapter starts. Returns (chapters, cost in USD)."""
    if client is None:
        from buddy.llm.claude import sync_client
        client = sync_client()
    found: dict[int, dict] = {}
    cost = 0.0
    for start in range(0, len(pages), PAGES_PER_CALL):
        part = pages[start:start + PAGES_PER_CALL]
        content: list[dict] = []
        for i, p in enumerate(part, start + 1):
            content.append({"type": "text", "text": f"page {i}"})
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": _thumb_b64(p)}})
        content.append({"type": "text", "text":
                        "These are pages of a school textbook (Class 4, India). List every "
                        "chapter (lesson) that STARTS on these pages: its number as printed "
                        "(or your best count), its title, and the page label (the 'page N' "
                        "text before the image) where it starts. Skip covers, contents, "
                        "prefaces and answer keys. Also say the subject of the book."})
        msg = client.messages.create(
            model=ESCALATION_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": SPLIT_SCHEMA}},
        )
        cost += cost_usd(ESCALATION_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
        data = json.loads(next(b.text for b in msg.content if b.type == "text"))
        for ch in data["chapters"]:
            if 1 <= ch["start_page"] <= len(pages):
                found.setdefault(ch["start_page"], ch)
        log(f"  looked at pages {start + 1}-{start + len(part)} "
            f"({data['subject'] or 'subject unknown'})")
    chapters = sorted(found.values(), key=lambda c: c["start_page"])
    return chapters, cost


def parse_split(spec: str) -> list[dict]:
    """--split "1:5,2:17,3:30" -> chapter 1 starts on page 5, chapter 2 on 17, ..."""
    out = []
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", part)
        if not m:
            raise SystemExit(f"Bad --split part {part!r}; use CHAPTER:START_PAGE,…")
        out.append({"number": int(m.group(1)), "title": "", "start_page": int(m.group(2))})
    return sorted(out, key=lambda c: c["start_page"])


def write_chapters(book_key: str, pages: list[Path], chapters: list[dict],
                   taken: set[int] | None = None) -> list[dict]:
    """Write one chNN.pdf per chapter: pages from its start to the next chapter's start."""
    taken = taken if taken is not None else set()  # shared across e-books of one book
    rows = []
    for i, ch in enumerate(chapters):
        first = ch["start_page"]
        last = chapters[i + 1]["start_page"] - 1 if i + 1 < len(chapters) else len(pages)
        if last < first:
            continue
        num = ch["number"]
        if num < 1 or num in taken:  # duplicate/odd numbering: continue after the highest
            num = max(taken | {0}) + 1
        taken.add(num)
        files_to_pdf(pages[first - 1:last], chapter_pdf_path(book_key, num))
        rows.append({"chapter": num, "title": ch["title"], "pages": f"{first}-{last}"})
    return rows


def import_ebook(book_key: str, ebook_id: str, split: str | None = None,
                 url_template: str = PAGE_URL, client=None, get=None,
                 log=print, taken: set[int] | None = None) -> list[dict]:
    get_book(book_key)  # must exist (add-book first)
    log(f"E-book {ebook_id}: downloading pages…")
    pages = fetch_pages(book_key, ebook_id, url_template, get=get, log=log)
    log(f"  {len(pages)} pages")
    folder = ebook_dir(book_key, ebook_id)
    if split:
        chapters, cost = parse_split(split), 0.0
    else:
        log("Finding where chapters start (Claude Haiku)…")
        chapters, cost = detect_chapters(pages, client=client, log=log)
        log(f"  ${cost:.3f}")
    if not chapters:
        raise SystemExit("No chapters found. Look at the pages in "
                         f"{folder} and give the split with --split \"1:5,2:17,...\"")
    rows = write_chapters(book_key, pages, chapters, taken)
    (folder / "chapters.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    return rows
