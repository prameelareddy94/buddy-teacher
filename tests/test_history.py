import time

from fastapi.testclient import TestClient

from buddy import fixes, logs, router
from tests.test_feedback_review import ask_local_ok
from tests.test_router import FakeClaude, run, seed

Q = "What do plants need to make food?"


def test_same_question_reuses_answer(monkeypatch):
    seed()
    calls = []

    async def fake_local(system, prompt):
        calls.append(1)
        return {"hint": "Sun!", "answer": "Sunlight, water and air.", "confident": True,
                "pages": [5]}, {}

    monkeypatch.setattr(router.ollama, "ask_local", fake_local)
    first = run(router.Ask(Q, subject="evs"))[-1]
    again = run(router.Ask("what do plants need to make food", subject="evs"))[-1]
    assert again["route"] == "cached" and again["answer"] == first["answer"]
    assert again["source"] == first["source"] and len(calls) == 1
    row = logs.get_question(again["id"])
    assert row["cost_usd"] == 0 and row["model"] == f"answer #{first['id']}"
    # a different subject, a photo, or "explain more" is answered fresh
    assert run(router.Ask(Q, subject="maths"))[-1]["route"] != "cached"


def test_disliked_old_or_fixed_answers_are_not_reused(monkeypatch):
    seed()
    ask_local_ok(monkeypatch)
    first = run(router.Ask(Q, subject="evs"))[-1]
    fixes.feedback(first["id"], -1)
    monkeypatch.setattr(router, "async_client",
                        lambda: FakeClaude("HINT: h\nANSWER: better\nSOURCE: EVS, Chapter 1, page 5"))
    assert run(router.Ask(Q, subject="evs"))[-1]["route"] != "cached"

    # older than 30 days
    q2 = "Where do birds live?"
    old = logs.log_question(question=q2, subject="evs", route="local", reason="default",
                            answer="In nests.", ts=time.time() - 40 * 86400)
    assert logs.find_previous_answer(q2, "evs") is None
    assert logs.get_question(old)

    # a parent fix beats the saved answer
    q3 = "What is a sapling?"
    bad = logs.log_question(question=q3, subject="evs", route="local", reason="default",
                            answer="A big tree.", source="EVS, Chapter 1, page 6")
    fixes.parent_fix(bad, "A young tree.", "Think small!", "evs", 1, 6, False)
    done = run(router.Ask(q3, subject="evs"))[-1]
    assert done["route"] == "verified" and done["answer"] == "A young tree."


def test_history_endpoint_pages_back(monkeypatch):
    from buddy.app.main import app

    seed()
    ids = [logs.log_question(question=f"Question {i}", subject="evs", route="local",
                             reason="default", hint="h", answer=f"Answer {i}") for i in range(5)]
    logs.log_question(question="[quiz] evs chapter 1", subject="evs", route="claude_haiku",
                      reason="quiz", answer="")
    logs.log_question(question="broken", subject="evs", route="error", reason="server_error",
                      answer="Oops")
    c = TestClient(app)
    assert c.get("/api/history").status_code == 401
    c.post("/login", data={"password": "kid"}, follow_redirects=False)
    page = c.get("/api/history", params={"limit": 3}).json()
    assert [r["question"] for r in page["items"]] == ["Question 4", "Question 3", "Question 2"]
    assert page["more"]
    older = c.get("/api/history", params={"limit": 3, "before_id": ids[2]}).json()
    assert [r["question"] for r in older["items"]] == ["Question 1", "Question 0"]
    assert not older["more"]
