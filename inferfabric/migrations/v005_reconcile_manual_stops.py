"""Migration 005: Reconcile manual_stops data from KV key to table (v5.x).

v003 created the manual_stops table but StateDB kept writing to the KV key
"manual_stops" in the state table. This migration re-migrates current KV data
into the table and removes the stale key, completing the v5.1 architectural
transition to table-backed manual stop tracking.
"""

import json


def upgrade(conn):
    # Read current KV key data
    row = conn.execute(
        "SELECT value FROM state WHERE key='manual_stops'"
    ).fetchone()

    migrated = 0
    if row and row[0]:
        try:
            data = json.loads(row[0])
            for model, ts in data.items():
                conn.execute(
                    "INSERT OR REPLACE INTO manual_stops (model, stop_ts) VALUES (?, ?)",
                    (model, ts),
                )
                migrated += 1
        except (json.JSONDecodeError, TypeError):
            pass

    # Remove the KV key (StateDB now delegates to IFFDB table)
    conn.execute("DELETE FROM state WHERE key='manual_stops'")
    conn.commit()

    print(f"v005: migrated {migrated} manual_stops entries from KV to table")