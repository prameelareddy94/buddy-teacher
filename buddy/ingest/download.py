"""Get chapter PDFs: download from NCERT, or import files you downloaded yourself."""
import re
import shutil
import time
import zipfile
from pathlib import Path

import httpx

from buddy.books import Book
from buddy.config import get_settings

NCERT_PDF = "https://ncert.nic.in/textbook/pdf/{code}{chapter:02d}.pdf"
NCERT_ZIP = "https://ncert.nic.in/textbook/pdf/{code}dd.zip"


def chapter_pdf_path(book_key: str, chapter: int) -> Path:
    return get_settings().raw_dir / book_key / f"ch{chapter:02d}.pdf"


def _manual_help(book: Book, chapter: int, why: str) -> str:
    dest = chapter_pdf_path(book.key, chapter)
    lines = [f"Could not download {book.label} chapter {chapter}: {why}", ""]
    if book.ncert_code:
        lines += [
            "ncert.nic.in is often slow or unreachable from outside India and from some "
            "networks. Download the PDF in a browser (or over a VPN / India connection) and "
            "import it:",
            f"  chapter: {NCERT_PDF.format(code=book.ncert_code, chapter=chapter)}",
            f"    python -m buddy.ingest add-pdf {book.key} {chapter} ~/Downloads/"
            f"{book.ncert_code}{chapter:02d}.pdf",
            f"  whole book: {NCERT_ZIP.format(code=book.ncert_code)}",
            f"    python -m buddy.ingest add-zip {book.key} ~/Downloads/"
            f"{book.ncert_code}dd.zip",
        ]
    lines.append(f"(or just copy the file to {dest})")
    return "\n".join(lines)


def download_chapter(book: Book, chapter: int, force: bool = False, attempts: int = 4,
                     missing_ok: bool = False) -> Path | None:
    """Download one chapter. With missing_ok, a 404 returns None (used to find the last chapter)."""
    dest = chapter_pdf_path(book.key, chapter)
    if dest.exists() and not force:
        return dest
    if not book.ncert_code:
        raise SystemExit(f"{book.label} has no download source. Put the chapter PDF at {dest} "
                         f"or run: python -m buddy.ingest add-pdf {book.key} {chapter} FILE")
    url = NCERT_PDF.format(code=book.ncert_code, chapter=chapter)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    # NCERT's server rejects some default client user agents.
    headers = {"User-Agent": "Mozilla/5.0 (BuddyTeacher personal study helper)"}
    # ncert.nic.in can take a minute to accept a connection from outside India.
    timeout = httpx.Timeout(180, connect=90)
    err = ""
    for i in range(attempts):
        try:
            with httpx.stream("GET", url, headers=headers, timeout=timeout,
                              follow_redirects=True) as r:
                if r.status_code == 404:
                    if missing_ok:
                        return None
                    raise SystemExit(_manual_help(book, chapter, f"{url} returned 404 "
                                                  "(NCERT may have renamed the book)"))
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            err = f"{type(e).__name__}: {e}"
            if i + 1 < attempts:
                print(f"  {book.key} ch{chapter:02d}: download failed ({err}); "
                      f"retrying in {15 * (i + 1)}s…")
                time.sleep(15 * (i + 1))
    else:
        tmp.unlink(missing_ok=True)
        raise SystemExit(_manual_help(book, chapter, err))
    return _accept(tmp, dest, url)


def _accept(src: Path, dest: Path, what: str) -> Path:
    """Check src is a PDF, then move (downloads) or copy (user files) it to dest."""
    temp = src.suffix == ".part"
    if src.read_bytes()[:5] != b"%PDF-":
        if temp:
            src.unlink()
        raise SystemExit(f"{what} is not a PDF")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if temp:
        src.replace(dest)
    else:
        shutil.copyfile(src, dest)
    return dest


def add_pdf(book: Book, chapter: int, src: Path) -> Path:
    """Import a chapter PDF downloaded by hand."""
    return _accept(src.expanduser(), chapter_pdf_path(book.key, chapter), str(src))


def add_zip(book: Book, src: Path) -> list[Path]:
    """Import NCERT's whole-book zip (<code>dd.zip, chapters named <code>NN.pdf)."""
    pat = re.compile(rf"(?:^|/){re.escape(book.ncert_code or '')}(\d\d)\.pdf$", re.I)
    out = []
    with zipfile.ZipFile(src.expanduser()) as z:
        for name in z.namelist():
            m = pat.search(name)
            if not m or int(m.group(1)) == 0:
                continue
            dest = chapter_pdf_path(book.key, int(m.group(1)))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(name))
            out.append(dest)
    if not out:
        raise SystemExit(f"No chapter PDFs named {book.ncert_code}NN.pdf found in {src}")
    return sorted(out)


def local_chapters(book: Book) -> list[int]:
    folder = get_settings().raw_dir / book.key
    return sorted(int(p.stem[2:]) for p in folder.glob("ch[0-9][0-9].pdf"))


def available_chapters(book: Book, fetch: bool = False, limit: int = 30) -> list[int]:
    """Chapters of a book. For books with an unknown count, use the PDFs already
    present; with fetch=True, download from NCERT until a chapter is missing."""
    if book.chapters:
        return list(range(1, book.chapters + 1))
    have = local_chapters(book)
    if have or not fetch or not book.ncert_code:
        return have
    found = []
    for ch in range(1, limit + 1):
        if download_chapter(book, ch, missing_ok=True) is None:
            break
        found.append(ch)
    return found
