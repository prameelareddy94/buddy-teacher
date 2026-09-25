"""Practice quiz for a chapter, shaped like the school's question papers when known."""
import json
import random

from buddy.books import BOOKS
from buddy.config import ESCALATION_MODEL, cost_usd
from buddy.llm.claude import async_client
from buddy.logs import log_question
from buddy.rag import store

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "question": {"type": "string"},
                    "hint": {"type": "string"},
                    "answer": {"type": "string"},
                    "source": {"type": "string"},
                },
                "required": ["kind", "question", "hint", "answer", "source"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def _parse_qa(text: str) -> tuple[str, str]:
    q, _, a = text.split("\n", 1)[-1].partition("\nA: ")
    return q.split("): ", 1)[-1], a


async def make_quiz(book: str, chapter: int, n: int = 5) -> list[dict]:
    subject = BOOKS[book].subject
    where = {"$and": [{"book": book}, {"chapter": chapter}, {"kind": "qa"}]}
    bank = store.get_by(where, limit=200)
    if not bank:
        return []
    patterns = store.get_by({"$and": [{"subject": subject}, {"kind": "pattern"}]}, limit=3)
    school_qs = store.get_by({"$and": [{"subject": subject}, {"kind": "school_qa"}]}, limit=15)
    picked = random.sample(bank, min(len(bank), n * 2))

    if not patterns and not school_qs:
        out = []
        for h in picked[:n]:
            q, a = _parse_qa(h.text)
            out.append({"kind": h.meta.get("qa_kind", ""), "question": q,
                        "hint": h.meta.get("hint", ""), "answer": a, "source": h.cite})
        return out

    # Rewrite book questions in the school's style (Haiku; small, cheap call).
    bank_txt = "\n\n".join(f"[{h.cite}] {h.text.split(chr(10), 1)[-1]} (hint: "
                           f"{h.meta.get('hint', '')})" for h in picked)
    style = "\n".join(p.text for p in patterns)
    examples = "\n".join(h.text for h in school_qs)
    msg = await async_client().messages.create(
        model=ESCALATION_MODEL,
        max_tokens=3000,
        system="You write practice quizzes for a 9-year-old in Class 4. Use only facts from "
               "the given question bank. Keep language simple.",
        messages=[{"role": "user", "content":
                   f"Question bank from the textbook:\n{bank_txt}\n\n"
                   f"How the school sets papers:\n{style or '(unknown)'}\n\n"
                   f"Real school questions for reference:\n{examples or '(none)'}\n\n"
                   f"Make {n} questions that look like the school's papers (same kinds, "
                   "wording and answer length), each answerable from the bank. source must be "
                   "the [cite] of the bank item used, copied exactly."}],
        output_config={"format": {"type": "json_schema", "schema": QUIZ_SCHEMA}},
    )
    data = json.loads(next(b.text for b in msg.content if b.type == "text"))
    log_question(question=f"[quiz] {book} chapter {chapter}", subject=subject,
                 route="claude_haiku", reason="quiz_school_pattern", model=ESCALATION_MODEL,
                 input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens,
                 cost_usd=cost_usd(ESCALATION_MODEL, msg.usage.input_tokens,
                                   msg.usage.output_tokens))
    return data["questions"][:n]
