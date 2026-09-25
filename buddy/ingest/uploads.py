"""School worksheets / notes / test papers (PDF or photos) -> ChromaDB.

Runs right away (not batched) because a parent is waiting on the upload page.
The extracted "pattern_notes" steer answers and quizzes toward the school's style.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from buddy.books import BOOKS
from buddy.config import INGEST_MODEL, cost_usd, get_settings
from buddy.ingest.index import split_text
from buddy.ingest.pdf import Page, load_upload
from buddy.ingest.prompts import (UPLOAD_INSTRUCTIONS, UPLOAD_SYSTEM, image_block,
                                  upload_schema)
from buddy.llm.claude import sync_client
from buddy.rag import store


def read_pages(files: list[Path]) -> list[Page]:
    pages: list[Page] = []
    for f in files:
        for p in load_upload(f):
            pages.append(Page(len(pages) + 1, p.text, p.jpeg))
    return pages


def extract(pages: list[Page], subject: str, chapter: int, kind: str) -> tuple[dict, dict]:
    content: list[dict] = []
    for p in pages:
        head = f"=== page {p.index} ==="
        if p.text:
            head += f"\nPDF text layer (may be garbled):\n{p.text}"
        content += [{"type": "text", "text": head}, image_block(p.jpeg)]
    content.append({"type": "text", "text": UPLOAD_INSTRUCTIONS.format(
        kind=kind, subject=subject, chapter=chapter or "unknown")})
    msg = sync_client().messages.create(
        model=INGEST_MODEL,
        max_tokens=16000,
        system=UPLOAD_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": upload_schema()}},
    )
    if msg.stop_reason != "end_turn":
        raise RuntimeError(f"Upload reading stopped early: {msg.stop_reason}")
    data = json.loads(next(b.text for b in msg.content if b.type == "text"))
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens,
             "cost_usd": round(cost_usd(INGEST_MODEL, msg.usage.input_tokens,
                                        msg.usage.output_tokens), 4)}
    return data, usage


def index_upload(upload_id: str, data: dict, subject: str, chapter: int, kind: str) -> int:
    if subject not in BOOKS:
        subject = data["subject_guess"] if data["subject_guess"] in BOOKS else "general"
    chapter = chapter or data["chapter_guess"] or 0
    label = BOOKS[subject].label if subject in BOOKS else "School"
    base = {"subject": subject, "chapter": chapter, "chapter_title": data["title"],
            "topic": data["title"], "source": "school", "upload_id": upload_id,
            "upload_kind": kind}

    def cite(page: int) -> str:
        return f"School {kind} \"{data['title']}\" ({label}), page {page}"

    ids, texts, metas = [], [], []
    for i, chunk in enumerate(split_text(data["text"])):
        ids.append(f"school-{upload_id}-text-{i}")
        texts.append(f"{label} school {kind}: {data['title']}\n{chunk}")
        metas.append({**base, "kind": "school_text", "page": 1, "pages": "1", "cite": cite(1)})
    for i, q in enumerate(data["questions"]):
        ids.append(f"school-{upload_id}-qa-{i}")
        ans = f"\nA: {q['answer']}" if q["answer"] else ""
        texts.append(f"{label} school {kind} question ({q['kind']}, {q['marks'] or '-'} marks): "
                     f"{q['question']}{ans}")
        metas.append({**base, "kind": "school_qa", "page": q["page"] or 1,
                      "pages": str(q["page"] or 1), "cite": cite(q["page"] or 1),
                      "qa_kind": q["kind"]})
    if data["pattern_notes"].strip():
        ids.append(f"school-{upload_id}-pattern")
        texts.append(f"How the school sets {label} {kind}s: {data['pattern_notes']}")
        metas.append({**base, "kind": "pattern", "page": 1, "pages": "1", "cite": cite(1)})
    store.add_chunks(ids, texts, metas)
    return len(ids)


def ingest_upload(path_or_paths, subject: str = "unknown", chapter: int = 0,
                  kind: str = "worksheet") -> dict:
    files = [Path(p) for p in (path_or_paths if isinstance(path_or_paths, list)
                               else [path_or_paths])]
    upload_id = uuid.uuid4().hex[:10]
    pages = read_pages(files)
    data, usage = extract(pages, subject, chapter, kind)
    n = index_upload(upload_id, data, subject, chapter, kind)
    out = get_settings().uploads_dir / upload_id / "extracted.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    record = {"upload_id": upload_id, "files": [f.name for f in files], "subject": subject,
              "chapter": chapter, "kind": kind, "chunks": n, "usage": usage,
              "at": datetime.now(timezone.utc).isoformat(), "result": data}
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return {k: record[k] for k in ("upload_id", "subject", "chapter", "kind", "chunks", "usage")} \
        | {"title": data["title"], "questions": len(data["questions"])}
