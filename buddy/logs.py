"""SQLite: every question and its path, 👍/👎 feedback, fixes, and review runs."""
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
    route TEXT NOT NULL,          -- local | claude_haiku | claude_sonnet | verified
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
CREATE TABLE IF NOT EXISTS fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    question_id INTEGER,
    question TEXT NOT NULL,
    subject TEXT,
    verdict TEXT NOT NULL,        -- fixed | correct | not_in_book | parent
    origin TEXT NOT NULL,         -- review | parent
    hint TEXT,
    answer TEXT NOT NULL,
    source TEXT,
    note TEXT,                    -- reviewer's reason
    old_answer TEXT,
    status TEXT NOT NULL DEFAULT 'active',   -- active | undone
    chunk_id TEXT,
    run_id INTEGER
);
CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    finished REAL,
    mode TEXT,                    -- batch | direct
    status TEXT,                  -- running | done | failed | nothing_to_do
    items INTEGER DEFAULT 0,
    fixed INTEGER DEFAULT 0,
    correct INTEGER DEFAULT 0,
    not_in_book INTEGER DEFAULT 0,
    rejected INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    batch_id TEXT,
    note TEXT
);
"""

# Columns added after v1; added to existing databases on open.
QUESTION_COLUMNS = {
    "feedback": "INTEGER",        # 1 = 👍, -1 = 👎
    "feedback_ts": "REAL",
    "reviewed": "INTEGER DEFAULT 0",  # 0 = not yet, 1 = reviewed, 2 = don't auto-review
    "review_verdict": "TEXT",
    "fix_id": "INTEGER",
}


def _conn() -> sqlite3.Connection:
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(s.db_path, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    have = {r["name"] for r in c.execute("PRAGMA table_info(questions)")}
    for col, typ in QUESTION_COLUMNS.items():
        if col not in have:
            c.execute(f"ALTER TABLE questions ADD COLUMN {col} {typ}")
    return c


def _insert(table: str, fields: dict) -> int:
    cols = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    with closing(_conn()) as c, c:
        return c.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})",
                         list(fields.values())).lastrowid


def _update(table: str, row_id: int, fields: dict) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with closing(_conn()) as c, c:
        c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*fields.values(), row_id])


def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ---------- questions ----------

def log_question(**fields) -> int:
    fields.setdefault("ts", time.time())
    return _insert("questions", fields)


def update_question(qid: int, **fields) -> None:
    _update("questions", qid, fields)


def get_question(qid: int) -> dict | None:
    rows = _rows("SELECT * FROM questions WHERE id = ?", (qid,))
    return rows[0] if rows else None


def recent(limit: int = 200) -> list[dict]:
    return _rows("SELECT * FROM questions ORDER BY id DESC LIMIT ?", (limit,))


def flagged_unfixed(limit: int = 100) -> list[dict]:
    return _rows("SELECT * FROM questions WHERE feedback = -1 AND fix_id IS NULL "
                 "ORDER BY id DESC LIMIT ?", (limit,))


def summary() -> dict:
    rows = _rows(
        "SELECT route, reason, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost, AVG(latency_ms) ms, "
        "SUM(feedback = 1) up, SUM(feedback = -1) down "
        "FROM questions GROUP BY route, reason ORDER BY n DESC")
    return {"by_path": rows,
            "total_cost_usd": sum(r["cost"] for r in rows),
            "total_questions": sum(r["n"] for r in rows)}


# ---------- fixes ----------

def add_fix(**fields) -> int:
    fields.setdefault("ts", time.time())
    return _insert("fixes", fields)


def update_fix(fix_id: int, **fields) -> None:
    _update("fixes", fix_id, fields)


def get_fix(fix_id: int) -> dict | None:
    rows = _rows("SELECT * FROM fixes WHERE id = ?", (fix_id,))
    return rows[0] if rows else None


def list_fixes(limit: int = 200) -> list[dict]:
    return _rows("SELECT * FROM fixes ORDER BY id DESC LIMIT ?", (limit,))


# ---------- review runs ----------

def start_run(mode: str) -> int:
    return _insert("review_runs", {"started": time.time(), "mode": mode, "status": "running"})


def finish_run(run_id: int, **fields) -> None:
    fields.setdefault("finished", time.time())
    _update("review_runs", run_id, fields)


def list_runs(limit: int = 20) -> list[dict]:
    return _rows("SELECT * FROM review_runs ORDER BY id DESC LIMIT ?", (limit,))
