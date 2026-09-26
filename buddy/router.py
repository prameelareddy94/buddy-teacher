"""Decide who answers, get the answer, log the path.

Before anything else: a fixed ("verified") answer that closely matches the question
is returned directly (route "verified", no model call).
Rules first (cheap, predictable):
  photo attached           -> Claude Sonnet 5 (vision)
  "Explain more" button    -> Claude Haiku 4.5
  Kannada question         -> Claude Haiku 4.5 (small local models are weak at Kannada)
  like an unfixed 👎 one   -> Claude Haiku 4.5
  multi-topic why/how      -> Claude Haiku 4.5
  low retrieval score      -> Claude Haiku 4.5
Otherwise the local model answers in ONE call returning JSON; if it is not confident
or cites no page from the retrieved passages, the question is re-asked to Claude.
"""
import asyncio
import base64
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import anthropic

from buddy.books import SUBJECTS, citation
from buddy.config import ESCALATION_MODEL, VISION_MODEL, cost_usd, get_settings
from buddy.kid_rules import (EXPLAIN_MORE_FORMAT, NOT_IN_BOOK, RULES, TEXT_FORMAT,
                             parse_sections, user_prompt)
from buddy.llm import ollama
from buddy.llm.claude import async_client
from buddy.logs import get_question, log_question
from buddy.rag import store
from buddy.rag.store import Hit

KANNADA = re.compile(r"[ಀ-೿]")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
BLANK = "____"
# Typed blanks (___, ---, ..., …) and spoken ones ("dash", "blank", खाली, रिक्त स्थान, ಖಾಲಿ).
_BLANK_WORD = (r"(?:dash|blank|khaali|khali|डैश|ब्लैंक|खाली\s*स्थान|रिक्त\s*स्थान|खाली"
               r"|ಖಾಲಿ\s*ಜಾಗ|ಖಾಲಿ)")
_WORDCHAR = r"[\wऀ-෿]"
_BLANK_RE = re.compile(
    rf"_{{2,}}|-{{2,}}|\.{{3,}}|…+"
    rf"|(?<!{_WORDCHAR}){_BLANK_WORD}(?:\s+{_BLANK_WORD})*(?!{_WORDCHAR})",
    re.IGNORECASE)
_FILL_PREFIX = re.compile(r"^\s*(fill\s+in\s+the\s+____s?|fill\s+in\s+the\s+blanks?|"
                          r"____\s*भरो|रिक्त\s*स्थान\s*भरो)\s*[:\-]?\s*", re.IGNORECASE)


def normalize_blanks(question: str) -> tuple[str, str]:
    """(question with every blank written as ____, text to search the books with).

    A child may type ___ or say "dash"/"blank"/"खाली"; speech recognition writes those
    as words, which would otherwise be searched for and answered literally."""
    q = _BLANK_RE.sub(BLANK, question)
    q = re.sub(r"(?:\s*____\s*){2,}", f" {BLANK} ", q)
    q = re.sub(r"\s+", " ", q).strip()
    if BLANK not in q:
        return question, question
    search = _FILL_PREFIX.sub("", q)
    search = re.sub(r"\s+", " ", search.replace(BLANK, " ")).strip()
    return q, search or q
WHY_HOW = re.compile(r"\b(why|how)\b|क्यों|कैसे|ಏಕೆ|ಯಾಕೆ|ಹೇಗೆ", re.IGNORECASE)
MULTI_TOPIC_BAND = 0.05  # topics scoring within this of the best count as "equally relevant"

LOCAL, HAIKU, SONNET, VERIFIED = "local", "claude_haiku", "claude_sonnet", "verified"


@dataclass
class Ask:
    question: str
    subject: str | None = None          # None = all subjects
    image: bytes | None = None
    image_type: str = "image/jpeg"
    explain_more_of: int | None = None  # log id of the answer to expand
    via: str = "typed"                  # typed | voice


@dataclass
class Decision:
    route: str
    reason: str


@dataclass
class Retrieval:
    hits: list[Hit]                 # verified fixes (if close) first, then book passages
    verified: list[Hit] = field(default_factory=list)
    flagged: Hit | None = None      # a similar question marked 👎 and not fixed yet


def decide(ask: Ask, hits: list[Hit], threshold: float | None = None,
           similar_flagged: bool = False) -> Decision:
    thr = get_settings().low_score_threshold if threshold is None else threshold
    if ask.image:
        return Decision(SONNET, "photo")
    if ask.explain_more_of:
        return Decision(HAIKU, "explain_more")
    if ask.subject == "kannada" or KANNADA.search(ask.question):
        return Decision(HAIKU, "kannada")
    if ask.subject == "hindi" or DEVANAGARI.search(ask.question):
        return Decision(HAIKU, "hindi")  # small local models are weak at Hindi too
    if similar_flagged:
        return Decision(HAIKU, "similar_flagged")
    book_hits = [h for h in hits if h.meta.get("kind") != "pattern"]
    top = max((h.score for h in book_hits), default=0.0)
    if top < thr:
        return Decision(HAIKU, "low_retrieval")
    if WHY_HOW.search(ask.question):
        near = {(h.meta.get("subject"), h.meta.get("chapter"), h.meta.get("topic"))
                for h in book_hits if h.score >= max(thr, top - MULTI_TOPIC_BAND)}
        if len(near) >= 2:
            return Decision(HAIKU, "multi_topic_why_how")
    s = get_settings()
    if s.answer_mode == "claude_only":
        return Decision(HAIKU, "claude_only")
    if top < s.local_min_score:
        return Decision(HAIKU, "weak_match")
    return Decision(LOCAL, "default")


def _pages(hit: Hit) -> set[int]:
    return {int(p) for p in str(hit.meta.get("pages", "")).split(",") if p.strip().isdigit()}


def cite_for_pages(hits: list[Hit], pages: list[int]) -> str | None:
    """Citation for the first retrieved passage that contains a cited page, else None."""
    for page in pages:
        for h in hits:
            if h.meta.get("kind") != "pattern" and page in _pages(h):
                m = h.meta
                if m.get("source") == "ncert":
                    return citation(m.get("book", m["subject"]), m["chapter"], page)
                return h.cite
    return None


def check_local(data: dict, hits: list[Hit]) -> tuple[str | None, str | None]:
    """(citation, None) if the local answer is usable, else (None, fallback reason)."""
    if not data.get("confident"):
        return None, "local_not_confident"
    if not str(data.get("answer", "")).strip():
        return None, "local_empty"
    pages = [p for p in data.get("pages") or [] if isinstance(p, int)]
    if not pages:
        return None, "local_no_page"
    cite = cite_for_pages(hits, pages)
    if not cite:
        return None, "local_page_not_retrieved"
    return cite, None


def valid_cites(hits: list[Hit]) -> set[str]:
    return {h.cite for h in hits if h.cite and h.meta.get("kind") != "pattern"}


def finalize_source(parsed: dict, hits: list[Hit]) -> dict:
    """Make sure a real citation is shown unless the answer is 'not in your book'."""
    if NOT_IN_BOOK.split(".")[0].lower() in parsed["answer"].lower():
        parsed["source"] = ""
        return parsed
    cites = valid_cites(hits)
    if parsed["source"] not in cites:
        # Accept a close match (model may change the page within the chunk), else top hit.
        m = re.search(r"page\s+(\d+)", parsed["source"] or "")
        fixed = cite_for_pages(hits, [int(m.group(1))]) if m else None
        book_hits = [h for h in hits if h.meta.get("kind") != "pattern"]
        parsed["source"] = fixed or (book_hits[0].cite if book_hits else "")
    return parsed


async def _retrieve(query: str, subject: str | None) -> Retrieval:
    s = get_settings()
    hits = await asyncio.to_thread(store.search, query, subject)
    verified = [h for h in await asyncio.to_thread(store.search, query, subject, 2, ["verified"])
                if h.score >= s.verified_threshold]
    flagged = [h for h in await asyncio.to_thread(store.search, query, subject, 1, ["flagged"])
               if h.score >= s.verified_threshold]
    subj = subject or next((h.meta.get("subject") for h in verified + hits), None)
    if subj in SUBJECTS:
        patterns = await asyncio.to_thread(
            store.get_by, {"$and": [{"subject": subj}, {"kind": "pattern"}]}, 2)
        seen = {h.id for h in hits}
        hits += [p for p in patterns if p.id not in seen]
    return Retrieval(verified + hits, verified, None if verified else (flagged or [None])[0])


async def _transcribe(ask: Ask) -> tuple[str, object]:
    """Read the text in a photo so we can search the books for it."""
    msg = await async_client().messages.create(
        model=ESCALATION_MODEL,
        max_tokens=800,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": ask.image_type,
                                         "data": base64.standard_b64encode(ask.image).decode()}},
            {"type": "text", "text": "Copy out the question or text in this photo exactly, "
                                     "in its original language. If there is no text, describe "
                                     "the picture in one sentence. Output only that."},
        ]}],
    )
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return text, msg.usage


async def answer(ask: Ask) -> AsyncIterator[dict]:
    """Yields events: meta, delta (text chunks), done."""
    t0 = time.monotonic()
    usage_in = usage_out = 0
    cost = 0.0
    previous = None
    question = ask.question.strip()

    if ask.explain_more_of:
        prev = get_question(ask.explain_more_of)
        if prev:
            question = question or prev["question"]
            ask.subject = ask.subject or prev["subject"]
            previous = f"{prev['hint']}\n{prev['answer']}".strip()

    question, query = normalize_blanks(question)
    ask.question = question
    if ask.image:
        seen, u = await _transcribe(ask)
        usage_in += u.input_tokens
        usage_out += u.output_tokens
        cost += cost_usd(ESCALATION_MODEL, u.input_tokens, u.output_tokens)
        query = f"{question}\n{seen}".strip()

    r = await _retrieve(query, ask.subject)
    hits = r.hits
    top = max((h.score for h in hits if h.meta.get("kind") != "pattern"), default=0.0)

    best = r.verified[0] if r.verified else None
    if (best and best.score >= get_settings().verified_direct and not ask.image
            and not ask.explain_more_of):
        m = best.meta
        parsed = {"hint": m.get("hint", ""), "answer": m.get("answer", ""), "source": best.cite}
        yield {"type": "meta", "route": VERIFIED, "reason": "verified_match"}
        text = f"HINT: {parsed['hint']}\nANSWER: {parsed['answer']}\nSOURCE: {best.cite}"
        yield {"type": "delta", "text": text}
        qid = log_question(
            question=question, subject=ask.subject, route=VERIFIED, reason="verified_match",
            model=f"fix #{m.get('fix_id')}", top_score=best.score, via=ask.via, had_image=0,
            latency_ms=int((time.monotonic() - t0) * 1000), input_tokens=0,
            output_tokens=0, cost_usd=0.0, **parsed)
        yield {"type": "done", "id": qid, "route": VERIFIED, "reason": "verified_match", **parsed}
        return

    decision = decide(ask, hits, similar_flagged=r.flagged is not None)
    yield {"type": "meta", "route": decision.route, "reason": decision.reason}

    local_attempt = None
    if decision.route == LOCAL:
        try:
            data, u = await ollama.ask_local(
                RULES, user_prompt(question, hits, ollama.LOCAL_FORMAT))
            cite, why_not = check_local(data, hits)
        except Exception as e:  # Ollama down, timeout, bad JSON
            data, cite, why_not = {}, None, f"local_error: {type(e).__name__}"
        if cite:
            parsed = {"hint": data.get("hint", "").strip(), "answer": data["answer"].strip(),
                      "source": cite}
            text = f"HINT: {parsed['hint']}\nANSWER: {parsed['answer']}\nSOURCE: {cite}"
            for i in range(0, len(text), 40):  # already complete; send in small pieces
                yield {"type": "delta", "text": text[i:i + 40]}
            qid = log_question(
                question=question, subject=ask.subject, route=LOCAL, reason="default",
                model=get_settings().ollama_model, top_score=top, via=ask.via, had_image=0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                cost_usd=0.0, **parsed)
            yield {"type": "done", "id": qid, "route": LOCAL, "reason": "default", **parsed}
            return
        local_attempt = str(data)[:1000] if data else None
        decision = Decision(HAIKU, why_not)
        yield {"type": "meta", "route": decision.route, "reason": decision.reason}

    # Claude path (streamed)
    model = VISION_MODEL if decision.route == SONNET else ESCALATION_MODEL
    fmt = EXPLAIN_MORE_FORMAT if ask.explain_more_of else TEXT_FORMAT
    prompt = user_prompt(question or "Please help me with this.", hits, fmt, previous)
    if ask.image:
        content = [{"type": "image", "source": {
            "type": "base64", "media_type": ask.image_type,
            "data": base64.standard_b64encode(ask.image).decode()}},
            {"type": "text", "text": "The child sent this photo.\n\n" + prompt}]
    else:
        content = prompt
    params = {"model": model, "max_tokens": 4000, "system": RULES,
              "messages": [{"role": "user", "content": content}]}
    if model == VISION_MODEL:
        params["output_config"] = {"effort": "low"}  # quick replies for a waiting child

    full = ""
    failed = False
    try:
        async with async_client().messages.stream(**params) as stream:
            async for chunk in stream.text_stream:
                full += chunk
                yield {"type": "delta", "text": chunk}
            final = await stream.get_final_message()
        if final.stop_reason == "refusal":
            full = f"ANSWER: {NOT_IN_BOOK}"
        usage_in += final.usage.input_tokens
        usage_out += final.usage.output_tokens
        cost += cost_usd(model, final.usage.input_tokens, final.usage.output_tokens)
    except anthropic.APIError as e:
        full = ("ANSWER: Oops, Buddy could not reach its helper right now. "
                "Please try again in a minute!")
        decision = Decision(decision.route, f"{decision.reason}; api_error: {type(e).__name__}")
        failed = True

    parsed = parse_sections(full)
    parsed = {**parsed, "source": ""} if failed else finalize_source(parsed, hits)
    qid = log_question(
        question=question, subject=ask.subject, route=decision.route, reason=decision.reason,
        model=model, top_score=top, via=ask.via, had_image=int(bool(ask.image)),
        latency_ms=int((time.monotonic() - t0) * 1000), input_tokens=usage_in,
        output_tokens=usage_out, cost_usd=round(cost, 6), local_attempt=local_attempt, **parsed)
    yield {"type": "done", "id": qid, "route": decision.route, "reason": decision.reason, **parsed}
