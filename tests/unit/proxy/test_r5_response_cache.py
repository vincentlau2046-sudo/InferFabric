"""
unit/proxy/test_r5_response_cache.py — R5: 精确匹配响应缓存

测试对象:
  - inferfabric.proxy.response_cache.ResponseCache
  - cachetools.LRUCache 集成

覆盖范围:
  - 缓存键一致：相同 model + body → 命中
  - 缓存键区分：不同 model → 不命中
  - 缓存键区分：不同 body → 不命中
  - stream=true → 不缓存
  - temperature=0.7 → 不缓存（allow_nonzero_temp=False）
  - 单条超标 → 不缓存
  - clear 清空
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# 添加 _deps 路径（cachetools 等额外依赖）
_deps = Path(__file__).parent.parent.parent.parent.parent / "_deps"
if _deps.is_dir():
    sys.path.insert(0, str(_deps))

import pytest
from inferfabric.proxy.response_cache import ResponseCache


# ═══════════════════════════════════════════════════════════════
# 1. 缓存键 & 命中
# ═══════════════════════════════════════════════════════════════


def test_cache_hit():
    """相同 model + body → 命中。"""
    cache = ResponseCache(maxsize=100)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("m", body, {"response": "hello"}, {"prompt_tokens": 5, "completion_tokens": 3})
    hit = cache.get("m", body)
    assert hit is not None
    assert hit["body"]["response"] == "hello"
    assert hit["usage"]["prompt_tokens"] == 5


def test_cache_miss_different_model():
    """不同 model → 不命中。"""
    cache = ResponseCache()
    body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("model-a", body, {"r": "a"}, {})
    hit = cache.get("model-b", body)
    assert hit is None


def test_cache_miss_different_body():
    """不同 body → 不命中。"""
    cache = ResponseCache()
    cache.put("m", {"messages": [{"role": "user", "content": "hello"}], "temperature": 0},
              {"r": "hello"}, {})
    hit = cache.get("m", {"messages": [{"role": "user", "content": "world"}], "temperature": 0})
    assert hit is None


def test_cache_stream_excluded_from_key():
    """stream 字段不影响缓存键：stream=true 和 stream=false 应命中同一缓存。"""
    cache = ResponseCache()
    # 存入 stream=false
    body_a = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0, "stream": False}
    cache.put("m", body_a, {"r": "same"}, {})
    # 用 stream=true 查询
    body_b = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0, "stream": True}
    # 但 stream=true 不在准入条件内，通过手动 get 测试键一致性
    key_a = cache._make_key("m", body_a)
    key_b = cache._make_key("m", body_b)
    assert key_a == key_b


# ═══════════════════════════════════════════════════════════════
# 2. 准入条件
# ═══════════════════════════════════════════════════════════════


def test_stream_not_cached():
    """stream=true → should_cache=False。"""
    assert not ResponseCache._should_cache({"stream": True, "temperature": 0}, 200)


def test_temperature_nonzero_not_cached():
    """temperature=0.7 → should_cache=False。"""
    assert not ResponseCache._should_cache({"temperature": 0.7}, 200)


def test_temperature_zero_cached():
    """temperature=0 → should_cache=True。"""
    assert ResponseCache._should_cache({"temperature": 0}, 200)


def test_temperature_none_cached():
    """temperature=None → should_cache=True（默认 0）。"""
    assert ResponseCache._should_cache({"temperature": None}, 200)


def test_non_200_not_cached():
    """status=500 → should_cache=False。"""
    assert not ResponseCache._should_cache({"temperature": 0}, 500)


# ═══════════════════════════════════════════════════════════════
# 3. 字节上限 & LRU 淘汰
# ═══════════════════════════════════════════════════════════════


def test_oversized_not_cached():
    """单条超过 max_body_bytes → 不缓存。"""
    cache = ResponseCache(maxsize=10, max_body_bytes=50)
    body = {"messages": [{"role": "user", "content": "x"}], "temperature": 0}
    huge = {"text": "x" * 500}
    cache.put("m", body, huge, {})
    assert cache.size == 0


def test_lru_eviction():
    """超过 maxsize 时淘汰最旧条目。"""
    cache = ResponseCache(maxsize=3)
    for i in range(4):
        body = {"messages": [{"role": "user", "content": str(i)}], "temperature": 0}
        cache.put("m", body, {"r": str(i)}, {})
    assert cache.size == 3
    # 第一条应该被淘汰
    hit = cache.get("m", {"messages": [{"role": "user", "content": "0"}], "temperature": 0})
    assert hit is None, "oldest entry should be evicted"


def test_lru_access_refreshes():
    """被访问的条目移到末尾，不被 LRU 淘汰。"""
    cache = ResponseCache(maxsize=3)
    for i in range(3):
        body = {"messages": [{"role": "user", "content": str(i)}], "temperature": 0}
        cache.put("m", body, {"r": str(i)}, {})

    # cache = [0, 1, 2]
    # 访问 0 → 移到末尾 → [1, 2, 0]
    body0 = {"messages": [{"role": "user", "content": "0"}], "temperature": 0}
    cache.get("m", body0)

    # 再 put 一条 → 淘汰最旧的 1 → [2, 0, 3]
    body3 = {"messages": [{"role": "user", "content": "3"}], "temperature": 0}
    cache.put("m", body3, {"r": "3"}, {})

    assert cache.get("m", body0) is not None, "accessed entry 0 should survive"
    assert cache.get("m", {"messages": [{"role": "user", "content": "1"}], "temperature": 0}) is None, "oldest 1 evicted"
    assert cache.get("m", {"messages": [{"role": "user", "content": "2"}], "temperature": 0}) is not None


# ═══════════════════════════════════════════════════════════════
# 4. 管理
# ═══════════════════════════════════════════════════════════════


def test_clear():
    """clear 清空所有条目。"""
    cache = ResponseCache()
    body = {"messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    cache.put("m", body, {"r": "x"}, {})
    assert cache.size == 1
    cache.clear()
    assert cache.size == 0
    assert cache.get("m", body) is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))