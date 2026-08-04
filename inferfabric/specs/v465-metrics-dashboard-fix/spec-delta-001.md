# IFF v4.6.5 Spec — Metrics & Dashboard 数据一致性修复

**日期**: 2026-08-02
**基线**: v4.6.3 (commit `6b2bd89`)
**版本**: v4.6.5 (跳过 v4.6.4)

---

## 问题陈述

v4.6.3 的 MetricsAggregator 和 Dashboard 存在 4 个数据一致性问题，影响指标可信度和用户体验。

---

## Bug 清单

### M-1: `/api/metrics` 空窗口返回截断 schema

**严重度**: P0
**现象**: Dashboard 切到「1h」窗口时，若该时段无请求，请求概览 4 个数字显示 `undefined`。Token/延迟/费用面板显示「加载中…」而非「暂无数据」。
**根因**: `MetricsAggregator.get_metrics()` 在 `not samples` 时返回 `{"window": window, "total": 0}`，缺少 `total_requests`/`success`/`fail`/`models`/`cost_yuan`/`success_rate` 字段。
**验收标准**:
- AC-1: 空窗口 `/api/metrics?window=1h` 返回完整 schema
- AC-2: 所有字段值合理（total_requests=0, success=0, fail=0, models={}, success_rate=0.0）
- AC-3: Dashboard JS 正确显示 0 而非 undefined

### M-2: 失败请求污染延迟分位统计

**严重度**: P0
**现象**: qwen27b-vl 的 E2E p95 显示 30,001ms，实际 200 请求 p95 仅 ~6,276ms，虚高 378%。
**根因**: `get_metrics()` 计算 ttfts/durations 时未排除 `status >= 400` 的样本。429 请求的 duration_ms ≈ 30,000ms 被计入 p95/p99。
**验收标准**:
- AC-4: 延迟分位统计只包含 `status < 400` 的样本
- AC-5: 失败请求的计数仍保留在 requests/success/fail 字段中
- AC-6: 存在 429 数据时，p95 不再被拉高到超时值

### D-1: 请求日志 Panel 未实现

**严重度**: P1
**现象**: Dashboard Monitor 第 5 面板「请求日志」始终显示占位文本"请求日志需 access log API（后续实现）"。
**根因**: `RequestLogDB.query_request_log()` 方法已存在但未暴露 HTTP 端点；`monitor.js` 的 `loadRequestLog()` 是占位函数。
**验收标准**:
- AC-7: `GET /api/request_log` 返回最近 50 条日志 JSON
- AC-8: 支持 `?limit=N` 和 `?since=timestamp` 参数
- AC-9: Dashboard 请求日志面板渲染可滚动表格，列：时间/模型/状态/Token/TTFT/Duration
- AC-10: 面板 5s 自动刷新

### D-2: 模型名显示 served_name 而非友好名

**严重度**: P2
**现象**: Dashboard Token/延迟表格显示 `vllm_qwen27b_vl` 而非 `Qwen3.6-27B VL`。
**根因**: Aggregator 按 `model` 字段（= served_name）分组，无 name→friendly 映射。IFF 配置中 `name` 和 `served_name` 的映射在 `ModelManager` 中存在但未传递到 Aggregator。
**验收标准**:
- AC-11: `/api/metrics` 返回的 models key 使用友好名
- AC-12: 不影响数据库存储和 API 请求路由（仍用 served_name）

---

## 版本变更

- `inferfabric/__init__.py`: `__version__ = "4.6.5"`

---

## 不做的事

- ❌ 不修改数据库存储格式
- ❌ 不修改 API 请求路由逻辑
- ❌ 不增加 Prometheus /metrics 导出
- ❌ 不修改流控逻辑
