# IFF v4.6.1 PR 清单

**日期**: 2026-07-31
**基线**: v4.6.0 (commit a61914e)

---

## PR-E: RateLimiterV2 集成

**优先级**: 中
**状态**: 待开发

### 背景

RateLimiterV2 (TokenBucket) 已在 `ratelimit.py` 中完整实现并通过基准测试，但 handler 仍使用旧的 `_RateLimiter` (Semaphore) + `_MODEL_RATE_LIMITERS` dict。

### 当前接口

```python
# handler.py:411-412 — 旧接口
from inferfabric.ratelimit import _get_model_rate_limiter
limiter = _get_model_rate_limiter(pm, model_name)
# 用法: with limiter: (Semaphore 上下文管理器)
```

### 目标接口

```python
# RateLimiterV2 — 新接口
ok, reason = pm.rate_limiter.acquire(model_name)
if not ok:
    return 429
# ... 执行请求 ...
pm.rate_limiter.release(model_name)
```

### 变更范围

1. `proxy/handler.py` — `_handle_chat()` 和 `_handle_messages()` 中的限流点从 `with _get_model_rate_limiter()` 改为 `acquire()/release()`
2. `proxy_manager.py` — 初始化 `RateLimiterV2` 实例，注册已知模型桶
3. `ratelimit.py` — 确认 V2 兼容层足够，迁移完成后可删除 `_RateLimiter` / `_MODEL_RATE_LIMITERS` / `_get_model_rate_limiter`
4. 测试 — 确认 429 行为、burst 支持、server+model 二级门控

### 风险

- 低：限流逻辑独立于路由/转发，替换只影响请求准入判断
- 需验证：cloud 路由也需加限流（当前无限流）

---

## PR-F: Dashboard Provider 持久化

**优先级**: 中
**状态**: 待开发

### 背景

通过 Dashboard 或 Admin API 添加的 cloud provider 只存在于内存，IFF 重启后丢失。需要将变更写回 `cloud_provider.yaml`。

### 当前行为

```
Dashboard "Add Provider" → 内存中 CloudDiscovery._providers 新增 → 重启 → 丢失
```

### 目标行为

```
Dashboard "Add Provider" → 内存新增 + 写回 cloud_provider.yaml → 重启 → 保留
```

### 变更范围

1. `cloud_discovery.py` — 新增 `save_config()` 方法，将当前 providers 序列化为 YAML 写回 `IFF_DATA_DIR / "cloud_provider.yaml"`
2. `proxy/handler.py` — `_handle_cloud_providers()` POST/DELETE 路由在内存操作成功后调用 `save_config()`
3. 安全 — 写入前备份原文件（`.bak`），写入后校验 YAML 可解析
4. 测试 — 添加/删除 provider → 重启 IFF → 确认持久化

### 风险

- 中：YAML 序列化可能丢失注释和格式
- 缓解：用 `ruamel.yaml`（保留注释）或仅追加差异段

---

## PR-G: 指标监控增强

**优先级**: 高
**状态**: 待开发

### 背景

当前 IFF 的指标监控存在三层缺失：

1. **数据采集不全** — `RequestLog` 有 `ttft_ms`/`tokens_in`/`tokens_out` 字段，但 cloud 路由和部分 local 路由未填入；29 条 access log 中 tokens 全为 0
2. **聚合能力缺失** — `TokenStatsCollector` 只从 vLLM Prometheus 拉取计数器（仅限本地），无 cloud 请求统计、无费用追踪、无延迟分位
3. **Dashboard 展示简陋** — 只有 vLLM 实时指标 + token 用量柱图 + GPU 指标 + 切换历史，无请求成功率、无延迟分布、无模型级对比、无费用概览

### LiteLLM 参考对比

| 功能 | LiteLLM | IFF 现状 | 差距 |
|------|---------|---------|------|
| Token 用量追踪 | ✅ 按日/模型/key 聚合 + 持久化 | ⚠️ 仅本地 vLLM Prometheus 计数器 | cloud 路由无统计 |
| 费用/成本追踪 | ✅ 按模型计费 + USD 换算 | ❌ 无 | 全缺 |
| 请求成功率 | ✅ success rate per model | ❌ 无 | access log 有 status 但无聚合 |
| 延迟分位 | ✅ p50/p95/p99 | ❌ 无 | access log 有 ttft/duration 但未填充 |
| 请求日志查询 | ✅ UI 可搜索/过滤 | ❌ 只有 JSONL 文件 | 全缺 |
| Prometheus 导出 | ✅ `/metrics` 标准格式 | ⚠️ 仅代理 vLLM 的 | IFF 自身无 metrics |
| 模型级对比 | ✅ 按模型分列 | ❌ 无 | 全缺 |
| API Key 级统计 | ✅ 按 key 追踪 | ❌ 无 | 单用户场景不需要 |

### 需求拆分

#### G-1: RequestLog 数据补全（基础层）

当前 cloud 路由的 `RequestLog` 缺失 tokens、ttft、duration。修复后所有请求的日志字段完整。

**变更**：
1. `chat_handlers.py` — cloud 路由：从上游响应 `usage` 字段提取 `tokens_in/tokens_out`，记录 `ttft_ms` 和 `duration_ms`
2. `chat_handlers.py` — local 路由：确认 streaming 和 non-streaming 均正确填充 tokens
3. `forwarder.py` — `forward_to_cloud()` 返回值增加 usage/timing 信息

**验证**：发起 cloud 请求后 access log 中 tokens_in/tokens_out/ttft_ms 非零

#### G-2: 请求统计聚合器（MetricsAggregator）

新建内存中的滑动窗口聚合器，从 `RequestLogger` 实时消费数据。

**指标**：
- 请求计数 (total/success/fail)，按 model + route 分组
- Token 计数 (prompt/completion)，按 model 分组
- 延迟分位 (ttft p50/p95/p99, e2e p50/p95/p99)，按 model 分组
- 费用估算 (按 model 的 price_per_1M_tokens 计算)
- 滑动窗口：1h / 24h / 7d / 全部

**变更**：
1. 新增 `metrics_aggregator.py` — `MetricsAggregator` 类，线程安全
2. `cloud_provider.yaml` 中 model 配置增加 `price_input`/`price_output` (per 1M tokens) 字段
3. `RequestLogger.log()` 调用后同步推送到 aggregator
4. 新增 `/api/metrics` 端点，返回 JSON 聚合数据

**费用模型**（单用户简化版）：
```yaml
# cloud_provider.yaml 中 model 配置增加
models:
  deepseek-v4-flash:
    price_input: 1.0    # ¥/1M tokens
    price_output: 2.0   # ¥/1M tokens
  deepseek-v4-pro:
    price_input: 4.0
    price_output: 16.0
  glm-5:
    price_input: 0.5
    price_output: 0.5
  glm-5.1:
    price_input: 0.5
    price_output: 0.5
```

#### G-3: Dashboard 指标监控页重做

重写 `tab-monitor`，新增以下面板：

| 面板 | 内容 | 数据源 |
|------|------|--------|
| **请求概览** | 总请求数、成功率、平均延迟 | MetricsAggregator |
| **Token 用量** | 按模型分色的堆叠柱图 + 日趋势 | MetricsAggregator |
| **延迟分布** | 按模型的 TTFT/E2E 分位图 | MetricsAggregator |
| **费用概览** | 按模型费用饼图 + 日趋势 | MetricsAggregator + price 配置 |
| **请求日志** | 可过滤/搜索的请求列表 (最近 100 条) | access log 文件 |
| **vLLM 性能** | (保留现有) KV Cache/TPOT/TTFT | vLLM /metrics |
| **GPU 实时** | (保留现有) VRAM/利用率/功耗 | nvidia-smi |

#### G-4: IFF 自身 Prometheus `/metrics` 端点

导出 IFF 级别的标准 Prometheus 指标，供 Grafana 等外部工具消费。

**指标**：
```
iff_requests_total{model,route,status}
iff_tokens_total{model,direction="input|output"}
iff_request_duration_seconds{model,quantile}
iff_ttft_seconds{model,quantile}
iff_cloud_cost_yuan_total{model}
```

### 实现优先级

| 子项 | 优先级 | 依赖 | 说明 |
|------|--------|------|------|
| G-1 | P0 | 无 | 基础层，不补全数据后面全白搭 |
| G-2 | P0 | G-1 | 核心聚合能力 |
| G-3 | P1 | G-2 | 可视化，G-1+G-2 就能看数据 |
| G-4 | P2 | G-2 | 外部集成，非必须 |

### 不做的事

- ❌ API Key 级统计 — 单用户场景无需求
- ❌ PostgreSQL/Redis 依赖 — 单机不需要，JSON 文件 + 内存聚合足够
- ❌ 实时 WebSocket 推送 — 轮询 5s 已足够
- ❌ 多租户/团队管理 — 单用户
- ❌ 预算限制/告警 — 过度工程化
