"""TokenStatsCollector — DB 驱动的引擎无关采集 + local/cloud 拆分 (P0 数据链修复)。

背景：旧 collector 只轮询 vllm/sglang 的 Prometheus 计数器（`_get_active_ports`
硬过滤 type in ("vllm","sglang")），ninfer/ollama 等引擎从不写入 token-stats.json，
导致 dashboard 按天数据卡在最后一个 vllm/sglang 活跃日。

本组测试锁死新契约：
  * `_state` / 文件 / `_load_full_state` / `query_db` 统一为
    `{"local": {date:{model:bucket}}, "cloud": {date:{model:bucket}}}` 双 scope 结构；
  * 有 `self._db` 时 `_collect_once` 走 DB 聚合（引擎无关，含 ninfer），
    按 `cloud_provider` 拆 local/cloud；无 db 时回退旧 Prometheus 路径（local only）；
  * 旧版扁平文件（`{date:{model:bucket}}`）加载时迁移到 local scope。

纯 mock，无 GPU / 网络（marker: unit）。
"""
import json
import time

import pytest

from inferfabric import token_stats
from inferfabric.token_stats import TokenStatsCollector


def _rows(now_ts):
    """构造 request_log 行：本地 ninfer + 云端模型（含 tokens_in/out）。"""
    return [
        {"model": "Qwen38-27B-TXT", "cloud_provider": None,
         "tokens_in": 1000, "tokens_out": 100, "timestamp": now_ts},
        {"model": "Qwen38-27B-TXT", "cloud_provider": None,
         "tokens_in": 500, "tokens_out": 50, "timestamp": now_ts + 5},
        {"model": "glm-5.1", "cloud_provider": "zhipu",
         "tokens_in": 2000, "tokens_out": 200, "timestamp": now_ts + 10},
    ]


class _MockDB:
    """最小 IFFDB 替身：query_request_log(since, limit) → rows。"""

    def __init__(self, rows):
        self._rows = rows

    def query_request_log(self, since=None, until=None, model=None, limit=10000):
        return list(self._rows)


def _mock_db(rows):
    return _MockDB(rows)


def _patch_state_file(monkeypatch, tmp_path):
    """把 STATE_FILE 指向临时文件，防止 _collect_once/_persist 写真实文件。"""
    sf = tmp_path / "token-stats.json"
    monkeypatch.setattr(token_stats, "STATE_FILE", sf)
    return sf


# ── 结构契约 ────────────────────────────────────────────────

def test_state_is_two_scope():
    c = TokenStatsCollector()
    assert set(c._state.keys()) == {"local", "cloud"}
    assert c._state["local"] == {} and c._state["cloud"] == {}


def test_load_full_state_two_scope_shape(monkeypatch, tmp_path):
    # 隔离真实 ~/.inferfabric/token-stats.json（_load_full_state 会 _load_from_file）
    _patch_state_file(monkeypatch, tmp_path)
    c = TokenStatsCollector()
    c._state["local"]["2026-09-21"] = {"m": {"prompt_tokens": 1, "generation_tokens": 2, "requests": 1}}
    out = c._load_full_state()
    assert out == {
        "local": {"2026-09-21": {"m": {"prompt_tokens": 1, "generation_tokens": 2, "requests": 1}}},
        "cloud": {},
    }


# ── DB 驱动采集（含 ninfer）─────────────────────────────

def test_collect_once_from_db_includes_ninfer_local_and_cloud(monkeypatch, tmp_path):
    _patch_state_file(monkeypatch, tmp_path)
    now = time.time()
    c = TokenStatsCollector(manager_ref=None, db=_mock_db(_rows(now)))
    c._collect_once()
    # ninfer（本地）计入 local；glm-5.1（云端）计入 cloud
    assert "Qwen38-27B-TXT" in c._state["local"][c._today_key()]
    b = c._state["local"][c._today_key()]["Qwen38-27B-TXT"]
    assert b["prompt_tokens"] == 1500
    assert b["generation_tokens"] == 150
    assert b["requests"] == 2
    assert "glm-5.1" in c._state["cloud"][c._today_key()]
    assert c._state["cloud"][c._today_key()]["glm-5.1"]["prompt_tokens"] == 2000


def test_collect_once_without_db_falls_back_to_prometheus(monkeypatch, tmp_path):
    """无 db（且 manager_ref 返回空 services）→ 不抛，local 保持空。"""
    _patch_state_file(monkeypatch, tmp_path)
    c = TokenStatsCollector(manager_ref=lambda: _EmptyMgr(), db=None)
    c._collect_once()
    assert c._state["local"] == {} and c._state["cloud"] == {}


# ── query_db 双 scope ──────────────────────────────────────

def test_query_db_returns_two_scope_sorted():
    now = time.time()
    c = TokenStatsCollector(manager_ref=None, db=_mock_db(_rows(now)))
    out = c.query_db("weekly")
    assert set(out.keys()) == {"local", "cloud"}
    assert "Qwen38-27B-TXT" in out["local"][c._today_key()]
    assert "glm-5.1" in out["cloud"][c._today_key()]
    assert list(out["local"].keys()) == sorted(out["local"].keys())


def test_query_db_no_db_returns_none():
    c = TokenStatsCollector(manager_ref=None, db=None)
    assert c.query_db("weekly") is None


# ── 旧扁平文件迁移 ─────────────────────────────────────────

def test_load_from_file_migrates_legacy_flat_to_local(monkeypatch, tmp_path):
    sf = tmp_path / "token-stats.json"
    legacy = {"2026-09-15": {"Qwen38-27B-VL": {"prompt_tokens": 9, "generation_tokens": 1, "requests": 3}}}
    sf.write_text(json.dumps(legacy))
    monkeypatch.setattr(token_stats, "STATE_FILE", sf)
    c = TokenStatsCollector()
    c._load_from_file()
    assert c._state["local"]["2026-09-15"]["Qwen38-27B-VL"]["requests"] == 3
    assert c._state["cloud"] == {}


def test_load_from_file_two_scope_roundtrip(monkeypatch, tmp_path):
    sf = tmp_path / "token-stats.json"
    two_scope = {
        "local": {"2026-09-20": {"a": {"prompt_tokens": 1, "generation_tokens": 1, "requests": 1}}},
        "cloud": {"2026-09-20": {"b": {"prompt_tokens": 2, "generation_tokens": 2, "requests": 1}}},
    }
    sf.write_text(json.dumps(two_scope))
    monkeypatch.setattr(token_stats, "STATE_FILE", sf)
    c = TokenStatsCollector()
    c._load_from_file()
    assert c._state["local"]["2026-09-20"]["a"]["prompt_tokens"] == 1
    assert c._state["cloud"]["2026-09-20"]["b"]["prompt_tokens"] == 2


# ── 持久化 / 清理 双 scope ─────────────────────────────────

def test_persist_writes_two_scope(monkeypatch, tmp_path):
    sf = _patch_state_file(monkeypatch, tmp_path)
    c = TokenStatsCollector()
    c._state["local"]["2026-09-21"] = {"m": {"prompt_tokens": 5, "generation_tokens": 5, "requests": 1}}
    c._state["cloud"]["2026-09-21"] = {"c": {"prompt_tokens": 7, "generation_tokens": 7, "requests": 1}}
    c._persist()
    data = json.loads(sf.read_text())
    assert data["local"]["2026-09-21"]["m"]["requests"] == 1
    assert data["cloud"]["2026-09-21"]["c"]["requests"] == 1


def test_cleanup_removes_expired_in_both_scopes():
    c = TokenStatsCollector()
    old = "2000-01-01"
    c._state["local"][old] = {"m": {"prompt_tokens": 0, "generation_tokens": 0, "requests": 0}}
    c._state["cloud"][old] = {"m": {"prompt_tokens": 0, "generation_tokens": 0, "requests": 0}}
    c._cleanup()
    assert old not in c._state["local"]
    assert old not in c._state["cloud"]


class _EmptyMgr:
    def status(self):
        return {"services_info": {}}


# ── 生产接线：TelemetryHub 启动 db-backed collector ─────────────

def test_start_token_collector_injects_manager_ref(monkeypatch):
    """入口点通过 TelemetryHub.start_token_collector 启动 db-backed collector。

    锁死修复：start_token_collector 必须把 manager_ref 写到 collector.manager_ref
    （构造器属性），而非不存在的 _manager_ref（旧 bug → 注入失效）。
    并确认它调用 start()。用 __new__ 跳过重 IFFDB/线程构造。
    """
    from unittest.mock import patch
    from inferfabric.telemetry import TelemetryHub
    from inferfabric.token_stats import TokenStatsCollector

    hub = TelemetryHub.__new__(TelemetryHub)  # 跳过 __init__（避免开真实 IFFDB/线程）
    hub.token_collector = TokenStatsCollector(
        manager_ref=None, interval=300, db=_mock_db(_rows(time.time())))

    fn = lambda: None
    with patch.object(hub.token_collector, "start") as start_spy:
        hub.start_token_collector(fn)

    assert hub.token_collector.manager_ref is fn, "manager_ref 未注入到 collector"
    start_spy.assert_called_once_with()
    # db-backed collector 的 _collect_once 走 DB 路径，聚合出双 scope
    hub.token_collector._collect_once()
    assert hub.token_collector._state["local"]  # ninfer 本地行
    assert hub.token_collector._state["cloud"]  # glm-5.1 云端行
