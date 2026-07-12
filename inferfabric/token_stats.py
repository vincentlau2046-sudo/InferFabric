"""
inferfabric/token_stats.py — Token usage statistics collector.
Polls vLLM Prometheus metrics, computes deltas, aggregates by day, persists to JSON.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

from .proxy.metrics import parse_prometheus_text

log = logging.getLogger("inferfabric.token_stats")

STATE_FILE = Path.home() / ".inferfabric" / "token-stats.json"


class TokenStatsCollector:
    """Collects token usage from active vLLM instances, aggregates by day."""

    def __init__(self, manager_ref=None, interval: int = 300):
        self.manager_ref = manager_ref  # callable returning manager instance
        self.interval = interval
        self._state = {}  # {date_str: {model: {prompt, generation, requests}}}
        self._snapshots = {}  # {port: {prompt_sum, gen_sum, req_total, ts}}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

    # --- 端口发现 ---

    def _get_active_ports(self) -> dict[int, str]:
        """Return {port: model_name} for active vllm services.
        Reads from manager.status() — services_info where type=='vllm' and port exists."""
        mapping = {}
        if self.manager_ref is None:
            return mapping
        try:
            mgr = self.manager_ref()
            status = mgr.status()
            for svc_name, info in status.get("services_info", {}).items():
                if info.get("type") == "vllm" and info.get("port"):
                    mapping[info["port"]] = svc_name
        except Exception as e:
            log.warning("Failed to get active ports: %s", e)
        return mapping

    # --- 指标拉取 ---

    def _fetch_port_metrics(self, port: int) -> dict | None:
        """Fetch Prometheus metrics from a vLLM port. Return dict or None on failure."""
        try:
            url = f"http://127.0.0.1:{port}/metrics"
            with urlopen(url, timeout=10) as resp:
                text = resp.read().decode("utf-8")
        except Exception as e:
            log.warning("Metrics fetch failed for port %d: %s", port, e)
            return None

        gauges, counters, histos = parse_prometheus_text(text)

        prompt_h = histos.get("vllm:request_prompt_tokens")
        gen_h = histos.get("vllm:request_generation_tokens")
        # parse_prometheus_text strips the _total suffix, so the key is without _total
        req_total = counters.get("vllm:num_requests_completed")

        result = {}
        if prompt_h:
            result["prompt_sum"] = int(prompt_h["sum"])
        if gen_h:
            result["gen_sum"] = int(gen_h["sum"])
        if req_total is not None:
            result["req_total"] = int(req_total)

        return result if result else None

    # --- Delta 计算 ---

    def _compute_deltas(self, port: int, current: dict) -> dict | None:
        """Compute deltas from previous snapshot. Return None if no valid delta."""
        prev = self._snapshots.get(port)
        if prev is None:
            # First collection for this port — store as baseline, no delta
            self._snapshots[port] = {**current, "ts": time.time()}
            return None

        delta = {}
        for key in ("prompt_sum", "gen_sum", "req_total"):
            cur_val = current.get(key, 0)
            prev_val = prev.get(key, 0)
            d = cur_val - prev_val
            if d < 0:
                # Counter reset (vLLM restart) — discard this round
                log.info("Counter reset on port %d for %s (delta=%d)", port, key, d)
                return None
            delta[key] = d

        self._snapshots[port] = {**current, "ts": time.time()}
        return delta

    # --- 聚合 ---

    def _today_key(self) -> str:
        return datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    def _aggregate(self, model_name: str, delta: dict):
        with self._lock:
            day = self._today_key()
            if day not in self._state:
                self._state[day] = {}
            day_data = self._state[day]
            if model_name not in day_data:
                day_data[model_name] = {"prompt_tokens": 0, "generation_tokens": 0, "requests": 0}
            day_data[model_name]["prompt_tokens"] += delta.get("prompt_sum", 0)
            day_data[model_name]["generation_tokens"] += delta.get("gen_sum", 0)
            day_data[model_name]["requests"] += delta.get("req_total", 0)

    # --- 持久化 ---

    def _persist(self):
        """Atomically write state to JSON using tmp + os.replace."""
        with self._lock:
            data = dict(self._state)

        tmp_path = str(STATE_FILE) + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, str(STATE_FILE))
        except Exception as e:
            log.error("Persist failed: %s", e)

    # --- 清理 ---

    def _cleanup(self):
        """Remove entries older than 30 days."""
        cutoff = datetime.now(tz=timezone(timedelta(hours=8))) - timedelta(days=30)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        with self._lock:
            expired = [d for d in self._state if d < cutoff_str]
            for d in expired:
                del self._state[d]
        if expired:
            log.info("Cleaned up %d expired date(s)", len(expired))
            self._persist()

    # --- 查询 ---

    def query(self, window: str = "weekly") -> list[dict]:
        """Query aggregated data for a time window.

        window: 'daily' | 'weekly' | 'monthly' | 'all'
        Returns: [{model, total_tokens, requests}]
        """
        self._load_from_file()  # ensure fresh
        now = datetime.now(tz=timezone(timedelta(hours=8)))

        if window == "daily":
            since = now - timedelta(hours=24)
        elif window == "weekly":
            since = now - timedelta(days=7)
        elif window == "monthly":
            since = now - timedelta(days=30)
        else:  # 'all'
            since = None

        # Load existing file data + in-memory state
        all_data = self._load_full_state()

        total = {}
        for date_str, models in all_data.items():
            if since and date_str < since.strftime("%Y-%m-%d"):
                continue
            for model, vals in models.items():
                if model not in total:
                    total[model] = {"total_tokens": 0, "requests": 0}
                total[model]["total_tokens"] += vals.get("prompt_tokens", 0) + vals.get("generation_tokens", 0)
                total[model]["requests"] += vals.get("requests", 0)

        return [{"model": m, "total_tokens": d["total_tokens"], "requests": d["requests"]} for m, d in total.items()]

    def _load_from_file(self):
        """Load state from JSON file into memory if file has newer data."""
        if not STATE_FILE.exists():
            return
        try:
            mtime = os.path.getmtime(str(STATE_FILE))
            with open(STATE_FILE) as f:
                file_data = json.load(f)
            with self._lock:
                for date_str, models in file_data.items():
                    if date_str not in self._state:
                        self._state[date_str] = models
        except Exception as e:
            log.warning("Load from file failed: %s", e)

    def _load_full_state(self) -> dict:
        """Return complete state (file + memory)."""
        self._load_from_file()
        with self._lock:
            return dict(self._state)

    # --- 采集循环 ---

    def _collect_once(self):
        """Single collection cycle."""
        ports = self._get_active_ports()
        for port, model_name in ports.items():
            current = self._fetch_port_metrics(port)
            if current is None:
                continue
            delta = self._compute_deltas(port, current)
            if delta is not None:
                self._aggregate(model_name, delta)

        self._persist()
        self._cleanup()

    def _run_loop(self):
        """Main collection loop."""
        while not self._stop_event.is_set():
            self._collect_once()
            self._stop_event.wait(self.interval)

    def start(self):
        """Start the background collection thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info("Token stats collector started (interval=%ds)", self.interval)

    def stop(self):
        """Stop the collector and persist final data."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._persist()
        log.info("Token stats collector stopped")