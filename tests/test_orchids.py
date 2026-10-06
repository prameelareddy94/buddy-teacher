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
        "orchids-art-tb": (5, "Art Textbook"),  # a subject Buddy didn't know: added
    }
    assert not [r for r in rows if r["skip"]]


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
    assert "orchids-eng-cs-annual" in out and "6 books" in out and "--go" in out

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
    monkeypatch.setattr(cli.batch, "submit", lambda c, items: sent.setdefault("i", items) and ["b1"])
    cli.main(["submit-all"])
    assert "1 chapters, 3 pages" in capsys.readouterr().out and not sent
    cli.main(["submit-all", "--yes", "--force"])
    assert sent["i"] == [("orchids-eng-gv-t1", 2)]


def test_new_school_subjects_become_subjects(tmp_path, capsys):
    from buddy.books import SUBJECTS
    from buddy.ingest import __main__ as cli

    hort = {"results": [entry(70, "Textbook_Hort_G4_Annual Book_26-27", 10, "2026-07-01",
                              subject="Horticulture"),
                        entry(71, "Textbook_IDP_G4_T1_26-27", 10, "2026-07-01", subject="IDP"),
                        entry(72, "Textbook_Sci_G4_T1_26-27", 10, "2026-07-01",
                              subject="Science")]}
    rows = {r["key"]: r for r in orchids.plan(hort)}
    assert rows["orchids-horticulture-tb-annual"]["name"] == "Horticulture Textbook (Annual)"
    assert rows["orchids-idp-tb-t1"]["name"] == "IDP Textbook (Term 1)"
    assert rows["orchids-evs-tb-t1"]["name"] == "Science Textbook (Term 1)"  # maps onto EVS

    haiku = FakeHaiku([{"number": 1, "title": "Soil", "start_page": 1}])
    orchids.import_one(rows["orchids-horticulture-tb-annual"], client=haiku,
                       get=lambda url: book_pdf(2), log=lambda *a: None)
    assert SUBJECTS["horticulture"] == "Horticulture"
    books.load_custom_books()  # survives a reload (server restart)
    assert SUBJECTS["horticulture"] == "Horticulture"

    # several listing files in one go
    a, b = tmp_path / "english.json", tmp_path / "hort.json"
    a.write_text(json.dumps(LISTING))
    b.write_text(json.dumps(hort))
    cli.main(["import-orchids", str(a), str(b)])
    out = capsys.readouterr().out
    assert "9 books" in out and "orchids-idp-tb-t1" in out


def test_long_subject_keys_are_short_and_valid():
    import re

    fl = {"results": [
        entry(80, "Financial_Literacy_G4_V1_AY 26-27 (1)", 10, "2026-07-01",
              subject="Financial Literacy"),
        entry(81, "Financial_Literacy_Textbook_G4_Vol_2_AY 26-27", 10, "2026-07-01",
              subject="Financial Literacy")]}
    rows = orchids.plan(fl)
    keys = [r["key"] for r in rows]
    assert keys == ["orchids-fl-tb-v1", "orchids-fl-tb-v2"]
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", k) for k in keys)


def test_previously_imported_title_keeps_its_key():
    books.add_school_book("orchids-old-key", "english", "English Grammar (Term 1)",
                          title="Textbook_Eng_GV_G4_T1_26-27")
    row = next(r for r in orchids.plan(LISTING) if r["entry"]["id"] == 915)
    assert row["key"] == "orchids-old-key"


def test_same_chapter_across_batches_is_merged():
    from buddy.ingest import ebook

    class TwoCalls(FakeHaiku):
        def create(self, **params):
            n = sum(1 for b in params["messages"][0]["content"] if b["type"] == "image")
            self.chapters = ([{"number": 6, "title": "Types of Income", "start_page": 87}]
                             if n == 90 else
                             [{"number": 7, "title": "Types of income", "start_page": 91}])
            return super().create(**params)

    chapters, _ = ebook.detect_chapters(["x"] * 102, client=TwoCalls([]), log=lambda *a: None)
    assert [c["start_page"] for c in chapters] == [87]


def test_one_failing_book_does_not_stop_the_rest(tmp_path, monkeypatch, capsys):
    from buddy.ingest import __main__ as cli

    f = tmp_path / "english.json"
    f.write_text(json.dumps(LISTING))
    done = []

    def fake_import(row, **kw):
        if row["key"] == "orchids-eng-cs-annual":
            raise ValueError("boom")
        done.append(row["key"])
        return []

    monkeypatch.setattr(orchids, "import_one", fake_import)
    cli.main(["import-orchids", str(f), "--go"])
    out = capsys.readouterr().out
    assert "FAILED: boom" in out and "--only orchids-eng-cs-annual" in out
    assert len(done) == 5


class FakeBatches:
    def __init__(self):
        self.created = []
        self.messages = self
        self.batches = self

    def create(self, requests):
        self.created.append(requests)
        from types import SimpleNamespace
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}")


def _chapters(key, n, pages=2):
    books.add_school_book(key, "english", "English Grammar (Term 1)")
    for ch in range(1, n + 1):
        p = chapter_pdf_path(key, ch)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(book_pdf(pages))


def test_large_submissions_are_split_into_several_batches():
    from buddy.ingest import batch

    _chapters("orchids-eng-gv-t1", 5)
    one = len(json.dumps(batch.chapter_params(books.BOOKS["orchids-eng-gv-t1"], 1)))
    fake = FakeBatches()
    ids = batch.submit(fake, [("orchids-eng-gv-t1", c) for c in range(1, 6)],
                       max_bytes=int(one * 2.5), log=lambda *a: None)
    assert ids == ["msgbatch_1", "msgbatch_2", "msgbatch_3"]
    assert [len(r) for r in fake.created] == [2, 2, 1]
    pending = batch.pending_batches()
    assert [p["batch_id"] for p in pending] == ids
    assert pending[2]["items"] == ["orchids-eng-gv-t1-ch05"]


def test_submit_all_skips_chapters_already_waiting(monkeypatch, capsys):
    from buddy.ingest import __main__ as cli
    from buddy.ingest import batch

    _chapters("orchids-eng-gv-t1", 3)
    batch.submit(FakeBatches(), [("orchids-eng-gv-t1", 1)], log=lambda *a: None)
    sent = {}
    monkeypatch.setattr(cli, "client", lambda: None)
    monkeypatch.setattr(cli.batch, "submit", lambda c, items: sent.setdefault("i", items) and ["b"])
    cli.main(["submit-all", "--yes", "--force"])
    assert sent["i"] == [("orchids-eng-gv-t1", 2), ("orchids-eng-gv-t1", 3)]


def test_collect_all(monkeypatch, capsys):
    from types import SimpleNamespace

    from buddy.ingest import __main__ as cli
    from buddy.ingest import batch

    _chapters("orchids-eng-gv-t1", 1)
    batch.submit(FakeBatches(), [("orchids-eng-gv-t1", 1)], log=lambda *a: None)
    from tests.conftest import SAMPLE_RESULT
    msg = SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5",
                          content=[SimpleNamespace(type="text", text=json.dumps(SAMPLE_RESULT))],
                          usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(
        retrieve=lambda bid: SimpleNamespace(processing_status="ended"),
        results=lambda bid: [SimpleNamespace(custom_id="orchids-eng-gv-t1-ch01",
                                             result=SimpleNamespace(type="succeeded",
                                                                    message=msg))])))
    monkeypatch.setattr(cli, "client", lambda: client)
    cli.main(["collect-all"])
    out = capsys.readouterr().out
    assert "indexed 1 chapters" in out
    assert batch.pending_batches() == []
    assert processed_path("orchids-eng-gv-t1", 1).exists()


def test_subject_variants_merge_into_one(tmp_path):
    from buddy.books import SUBJECTS, canonical_subject
    from buddy.rag import store

    assert canonical_subject("hindi-3rd-language", "Hindi 3rd Language") == "hindi"
    assert canonical_subject("x", "हिंदी") == "hindi"
    assert canonical_subject("general-science", "General Science") == "evs"
    assert canonical_subject("computer-science", "Computer Science") is None
    assert canonical_subject("horticulture", "Horticulture") is None
    assert orchids.subject_of({"subject_name": "Hindi 3rd Language"}) == ("hindi", "Hindi 3rd Language")

    # A book imported earlier as its own "Hindi 3rd Language" subject is merged on load,
    # and its indexed chunks follow.
    f = books._custom_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps([{
        "subject": "hindi-3rd-language", "grade": 4, "title": "Textbook_Hin_G4_T1",
        "language": "Hindi", "text_mode": "image", "ncert_code": None, "chapters": None,
        "custom_key": "orchids-hindi-3rd-tb-t1", "name": "Hindi 3rd Language Textbook (Term 1)",
        "subject_label": "Hindi 3rd Language"}]))
    store.add_chunks(["h1"], ["पेड़ हमें छाया देते हैं"], [{
        "subject": "hindi-3rd-language", "book": "orchids-hindi-3rd-tb-t1", "kind": "text",
        "source": "ncert", "cite": "x", "page": 1, "pages": "1"}])
    books.load_custom_books()
    assert books.BOOKS["orchids-hindi-3rd-tb-t1"].subject == "hindi"
    assert "hindi-3rd-language" not in SUBJECTS
    assert list(SUBJECTS).count("hindi") == 1
    assert store.get_by({"subject": "hindi"})[0].id == "h1"
    assert json.loads(f.read_text())[0]["subject"] == "hindi"  # saved, so it stays merged
