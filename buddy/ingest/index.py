"""Load processed chapter JSON into ChromaDB."""
import json
import re

from buddy.books import citation, get_book
from buddy.ingest.batch import processed_path
from buddy.rag import store

CHUNK_CHARS = 900


def split_text(text: str, size: int = CHUNK_CHARS) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > size:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
        while len(cur) > size * 1.5:  # one very long paragraph
            cut = cur.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            chunks.append(cur[:cut])
            cur = cur[cut:].strip()
    if cur:
        chunks.append(cur)
    return chunks


def printed_pages(page_numbers: list[dict]) -> dict[int, int]:
    """pdf page -> printed page. Pages with no printed number (unit openers, full-page
    pictures) take the number implied by the nearest numbered page, so chapter 2's
    unnumbered opener is cited as e.g. page 17, not page 1."""
    known = {p["pdf_page"]: p["printed_page"] for p in page_numbers if p["printed_page"] > 0}
    out = {}
    for p in page_numbers:
        pdf = p["pdf_page"]
        if pdf in known:
            out[pdf] = known[pdf]
        elif known:
            near = min(known, key=lambda k: (abs(k - pdf), k))
            out[pdf] = max(1, known[near] + (pdf - near))
        else:
            out[pdf] = pdf
    return out


def index_chapter(book_key: str, chapter: int) -> int:
    book = get_book(book_key)
    data = json.loads(processed_path(book_key, chapter).read_text())["result"]
    printed = printed_pages(data["page_numbers"])

    def pg(pdf_pages: list[int]) -> list[int]:
        return [printed.get(p, p) for p in pdf_pages] or [0]

    title = data["chapter_title"]
    ids, texts, metas = [], [], []

    def add(kind: str, n: int, text: str, pages: list[int], topic: str, **extra) -> None:
        head = f"{book.label} Chapter {chapter} \"{title}\" - {topic}"
        ids.append(f"{book.key}-ch{chapter:02d}-{kind}-{n}")
        texts.append(f"{head}\n{text}")
        meta = {
            "subject": book.subject, "grade": book.grade, "book": book.key,
            "chapter": chapter, "chapter_title": title,
            "topic": topic, "kind": kind, "source": "ncert",
            "page": pages[0], "pages": ",".join(str(p) for p in pages),
            "cite": citation(book.key, chapter, pages[0]),
        }
        meta.update(extra)
        metas.append(meta)

    n = 0
    for t in data["topics"]:
        pages = pg(t["pdf_pages"])
        for chunk in split_text(t["clean_text"]):
            add("text", n, chunk, pages, t["title"]); n += 1
        add("explain", n, t["kid_explanation"], pages, t["title"]); n += 1
        for d in t["diagrams"]:
            p = pg([d["pdf_page"]])
            add("diagram", n, f"Picture on page {p[0]}: {d['description']}", p, t["title"]); n += 1
        for qa in t["qa"]:
            add("qa", n, f"Q ({qa['kind']}): {qa['question']}\nA: {qa['answer']}",
                pg(qa["pdf_pages"]) if qa["pdf_pages"] else pages, t["title"],
                hint=qa["hint"], qa_kind=qa["kind"]); n += 1
    vocab = data.get("vocabulary", [])
    for i in range(0, len(vocab), 10):
        group = vocab[i:i + 10]
        text = "\n".join(f"{v['word']}: {v['meaning']}" for v in group)
        add("vocab", n, "Word meanings:\n" + text, pg([group[0]["pdf_page"]]), "Vocabulary"); n += 1

    store.delete_where({"$and": [{"book": book.key}, {"chapter": chapter},
                                 {"source": "ncert"}]})
    store.add_chunks(ids, texts, metas)
    return len(ids)
