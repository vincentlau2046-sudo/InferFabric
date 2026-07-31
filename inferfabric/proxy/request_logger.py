"""单用户请求日志 — JSONL 按日轮转

每个请求完成时写一行 JSONL 到 logs/access-YYYY-MM-DD.jsonl。
iff.yaml 新增 access_log: true（默认 true），false 时不写日志。
线程安全（threading.Lock 保护 fd 读写）。
"""

import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import IO, TextIO

log = logging.getLogger("inferfabric.request_logger")


@dataclass
class RequestLog:
    """单个请求的结构化日志记录。"""
    req_id: str
    key_name: str
    model: str
    status: int
    ttft_ms: float | None = None       # 首 token 延迟
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: float = 0.0
    route: str = "local"               # "local" | "cloud"
    cloud_provider: str | None = None   # "baidu-codingplan" | None
    error: str | None = None
    timestamp: float = 0.0   # time.time() at log time (G-2: sliding window)
    ts: str = ""


class RequestLogger:
    """JSONL 按日轮转请求日志。线程安全。

    - enabled=False → 所有 log() 调用为空操作
    - 按日自动轮转，实时 flush
    """

    def __init__(self, log_dir: str | Path = "logs", enabled: bool = True,
                 on_log_queue: queue.Queue | None = None):
        self._log_dir = Path(log_dir)
        self._enabled = enabled
        self._current_date: str = ""
        self._fd: IO | None = None
        self._lock = threading.Lock()
        self._on_log_queue = on_log_queue

    @property
    def enabled(self) -> bool:
        return self._enabled

    def log(self, entry: RequestLog):
        """写一行 JSONL，自动按日轮转。线程安全。"""
        if not self._enabled:
            return
        # G-2: auto-fill timestamp if not set
        if entry.timestamp == 0:
            entry.timestamp = time.time()
        with self._lock:
            try:
                if not entry.ts:
                    entry.ts = datetime.now(tz=timezone.utc).isoformat()
                today = date.today().isoformat()
                if today != self._current_date:
                    self._rotate(today)
                if self._fd:
                    line = json.dumps(asdict(entry), ensure_ascii=False)
                    self._fd.write(line + "\n")
                    self._fd.flush()
            except Exception as e:
                log.warning("Failed to write request log: %s", e)

        # 推送 aggregator（在锁外，避免嵌套锁死锁）
        if self._on_log_queue is not None:
            try:
                self._on_log_queue.put_nowait(entry)
            except Exception:
                log.warning("Failed to enqueue for aggregator", exc_info=True)

    def close(self):
        """关闭当前文件句柄。"""
        with self._lock:
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
                self._current_date = ""

    def _rotate(self, new_date: str):
        """按日轮转到新文件（调用者需持锁）。"""
        # Close old fd
        if self._fd:
            try:
                self._fd.close()
            except Exception:
                pass
            self._fd = None
            self._current_date = ""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            path = self._log_dir / f"access-{new_date}.jsonl"
            self._fd = open(path, "a", encoding="utf-8")
            self._current_date = new_date
            log.debug("Rotated access log to %s", path)
        except Exception as e:
            log.error("Failed to rotate access log: %s", e)
            self._fd = None
