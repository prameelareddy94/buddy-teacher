"""Local embeddings. bge-m3 is multilingual (English/Hindi/Kannada) and runs on CPU."""
import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from buddy.config import get_settings


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class BgeM3Embedder:
    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer  # heavy import, load lazily

        self.model = SentenceTransformer("BAAI/bge-m3", device="cpu")
        self.model.max_seq_length = 1024  # chunks are short; keeps RAM/latency down

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self.model.encode(texts, batch_size=8, normalize_embeddings=True)
        return [v.tolist() for v in vecs]


class HashEmbedder:
    """Deterministic character-trigram hashing. Only for tests and offline dev."""

    dims = 512

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dims
            words = re.findall(r"\w+", t.lower())
            for w in words:
                w = f" {w} "
                for i in range(len(w) - 2):
                    h = int(hashlib.md5(w[i:i + 3].encode()).hexdigest()[:8], 16)
                    v[h % self.dims] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


@lru_cache
def get_embedder() -> Embedder:
    name = get_settings().embedder
    if name == "hash":
        return HashEmbedder()
    if name == "bge-m3":
        return BgeM3Embedder()
    raise ValueError(f"Unknown EMBEDDER {name!r} (use bge-m3 or hash)")
