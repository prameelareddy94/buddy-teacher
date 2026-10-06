"""Practice quiz for a chapter: questions she can answer (tap an option, or type/say a
word or sentence), checked with friendly feedback, and shaped like the school's papers
when school worksheets have been uploaded."""
import json
import random
import re

import anthropic

from buddy import logs
from buddy.books import BOOKS
from buddy.config import ESCALATION_MODEL, cost_usd
from buddy.llm.claude import async_client
from buddy.rag import store

KINDS = ["mcq", "true_false", "fill_blank", "short"]

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": KINDS},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "answer": {"type": "string"},
                    "hint": {"type": "string"},
                    "explanation": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["kind", "question", "options", "answer", "hint", "explanation",
                             "source"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["yes", "partly", "no"]},
        "feedback": {"type": "string"},
    },
    "required": ["verdict", "feedback"],
    "additionalProperties": False,
}

QUIZ_SYSTEM = """You write practice quizzes for a 9-year-old in Class 4 (CBSE, Karnataka). \
Use only facts from the given question bank. Keep the language simple and friendly. \
Write in the book's language (Hindi books in Hindi, Kannada books in Kannada)."""

JUDGE_SYSTEM = """You check a 9-year-old's answer to a quiz question from her school book. \
Her answer may have come from speech recognition or have spelling mistakes: judge the \
meaning, not the spelling. "yes" = correct (same meaning is fine); "partly" = has the main \
idea but misses something important; "no" = wrong or unrelated. feedback: one short, warm \
sentence for her. If the verdict is not "yes", do not give away the answer."""


def _parse_qa(text: str) -> tuple[str, str]:
    q, _, a = text.split("\n", 1)[-1].partition("\nA: ")
    return q.split("): ", 1)[-1], a


def _norm(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    s = re.sub(r"\b(a|an|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _clean(q: dict) -> dict | None:
    """Make a generated question safe to show: options present, answer among them."""
    kind = q["kind"] if q["kind"] in KINDS else "short"
    opts = [o.strip() for o in q.get("options", []) if o.strip()]
    if kind == "true_false":
        opts = ["True", "False"]
        q["answer"] = "True" if _norm(q["answer"]).startswith(("true", "yes", "सही")) else "False"
    elif kind == "mcq":
        match = next((o for o in opts if _norm(o) == _norm(q["answer"])), None)
        if not match or len(opts) < 2:
            return None
        q["answer"] = match
        random.shuffle(opts)
    else:
        opts = []
    if not q["question"].strip() or not q["answer"].strip():
        return None
    return {**q, "kind": kind, "options": opts}


def _from_bank(picked: list, n: int) -> list[dict]:
    """No Claude available: the book's own Q&A, typed answers (true/false as buttons)."""
    out = []
    for h in picked[:n]:
        q, a = _parse_qa(h.text)
        kind = h.meta.get("qa_kind", "")
        kind = kind if kind in ("true_false", "fill_blank") else "short"
        item = _clean({"kind": kind, "question": q, "options": [], "answer": a,
                       "hint": h.meta.get("hint", ""), "explanation": "", "source": h.cite})
        if item:
            out.append(item)
    return out


async def make_quiz(book: str, chapter: int, n: int = 5) -> list[dict]:
    subject = BOOKS[book].subject
    bank = store.get_by({"$and": [{"book": book}, {"chapter": chapter}, {"kind": "qa"}]},
                        limit=200)
    if not bank:
        return []
    patterns = store.get_by({"$and": [{"subject": subject}, {"kind": "pattern"}]}, limit=3)
    school_qs = store.get_by({"$and": [{"subject": subject}, {"kind": "school_qa"}]}, limit=15)
    picked = random.sample(bank, min(len(bank), n * 2))

    bank_txt = "\n\n".join(f"[{h.cite}] {h.text.split(chr(10), 1)[-1]} (hint: "
                           f"{h.meta.get('hint', '')})" for h in picked)
    style = "\n".join(p.text for p in patterns) or "(not known)"
    examples = "\n".join(h.text for h in school_qs) or "(none)"
    try:
        msg = await async_client().messages.create(
            model=ESCALATION_MODEL,
            max_tokens=4000,
            system=QUIZ_SYSTEM,
            messages=[{"role": "user", "content":
                       f"Question bank from her textbook:\n{bank_txt}\n\n"
                       f"How her school sets papers:\n{style}\n\n"
                       f"Real school questions for reference:\n{examples}\n\n"
                       f"Make {n} questions she can answer on a tablet. Mix the kinds: mostly "
                       "\"mcq\" (3 or 4 short options, exactly one correct, the others "
                       "believable) and \"true_false\", plus one \"fill_blank\" (question has "
                       "____, answer is the missing word or words) and at most one \"short\" "
                       "(answer in one sentence). answer must be the correct option's exact "
                       "text for mcq, True or False for true_false. hint helps her think "
                       "without giving the answer away. explanation is one simple sentence "
                       "saying why the answer is right. source is the [cite] of the bank item "
                       "used, copied exactly. Follow the school's style where it is known."}],
            output_config={"format": {"type": "json_schema", "schema": QUIZ_SCHEMA}},
        )
    except (anthropic.APIError, RuntimeError):  # no key / offline: the book's own Q&A
        return _from_bank(picked, n)
    data = json.loads(next(b.text for b in msg.content if b.type == "text"))
    logs.log_question(question=f"[quiz] {book} chapter {chapter}", subject=subject,
                      route="claude_haiku", reason="quiz", model=ESCALATION_MODEL,
                      input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
                      cost_usd=cost_usd(ESCALATION_MODEL, msg.usage.input_tokens,
                                        msg.usage.output_tokens))
    out = [q for q in (_clean(q) for q in data["questions"]) if q]
    return out[:n] or _from_bank(picked, n)


async def check_answer(kind: str, question: str, expected: str, given: str) -> dict:
    """{"verdict": yes|partly|no, "feedback": "..."}"""
    given = given.strip()
    if not given:
        return {"verdict": "no", "feedback": "Have a go! Type or say your answer."}
    if kind in ("mcq", "true_false"):
        ok = _norm(given) == _norm(expected)
        return {"verdict": "yes" if ok else "no",
                "feedback": "Great job!" if ok else "Not quite. Have another look!"}
    g, e = _norm(given), _norm(expected)
    if g == e or (len(e.split()) <= 3 and e and re.search(rf"\b{re.escape(e)}\b", g)):
        return {"verdict": "yes", "feedback": "Great job!"}
    try:
        msg = await async_client().messages.create(
            model=ESCALATION_MODEL, max_tokens=300, system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content":
                       f"Question: {question}\nCorrect answer: {expected}\n"
                       f"Her answer: {given}"}],
            output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        )
        return json.loads(next(b.text for b in msg.content if b.type == "text"))
    except (anthropic.APIError, RuntimeError):
        return {"verdict": "no", "feedback": "Hmm, let's check it together."}
