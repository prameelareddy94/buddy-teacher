import asyncio
import json
from types import SimpleNamespace

from buddy import router
from buddy.ingest import batch
from buddy.ingest.index import index_chapter
from buddy.kid_rules import NOT_IN_BOOK, parse_sections
from buddy.rag.store import Hit
from tests.conftest import SAMPLE_RESULT
from tests.test_ingest import fake_batch_client, put_sample_pdf


def hit(score, topic="A", chapter=1, kind="text", pages="5", subject="evs"):
    return Hit(f"id-{topic}-{score}", "text", {
        "subject": subject, "chapter": chapter, "topic": topic, "kind": kind,
        "pages": pages, "source": "ncert", "cite": f"EVS, Chapter {chapter}, page {pages.split(',')[0]}",
    }, score)


def test_rules_order():
    d = router.decide
    good = [hit(0.8)]
    assert d(router.Ask("q", image=b"x"), good).reason == "photo"
    assert d(router.Ask("q", explain_more_of=3), good).reason == "explain_more"
    assert d(router.Ask("ಮರ ಎಂದರೇನು"), good).reason == "kannada"
    assert d(router.Ask("anything", subject="kannada"), good).reason == "kannada"
    assert d(router.Ask("what is a tree"), [hit(0.1)], threshold=0.45).reason == "low_retrieval"
    assert d(router.Ask("what is a tree"), [], threshold=0.45).reason == "low_retrieval"
    multi = [hit(0.8, "A"), hit(0.78, "B", chapter=2)]
    assert d(router.Ask("Why do leaves fall and how do seeds travel?"), multi,
             threshold=0.45).reason == "multi_topic_why_how"
    assert d(router.Ask("what is a sapling"), multi, threshold=0.45).route == router.LOCAL
    assert d(router.Ask("why do we water plants"), [hit(0.8), hit(0.5, "B")],
             threshold=0.45).route == router.LOCAL  # second topic far behind


def test_check_local():
    hits = [hit(0.8, pages="5,6")]
    assert router.check_local({"confident": False, "answer": "x", "pages": [5]}, hits)[1] \
        == "local_not_confident"
    assert router.check_local({"confident": True, "answer": "x", "pages": []}, hits)[1] \
        == "local_no_page"
    assert router.check_local({"confident": True, "answer": "x", "pages": [99]}, hits)[1] \
        == "local_page_not_retrieved"
    cite, why = router.check_local({"confident": True, "answer": "x", "pages": [6]}, hits)
    assert cite == "EVS, Chapter 1, page 6" and why is None


def test_parse_and_finalize():
    p = parse_sections("HINT: think\nof sun\nANSWER: Sunlight.\nSOURCE: EVS, Chapter 1, page 5")
    assert p == {"hint": "think\nof sun", "answer": "Sunlight.", "source": "EVS, Chapter 1, page 5"}
    hits = [hit(0.8, pages="5,6")]
    assert router.finalize_source(parse_sections("ANSWER: x\nSOURCE: made up"), hits)["source"] \
        == "EVS, Chapter 1, page 5"
    assert router.finalize_source(parse_sections("ANSWER: x\nSOURCE: EVS ch1 page 6"),
                                  hits)["source"] == "EVS, Chapter 1, page 6"
    assert router.finalize_source(parse_sections(f"ANSWER: {NOT_IN_BOOK}"), hits)["source"] == ""


# ---------- full answer() flow with fakes ----------

def seed():
    put_sample_pdf()
    batch.collect(fake_batch_client(SAMPLE_RESULT), "b")
    index_chapter("evs", 1)


class FakeStream:
    def __init__(self, text):
        self.text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    async def text_stream(self):
        for i in range(0, len(self.text), 7):
            yield self.text[i:i + 7]

    async def get_final_message(self):
        return SimpleNamespace(stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


class FakeClaude:
    def __init__(self, text):
        self.calls = []
        self.messages = self
        self.text = text

    def stream(self, **params):
        self.calls.append(params)
        return FakeStream(self.text)

    async def create(self, **params):  # photo transcription
        self.calls.append(params)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="What do plants need?")],
                               usage=SimpleNamespace(input_tokens=500, output_tokens=10))


def run(ask):
    async def go():
        return [ev async for ev in router.answer(ask)]
    return asyncio.run(go())


def test_local_path(monkeypatch):
    seed()

    async def fake_local(system, prompt):
        assert "Plants make their own food" in prompt
        return {"hint": "Look up!", "answer": "Sunlight, water and air.", "confident": True,
                "pages": [5]}, {"input_tokens": 900, "output_tokens": 30}

    monkeypatch.setattr(router.ollama, "ask_local", fake_local)
    evs = run(router.Ask("What do plants need to make food?", subject="evs"))
    done = evs[-1]
    assert done["route"] == "local" and done["source"] == "EVS, Chapter 1, page 5"
    assert done["hint"] == "Look up!"
    from buddy.logs import get_question
    assert get_question(done["id"])["route"] == "local"


def test_local_not_confident_falls_back_to_claude(monkeypatch):
    seed()

    async def fake_local(system, prompt):
        return {"hint": "", "answer": "maybe", "confident": False, "pages": []}, {}

    fake = FakeClaude("HINT: Think of the sun.\nANSWER: Sunlight, water and air.\n"
                      "SOURCE: EVS, Chapter 1, page 5")
    monkeypatch.setattr(router.ollama, "ask_local", fake_local)
    monkeypatch.setattr(router, "async_client", lambda: fake)
    evs = run(router.Ask("What do plants need to make food?", subject="evs"))
    metas = [e for e in evs if e["type"] == "meta"]
    assert metas[-1] == {"type": "meta", "route": "claude_haiku", "reason": "local_not_confident"}
    done = evs[-1]
    assert done["answer"] == "Sunlight, water and air."
    assert fake.calls[0]["model"] == "claude-haiku-4-5"
    from buddy.logs import get_question
    row = get_question(done["id"])
    assert row["reason"] == "local_not_confident" and row["cost_usd"] > 0


def test_ollama_down_falls_back(monkeypatch):
    seed()

    async def boom(system, prompt):
        raise ConnectionError("no ollama")

    monkeypatch.setattr(router.ollama, "ask_local", boom)
    monkeypatch.setattr(router, "async_client", lambda: FakeClaude("ANSWER: ok\nSOURCE: none"))
    done = run(router.Ask("What do plants need to make food?", subject="evs"))[-1]
    assert done["reason"].startswith("local_error") and done["source"].startswith("EVS")


def test_photo_uses_sonnet(monkeypatch):
    seed()
    fake = FakeClaude("HINT: h\nANSWER: a\nSOURCE: EVS, Chapter 1, page 5")
    monkeypatch.setattr(router, "async_client", lambda: fake)
    done = run(router.Ask("", image=b"\x89PNG fake", image_type="image/png"))[-1]
    assert done["route"] == "claude_sonnet" and done["reason"] == "photo"
    assert fake.calls[0]["model"] == "claude-haiku-4-5"      # transcription
    assert fake.calls[1]["model"] == "claude-sonnet-5"       # answer with the image
    assert fake.calls[1]["messages"][0]["content"][0]["type"] == "image"


def test_api_error_gives_friendly_reply_without_citation(monkeypatch):
    import anthropic
    import httpx2

    seed()

    class Failing(FakeClaude):
        def stream(self, **params):
            raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))

    async def boom(system, prompt):
        raise ConnectionError

    monkeypatch.setattr(router.ollama, "ask_local", boom)
    monkeypatch.setattr(router, "async_client", lambda: Failing(""))
    done = run(router.Ask("What do plants need to make food?", subject="evs"))[-1]
    assert "try again" in done["answer"] and done["source"] == ""
    assert "api_error" in done["reason"]
