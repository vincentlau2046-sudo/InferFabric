"""Migration 002: Initialize request_log table (v4.x compatible + v5.0 extras)."""


def upgrade(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            req_id TEXT NOT NULL UNIQUE,
            key_name TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL,
            status INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
            ttft_ms REAL CHECK (ttft_ms IS NULL OR ttft_ms >= 0),
            tokens_in INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
            tokens_out INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
            duration_ms REAL NOT NULL DEFAULT 0.0 CHECK (duration_ms >= 0),
            route TEXT NOT NULL DEFAULT 'local',
            cloud_provider TEXT,
            error TEXT,
            timestamp REAL NOT NULL CHECK (timestamp > 0),
            ts TEXT NOT NULL DEFAULT '',
            cost TEXT DEFAULT 'local',
            metadata TEXT DEFAULT '{}'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_model_ts ON request_log(model, timestamp)")
    conn.commit()