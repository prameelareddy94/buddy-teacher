import json
from types import SimpleNamespace

import pymupdf
from fastapi.testclient import TestClient

from buddy import router
from buddy.app.main import app
from tests.test_router import seed


def login(c, pw):
    return c.post("/login", data={"password": pw}, follow_redirects=False)


def sse(resp):
    return [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: ")]


def test_login_and_roles():
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/api/ask", data={"question": "hi"}).status_code == 401
    assert login(c, "wrong").headers["location"] == "/login?bad=1"
    assert login(c, "kid").headers["location"] == "/"
    assert c.get("/api/me").json()["role"] == "kid"
    assert c.get("/api/log").status_code == 403           # kid can't see the log
    assert c.post("/api/upload", files={"files": ("a.pdf", b"x")}).status_code == 403
    p = TestClient(app)
    assert login(p, "parent").headers["location"] == "/parent"
    assert p.get("/api/log").status_code == 200


def test_ask_streams_and_logs(monkeypatch):
    seed()

    async def fake_local(system, prompt):
        return {"hint": "Sun!", "answer": "Sunlight, water, air.", "confident": True,
                "pages": [5]}, {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(router.ollama, "ask_local", fake_local)
    c = TestClient(app)
    login(c, "kid")
    r = c.post("/api/ask", data={"question": "What do plants need to make food?",
                                 "subject": "evs"})
    assert r.headers["content-type"].startswith("text/event-stream")
    evs = sse(r)
    assert evs[0]["type"] == "meta" and evs[-1]["type"] == "done"
    assert evs[-1]["source"] == "EVS, Chapter 1, page 5"
    p = TestClient(app)
    login(p, "parent")
    log = p.get("/api/log").json()
    assert log["rows"][0]["route"] == "local"
    assert log["summary"]["total_questions"] == 1
    assert c.get("/api/chapters", params={"subject": "evs"}).json() == \
        [{"book": "evs", "chapter": 1, "grade": 4, "title": "Nurturing Nature", "label": "EVS"}]


def test_upload_worksheet(monkeypatch, tmp_path):
    from buddy.ingest import uploads

    extracted = {
        "title": "Plants worksheet", "subject_guess": "evs", "chapter_guess": 1,
        "text": "Fill in the blanks. Plants need ____ to make food.",
        "questions": [{"kind": "fill in the blanks", "question": "Plants need ____ to make food.",
                       "answer": "sunlight", "marks": "1", "page": 1}],
        "pattern_notes": "Section A: fill in the blanks (1 mark). Answers in one word.",
    }
    calls = []

    def create(**params):
        calls.append(params)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(extracted))],
                               usage=SimpleNamespace(input_tokens=3000, output_tokens=400))

    monkeypatch.setattr(uploads, "sync_client",
                        lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    doc = pymupdf.open(); doc.new_page().insert_text((72, 72), "Worksheet"); pdf = doc.tobytes()
    p = TestClient(app)
    login(p, "parent")
    r = p.post("/api/upload", data={"subject": "evs", "chapter": "1", "kind": "worksheet"},
               files=[("files", ("ws.pdf", pdf, "application/pdf"))])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["questions"] == 1 and j["chunks"] == 3
    assert calls[0]["model"] == "claude-sonnet-5"
    from buddy.rag import store
    pat = store.get_by({"$and": [{"subject": "evs"}, {"kind": "pattern"}]})
    assert "fill in the blanks" in pat[0].text


def test_api_key_never_served():
    c = TestClient(app)
    for path in ("/login", "/static/app.js", "/static/index.html", "/static/parent.html"):
        assert "test-key" not in c.get(path).text


def test_quiz_from_book_bank():
    seed()
    c = TestClient(app)
    login(c, "kid")
    qs = c.post("/api/quiz", data={"book": "evs", "chapter": "1"}).json()["questions"]
    assert len(qs) == 2
    assert {q["question"] for q in qs} == {"What do plants need to make food?",
                                           "Trees give homes to birds. True or false?"}
    assert all(q["hint"] and q["answer"] and q["source"].startswith("EVS") for q in qs)
