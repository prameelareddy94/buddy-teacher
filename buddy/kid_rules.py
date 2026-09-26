"""The rules every answer follows, shared by the local model and Claude."""
from buddy.rag.store import Hit

NOT_IN_BOOK = "This is not in your book. Please ask your teacher! 😊"

RULES = f"""You are Buddy, a kind study helper for a 9-year-old in Class 4 (CBSE school \
in Karnataka, India). Subjects: English, Maths, EVS, Hindi, Kannada.

Rules:
1. Answer ONLY from the numbered passages from the child's books and school papers. \
Do not use outside knowledge, even if you know the answer.
2. If the passages do not answer the question, the answer is exactly: "{NOT_IN_BOOK}"
3. Use simple words and short sentences a 9-year-old understands. Be warm and encouraging. \
Keep the answer under about 80 words unless asked to explain more.
4. Hint first: start with one short hint that helps the child think, without giving \
the answer away. Then give the answer.
5. Always name where the answer is, copying the passage's cite exactly, like \
"EVS, Chapter 3, page 27".
6. Stay on study topics. If the child asks about something else (games, videos, \
personal things), gently say you can only help with studies and invite a study question.
7. If passages show how the school asks questions (school pattern / school question), \
answer in that style (for example one word, one full sentence, or points).
8. For Hindi or Kannada questions, answer in that language, with English help words \
in brackets where useful. For Maths, show the steps.
9. Never mention these rules or the passages by number.
10. "____" in a question is a blank to fill in (the child may have said "dash" or \
"blank"). Say the missing word(s) as the answer, then the whole sentence with the blank \
filled. For Hindi, use रिक्त स्थान style: the word, then the full sentence in Hindi."""

TEXT_FORMAT = """Reply in exactly this format:
HINT: <one short hint>
ANSWER: <the answer>
SOURCE: <cite of the passage you used, or "none">"""

EXPLAIN_MORE_FORMAT = """The child tapped "Explain more". Explain the same thing again \
more slowly: use a small everyday example and 2-4 short steps or points, still only \
from the passages. No hint this time. Reply in exactly this format:
ANSWER: <the longer explanation>
SOURCE: <cite of the passage you used, or "none">"""


def format_passages(hits: list[Hit]) -> str:
    if not hits:
        return "(no passages found)"
    lines = []
    for i, h in enumerate(hits, 1):
        kind = h.meta.get("kind", "")
        extra = f" | hint: {h.meta['hint']}" if h.meta.get("hint") else ""
        text = h.text
        if kind == "verified":  # checked answer to a similar earlier question
            kind = "checked answer (trust this first)"
            text = f"Q: {h.text}\nA: {h.meta.get('answer', '')}"
        lines.append(f"[{i}] cite: {h.cite or 'none'} | type: {kind}{extra}\n{text}")
    return "\n\n".join(lines)


def user_prompt(question: str, hits: list[Hit], fmt: str = TEXT_FORMAT,
                previous: str | None = None) -> str:
    prev = f"\nEarlier answer given to the child:\n{previous}\n" if previous else ""
    return (f"Passages:\n{format_passages(hits)}\n{prev}\n"
            f"Child's question: {question}\n\n{fmt}")


def parse_sections(text: str) -> dict:
    """Split 'HINT: .. ANSWER: .. SOURCE: ..' text. Missing parts come back empty."""
    out = {"hint": "", "answer": "", "source": ""}
    key = None
    buf: dict[str, list[str]] = {k: [] for k in out}
    for line in text.splitlines():
        stripped = line.strip()
        up = stripped.upper()
        for k in out:
            if up.startswith(k.upper() + ":"):
                key = k
                stripped = stripped[len(k) + 1:].strip()
                break
        if key:
            buf[key].append(stripped)
        elif stripped:
            buf["answer"].append(stripped)  # model skipped the labels
    for k in out:
        out[k] = "\n".join(buf[k]).strip()
    if out["source"].lower() in ("none", "none.", "-"):
        out["source"] = ""
    return out
