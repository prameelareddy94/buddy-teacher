"""Settings loaded from environment / .env (server side only)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Model IDs. Ingestion and photo questions use Sonnet 5; text escalations use Haiku 4.5.
INGEST_MODEL = "claude-sonnet-5"
VISION_MODEL = "claude-sonnet-5"
ESCALATION_MODEL = "claude-haiku-4-5"

# USD per million tokens (standard rates; Batch API is 50% of these).
PRICES = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    # Only for API keys not scoped to a workspace (the API then asks for this header).
    anthropic_workspace_id: str = ""
    kid_password: str = "change-me-kid"
    parent_password: str = "change-me-parent"
    session_secret: str = ""

    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    ollama_timeout: float = 120.0

    embedder: str = "bge-m3"
    data_dir: Path = Path("./data")
    low_score_threshold: float = 0.45
    top_k: int = 6

    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def batches_dir(self) -> Path:
        return self.data_dir / "batches"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "buddy.db"

    @property
    def cost_log(self) -> Path:
        return self.data_dir / "ingest_costs.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def cost_usd(model: str, input_tokens: int, output_tokens: int, batch: bool = False) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    c = (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000
    return c * 0.5 if batch else c
