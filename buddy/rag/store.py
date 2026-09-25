"""ChromaDB wrapper. One collection; every chunk tagged subject/chapter/page/source."""
from dataclasses import dataclass
from functools import lru_cache

import chromadb

from buddy.config import get_settings
from buddy.rag.embed import get_embedder

COLLECTION = "buddy"


@dataclass
class Hit:
    id: str
    text: str
    meta: dict
    score: float  # cosine similarity, higher is better

    @property
    def cite(self) -> str:
        return self.meta.get("cite", "")


@lru_cache
def get_collection():
    s = get_settings()
    s.chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(s.chroma_dir))
    return client.get_or_create_collection(
        COLLECTION, metadata={"hnsw:space": "cosine"}, embedding_function=None
    )


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


def delete_where(where: dict) -> None:
    get_collection().delete(where=where)


def search(query: str, subject: str | None = None, k: int | None = None,
           kinds: list[str] | None = None) -> list[Hit]:
    k = k or get_settings().top_k
    col = get_collection()
    if col.count() == 0:
        return []
    filters = []
    if subject:
        filters.append({"subject": subject})
    if kinds:
        filters.append({"kind": {"$in": kinds}})
    where = None if not filters else filters[0] if len(filters) == 1 else {"$and": filters}
    res = col.query(
        query_embeddings=get_embedder().embed([query]),
        n_results=min(k, col.count()),
        where=where,
    )
    hits = []
    for id_, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                    res["metadatas"][0], res["distances"][0]):
        hits.append(Hit(id_, doc, meta or {}, 1.0 - dist))
    return hits


def get_by(where: dict, limit: int = 50) -> list[Hit]:
    res = get_collection().get(where=where, limit=limit)
    return [Hit(i, d, m or {}, 1.0) for i, d, m in
            zip(res["ids"], res["documents"], res["metadatas"])]
