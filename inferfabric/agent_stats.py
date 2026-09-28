"""inferfabric/agent_stats.py — 客户端 Agent 统计纯聚合（v6.5）。

从 request_log 行做时间分桶 × Agent 分组；只依赖本模块 + metrics_aggregator，
不依赖 HTTP/DB——便于单测。数据源与语义对齐 /api/token-curve。
"""
from __future__ import annotations

import time
from collections import defaultdict, Counter
from typing import Mapping

from inferfabric.metrics_aggregator import cost_of_row

# v7.0 调色板系列槽（与 charts.js PALETTES 对齐；跳过琥珀[1] 累计线专用槽）。
# 用户认领的 agent（source='user'）按 rank 取色；内置 agent 保留品牌色。
PALETTE_SERIES = ['#2563eb', '#0891b2', '#15803d', '#db2777']  # 蓝/青/绿/粉（跳琥珀）

AGENT_GRAN = {
    "minute": {"since": 3600,       "n": 12, "width_s": 5 * 60},
    "hour":   {"since": 24 * 3600,  "n": 24, "width_s": 3600},
    "day":    {"since": 30 * 86400, "n": 30, "width_s": 86400},
    "week":   {"since": 90 * 86400, "n": 13, "width_s": 7 * 86400},
}

_UNKNOWN_META = {"name": "未识别", "color": "#9ca3af", "source": "observed"}


def _empty_buckets(n):
    return [{"x": i, "requests": 0, "tokens": 0, "cost": 0.0} for i in range(n)]


def aggregate_agent_stats(rows, gran, scope, prices, meta,
                          now: float | None = None) -> dict:
    """rows: request_log dict 列表; gran/scope 见端点; meta: {agent: {name,color,source}};
    now: 墙钟右缘（缺省 time.time()；测试注入固定值保证分桶可断言）。"""
    spec = AGENT_GRAN[gran]
    n, w_s = spec["n"], spec["width_s"]
    now = time.time() if now is None else now

    def in_scope(r):
        cp = r.get("cloud_provider")
        if scope == "local":
            return not cp
        if scope == "cloud":
            return bool(cp)
        return True

    series: dict[str, list] = {}
    totals: dict[str, dict] = {}
    unk_counter: Counter = Counter()
    for r in rows:
        if not in_scope(r):
            continue
        # v6.6: agent='' 的行是采集上线前的历史存量 / 采集漏点（无可靠信号），
        # 不进聚合——否则 14 万历史空行淹没真实识别率。agent='unknown'（分类器
        # 跑过未命中）保留。
        if not r.get("agent"):
            continue
        ts = r.get("timestamp")
        if not ts:
            continue
        idx = n - 1 - int((now - ts) // w_s)
        if not (0 <= idx < n):
            continue
        agent = r.get("agent") or "unknown"
        ser = series.setdefault(agent, _empty_buckets(n))
        tin = int(r.get("tokens_in") or 0)
        tout = int(r.get("tokens_out") or 0)
        b = ser[idx]
        b["requests"] += 1
        b["tokens"] += tin + tout
        b["cost"] += cost_of_row(prices, r)
        agg = totals.setdefault(agent, {
            "agent": agent, "name": agent, "color": "#9ca3af", "source": "observed",
            "requests": 0, "success": 0, "errors": 0, "success_rate": 0.0,
            "tokens_in": 0, "tokens_out": 0, "cost_yuan": 0.0,
            "ttft": [], "top_models": Counter(),
        })
        agg["requests"] += 1
        agg["success"] += 1 if (r.get("status") or 0) < 400 and not r.get("error") else 0
        agg["errors"] += 0 if (r.get("status") or 0) < 400 and not r.get("error") else 1
        agg["tokens_in"] += tin
        agg["tokens_out"] += tout
        agg["cost_yuan"] += cost_of_row(prices, r)
        ttft = r.get("ttft_ms")
        if ttft and ttft > 0 and (r.get("status") or 0) < 400:
            agg["ttft"].append(float(ttft))
        agg["top_models"][r.get("model") or ""] += 1
        if agent == "unknown":
            ua = str(r.get("ua") or "")[:120]
            if ua:
                unk_counter[ua] += 1

    result: list[dict] = []
    for agent, agg in totals.items():
        m = meta.get(agent, _UNKNOWN_META)
        ttfts = sorted(agg["ttft"])
        def _q(q):
            if not ttfts:
                return None
            k = max(1, round(len(ttfts) * q)) - 1
            return round(ttfts[k], 1)
        top = [{"model": mname, "requests": c}
               for mname, c in agg["top_models"].most_common(3)]
        result.append({
            "agent": agent, "name": m.get("name", agent), "color": m.get("color", "#9ca3af"),
            "source": m.get("source", "observed"),
            "requests": agg["requests"], "success": agg["success"], "errors": agg["errors"],
            "success_rate": round(agg["success"] / max(agg["requests"], 1) * 100, 1),
            "tokens_in": agg["tokens_in"], "tokens_out": agg["tokens_out"],
            "cost_yuan": round(agg["cost_yuan"], 4),
            "ttft_p50": _q(0.50), "ttft_p95": _q(0.95), "top_models": top,
        })

    total_req = max(sum(t["requests"] for t in result), 1)
    for t in result:
        t["pct_of_requests"] = round(t["requests"] / total_req * 100, 1)
    result.sort(key=lambda t: -t["requests"])
    unk = next((t for t in result if t["agent"] == "unknown"), None)
    if unk:
        result.remove(unk)
        result.insert(0, unk)  # 未识别置顶（设计 §5）
    # v6.6: 用户认领 agent（source='user'）按 rank 走 palette 补色（跳琥珀[1]）；
    # builtin 保留品牌色；unknown 保留灰。仅对 totals 中的非 unknown、非 builtin 行赋色。
    pal_idx = 0
    for t in result:
        if t["agent"] == "unknown":
            continue
        if t.get("source") == "user":
            t["color"] = PALETTE_SERIES[pal_idx % len(PALETTE_SERIES)]
            pal_idx += 1
    for _, ser in series.items():
        for b in ser:
            b["cost"] = round(b["cost"], 4)

    unassigned = [{"ua": u, "requests": c} for u, c in unk_counter.most_common(20)]
    return {
        "n": n, "width_s": w_s, "window_requests": sum(t["requests"] for t in result),
        "series": series, "totals": result, "unassigned": unassigned,
    }
