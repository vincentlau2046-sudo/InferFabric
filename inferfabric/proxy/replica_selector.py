"""
inferfabric/proxy/replica_selector.py — 多副本负载均衡 (R7 Step 2+3)

为同一模型的多副本提供 least_busy / round_robin 路由选择。
零新依赖。
"""

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ReplicaInfo:
    """单个副本信息。"""
    port: int
    weight: float = 1.0
    healthy: bool = True


class ReplicaSelector:
    """多副本选择器 — 线程安全，支持 least_busy / round_robin。

    least_busy（默认）：选择 active_requests 最少的副本。
    round_robin：轮转选择。
    """

    def __init__(self, replicas: list[ReplicaInfo], strategy: str = "least_busy"):
        self._replicas = replicas
        self._strategy = strategy
        self._rr_index = 0
        self._active: dict[int, int] = defaultdict(int)  # port → 在飞请求数
        self._lock = threading.Lock()

    def select(self) -> int:
        """选择一个副本端口。"""
        with self._lock:
            healthy = [r for r in self._replicas if r.healthy]
            if not healthy:
                healthy = self._replicas  # fallback: 全部不健康也用第一个

            if self._strategy == "round_robin":
                r = healthy[self._rr_index % len(healthy)]
                self._rr_index += 1
            else:  # least_busy
                r = min(healthy, key=lambda x: self._active.get(x.port, 0))

            self._active[r.port] += 1
            return r.port

    def release(self, port: int):
        """释放副本的并发计数。"""
        with self._lock:
            if port in self._active and self._active[port] > 0:
                self._active[port] -= 1

    def mark_healthy(self, port: int, healthy: bool):
        """设置副本健康状态。"""
        with self._lock:
            for r in self._replicas:
                if r.port == port:
                    r.healthy = healthy
                    break

    @property
    def all_ports(self) -> list[int]:
        return [r.port for r in self._replicas]

    @property
    def healthy_ports(self) -> list[int]:
        return [r.port for r in self._replicas if r.healthy]