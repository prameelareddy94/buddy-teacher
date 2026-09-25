import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from buddy import fixes, logs, review, router
from buddy.books import BOOKS, citation
from buddy.ingest import batch
from buddy.ingest.download import available_chapters, chapter_pdf_path
from buddy.ingest.index import index_chapter
from buddy.kid_rules import NOT_IN_BOOK
from buddy.rag import store
from tests.conftest import SAMPLE_RESULT, make_pdf
from tests.test_router import FakeClaude, run, seed

Q = "What do plants need to make food?"


# ---------- books across classes ----------

def test_book_keys_and_citations():
    assert BOOKS["evs"].grade == 4 and BOOKS["evs-c3"].grade == 3
    assert "evs-c1" not in BOOKS  # no EVS book in Classes 1-2
    assert citation("evs", 2, 14) == "EVS, Chapter 2, page 14"
    assert citation("english-c1", 3, 7) == "English (Class 1), Chapter 3, page 7"


def seed_class3():
    p = chapter_pdf_path("evs-c3", 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    make_pdf(p, ["Animals around us"])
    result = json.loads(json.dumps(SAMPLE_RESULT))
    result["chapter_title"] = "Animals Around Us"
    result["topics"] = [{
        "title": "Where animals live", "pdf_pages": [1],
        "clean_text": "Fish live in water. Birds build nests in trees. Rabbits live in burrows.",
        "diagrams": [], "kid_explanation": "Every animal has a home.",
        "qa": [{"kind": "short", "question": "Where do rabbits live?", "answer": "In burrows.",
                "hint": "Think under the ground.", "pdf_pages": [1]}]}]
    msg = SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5",
                          content=[SimpleNamespace(type="text", text=json.dumps(result))],
                          usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    item = SimpleNamespace(custom_id="evs-c3-ch01",
                           result=SimpleNamespace(type="succeeded", message=msg))
    client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(
        results=lambda _id: [item])))
    [rep] = batch.collect(client, "b3")
    assert rep["book"] == "evs-c3"
    index_chapter("evs-c3", 1)


def test_lower_class_book_is_searched_and_cited():
    seed()
    seed_class3()
    hits = store.search("where do rabbits live", subject="evs")
    top = hits[0]
    assert top.meta["book"] == "evs-c3" and top.meta["grade"] == 3
    assert top.cite.startswith("EVS (Class 3), Chapter 1, page ")
    assert router.cite_for_pages([top], [5]) == "EVS (Class 3), Chapter 1, page 5"


def test_unknown_chapter_count_is_probed(monkeypatch):
    from buddy.ingest import download

    got = []

    def fake(book, ch, missing_ok=False, **k):
        if ch > 3:
            return None
        got.append(ch)
        return chapter_pdf_path(book.key, ch)

    monkeypatch.setattr(download, "download_chapter", fake)
    assert available_chapters(BOOKS["english-c2"], fetch=True) == [1, 2, 3]
    assert available_chapters(BOOKS["english-c2"]) == []  # without fetch: local files only


# ---------- feedback, parent fixes, undo ----------

def ask_local_ok(monkeypatch):
    async def fake_local(system, prompt):
        return {"hint": "Sun!", "answer": "Sunlight, water and air.", "confident": True,
                "pages": [5]}, {"input_tokens": 1, "output_tokens": 1}
    monkeypatch.setattr(router.ollama, "ask_local", fake_local)


def test_thumbs_down_routes_similar_questions_to_claude(monkeypatch):
    seed()
    ask_local_ok(monkeypatch)
    done = run(router.Ask(Q, subject="evs"))[-1]
    assert done["route"] == "local"
    fixes.feedback(done["id"], -1)
    monkeypatch.setattr(router, "async_client",
                        lambda: FakeClaude("HINT: h\nANSWER: a\nSOURCE: EVS, Chapter 1, page 5"))
    again = run(router.Ask(Q, subject="evs"))[-1]
    assert again["route"] == "claude_haiku" and again["reason"] == "similar_flagged"
    fixes.feedback(done["id"], 1)  # changing to 👍 clears the flag
    assert run(router.Ask(Q, subject="evs"))[-1]["route"] == "local"


def test_parent_fix_is_used_then_undone(monkeypatch):
    seed()
    ask_local_ok(monkeypatch)
    first = run(router.Ask(Q, subject="evs"))[-1]
    fixes.feedback(first["id"], -1)
    fix_id = fixes.parent_fix(first["id"], "Plants need sunlight, water and air.",
                              "Look at the sun!", "evs", 1, 5, False)
    assert logs.get_question(first["id"])["fix_id"] == fix_id
    assert not logs.flagged_unfixed()

    ev = run(router.Ask(Q, subject="evs"))
    done = ev[-1]
    assert done["route"] == "verified" and done["answer"] == "Plants need sunlight, water and air."
    assert done["source"] == "EVS, Chapter 1, page 5" and done["hint"] == "Look at the sun!"

    fixes.undo(fix_id)
    assert logs.get_fix(fix_id)["status"] == "undone"
    assert logs.get_question(first["id"])["reviewed"] == 2  # review won't redo it
    after = run(router.Ask(Q, subject="evs"))[-1]
    assert after["route"] != "verified"


def test_parent_not_in_book_and_parent_note():
    seed()
    qid = logs.log_question(question="Who is the prime minister?", subject="evs",
                            route="claude_haiku", reason="low_retrieval", answer="x")
    fid = fixes.parent_fix(qid, "", "", None, None, None, True)
    f = logs.get_fix(fid)
    assert f["answer"] == NOT_IN_BOOK and f["source"] == "" and f["verdict"] == "not_in_book"
    qid2 = logs.log_question(question="What time is homework?", subject="evs",
                             route="local", reason="default", answer="x")
    fid2 = fixes.parent_fix(qid2, "After snack time.", "", None, None, None, False)
    assert logs.get_fix(fid2)["source"] == fixes.PARENT_NOTE


# ---------- nightly review ----------

class FakeReviewClient:
    """Answers each review request from a {question: verdict-json} table."""

    def __init__(self, answers):
        self.answers = answers
        self.created = []
        self.messages = self
        self.batches = self

    def _reply(self, params):
        text = params["messages"][0]["content"]
        q = text.split("Child's question: ", 1)[1].split("\n", 1)[0]
        data = self.answers[q]
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(data))],
                               usage=SimpleNamespace(input_tokens=4000, output_tokens=500))

    def create(self, requests=None, **params):
        if requests is not None:  # batches.create
            self.created = requests
            return SimpleNamespace(id="msgbatch_r")
        return self._reply(params)

    def retrieve(self, _id):
        return SimpleNamespace(processing_status="ended")

    def results(self, _id):
        return [SimpleNamespace(custom_id=r["custom_id"], result=SimpleNamespace(
            type="succeeded", message=self._reply(r["params"]))) for r in self.created]


def weak_questions():
    ids = {
        "bad": logs.log_question(question=Q, subject="evs", route="local", reason="default",
                                 top_score=0.7, answer="Plants eat soil.", source="EVS, Chapter 1, page 5"),
        "nib": logs.log_question(question="Who invented the telephone?", subject="evs",
                                 route="claude_haiku", reason="low_retrieval", top_score=0.1,
                                 answer=NOT_IN_BOOK),
        "fake": logs.log_question(question="Trees give homes to birds?", subject="evs",
                                  route="claude_haiku", reason="local_no_page", top_score=0.6,
                                  answer="yes"),
        "good": logs.log_question(question="What is a sapling?", subject="evs", route="local",
                                  reason="default", top_score=0.8, answer="A young tree."),
    }
    fixes.feedback(ids["bad"], -1)
    return ids


REVIEW_ANSWERS = {
    Q: {"verdict": "fixed", "hint": "Think of the sun.", "answer": "Sunlight, water and air.",
        "source": "EVS, Chapter 1, page 5", "note": "Buddy said soil."},
    "Who invented the telephone?": {"verdict": "not_in_book", "hint": "", "answer": "",
                                    "source": "", "note": "Not in the books."},
    "Trees give homes to birds?": {"verdict": "fixed", "hint": "h", "answer": "True.",
                                   "source": "Science, Chapter 9, page 99", "note": "made up"},
}


@pytest.mark.parametrize("mode", ["direct", "batch"])
def test_review_applies_validates_and_records(mode, monkeypatch):
    seed()
    ids = weak_questions()
    cands = {q["id"] for q in review.candidates(10)}
    assert cands == {ids["bad"], ids["nib"], ids["fake"]}  # "good" isn't weak

    res = review.run(mode, client=FakeReviewClient(REVIEW_ANSWERS), poll_seconds=0)
    assert res["status"] == "done" and res["items"] == 3
    assert (res["fixed"], res["not_in_book"], res["rejected"]) == (1, 1, 1)
    assert res["cost_usd"] > 0
    assert logs.get_question(ids["fake"])["review_verdict"].startswith("rejected")
    assert review.candidates(10) == []  # nothing reviewed twice

    ask_local_ok(monkeypatch)
    done = run(router.Ask(Q, subject="evs"))[-1]
    assert done["route"] == "verified" and done["answer"] == "Sunlight, water and air."
    run_row = logs.list_runs()[0]
    assert run_row["status"] == "done" and run_row["fixed"] == 1


def test_review_respects_budget(monkeypatch):
    seed()
    weak_questions()
    monkeypatch.setenv("REVIEW_BUDGET_USD", "0.0001")
    from buddy import config
    config.get_settings.cache_clear()
    res = review.run("direct", client=FakeReviewClient(REVIEW_ANSWERS))
    assert res["status"] == "nothing_to_do"


def test_review_nothing_to_do():
    seed()
    assert review.run("direct", client=FakeReviewClient({}))["status"] == "nothing_to_do"


# ---------- app endpoints ----------

def login(c, pw):
    c.post("/login", data={"password": pw}, follow_redirects=False)


def test_feedback_correct_undo_endpoints(monkeypatch):
    from buddy.app.main import app

    seed()
    qid = logs.log_question(question=Q, subject="evs", route="local", reason="default",
                            answer="wrong")
    kid, parent = TestClient(app), TestClient(app)
    login(kid, "kid")
    login(parent, "parent")
    assert kid.post("/api/feedback", data={"id": qid, "vote": "down"}).json() == {"ok": True}
    assert kid.get("/api/review").status_code == 403
    assert kid.post("/api/review/run").status_code == 403

    state = parent.get("/api/review").json()
    assert [q["id"] for q in state["flagged"]] == [qid]
    r = parent.post("/api/correct", data={"question_id": qid, "answer": "Sunlight, water, air.",
                                          "book": "evs", "chapter": 1, "page": 5})
    fix_id = r.json()["fix_id"]
    state = parent.get("/api/review").json()
    assert state["flagged"] == [] and state["fixes"][0]["source"] == "EVS, Chapter 1, page 5"
    assert parent.post(f"/api/fixes/{fix_id}/undo").json() == {"ok": True}
    assert parent.get("/api/review").json()["fixes"][0]["status"] == "undone"
    assert any(b["key"] == "english-c1" for b in parent.get("/api/books").json())


def test_next_review_at():
    from buddy.app.main import next_review_at

    tz = ZoneInfo("Asia/Kolkata")
    before = datetime(2026, 9, 25, 1, 0, tzinfo=tz)
    assert next_review_at(before, "02:30", "Asia/Kolkata") == datetime(2026, 9, 25, 2, 30, tzinfo=tz)
    after = datetime(2026, 9, 25, 3, 0, tzinfo=tz)
    assert next_review_at(after, "02:30", "Asia/Kolkata") == datetime(2026, 9, 26, 2, 30, tzinfo=tz)


# ---------- index changes from another process ----------

def test_store_reloads_after_outside_write():
    import os
    import subprocess
    import sys

    seed()
    before = store.search("penguins in snow", subject="evs")
    assert all("penguin" not in h.text.lower() for h in before)
    code = ("from buddy.rag import store;"
            "store.add_chunks(['x1'], ['Penguins live in the cold snow.'],"
            "[{'subject':'evs','kind':'text','source':'ncert','cite':'EVS, Chapter 9, page 1',"
            "'page':1,'pages':'1'}])")
    subprocess.run([sys.executable, "-c", code], check=True, env=os.environ.copy(),
                   cwd=os.getcwd())
    after = store.search("penguins in snow", subject="evs")
    assert "Penguins" in after[0].text
