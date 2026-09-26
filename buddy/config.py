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
    # "local_first": the local model answers strong book matches, Claude the rest.
    # "claude_only": every question goes to Claude Haiku (~$0.002 each).
    answer_mode: str = "local_first"
    local_min_score: float = 0.55       # below this the local model isn't trusted
    school_book_boost: float = 0.08     # her school's own books rank above NCERT
    # A fixed (verified) answer this similar to a new question is shown to the model
    # first; at verified_direct it is answered straight from the fix, no model call.
    verified_threshold: float = 0.80
    verified_direct: float = 0.92

    # Nightly review of weak answers by Claude (runs inside the server)
    review_enabled: bool = True
    review_time: str = "02:30"          # HH:MM in review_tz
    review_tz: str = "Asia/Kolkata"
    review_budget_usd: float = 0.50     # hard cap per run (estimated before sending)
    review_max_items: int = 40

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
