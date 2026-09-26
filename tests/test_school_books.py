import json
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi.testclient import TestClient

from buddy import books, router
from buddy.ingest.batch import processed_path
from buddy.ingest.download import chapter_pdf_path
from buddy.ingest.schoolbook import ingest_now, save_chapter_files
from buddy.rag import store
from tests.conftest import SAMPLE_RESULT
from tests.test_router import hit, seed


# ---------- fill in the blanks ----------

@pytest.mark.parametrize("spoken, expected", [
    ("The sun rises in the dash", "The sun rises in the ____"),
    ("Plants need ___ to make food", "Plants need ____ to make food"),
    ("मैं dash dash स्कूल जाती हूँ", "मैं ____ स्कूल जाती हूँ"),
    ("पेड़ हमें खाली स्थान देते हैं", "पेड़ हमें ____ देते हैं"),
    ("राम रिक्त स्थान खाता है", "राम ____ खाता है"),
    ("ಮರ ಖಾಲಿ ನೀಡುತ್ತದೆ", "ಮರ ____ ನೀಡುತ್ತದೆ"),
    ("Trees give us ... and fruits", "Trees give us ____ and fruits"),
])
def test_blanks_are_normalized(spoken, expected):
    q, search = router.normalize_blanks(spoken)
    assert q == expected
    assert "____" not in search and "dash" not in search


def test_normal_questions_untouched():
    for q in ["What is a blanket?", "How do plants make food?", "Explain the dashboard"]:
        assert router.normalize_blanks(q) == (q, q)


def test_blank_question_reaches_model_as_blank(monkeypatch):
    seed()
    seen = {}

    async def fake_local(system, prompt):
        seen["prompt"] = prompt
        seen["rules"] = system
        return {"hint": "h", "answer": "sunlight", "confident": True, "pages": [5]}, {}

    monkeypatch.setattr(router.ollama, "ask_local", fake_local)
    from tests.test_router import run
    done = run(router.Ask("Plants need dash to make food", subject="evs"))[-1]
    assert "Plants need ____ to make food" in seen["prompt"] and "dash" not in seen["prompt"]
    assert "blank to fill in" in seen["rules"]
    from buddy.logs import get_question
    assert get_question(done["id"])["question"] == "Plants need ____ to make food"


# ---------- routing changes ----------

def test_hindi_goes_to_claude():
    good = [hit(0.9)]
    assert router.decide(router.Ask("पेड़ क्या देते हैं?"), good).reason == "hindi"
    assert router.decide(router.Ask("what is a noun", subject="hindi"), good).reason == "hindi"


def test_claude_only_and_weak_match(monkeypatch):
    from buddy import config

    good = [hit(0.9)]
    monkeypatch.setenv("ANSWER_MODE", "claude_only")
    config.get_settings.cache_clear()
    assert router.decide(router.Ask("what is a sapling"), good, threshold=0.45).reason == "claude_only"
    monkeypatch.setenv("ANSWER_MODE", "local_first")
    monkeypatch.setenv("LOCAL_MIN_SCORE", "0.7")
    config.get_settings.cache_clear()
    assert router.decide(router.Ask("what is a sapling"), [hit(0.6)], threshold=0.45).reason == "weak_match"
    assert router.decide(router.Ask("what is a sapling"), [hit(0.8)], threshold=0.45).route == "local"


# ---------- school books ----------

def photo(path, text):
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((50, 80), text, fontsize=20)
    page.get_pixmap().save(str(path))


class FakeStreamClient:
    def __init__(self, result):
        self.result = result
        self.messages = self
        self.params = None

    def stream(self, **params):
        self.params = params
        msg = SimpleNamespace(
            stop_reason="end_turn", model="claude-sonnet-5", id="msg_1",
            content=[SimpleNamespace(type="text", text=json.dumps(self.result))],
            usage=SimpleNamespace(input_tokens=20_000, output_tokens=8_000))

        class S:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def get_final_message(s): return msg
        return S()


def school_result():
    r = json.loads(json.dumps(SAMPLE_RESULT))
    r["chapter_title"] = "Our Green Friends"
    r["topics"][0]["clean_text"] = ("Plants make their own food using sunlight, water and air. "
                                    "This is called photosynthesis in our school book.")
    return r


def test_add_school_book_validates():
    b = books.add_school_book("orchids-evs", "evs", "EVS book")
    assert b.is_school and b.key == "orchids-evs" and b.label == "EVS book"
    assert b.text_mode == "image"
    books.load_custom_books()
    assert "orchids-evs" in books.BOOKS and books.school_book_keys() == {"orchids-evs"}
    with pytest.raises(ValueError):
        books.add_school_book("evs", "evs", "clash with NCERT key")
    with pytest.raises(ValueError):
        books.add_school_book("Bad Key!", "evs", "x")
    with pytest.raises(ValueError):
        books.add_school_book("orchids-art", "art", "x")


def test_photos_become_a_chapter_that_outranks_ncert(tmp_path):
    seed()  # NCERT EVS chapter 1 with the same facts
    books.add_school_book("orchids-evs", "evs", "EVS book")
    files = []
    for i in range(3):
        f = tmp_path / f"IMG_{i}.jpg"
        photo(f, f"page {i + 1}")
        files.append(f)
    assert save_chapter_files("orchids-evs", 2, files) == 3
    assert chapter_pdf_path("orchids-evs", 2).exists()

    client = FakeStreamClient(school_result())
    r = ingest_now("orchids-evs", 2, client=client)
    assert r["status"] == "ok" and r["chunks"] > 0
    assert r["cost_usd"] == round((20_000 * 2 + 8_000 * 10) / 1e6, 4)  # normal price
    content = client.params["messages"][0]["content"]
    assert not any("PDF text layer" in b.get("text", "") for b in content)  # photos: image mode
    assert processed_path("orchids-evs", 2).exists()

    hits = store.search("what do plants need to make food", subject="evs")
    assert hits[0].meta["book"] == "orchids-evs"
    assert hits[0].cite.startswith("EVS book, Chapter 2, page ")
    from buddy import logs
    assert logs.list_jobs()[0]["status"] == "done"


def test_parent_book_endpoints(tmp_path, monkeypatch):
    from buddy.app.main import app
    from buddy.ingest import schoolbook

    seed()
    c = TestClient(app)
    c.post("/login", data={"password": "parent"}, follow_redirects=False)
    r = c.post("/api/books", data={"subject": "evs", "name": "EVS book"})
    assert r.json() == {"key": "school-evs", "label": "EVS book"}
    f = tmp_path / "p1.jpg"
    photo(f, "page one")

    calls = {}

    def fake_ingest(book, chapter, job_id=None):
        calls["args"] = (book, chapter, job_id)

    monkeypatch.setattr(schoolbook, "ingest_now", fake_ingest)
    r = c.post("/api/book-chapter", data={"book": "school-evs", "chapter": "1"},
               files=[("files", ("p1.jpg", f.read_bytes(), "image/jpeg"))])
    assert r.status_code == 200 and r.json()["pages"] == 1
    import time
    for _ in range(50):
        if "args" in calls:
            break
        time.sleep(0.05)
    assert calls["args"][:2] == ("school-evs", 1)
    assert c.get("/api/jobs").json()[0]["book"] == "school-evs"

    listed = {b["key"]: b for b in c.get("/api/books").json()}
    assert listed["school-evs"]["school"] and listed["evs"]["chunks"] > 0
    assert c.post("/api/books/evs/hide").json() == {"ok": True}
    assert {b["key"]: b for b in c.get("/api/books").json()}["evs"]["chunks"] == 0

    kid = TestClient(app)
    kid.post("/login", data={"password": "kid"}, follow_redirects=False)
    assert kid.post("/api/books", data={"subject": "evs", "name": "x"}).status_code == 403


def test_cli_add_book_and_photos(tmp_path, monkeypatch, capsys):
    from buddy.ingest import __main__ as cli

    cli.main(["add-book", "orchids-maths", "--subject", "maths", "--name", "Maths book"])
    f = tmp_path / "a.png"
    photo(f, "Add 2 and 3")
    cli.main(["add-photos", "orchids-maths", "1", str(f)])
    out = capsys.readouterr().out
    assert "Added school book orchids-maths" in out and "1 pages saved" in out
    assert chapter_pdf_path("orchids-maths", 1).exists()
