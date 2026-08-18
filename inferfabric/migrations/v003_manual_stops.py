"""Migration 003: manual_stops table (v5.2)."""

import json


def upgrade(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS manual_stops (
            model TEXT PRIMARY KEY,
            stop_ts REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_stops_ts ON manual_stops(stop_ts)")

    # Migrate from old JSON dict in state table
    row = conn.execute(
        "SELECT value FROM state WHERE key='manual_stops'"
    ).fetchone()
    if row and row[0]:
        try:
            data = json.loads(row[0])
            for model, ts in data.items():
                conn.execute(
                    "INSERT OR IGNORE INTO manual_stops (model, stop_ts) VALUES (?, ?)",
                    (model, ts),
                )
        except (json.JSONDecodeError, TypeError):
            pass
    conn.commit()