import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from buddy.app import quiz
from tests.test_router import seed


class FakeHaiku:
    def __init__(self, payload):
        self.payload, self.messages, self.calls = payload, self, []

    async def create(self, **params):
        self.calls.append(params)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
                               usage=SimpleNamespace(input_tokens=2000, output_tokens=400))


GENERATED = {"questions": [
    {"kind": "mcq", "question": "What do plants need to make food?",
     "options": ["Sunlight, water and air", "Only soil", "Milk"], "answer": "sunlight, water and air",
     "hint": "Look up!", "explanation": "Plants use sunlight, water and air.", "source": "EVS, Chapter 1, page 5"},
    {"kind": "mcq", "question": "Broken one", "options": ["A", "B"], "answer": "C",
     "hint": "", "explanation": "", "source": ""},
    {"kind": "true_false", "question": "Trees give homes to birds.", "options": [],
     "answer": "true", "hint": "Nests!", "explanation": "Birds build nests in trees.", "source": "x"},
    {"kind": "fill_blank", "question": "A young tree is called a ____.", "options": ["x"],
     "answer": "sapling", "hint": "s…", "explanation": "", "source": "x"},
]}


def test_generated_quiz_is_cleaned(monkeypatch):
    seed()
    fake = FakeHaiku(GENERATED)
    monkeypatch.setattr(quiz, "async_client", lambda: fake)
    import asyncio
    qs = asyncio.run(quiz.make_quiz("evs", 1, n=5))
    assert [q["kind"] for q in qs] == ["mcq", "true_false", "fill_blank"]  # broken mcq dropped
    mcq = qs[0]
    assert mcq["answer"] == "Sunlight, water and air" and mcq["answer"] in mcq["options"]
    assert qs[1]["options"] == ["True", "False"] and qs[1]["answer"] == "True"
    assert qs[2]["options"] == []
    assert fake.calls[0]["model"] == "claude-haiku-4-5"


@pytest.mark.parametrize("kind, expected, given, verdict", [
    ("mcq", "Sunlight, water and air", "Sunlight, water and air", "yes"),
    ("mcq", "Sunlight, water and air", "Only soil", "no"),
    ("true_false", "True", "true", "yes"),
    ("fill_blank", "sapling", "Sapling.", "yes"),
    ("fill_blank", "sapling", "it is a sapling", "yes"),
    ("short", "", "", "no"),
])
def test_quick_checks_need_no_model(monkeypatch, kind, expected, given, verdict):
    def boom():
        raise AssertionError("should not call Claude")
    monkeypatch.setattr(quiz, "async_client", boom)
    import asyncio
    assert asyncio.run(quiz.check_answer(kind, "q", expected, given))["verdict"] == verdict


def test_typed_answer_judged_by_meaning(monkeypatch):
    fake = FakeHaiku({"verdict": "yes", "feedback": "Yes! Plants need those to cook food."})
    monkeypatch.setattr(quiz, "async_client", lambda: fake)
    import asyncio
    r = asyncio.run(quiz.check_answer("short", "What do plants need?",
                                      "Sunlight, water and air", "sun light and water and air"))
    assert r["verdict"] == "yes"
    assert "speech recognition" in fake.calls[0]["system"]


def test_answer_endpoint_logs_attempts(monkeypatch):
    from buddy.app.main import app

    seed()
    kid, parent = TestClient(app), TestClient(app)
    kid.post("/login", data={"password": "kid"}, follow_redirects=False)
    parent.post("/login", data={"password": "parent"}, follow_redirects=False)
    base = {"book": "evs", "chapter": "1", "kind": "mcq", "question": "What do plants need?",
            "expected": "Sunlight, water and air"}
    assert kid.post("/api/quiz/answer", data={**base, "given": "Only soil", "try_no": "1"}).json()["verdict"] == "no"
    assert kid.post("/api/quiz/answer", data={**base, "given": "Sunlight, water and air", "try_no": "2"}).json()["verdict"] == "yes"
    res = parent.get("/api/quiz/results").json()
    assert [a["verdict"] for a in res["attempts"]] == ["yes", "no"]
    assert res["chapters"][0]["questions"] == 1 and res["chapters"][0]["first_try"] == 0
    assert kid.get("/api/quiz/results").status_code == 403
