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
        self._maxsize = maxsize
        self._hits = 0  # 累计命中次数（clear 不归零，供网关控制卡展示）
        self._lock = threading.Lock()

    # ── 缓存键 ──

    @staticmethod
    def _make_key(model: str, body: dict) -> str:
        """生成缓存键：sha256(model + sort_keys_json(body))。

        排除 stream 字段（stream=true/false 不影响模型输出）。
        排除 body 的 model 字段（A2）：转发路径会把 body.model 改写为
        served_name，而 GET 侧用客户端原名（别名）——纳入键则两侧键
        永不一致。模型隔离仅由 model 参数（客户端原名）承担。
        """
        canonical = {k: v for k, v in body.items() if k not in ("stream", "model")}
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
                self._hits += 1
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

    def stats(self) -> dict:
        """LRU 生效可见性（网关控制卡展示）：命中次数 / 在用条目 / 上限。

        累计值语义：hits 在当前缓存实例内单调递增，clear() 不归零；
        开关 关→开 会重建实例（计数随之归零，与新 LRU 对齐）。"""
        with self._lock:
            return {"hits": self._hits, "size": len(self._cache),
                    "max": self._maxsize}