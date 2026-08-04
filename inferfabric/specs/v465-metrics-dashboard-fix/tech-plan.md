# IFF v4.6.5 Tech Plan

## M-1: 空窗口 schema 修复

**文件**: `inferfabric/metrics_aggregator.py`
**改动**: 1 行 → 7 行

```python
# Before:
if not samples:
    return {"window": window, "total": 0}

# After:
if not samples:
    return {
        "window": window, "total_requests": 0, "success": 0,
        "fail": 0, "models": {}, "cost_yuan": 0.0, "success_rate": 0.0,
    }
```

**风险**: 零 — 纯加字段，不改现有逻辑

---

## M-2: 失败请求过滤

**文件**: `inferfabric/metrics_aggregator.py`
**改动**: 2 行条件增加

```python
# Before:
ttfts = [s["ttft_ms"] for s in msamples if s.get("ttft_ms") and s["ttft_ms"] > 0]
durations = [s["duration_ms"] for s in msamples if s.get("duration_ms") and s["duration_ms"] > 0]

# After:
ttfts = [s["ttft_ms"] for s in msamples if s["status"] < 400 and s.get("ttft_ms") and s["ttft_ms"] > 0]
durations = [s["duration_ms"] for s in msamples if s["status"] < 400 and s.get("duration_ms") and s["duration_ms"] > 0]
```

**风险**: 低 — 只影响分位计算，不影响计数（success/fail 字段逻辑不变）

---

## D-1: 请求日志 API + Panel

**后端**: `inferfabric/proxy/handler.py`
- 新增 `_handle_request_log(self, pm)` 方法
- `do_GET` 路由 `/api/request_log` → 调用 `pm.request_log_db.query_request_log()`
- 参数: `limit` (默认 50), `since` (timestamp)
- 返回: JSON 数组，每条含 timestamp/model/status/tokens_in/tokens_out/ttft_ms/duration_ms/route

**前端**: `inferfabric/dashboard/js/monitor.js`
- `loadRequestLog()` 改为 fetch `/api/request_log?limit=50`
- 渲染可滚动表格
- 5s 自动刷新

**风险**: 中 — 新增端点 + 前端渲染，需验证 XSS 防护（已有 `esc()` 函数）

---

## D-2: 模型名映射

**文件**: `inferfabric/proxy_manager.py` + `inferfabric/metrics_aggregator.py`

1. `ProxyManager.__init__()` 构造 name_map:
```python
self._metrics_name_map = {}
for m in self.mgr.list_models():
    if hasattr(m, 'served_name') and m.served_name:
        self._metrics_name_map[m.served_name] = m.name
```

2. `MetricsAggregator.__init__()` 增加 `model_name_map` 参数

3. `get_metrics()` 输出时映射:
```python
friendly = self._name_map.get(model, model)
result["models"][friendly] = m
```

**风险**: 低 — 纯展示层映射，不改存储

---

## 版本号

`inferfabric/__init__.py`: `__version__ = "4.6.5"`

---

## 任务依赖图

```
M-1 ──→ (独立)
M-2 ──→ (独立)
D-1 ──→ (独立，但测试时需 proxy 运行)
D-2 ──→ (依赖 ProxyManager 初始化逻辑)
Version ──→ (最后执行)
```

M-1 和 M-2 可并行；D-1 和 D-2 可并行。
