"""
inferfabric/proxy/response_cache.py — 精确匹配响应缓存 (R5)

使用 cachetools.LRUCache（内存 LRU，线程安全）。
仅缓存 non-streaming / temperature=0 的 200 响应。
"""

import hashlib
import json
import threading
import time
from typing import Any, Optional

from cachetools import LRUCache


class ResponseCache:
    """精确匹配响应缓存。

    缓存键 = sha256(model + canonical_json(body))。
    用于 agent 场景 system prompt 重复时的去重生成。
    """

    def __init__(self, maxsize: int = 500, max_body_bytes: int = 512 * 1024):
        self._cache = LRUCache(maxsize=maxsize)
        self._max_body_bytes = max_body_bytes
        self._lock = threading.Lock()

    # ── 缓存键 ──

    @staticmethod
    def _make_key(model: str, body: dict) -> str:
        """生成缓存键：sha256(model + sort_keys_json(body))。

        排除 stream 字段（stream=true/false 不影响模型输出）。
        """
        canonical = {k: v for k, v in body.items() if k != "stream"}
        raw = model + "\n" + json.dumps(canonical, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ── 准入条件 ──

    @staticmethod
    def _should_cache(data: dict, status: int, allow_nonzero_temp: bool = False) -> bool:
        """判断此次响应是否应被缓存。"""
        if data.get("stream", False):
            return False
        if status != 200:
            return False
        temp = data.get("temperature", 0)
        if temp is None:
            temp = 0
        if temp != 0 and not allow_nonzero_temp:
            return False
        return True

    # ── 读写 ──

    def get(self, model: str, body: dict) -> Optional[dict]:
        """查缓存。命中时返回完整响应体，未命中返回 None。"""
        key = self._make_key(model, body)
        with self._lock:
            try:
                entry = self._cache[key]  # LRUCache.__getitem__ → 自动 move_to_end
                return entry
            except KeyError:
                return None

    def put(self, model: str, body: dict, response_body: dict, usage: dict) -> None:
        """写入缓存。超过单条体积上限不缓存。"""
        key = self._make_key(model, body)
        entry = {
            "body": response_body,
            "usage": usage,
            "cached_at": time.time(),
        }
        # 估算体积
        approx_size = len(json.dumps(entry, ensure_ascii=False))
        if approx_size > self._max_body_bytes:
            return
        with self._lock:
            self._cache[key] = entry

    # ── 管理 ──

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)