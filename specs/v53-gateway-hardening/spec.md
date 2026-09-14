# v53 — 网关加固需求（已批准范围）

> 状态：待审核（REVIEW）
> 范围基线：用户已逐项确认的需求；其余需求（预算/花费、per-key 限流、熔断、guardrails）明确不接受。
> 架构边界（用户硬性约束）：
> - IFF 是**透明协议网关**：客户端用什么协议（OpenAI/Anthropic）就透传到对应端点，**不做协议翻译**。
> - **模型自动切换是 Agent 的职责**：IFF 不替 Agent 做模型/路由层的自动切换或 fallback；当本地模型宕机时返回清晰错误让 Agent 决策，绝不能悄悄把请求转到其他模型/云端。
> - IFF 负责把"不同模型的批量并发请求"支撑好。
> - **不知道模型名就丢弃，不能静默 fallback**：不认识的模型名 → 404 + 异常记录，不能悄悄转发到任意跑着的 LLM 或云端。

---

## 需求清单（按优先级）

### R0. 🔴 修复：本地模型宕机后 `ensure_service` 无限重试循环
**状态：✅ 已实施并部署**

**现状**：当 vLLM 因 OOM/崩溃导致健康检查失败后，watchdog 触发 auto-restart → 启动失败 → 清空 `active_services`。此后每个请求都会触发 `ensure_service` → `switch()` → 又失败 → 因为 `_last_switch` 只在**成功**时设置（`proxy_manager.py:348`），失败路径不设 cooldown → 下一个请求再次触发 → SWITCHING 状态阻塞并发请求 → **无限 503 风暴**。

**要求**：`ensure_service` 的 switch **失败时也设置 `_last_switch`**，触发 10s cooldown；cooldown 期间的请求返回明确 503 + `Retry-After` 头。

**涉及**：`inferfabric/proxy_manager.py`（`ensure_service` 1 行 fix）+ `handler.py`/`chat_handlers.py`（cooldown 路径 503 补 `Retry-After` 头）。

---

### R8. 🔴 削除 Step 6/7 静默 fallback（新确认，最高优先级）

**现状**：`_handle_messages`（`handler.py:413-458`）底部有两个兜底路径：

**Step 6 — fallback to first active LLM**（line 413-425）：
```python
active_llm = None
for svc in pm.mgr.active_services:
    model_obj = pm.mgr.get_model(svc)
    if model_obj and model_obj.model_type in LOCAL_LLM_TYPES:
        if model_obj.port:
            active_llm = model_obj
            break
if active_llm:
    # 客户端发 model=xyz-not-exist，IFF 不认识
    # → 静默转发到 qwen38:8002
    # → 客户端收到莫名其妙的输出
    _forward_local(data, active_llm)
```

**Step 7 — cloud fallback**（line 427-458）：
```python
else:  # 无活跃 LLM
    # 遍历所有 cloud_models，随便找一个能匹配 short_name 的
    # → 发到云端 → 客户被扣费
    # → 云端收到不认识的 model_id → 可能报错或乱输出
    cloud_model = find_any_cloud_match(short_name)
    if cloud_model:
        forward_to_cloud(data, cloud_model)
```

**问题**：
1. 违反架构边界——"当本地模型宕机时返回清晰错误让 Agent 决策，绝不能悄悄把请求转到其他模型"。
2. Step 6 与 `resolve_route`（Step 5）功能重叠——`resolve_route` 已做过精确匹配，Step 6 是`resolve_route` 出现前的老逻辑。
3. Step 7 的 key 搜索逻辑与 `resolve_route` 的 `_cloud_models.get(short_name)` 完全等价（`cloud_models` 已同时缓存 `{model_id}` 和 `{provider}/{model_id}` 两种 key），**功能冗余**。
4. 客户端收到不期望的输出，无法区分是模型不匹配还是正确响应。

**要求**：
1. 削除 Step 6：不再 fallback 到首个活跃 LLM。改为记录 `AnomalyEvent` + 返回 404 `"Unknown model: {model}"`。
2. 削除 Step 7：不再遍历 cloud_models 硬匹配。与 Step 6 统一为 404。
3. 两个路径合并为一个统一的**未知模型拒绝逻辑**，共享异常记录。

**涉及**：`inferfabric/proxy/handler.py`（`_handle_messages` 底部 fallback 块）。

---

### R9. 🟡 AnomalyCollector 基础设施

**现状**：异常事件（未知模型、switch 失败、云端配置丢失等）无结构化记录，仅靠文本日志 `log.info/warning`，dashboard 无法聚合展示。

**要求**：
1. 新增 `AnomalyEvent` 数据结构 + `AnomalyCollector`（~60 行）。
2. `AnomalyCollector` 是线程安全的环形缓冲，最多保留 500 条事件。
3. 暴露 `GET /api/anomalies` 端点，支持按 `since`、`category`、`severity`、`limit` 查询。
4. 将 `AnomalyEvent` vs HTTP 状态码的对应关系固化为规范（见附录"错误码·故障矩阵"）。
5. 在各采集点（R0/R1/R8 覆盖的所有早退路径）调用 `pm.anomalies.record(...)`。
6. **零新 Python 依赖**，纯 `threading.Lock` + `dataclasses` + 环形缓冲。

**涉及**：新增 `inferfabric/anomaly_collector.py`；修改 `inferfabric/proxy_manager.py`（`__init__` 初始化）；修改 `handler.py`/`chat_handlers.py`（各采集点注入）。

---

### R10. 🟡 异常看板 dashboard tab

**现状**：IFF dashboard（`:8999/dashboard`）有 5 个 tab（总览、推理、监控、部署、云端），无异常聚合视图。

**要求**：
1. dashboard 新增"异常"tab——与推理/监控等现有 tab 并列。
2. tab 内容：
   - **实时异常事件列表**——按时间倒序排列。
   - 每条事件显示：时间 / HTTP 状态 / 模型名 / 错误消息 / 可能原因。
   - **按 severity 着色**：🟢 info → 蓝色 / 🟡 warning → 黄色 / 🔴 error → 红色 / ⚫ critical → 深红。
   - **筛选器**：按 category / severity 过滤。
   - **自动刷新**：每 10s 轮询 `/api/anomalies`。
3. 已有的 `AnomalyEvent.possible_cause` 字段直接渲染，让运维人员一眼知道"为什么"。

**涉及**：新增 `dashboard/fragments/anomaly.html`、`dashboard/js/anomaly.js`；修改 `dashboard/base.html`（+nav tab）、`dashboard/__init__.py`（+`"anomaly"` fragment）。

---

### R1. request_log 补盲区（P0）

**现状**：`_handle_messages` 的 SWITCHING guard 503 early-return 路径、以及其它 409/503/404 早退路径不写 `request_log`，排障时丢记录。

**要求**：所有非 200 的早期返回（503/409/404/401 等）都写入 `RequestLog`，保证每个请求（无论成功失败）在 `request_log` 中都有记录。

**涉及**：`inferfabric/proxy/handler.py`（`_handle_messages` 的 5 个早退分支）+ `inferfabric/proxy/chat_handlers.py`（`handle_chat` 的 3 个早退分支）。

---

### R2. 云端转发退避重试（P0）

**现状**：`forwarder.forward_to_cloud` 是**单次**请求；云端 429/5xx/timeout 直接透传，无重试。

**要求**：对云端转发增加 429/5xx/timeout 的指数退避重试（可配置次数与退避序列）。

**约束**：仅重试"可重试"的失败（429、5xx、连接/超时）；4xx（除 429 外）不重试。

**涉及**：`inferfabric/forwarder.py`（`forward_to_cloud`）。

---

### R3. 提高云端默认超时（60s → 600s）（P0）

**现状**：`ProviderConfig.timeout` 默认 60s；`cloud_provider.yaml` 里 baidu 未配置 timeout，实际用默认 60s。64k 长输出会在 60s 被掐断。

**要求**：把云端请求的默认超时提高到 600s（10 分钟），支撑长输出。允许 per-provider 通过 YAML `timeout` 覆盖。

**涉及**：`inferfabric/cloud_discovery.py`（`ProviderConfig.timeout` 默认值 + YAML 加载默认值 + preset 默认值）。

---

### R4. 增强 proxy 多模型批量请求与并发能力（P1）

**现状**：`DualGateLimiter` 的并发门是**全局** `threading.Semaphore(max_concurrent)`，所有模型共享一个并发池；多模型批量并发时互相抢槽。

**要求**：把并发控制做成 **per-model**（每个模型独立并发池），或提供可配置的全局并发上限 + per-model 上限。

**涉及**：`inferfabric/ratelimit.py`（`DualGateLimiter` 并发门 per-model 化）+ 配置（`iff.yaml` 的 `rate_limit.max_concurrent`）。

---

### R5. 精确匹配响应缓存（P1）

**现状**：无缓存；agent 场景 system prompt 高度重复，每次都重新生成，浪费 token。

**要求**：增加**精确匹配**响应缓存：以 (model + 规范化请求体哈希) 为键，命中则直接返回缓存响应。

**涉及**：新增 `inferfabric/proxy/response_cache.py` + `handler.py`/`chat_handlers.py` 缓存插入点 + 配置项。

---

### R6. 标准 /metrics（Prometheus 文本格式）+ 可选 OTel 导出（P1）

**要求**：
- 暴露标准 **Prometheus 文本格式**的 `/metrics`（`text/plain; version=0.0.4`）。
- 提供**可选**的 OpenTelemetry 导出（OTLP HTTP）开关与配置；默认关闭。

**涉及**：`inferfabric/proxy/metrics.py`（或新增 prometheus 端点）+ `inferfabric/telemetry.py`（OTel 导出）+ 配置。

---

### R7. 服务端模型升级：threaded → async + 多副本 + 最空闲/延迟路由（P2）

**要求**：
- HTTP 服务端从单进程 threaded 升级为 **async**（aiohttp）。
- 支持**多副本**部署 + 副本健康探测。
- 同模型多副本时提供 **least_busy / latency / round_robin** 路由策略。

**涉及**：`inferfabric/proxy/handler.py`（`ThreadedHTTPServer` + `main()`）+ 新增 async server 模块 + 副本注册与健康探测。

---

## 优先级与实施顺序

| 优先级 | 需求 | 说明 |
|--------|------|------|
| **R0** 🔴 | **ensure_service cooldown 修复** | ✅ 已实施部署 |
| **R8** 🔴 | **削除 Step 6/7 静默 fallback** | **本次优先实施——违反架构红线** |
| **R9** 🟡 | **AnomalyCollector 基础设施** | R8 的前提（fallback 改为 record anomaly + 404） |
| **R10** 🟡 | **异常看板 dashboard tab** | R9 的可视化输出 |
| P0 | R1 request_log 补盲区 | 用户原指定最高优先级 |
| P0 | R2 云端退避重试 | 解决 baidu 429/500/timeout 报错 |
| P0 | R3 超时 60s→600s | 长输出不被掐断 |
| P1 | R4 多模型批量并发 | 增强 proxy 多模型批量与并发 |
| P1 | R5 精确匹配响应缓存 | agent system prompt 去重省 token |
| P1 | R6 标准 /metrics + OTel | 可观测对齐成熟网关 |
| P2 | R7 async + 多副本 + 负载均衡 | 最大改动，最后做 |

## 明确不做（用户不接受）

- 预算/花费管理（per-key/per-provider cost 核算 + 预算上限）
- 每 key 限流（per-key RPM/TPM）
- 云端熔断（provider cooldown）
- Guardrails（提示词注入 / PII 检测）

---

## 附录 A：错误码 · 故障矩阵

IFF 返回给客户端的所有 HTTP 状态码，对应 Anomaly 事件、严重度、以及 Agent 应对指引。

| 状态码 | 场景 | Anomaly 类别 | 严重度 | 可能原因 | Agent 应对 |
|--------|------|-------------|--------|---------|-----------|
| **401** | Auth 检查失败 | `auth` | warning | API key 错误/过期/无权访问模型 | 检查 `x-api-key` / `Authorization` |
| **404** | 未知模型名（R8 后） | `routing` | warning | 模型名在本地 served_names 和 cloud_models 中都无匹配 | 检查模型名拼写 / 更新 cloud_provider.yaml |
| **409** | Switch 冲突（锁已占用） | `routing` | info | 另一请求正在发起同模型切换 | 立即重试（线程锁很快释放） |
| **429** | Rate limit 命中 | —（已有 RequestLog） | info | 服务器 RPM 上限 / 模型 RPM 上限 / 并发池满 | 等待后重试 |
| **503** | SWITCHING guard | `routing` | warning | 本地模型正在切换，请求的是另一本地模型 | 等待 `Retry-After: 30` 后重试 |
| **503** | Auto-switch 失败 | `model` | error | vLLM 启动失败 / OOM / 配置错误 | 等 `Retry-After: 10` 后重试，或切到云端 |
| **503** | 模型未激活且 AUTO_SWITCH=off | `config` | warning | 模型在配置中但未启动，auto-switch 被禁用 | 手动 `/switch` 或启用 auto-switch |
| **503** | Cooldown 阻挡（R0） | `model` | info | 上次 switch 失败，10s 冷却中 | 等 `Retry-After: 10` 后重试 |
| **503** | 无活跃 LLM + 无云端匹配 | `routing` | **critical** | 所有服务不可用 / cloud_provider.yaml 断裂 | 检查本地模型状态和云端配置 |
| **501** | 云端不支持指定协议 | `config` | error | provider 未配置 anthropic_base / openai_base | 检查 cloud_provider.yaml |
| **502** | 云端 HTTP 错误（重试耗尽） | `cloud` | error | 云端限频/故障/超时，R2 重试耗尽 | 后续重试，或切到其他 provider |

---

## 附录 B：Anomaly 事件体系

```python
@dataclass
class AnomalyEvent:
    id: str                # 唯一 ID（hex 时间戳 + 递进计数器）
    ts: float              # 事件时间（time.time()）
    category: str          # "routing" | "model" | "auth" | "config" | "cloud"
    severity: str          # "info" | "warning" | "error" | "critical"
    model: str             # 请求中的模型名
    message: str           # 人类可读描述
    status_code: int       # 返回给客户端的 HTTP 状态码
    possible_cause: str    # 面向运维的原因（直接可读）
    detail: dict | None    # 额外上下文（如 current_active_services 等）
```

### Anomaly 类别说明

| 类别 | 含义 | 典型事件 |
|------|------|---------|
| `routing` | 路由层异常 | 未知模型名被拒绝、SWITCHING guard、无可用路由 |
| `model` | 模型层异常 | switch 失败、cooldown 阻挡、watchdog 重启 |
| `auth` | 认证层异常 | API key 无效 |
| `config` | 配置层异常 | auto-switch 未开、云端配置缺失 |
| `cloud` | 云端通信异常 | 协议不支持、重试耗尽后仍 5xx |

### 各采集点的 Anomaly 事件映射

| 代码位置 | 触发条件 | category | severity | status | 采集时机 |
|---------|---------|---------|----------|--------|---------|
| `handler.py` SWITCHING guard | 请求的本地模型不是 switching_target | routing | warning | 503 | 返回 503 前 |
| `handler.py` auto-switch conflict | 锁被占用 | routing | info | 409 | 返回 409 前 |
| `handler.py` auto-switch 失败 | ensure_service 返回 False | model | error | 503 | 返回 503 前 |
| `handler.py` AUTO_SWITCH=off | 模型未激活，AE不配置 | config | warning | 503 | 返回 503 前 |
| `handler.py` 云端配置缺失 | resolve_route 匹配但 config 无 | config | error | — | 仅日志 + anomaly |
| `handler.py` **Step 6/7（R8）** | **未知模型名（R8 削除后）** | **routing** | **warning** | **404** | **拒绝请求 + anomaly** |
| `handler.py` 无活跃无云端（R8 后） | 无 route | routing | critical | 503 | 拒绝请求 + anomaly |
| `proxy_manager.py` cooldown | R0 冷却期阻挡 | model | info | 503 | 返回 False 前 |
| `chat_handlers.py` switch conflict | 同 409 | routing | info | 409 | 返回 409 前 |
| `chat_handlers.py` cannot switch | ensure_service 返回 False | model | warning | 503 | 返回 503 前 |
| `watchdog.py` auto-restart | 5 次健康检查失败 | model | warning | — | 重启触发时 |
| `forwarder.py` 云端重试耗尽 | R2 重试后仍 5xx/timeout | cloud | error | 502 | 返回 502 前 |