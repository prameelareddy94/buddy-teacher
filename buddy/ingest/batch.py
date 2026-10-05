"""One-time chapter processing with Claude via the Message Batches API (50% price).

Flow: submit() -> (wait, usually minutes, max 24h) -> collect(). State is kept in
data/batches/<batch_id>.json so a laptop can be closed between the two steps.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from buddy.books import Book, get_book
from buddy.config import INGEST_MODEL, cost_usd, get_settings
from buddy.ingest.download import chapter_pdf_path
from buddy.ingest.pdf import load_pages
from buddy.ingest.prompts import CHAPTER_SCHEMA, CHAPTER_SYSTEM, chapter_content

MAX_TOKENS = 48000  # thinking + a long JSON; batch requests have no HTTP timeout issue


def processed_path(book_key: str, chapter: int) -> Path:
    return get_settings().processed_dir / book_key / f"ch{chapter:02d}.json"


def chapter_params(book: Book, chapter: int) -> dict:
    pages = load_pages(chapter_pdf_path(book.key, chapter),
                       use_text=book.text_mode == "text")
    return {
        "model": INGEST_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": CHAPTER_SYSTEM,
        "messages": [{"role": "user", "content": chapter_content(book, chapter, pages)}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": CHAPTER_SCHEMA},
        },
    }


def custom_id(book_key: str, chapter: int) -> str:
    return f"{book_key}-ch{chapter:02d}"


def parse_custom_id(cid: str) -> tuple[str, int]:
    book_key, ch = cid.rsplit("-ch", 1)
    return book_key, int(ch)


def count_input_tokens(client: anthropic.Anthropic, params: dict) -> int:
    """Pre-flight estimate of input tokens (free endpoint)."""
    p = {k: v for k, v in params.items() if k not in ("max_tokens",)}
    return client.messages.count_tokens(**p).input_tokens


# The Batch API takes at most 256 MB per batch; page images make chapters a few MB each.
MAX_BATCH_BYTES = 200 * 1024 * 1024


def _create(client: anthropic.Anthropic, requests: list, ids: list[str]) -> str:
    batch = client.messages.batches.create(requests=requests)
    s = get_settings()
    s.batches_dir.mkdir(parents=True, exist_ok=True)
    (s.batches_dir / f"{batch.id}.json").write_text(json.dumps({
        "batch_id": batch.id,
        "items": ids,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "collected": False,
    }, indent=2))
    return batch.id


def submit(client: anthropic.Anthropic, items: list[tuple[str, int]],
           max_bytes: int = MAX_BATCH_BYTES, log=print) -> list[str]:
    """Submit chapters, as several batches when they don't fit in one. Builds and sends
    one batch at a time so the page images of hundreds of chapters aren't all in memory."""
    batch_ids: list[str] = []
    requests, ids, size = [], [], 0
    for book_key, chapter in items:
        params = chapter_params(get_book(book_key), chapter)
        n = len(json.dumps(params)) + 512
        if n > max_bytes:
            log(f"  skipped {custom_id(book_key, chapter)}: too big for one request "
                f"({n / 1e6:.0f} MB); split that chapter into smaller ones")
            continue
        if requests and size + n > max_bytes:
            batch_ids.append(_create(client, requests, ids))
            log(f"  sent batch {batch_ids[-1]} ({len(ids)} chapters, {size / 1e6:.0f} MB)")
            requests, ids, size = [], [], 0
        requests.append(Request(custom_id=custom_id(book_key, chapter),
                                params=MessageCreateParamsNonStreaming(**params)))
        ids.append(custom_id(book_key, chapter))
        size += n
    if requests:
        batch_ids.append(_create(client, requests, ids))
        log(f"  sent batch {batch_ids[-1]} ({len(ids)} chapters, {size / 1e6:.0f} MB)")
    return batch_ids


def pending_batches() -> list[dict]:
    """Batches submitted from this computer and not collected yet."""
    d = get_settings().batches_dir
    if not d.exists():
        return []
    out = [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]
    return [b for b in out if not b.get("collected")]


def wait(client: anthropic.Anthropic, batch_id: str, poll_seconds: int = 30) -> None:
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            return
        c = b.request_counts
        print(f"  batch {batch_id}: {b.processing_status} "
              f"(processing={c.processing}, succeeded={c.succeeded}, errored={c.errored})")
        time.sleep(poll_seconds)


def save_result(book_key: str, chapter: int, msg, source_id: str, batch: bool) -> dict:
    """Save one processed chapter to data/processed and log its cost. Returns a report."""
    cid = custom_id(book_key, chapter)
    if msg.stop_reason != "end_turn":
        return {"id": cid, "status": f"stop_reason={msg.stop_reason}"}
    text = next(b.text for b in msg.content if b.type == "text")
    data = json.loads(text)
    u = msg.usage
    report = {
        "id": cid,
        "status": "ok",
        "book": book_key,  # e.g. "evs" (Class 4), "evs-c3", or a school book
        "chapter": chapter,
        "model": msg.model,
        "batch_id": source_id if batch else None,
        "mode": "batch" if batch else "direct",
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cost_usd": round(cost_usd(INGEST_MODEL, u.input_tokens, u.output_tokens,
                                   batch=batch), 4),
        "topics": len(data["topics"]),
        "qa_pairs": sum(len(t["qa"]) for t in data["topics"]),
        "at": datetime.now(timezone.utc).isoformat(),
    }
    out = processed_path(book_key, chapter)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"meta": report, "result": data}, ensure_ascii=False, indent=2))
    with open(get_settings().cost_log, "a") as f:
        f.write(json.dumps(report) + "\n")
    return report


def collect(client: anthropic.Anthropic, batch_id: str) -> list[dict]:
    """Save each succeeded chapter of a batch."""
    s = get_settings()
    reports = []
    for res in client.messages.batches.results(batch_id):
        book_key, chapter = parse_custom_id(res.custom_id)
        if res.result.type != "succeeded":
            detail = getattr(res.result, "error", None)
            reports.append({"id": res.custom_id, "status": res.result.type, "detail": str(detail)})
            continue
        reports.append(save_result(book_key, chapter, res.result.message, batch_id, batch=True))
    state = s.batches_dir / f"{batch_id}.json"
    if state.exists():
        st = json.loads(state.read_text())
        st["collected"] = True
        state.write_text(json.dumps(st, indent=2))
    return reports


def process_now(client: anthropic.Anthropic, book_key: str, chapter: int) -> dict:
    """Process one chapter right away with the normal API (full price, a few minutes).
    Used for school-book chapters uploaded from the parent page."""
    params = chapter_params(get_book(book_key), chapter)
    # Long output: stream so the HTTP request doesn't time out.
    with client.messages.stream(**params) as stream:
        msg = stream.get_final_message()
    return save_result(book_key, chapter, msg, msg.id, batch=False)


def cost_history() -> list[dict]:
    p = get_settings().cost_log
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
