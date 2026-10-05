import json

import pymupdf
import pytest

from buddy import books
from buddy.ingest import orchids
from buddy.ingest.batch import processed_path
from buddy.ingest.download import chapter_pdf_path
from tests.test_ebook import FakeHaiku


def entry(id_, name, size, created, subject="English"):
    return {"id": id_, "title": name, "book_name": name, "session_year": "2026-27",
            "file_url": f"https://cdn.example/ebooks/{id_}.pdf", "file_name": f"{name}.pdf",
            "file_size": size, "grade_name": "Grade 4", "subject_name": subject,
            "created_at": created, "pages_ready": True}


# A slice of the real listing: the same titles uploaded several times.
LISTING = {"results": [
    entry(1749, "Workbook_Eng_G4_V4_26-27", 8364458, "2026-07-13T15:36:47"),
    entry(1748, "Textbook_WS_Eng_G4_T2_26-27", 18699279, "2026-07-13T15:36:27"),
    entry(1745, "Textbook_Eng_CS_G4_Annual Book_26-27", 17906715, "2026-07-13T15:35:21"),
    entry(1667, "Textbook_WS_Eng_G4_T2_26-27", 18699279, "2026-07-13T13:27:55"),
    entry(1664, "Textbook_Eng_CS_G4_Annual Book_26-27", 17906715, "2026-07-13T13:26:58"),
    entry(1455, "Textbook_Eng_RC_G4_T1_26-27", 155006882, "2026-05-30T16:57:25"),
    entry(914, "Textbook_Eng_RC_G4_T1_26-27", 16276281, "2026-04-27T15:45:58"),
    entry(915, "Textbook_Eng_GV_G4_T1_26-27", 28304002, "2026-04-27T15:46:52"),
    entry(191, "Textbook_Eng_CS_G4_Annual Book_26-27", 17906715, "2026-03-30T10:13:20"),
    entry(5, "Textbook_Art_G4_26-27", 100, "2026-03-30T10:13:20", subject="Art"),
]}


def test_plan_keeps_newest_copy_and_names_books():
    rows = orchids.plan(LISTING)
    got = {r["key"]: (r["entry"]["id"], r["name"]) for r in rows if not r["skip"]}
    assert got == {
        "orchids-eng-wb-v4": (1749, "English Workbook Vol 4"),
        "orchids-eng-ws-t2": (1748, "English Writing (Term 2)"),
        "orchids-eng-cs-annual": (1745, "English Coursebook (Annual)"),
        "orchids-eng-rc-t1": (1455, "English Reading (Term 1)"),
        "orchids-eng-gv-t1": (915, "English Grammar (Term 1)"),
    }
    assert [r["skip"] for r in rows if r["skip"]] == ["unknown subject 'Art'"]


def book_pdf(n_pages):
    doc = pymupdf.open()
    for i in range(n_pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    return doc.tobytes()


def test_import_one_downloads_registers_and_splits():
    row = next(r for r in orchids.plan(LISTING) if r.get("key") == "orchids-eng-gv-t1")
    haiku = FakeHaiku([{"number": 1, "title": "Nouns", "start_page": 3},
                       {"number": 2, "title": "Verbs", "start_page": 7}])
    rows = orchids.import_one(row, client=haiku, get=lambda url: book_pdf(10),
                              log=lambda *a: None)
    assert [(r["chapter"], r["pages"]) for r in rows] == [(1, "3-6"), (2, "7-10")]
    assert books.BOOKS["orchids-eng-gv-t1"].label == "English Grammar (Term 1)"
    assert pymupdf.open(chapter_pdf_path("orchids-eng-gv-t1", 2)).page_count == 4
    # second run: no download, no detection (split saved), chapters rewritten the same
    again = orchids.import_one(row, client=None, get=lambda url: pytest.fail("downloaded"),
                               log=lambda *a: None)
    assert again == rows


def test_not_a_pdf_is_rejected():
    row = next(r for r in orchids.plan(LISTING) if r.get("key") == "orchids-eng-gv-t1")
    with pytest.raises(SystemExit, match="did not return a PDF"):
        orchids.import_one(row, client=None, get=lambda url: b"<html>login</html>",
                           log=lambda *a: None)


def test_cli_plan_then_submit_all(tmp_path, monkeypatch, capsys):
    from buddy.ingest import __main__ as cli

    f = tmp_path / "english.json"
    f.write_text(json.dumps(LISTING))
    cli.main(["import-orchids", str(f)])
    out = capsys.readouterr().out
    assert "orchids-eng-cs-annual" in out and "5 books" in out and "--go" in out

    books.add_school_book("orchids-eng-gv-t1", "english", "English Grammar (Term 1)")
    for ch in (1, 2):
        p = chapter_pdf_path("orchids-eng-gv-t1", ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(book_pdf(3))
    done = processed_path("orchids-eng-gv-t1", 1)
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("{}")
    sent = {}
    monkeypatch.setattr(cli, "client", lambda: None)
    monkeypatch.setattr(cli.batch, "submit", lambda c, items: sent.setdefault("i", items) and "b1")
    cli.main(["submit-all"])
    assert "1 chapters, 3 pages" in capsys.readouterr().out and not sent
    cli.main(["submit-all", "--yes", "--force"])
    assert sent["i"] == [("orchids-eng-gv-t1", 2)]
