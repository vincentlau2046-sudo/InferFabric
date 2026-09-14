"""InferFabric Anomaly Collector — thread-safe ring buffer for anomaly events.

R9: 结构化异常事件采集，用于 dashboard 异常看板。
零新依赖（threading.Lock + dataclasses）。
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AnomalyEvent:
    """单个异常事件的完整结构。"""

    # 自动生成
    id: str = ""                    # hex(ts_ms) + "-" + 4-hex-counter (by collector)
    ts: float = 0.0                 # time.time()

    # 分类
    category: str = ""              # "routing" | "model" | "auth" | "config" | "cloud"
    severity: str = ""              # "info" | "warning" | "error" | "critical"

    # 上下文
    model: str = ""                 # 请求中的模型名（或 "" 若非请求触发）
    message: str = ""               # 人类可读描述
    status_code: int = 0            # HTTP 返回码（0 若非 HTTP 场景）
    possible_cause: str = ""        # 面向运维的结构化原因

    # 可选扩展
    detail: Optional[dict] = None   # 额外上下文（如 active_services 快照）


class AnomalyCollector:
    """Thread-safe ring buffer for anomaly events.

    纯内存、不持久化。最多保留 maxsize 条，超出时淘汰最旧事件。
    """

    def __init__(self, maxsize: int = 500):
        self._maxsize = maxsize
        self._events: list[AnomalyEvent] = []
        self._counter = 0
        self._lock = threading.Lock()

    def record(self, event: AnomalyEvent) -> None:
        """记录一条异常事件（线程安全）。"""
        with self._lock:
            event.id = f"{int(event.ts * 1000):x}-{self._counter:04x}"
            event.ts = event.ts or time.time()
            self._counter += 1
            self._events.append(event)
            if len(self._events) > self._maxsize:
                self._events.pop(0)

    def query(
        self,
        since: float = 0.0,
        limit: int = 100,
        category: Optional[str] = None,
        severity: Optional[str] = None,
    ) -> list[AnomalyEvent]:
        """查询异常事件（按时间倒序）。"""
        with self._lock:
            result = [e for e in self._events if e.ts >= since]
            if category:
                result = [e for e in result if e.category == category]
            if severity:
                result = [e for e in result if e.severity == severity]
            result.sort(key=lambda e: e.ts, reverse=True)
            return result[:limit]

    def count(self) -> int:
        """当前缓存的事件数。"""
        with self._lock:
            return len(self._events)

    def clear(self) -> None:
        """清空所有事件（调试/管理用）。"""
        with self._lock:
            self._events.clear()