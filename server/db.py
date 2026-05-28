"""
Simple SQLite persistence for Roaster Bot results.
Saves every audited business so report links survive restarts.
"""

import sqlite3
import json
import os
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/tmp/roasterbot.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON results(session_id, idx)")
        conn.commit()

def save_results(session_id: str, results: list):
    with get_conn() as conn:
        conn.execute("DELETE FROM results WHERE session_id = ?", (session_id,))
        for i, r in enumerate(results):
            conn.execute(
                "INSERT INTO results (session_id, idx, data) VALUES (?, ?, ?)",
                (session_id, i, json.dumps(r))
            )
        conn.commit()

def get_result(session_id: str, idx: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM results WHERE session_id = ? AND idx = ?",
            (session_id, idx)
        ).fetchone()
        return json.loads(row["data"]) if row else None

def get_latest_session() -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM results ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["session_id"] if row else None

def get_session_results(session_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT data FROM results WHERE session_id = ? ORDER BY idx",
            (session_id,)
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

def make_slug(name: str) -> str:
    import re
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower().strip())
    return slug.strip('-')[:50]

def save_result_by_name(biz: dict) -> str:
    slug = make_slug(biz.get("name", "business"))
    with get_conn() as conn:
        conn.execute("DELETE FROM results WHERE session_id = ?", (slug,))
        conn.execute(
            "INSERT INTO results (session_id, idx, data) VALUES (?, ?, ?)",
            (slug, 0, json.dumps(biz))
        )
        conn.commit()
    return slug

def get_result_by_slug(slug: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data FROM results WHERE session_id = ? AND idx = 0",
            (slug,)
        ).fetchone()
        return json.loads(row["data"]) if row else None

def cleanup_old_results(days: int = 7):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM results WHERE created_at < datetime('now', ? || ' days')",
            (f"-{days}",)
        )
        conn.commit()
