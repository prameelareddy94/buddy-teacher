"""SQLite log of every question and which path answered it (parent view)."""
import sqlite3
import time
from contextlib import closing

from buddy.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    question TEXT NOT NULL,
    subject TEXT,
    route TEXT NOT NULL,          -- local | claude_haiku | claude_sonnet
    reason TEXT NOT NULL,         -- why the router chose it
    model TEXT,
    top_score REAL,
    had_image INTEGER DEFAULT 0,
    hint TEXT,
    answer TEXT,
    source TEXT,
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    local_attempt TEXT            -- what the local model said before a fallback
);
"""


def _conn() -> sqlite3.Connection:
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(s.db_path)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def log_question(**fields) -> int:
    fields.setdefault("ts", time.time())
    cols = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    with closing(_conn()) as c, c:
        cur = c.execute(f"INSERT INTO questions ({cols}) VALUES ({qs})", list(fields.values()))
        return cur.lastrowid


def get_question(qid: int) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
        return dict(row) if row else None


def recent(limit: int = 200) -> list[dict]:
    with closing(_conn()) as c:
        rows = c.execute("SELECT * FROM questions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def summary() -> dict:
    with closing(_conn()) as c:
        rows = c.execute(
            "SELECT route, reason, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, "
            "AVG(latency_ms) ms FROM questions GROUP BY route, reason ORDER BY n DESC"
        ).fetchall()
        return {"by_path": [dict(r) for r in rows],
                "total_cost_usd": sum(r["cost"] for r in rows),
                "total_questions": sum(r["n"] for r in rows)}
