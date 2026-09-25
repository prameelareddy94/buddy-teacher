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


def processed_path(subject: str, chapter: int) -> Path:
    return get_settings().processed_dir / subject / f"ch{chapter:02d}.json"


def chapter_params(book: Book, chapter: int) -> dict:
    pages = load_pages(chapter_pdf_path(book.subject, chapter),
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


def custom_id(subject: str, chapter: int) -> str:
    return f"{subject}-ch{chapter:02d}"


def parse_custom_id(cid: str) -> tuple[str, int]:
    subject, ch = cid.rsplit("-ch", 1)
    return subject, int(ch)


def count_input_tokens(client: anthropic.Anthropic, params: dict) -> int:
    """Pre-flight estimate of input tokens (free endpoint)."""
    p = {k: v for k, v in params.items() if k not in ("max_tokens",)}
    return client.messages.count_tokens(**p).input_tokens


def submit(client: anthropic.Anthropic, items: list[tuple[str, int]]) -> str:
    requests = []
    for subject, chapter in items:
        params = chapter_params(get_book(subject), chapter)
        requests.append(Request(custom_id=custom_id(subject, chapter),
                                params=MessageCreateParamsNonStreaming(**params)))
    batch = client.messages.batches.create(requests=requests)
    s = get_settings()
    s.batches_dir.mkdir(parents=True, exist_ok=True)
    (s.batches_dir / f"{batch.id}.json").write_text(json.dumps({
        "batch_id": batch.id,
        "items": [custom_id(*i) for i in items],
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "collected": False,
    }, indent=2))
    return batch.id


def wait(client: anthropic.Anthropic, batch_id: str, poll_seconds: int = 30) -> None:
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            return
        c = b.request_counts
        print(f"  batch {batch_id}: {b.processing_status} "
              f"(processing={c.processing}, succeeded={c.succeeded}, errored={c.errored})")
        time.sleep(poll_seconds)


def collect(client: anthropic.Anthropic, batch_id: str) -> list[dict]:
    """Save each succeeded chapter to data/processed and log its cost."""
    s = get_settings()
    reports = []
    for res in client.messages.batches.results(batch_id):
        subject, chapter = parse_custom_id(res.custom_id)
        if res.result.type != "succeeded":
            detail = getattr(res.result, "error", None)
            reports.append({"id": res.custom_id, "status": res.result.type, "detail": str(detail)})
            continue
        msg = res.result.message
        if msg.stop_reason != "end_turn":
            reports.append({"id": res.custom_id, "status": f"stop_reason={msg.stop_reason}"})
            continue
        text = next(b.text for b in msg.content if b.type == "text")
        data = json.loads(text)
        u = msg.usage
        report = {
            "id": res.custom_id,
            "status": "ok",
            "subject": subject,
            "chapter": chapter,
            "model": msg.model,
            "batch_id": batch_id,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cost_usd": round(cost_usd(INGEST_MODEL, u.input_tokens, u.output_tokens,
                                       batch=True), 4),
            "topics": len(data["topics"]),
            "qa_pairs": sum(len(t["qa"]) for t in data["topics"]),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        out = processed_path(subject, chapter)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"meta": report, "result": data}, ensure_ascii=False, indent=2))
        with open(s.cost_log, "a") as f:
            f.write(json.dumps(report) + "\n")
        reports.append(report)
    state = s.batches_dir / f"{batch_id}.json"
    if state.exists():
        st = json.loads(state.read_text())
        st["collected"] = True
        state.write_text(json.dumps(st, indent=2))
    return reports


def cost_history() -> list[dict]:
    p = get_settings().cost_log
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
