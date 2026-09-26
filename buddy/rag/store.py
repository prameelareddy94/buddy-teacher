"""ChromaDB wrapper. One collection; every chunk tagged subject/grade/book/chapter/page/source.

Chunk kinds: text, explain, diagram, qa, vocab (textbooks); school_text, school_qa,
pattern (school uploads); verified (fixed answers); flagged (questions marked 👎 that
are not fixed yet). Normal book search leaves out verified and flagged.

Another process (the ingest CLI) can add chunks while the server runs. Chroma keeps
its search index in memory, so writers touch a marker file and readers reopen the
collection when the marker is newer than what they loaded.
"""
import os
from dataclasses import dataclass
from functools import lru_cache

import chromadb
from chromadb.api.client import SharedSystemClient

from buddy.config import get_settings
from buddy.rag.embed import get_embedder

COLLECTION = "buddy"
SPECIAL_KINDS = ["verified", "flagged"]
_loaded_mtime = 0.0


@dataclass
class Hit:
    id: str
    text: str
    meta: dict
    score: float  # cosine similarity, higher is better

    @property
    def cite(self) -> str:
        return self.meta.get("cite", "")

    @property
    def kind(self) -> str:
        return self.meta.get("kind", "")


def _marker():
    return get_settings().chroma_dir / ".changed"


def _marker_mtime() -> float:
    try:
        return os.stat(_marker()).st_mtime
    except FileNotFoundError:
        return 0.0


@lru_cache
def _open():
    global _loaded_mtime
    s = get_settings()
    s.chroma_dir.mkdir(parents=True, exist_ok=True)
    _loaded_mtime = _marker_mtime()
    client = chromadb.PersistentClient(path=str(s.chroma_dir))
    return client.get_or_create_collection(
        COLLECTION, metadata={"hnsw:space": "cosine"}, embedding_function=None
    )


def get_collection():
    if _open.cache_info().currsize and _marker_mtime() > _loaded_mtime:
        _open.cache_clear()
        SharedSystemClient.clear_system_cache()
    return _open()


def _touch() -> None:
    global _loaded_mtime
    m = _marker()
    m.parent.mkdir(parents=True, exist_ok=True)
    m.touch()
    _loaded_mtime = _marker_mtime()  # our own write is already in our index


def reset_cache() -> None:
    _open.cache_clear()
    SharedSystemClient.clear_system_cache()


def add_chunks(ids: list[str], texts: list[str], metas: list[dict]) -> None:
    if not ids:
        return
    col = get_collection()
    emb = get_embedder()
    for i in range(0, len(ids), 32):
        col.upsert(
            ids=ids[i:i + 32],
            documents=texts[i:i + 32],
            metadatas=metas[i:i + 32],
            embeddings=emb.embed(texts[i:i + 32]),
        )
    _touch()


def delete_where(where: dict) -> None:
    get_collection().delete(where=where)
    _touch()


def delete_ids(ids: list[str]) -> None:
    if ids:
        get_collection().delete(ids=ids)
        _touch()


def _where(filters: list[dict]) -> dict | None:
    return None if not filters else filters[0] if len(filters) == 1 else {"$and": filters}


def search(query: str, subject: str | None = None, k: int | None = None,
           kinds: list[str] | None = None, book: str | None = None) -> list[Hit]:
    """Book/school passages (never verified/flagged unless asked via kinds)."""
    k = k or get_settings().top_k
    col = get_collection()
    if col.count() == 0:
        return []
    filters: list[dict] = []
    if subject:
        filters.append({"subject": subject})
    if book:
        filters.append({"book": book})
    filters.append({"kind": {"$in": kinds}} if kinds else {"kind": {"$nin": SPECIAL_KINDS}})
    from buddy.books import school_book_keys

    school = school_book_keys() if not book else set()
    res = col.query(
        query_embeddings=get_embedder().embed([query]),
        n_results=min(k * 2 if school else k, col.count()),
        where=_where(filters),
    )
    hits = [Hit(i, d, m or {}, 1.0 - dist) for i, d, m, dist in
            zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])]
    if school:  # her school's own books first when they match about as well
        boost = get_settings().school_book_boost
        for h in hits:
            if h.meta.get("book") in school:
                h.score = min(1.0, h.score + boost)
        hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


def get_by(where: dict, limit: int = 50) -> list[Hit]:
    res = get_collection().get(where=where, limit=limit)
    return [Hit(i, d, m or {}, 1.0) for i, d, m in
            zip(res["ids"], res["documents"], res["metadatas"])]
