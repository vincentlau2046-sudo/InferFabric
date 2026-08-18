"""Migration 004: sleep_state table (v5.2)."""

import json


def upgrade(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sleep_state (
            model TEXT PRIMARY KEY,
            level TEXT NOT NULL CHECK (level IN ('l1', 'l2'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sleep_state_level ON sleep_state(level)")

    # Migrate from old JSON dict in state table
    row = conn.execute(
        "SELECT value FROM state WHERE key='sleep_state'"
    ).fetchone()
    if row and row[0]:
        try:
            data = json.loads(row[0])
            for model, level in data.items():
                conn.execute(
                    "INSERT OR IGNORE INTO sleep_state (model, level) VALUES (?, ?)",
                    (model, level),
                )
        except (json.JSONDecodeError, TypeError):
            pass
    conn.commit()