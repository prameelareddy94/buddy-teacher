import json
from types import SimpleNamespace

import httpx
import pymupdf
import pytest

from buddy import books
from buddy.ingest import ebook
from buddy.ingest.download import chapter_pdf_path


def png_bytes(text):
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 100), text, fontsize=30)
    return page.get_pixmap().tobytes("png")


class FakeServer:
    """page_1..page_N exist; the next one is 404 (or 403 like S3/CloudFront)."""

    def __init__(self, n, missing=404):
        self.n, self.missing, self.calls = n, missing, []

    def get(self, url):
        self.calls.append(url)
        page = int(url.rsplit("page_", 1)[1].split(".")[0])
        if page > self.n:
            return httpx.Response(self.missing, request=httpx.Request("GET", url))
        return httpx.Response(200, content=png_bytes(f"page {page}"),
                              headers={"content-type": "image/png"},
                              request=httpx.Request("GET", url))


class FakeHaiku:
    def __init__(self, chapters):
        self.chapters, self.messages, self.calls = chapters, self, []

    def create(self, **params):
        self.calls.append(params)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(
                {"chapters": self.chapters, "subject": "EVS"}))],
            usage=SimpleNamespace(input_tokens=30000, output_tokens=300))


@pytest.fixture
def school_book():
    books.add_school_book("orchids-evs", "evs", "EVS book")


@pytest.mark.parametrize("missing", [404, 403])
def test_fetch_stops_at_missing_page_and_resumes(school_book, missing, monkeypatch):
    monkeypatch.setattr(ebook.time, "sleep", lambda s: None)
    srv = FakeServer(7, missing)
    pages = ebook.fetch_pages("orchids-evs", "1749", get=srv.get, log=lambda *a: None)
    assert len(pages) == 7 and len(srv.calls) == 8
    assert srv.calls[0].endswith("/ebooks/1749/pages/page_1.png")
    again = FakeServer(7, missing)
    assert len(ebook.fetch_pages("orchids-evs", "1749", get=again.get, log=lambda *a: None)) == 7
    assert len(again.calls) == 1  # only re-checked the end


def test_fetch_unknown_ebook(school_book, monkeypatch):
    monkeypatch.setattr(ebook.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="page 1 not found"):
        ebook.fetch_pages("orchids-evs", "9999", get=FakeServer(0).get, log=lambda *a: None)


def test_import_detects_and_writes_chapters(school_book, monkeypatch):
    monkeypatch.setattr(ebook.time, "sleep", lambda s: None)
    haiku = FakeHaiku([{"number": 1, "title": "Plants", "start_page": 3},
                       {"number": 2, "title": "Animals", "start_page": 6},
                       {"number": 2, "title": "dup", "start_page": 6}])
    rows = ebook.import_ebook("orchids-evs", "1749", client=haiku,
                              get=FakeServer(9).get, log=lambda *a: None)
    assert rows == [{"chapter": 1, "title": "Plants", "pages": "3-5"},
                    {"chapter": 2, "title": "Animals", "pages": "6-9"}]
    assert pymupdf.open(chapter_pdf_path("orchids-evs", 1)).page_count == 3
    assert pymupdf.open(chapter_pdf_path("orchids-evs", 2)).page_count == 4
    imgs = [b for b in haiku.calls[0]["messages"][0]["content"] if b["type"] == "image"]
    assert len(imgs) == 9 and imgs[0]["source"]["media_type"] == "image/jpeg"
    assert haiku.calls[0]["model"] == "claude-haiku-4-5"


def test_manual_split_and_numbering_across_ebooks(school_book, monkeypatch):
    monkeypatch.setattr(ebook.time, "sleep", lambda s: None)
    taken = set()
    a = ebook.import_ebook("orchids-evs", "100", split="1:2,2:4", get=FakeServer(5).get,
                           log=lambda *a: None, taken=taken)
    b = ebook.import_ebook("orchids-evs", "101", split="1:1", get=FakeServer(3).get,
                           log=lambda *a: None, taken=taken)
    assert [r["chapter"] for r in a] == [1, 2]
    assert b == [{"chapter": 3, "title": "", "pages": "1-3"}]  # term 2 book continues at 3
    with pytest.raises(SystemExit):
        ebook.parse_split("1-5")


def test_cli_needs_a_school_book(capsys):
    from buddy.ingest import __main__ as cli

    with pytest.raises(SystemExit, match="add-book"):
        cli.main(["fetch-ebook", "evs", "1749"])
