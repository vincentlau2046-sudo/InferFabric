"""Migration 001: Initialize state table and history table."""


def upgrade(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            from_profile TEXT,
            to_profile TEXT,
            duration REAL,
            status TEXT
        )
    """)
    defaults = [
        ("gpu_mode", "idle"),
        ("active_services", "[]"),
        ("sleep_state", "{}"),
        ("current_profile", "idle"),
        ("switch_target", ""),
        ("vllm_pid", ""),
        ("comfyui_pid", ""),
        ("manual_stops", "{}"),
    ]
    conn.executemany("INSERT OR IGNORE INTO state VALUES (?, ?)", defaults)
    conn.commit()