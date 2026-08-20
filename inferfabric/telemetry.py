"""
inferfabric/telemetry.py — Unified telemetry pipeline.

Single entry point: TelemetryHub coordinates three telemetry channels:
  1. RequestLogger (JSONL + SQLite via IFFDB)
  2. MetricsAggregator + AggregatorThread (in-memory sliding window)
  3. TokenStatsCollector (Prometheus polling -> daily aggregate -> JSON)

Usage:
    hub = TelemetryHub(data_dir)
    hub.record(RequestLog(...))
    hub.get_metrics("1h")
    hub.query_request_log(since=now-3600)
    hub.token_collector.query("daily")
"""

from __future__ import annotations
import queue as _queue
import logging
from pathlib import Path
from typing import Any, Callable

from inferfabric.db import IFFDB
from inferfabric.proxy.request_logger import RequestLogger, RequestLog
from inferfabric.metrics_aggregator import MetricsAggregator, AggregatorThread
from inferfabric.token_stats import TokenStatsCollector

log = logging.getLogger("inferfabric.telemetry")


class _IFFDBRequestLogAdapter:
    """Adapter: old RequestLogDB interface -> new IFFDB.

    DB schema now has both old fields (key_name, route, ts) and
    new fields (cost, metadata). Adapter fills both sides.
    """

    def __init__(self, if_db: IFFDB):
        self._db = if_db

    def insert_request_log(self, entries: list[dict]):
        if not entries:
            return
        self._db.insert_request_log(entries)

    def query_request_log(self, since: float, until: float | None = None,
                          model: str | None = None, limit: int = 100000) -> list[dict]:
        return self._db.query_request_log(since=since, until=until,
                                          model=model, limit=limit)

    def prune_request_log(self, before: float) -> int:
        return self._db.prune_request_log(before)

    def checkpoint(self):
        self._db.wal_checkpoint()

    def close(self):
        self._db.close()


class TelemetryHub:
    """Single entry point for all IFF telemetry.

    Coordinates:
      - RequestLogger (JSONL + SQLite)
      - MetricsAggregator (in-memory sliding window)
      - TokenStatsCollector (Prometheus polling)
    """

    def __init__(self, data_dir: Path, if_yaml_config: dict | None = None):
        self._data_dir = data_dir
        self._runtime_cfg = if_yaml_config or {}

        # Core: unified DB
        import inferfabric.migrations  # noqa: F401 — register migrations before IFFDB init
        self._db = IFFDB(data_dir)

        # Adapter for existing RequestLogger + MetricsAggregator
        self._legacy_db = _IFFDBRequestLogAdapter(self._db)

        # Queue for async aggregator
        self._agg_queue: _queue.Queue = _queue.Queue()

        # Metrics aggregator with 30-day sliding window
        self._metrics_name_map: dict[str, str] = {}
        self.metrics = MetricsAggregator(
            db=self._legacy_db,
            replay_hours=720.0,
            model_name_map=self._metrics_name_map,
        )
        self._agg_thread = AggregatorThread(self.metrics, self._agg_queue)
        self._agg_thread.daemon = True
        self._agg_thread.start()

        # Request logger
        self.logger = RequestLogger(
            log_dir=data_dir / "logs",
            enabled=True,
            on_log_queue=self._agg_queue,
            db=self._legacy_db,
            jsonl_enabled=self._runtime_cfg.get("access_log_jsonl", True),
            retention_days=self._runtime_cfg.get("request_log_retention_days", 90),
        )

        # Token collector (manager_ref injected later)
        self.token_collector = TokenStatsCollector(manager_ref=None, interval=300, db=self._db)

    def start_token_collector(self, manager_ref: Callable[[], Any]):
        """Inject manager ref and start token collection."""
        self.token_collector._manager_ref = manager_ref
        self.token_collector.start()

    def record(self, entry: RequestLog) -> None:
        self.logger.log(entry)

    def get_metrics(self, window: str = "24h") -> dict:
        return self.metrics.get_metrics(window)

    def query_request_log(self, since: float, until: float | None = None,
                           model: str | None = None, limit: int = 10000) -> list[dict]:
        """Query request logs via the unified adapter. Thread-safe."""
        return self._legacy_db.query_request_log(since=since, until=until,
                                                  model=model, limit=limit)

    def get_token_stats(self, window: str = "weekly") -> list[dict]:
        return self.token_collector.query(window)

    def update_metrics_name_map(self, name_map: dict[str, str]):
        self._metrics_name_map = name_map
        self.metrics._name_map = name_map

    def close(self):
        if self.logger:
            try:
                self.logger.close()
            except Exception as e:
                log.debug("Telemetry error: %s", e)
        try:
            self.token_collector.stop()
        except Exception as e:
            log.debug("Telemetry error: %s", e)
        try:
            self._db.close()
        except Exception as e:
            log.debug("Telemetry error: %s", e)