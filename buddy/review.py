"""Nightly review: Claude re-checks weak answers and auto-applies fixes (undo in parent view).

Candidates: answers marked 👎, answers where the local model was overruled, low retrieval
scores and "not in your book" replies that have not been reviewed yet. For each one,
Sonnet 5 gets the question, Buddy's answer and a wider slice of the book than the chat had,
and returns a verdict: fixed / correct / not_in_book. Fixes whose citation doesn't match a
passage it was shown are rejected. Accepted ones become "verified" answers.

Runs inside the server (see app/main.py) so new fixes are searchable at once; also
`python -m buddy.review run|list` by hand.
"""
import argparse
import json
import re
import threading
import time

from buddy import fixes, logs
from buddy.config import INGEST_MODEL, cost_usd, get_settings
from buddy.kid_rules import NOT_IN_BOOK, RULES, format_passages
from buddy.rag import store
from buddy.rag.store import Hit

REVIEW_MODEL = INGEST_MODEL  # Sonnet 5
FALLBACK_REASONS = ("local_not_confident", "local_no_page", "local_page_not_retrieved",
                    "local_empty")
CONTEXT_CHARS = 14_000
EST_OUTPUT_TOKENS = 1_500
_lock = threading.Lock()

REVIEW_SYSTEM = f"""You check answers that Buddy, a study helper, gave to a 9-year-old in \
Class 4 (CBSE, Karnataka). Buddy follows these rules:

{RULES}

You are given the child's question, Buddy's answer, whether the child marked it unhelpful, \
and passages from her textbooks (Classes 1-4) and school papers. Decide:
- "fixed": Buddy's answer was wrong, unhelpful, off-topic, or said "not in your book" when \
the passages do answer it. Write a better hint and answer following the rules.
- "correct": Buddy's answer was right. Return it cleaned up (hint + answer) as the model answer.
- "not_in_book": the passages really don't answer the question.
source must be copied exactly from the cite of the passage the answer comes from \
("" for not_in_book). note says in one sentence what was wrong or why it was right."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["fixed", "correct", "not_in_book"]},
        "hint": {"type": "string"},
        "answer": {"type": "string"},
        "source": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["verdict", "hint", "answer", "source", "note"],
    "additionalProperties": False,
}


def candidates(limit: int) -> list[dict]:
    thr = get_settings().low_score_threshold
    marks = ",".join("?" for _ in FALLBACK_REASONS)
    return logs._rows(
        f"""SELECT * FROM questions
            WHERE COALESCE(reviewed, 0) = 0 AND fix_id IS NULL
              AND route NOT IN ('error', 'verified') AND question NOT LIKE '[quiz]%'
              AND COALESCE(feedback, 0) != 1
              AND (feedback = -1 OR reason IN ({marks}) OR top_score < ?
                   OR answer LIKE '%not in your book%')
            ORDER BY COALESCE(feedback, 0) ASC, id DESC LIMIT ?""",
        (*FALLBACK_REASONS, thr, limit))


def build_context(q: dict) -> list[Hit]:
    subject = q["subject"] or None
    hits = store.search(q["question"], subject, k=10)
    top = next((h for h in hits if h.meta.get("source") == "ncert"), None)
    if top:
        extra = store.get_by({"$and": [{"book": top.meta.get("book", "")},
                                       {"chapter": top.meta["chapter"]},
                                       {"kind": {"$in": ["explain", "qa", "diagram"]}}]}, 40)
        seen = {h.id for h in hits}
        hits += [h for h in extra if h.id not in seen]
    out, size = [], 0
    for h in hits:
        if size + len(h.text) > CONTEXT_CHARS:
            break
        out.append(h)
        size += len(h.text)
    return out


def request_params(q: dict, hits: list[Hit]) -> dict:
    flag = "The child marked this answer as NOT helpful (👎)." if q["feedback"] == -1 else ""
    old = f"HINT: {q['hint'] or ''}\nANSWER: {q['answer'] or ''}\nSOURCE: {q['source'] or ''}"
    return {
        "model": REVIEW_MODEL,
        "max_tokens": 8000,
        "system": REVIEW_SYSTEM,
        "messages": [{"role": "user", "content":
                      f"Passages:\n{format_passages(hits)}\n\n"
                      f"Child's question: {q['question']}\n\n"
                      f"Buddy's answer (path: {q['route']}, {q['reason']}):\n{old}\n{flag}"}],
        "output_config": {"effort": "medium",
                          "format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
    }


def estimate(params: dict, batch: bool) -> float:
    in_tokens = len(json.dumps(params, ensure_ascii=False)) / 3.2
    return cost_usd(REVIEW_MODEL, int(in_tokens), EST_OUTPUT_TOKENS, batch=batch)


def validate(data: dict, hits: list[Hit]) -> tuple[str, str | None]:
    """(source to store, None) or ("", reason for rejecting)."""
    from buddy.router import cite_for_pages, valid_cites

    if data["verdict"] == "not_in_book":
        return "", None
    if not data["answer"].strip() or NOT_IN_BOOK.split(".")[0] in data["answer"]:
        return "", "empty or contradictory answer"
    if data["source"] in valid_cites(hits):
        return data["source"], None
    m = re.search(r"page\s+(\d+)", data["source"])
    cite = cite_for_pages(hits, [int(m.group(1))]) if m else None
    return (cite, None) if cite else ("", f"source {data['source']!r} not in passages")


def apply(q: dict, hits: list[Hit], data: dict, run_id: int) -> str:
    source, why = validate(data, hits)
    if why:
        logs.update_question(q["id"], reviewed=1, review_verdict=f"rejected: {why}"[:200])
        return "rejected"
    fixes.apply_fix(question=q["question"], answer=data["answer"], hint=data["hint"],
                    source=source, verdict=data["verdict"], origin="review",
                    subject=q["subject"], question_id=q["id"], note=data["note"],
                    run_id=run_id)
    return data["verdict"]


def _message_json(msg) -> dict | None:
    if msg.stop_reason != "end_turn":
        return None
    text = next((b.text for b in msg.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def run(mode: str = "batch", client=None, poll_seconds: int = 60,
        max_wait_s: int = 12 * 3600) -> dict:
    """Review pending answers. mode: "batch" (half price, minutes to hours) or "direct"."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        return _run(mode, client, poll_seconds, max_wait_s)
    finally:
        _lock.release()


def _run(mode, client, poll_seconds, max_wait_s) -> dict:
    s = get_settings()
    if client is None:
        from buddy.llm.claude import sync_client
        client = sync_client()
    run_id = logs.start_run(mode)
    counts = {"fixed": 0, "correct": 0, "not_in_book": 0, "rejected": 0}
    batch = mode == "batch"

    # Pick work within the budget.
    work, planned = [], 0.0
    for q in candidates(s.review_max_items):
        covered = store.search(q["question"], q["subject"] or None, 1, ["verified"])
        if covered and covered[0].score >= s.verified_threshold:
            logs.update_question(q["id"], reviewed=1, review_verdict="covered")
            continue
        hits = build_context(q)
        params = request_params(q, hits)
        est = estimate(params, batch)
        if planned + est > s.review_budget_usd:
            break
        planned += est
        work.append((q, hits, params))
    if not work:
        logs.finish_run(run_id, status="nothing_to_do")
        return {"status": "nothing_to_do", "run_id": run_id}

    cost = 0.0
    results: dict[int, dict | None] = {}
    try:
        if batch:
            from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
            from anthropic.types.messages.batch_create_params import Request

            b = client.messages.batches.create(requests=[
                Request(custom_id=f"q{q['id']}", params=MessageCreateParamsNonStreaming(**p))
                for q, _, p in work])
            logs.finish_run(run_id, status="running", finished=None, batch_id=b.id,
                            items=len(work))
            deadline = time.time() + max_wait_s
            while client.messages.batches.retrieve(b.id).processing_status != "ended":
                if time.time() > deadline:
                    raise TimeoutError(f"batch {b.id} not finished after {max_wait_s}s")
                time.sleep(poll_seconds)
            for res in client.messages.batches.results(b.id):
                qid = int(res.custom_id[1:])
                if res.result.type == "succeeded":
                    msg = res.result.message
                    cost += cost_usd(REVIEW_MODEL, msg.usage.input_tokens,
                                     msg.usage.output_tokens, batch=True)
                    results[qid] = _message_json(msg)
        else:
            for q, _, p in work:
                msg = client.messages.create(**p)
                cost += cost_usd(REVIEW_MODEL, msg.usage.input_tokens, msg.usage.output_tokens)
                results[q["id"]] = _message_json(msg)
    except Exception as e:
        logs.finish_run(run_id, status="failed", note=f"{type(e).__name__}: {e}"[:500],
                        cost_usd=round(cost, 4))
        raise

    for q, hits, _ in work:
        if q["id"] not in results:
            continue  # errored/expired: stays pending for the next run
        data = results[q["id"]]
        if data is None:
            logs.update_question(q["id"], reviewed=1, review_verdict="rejected: no answer")
            counts["rejected"] += 1
            continue
        counts[apply(q, hits, data, run_id)] += 1
    logs.finish_run(run_id, status="done", items=len(work), cost_usd=round(cost, 4), **counts)
    return {"status": "done", "run_id": run_id, "items": len(work),
            "cost_usd": round(cost, 4), **counts}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m buddy.review")
    p.add_argument("cmd", choices=["run", "list"])
    p.add_argument("--direct", action="store_true",
                   help="use the normal API (full price, immediate) instead of a batch")
    a = p.parse_args(argv)
    if a.cmd == "list":
        for q in candidates(get_settings().review_max_items):
            print(f"#{q['id']} [{q['route']}/{q['reason']}] fb={q['feedback']} {q['question']}")
        return
    print(json.dumps(run("direct" if a.direct else "batch"), indent=2))
    print("Note: a server that is already running picks the fixes up automatically.")


if __name__ == "__main__":
    main()
