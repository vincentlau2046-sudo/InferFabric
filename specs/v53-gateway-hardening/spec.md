# v53 — 网关加固需求（已批准范围）

> 状态：待审核（REVIEW）
> 范围基线：用户已逐项确认的需求；其余需求（预算/花费、per-key 限流、熔断、guardrails）明确不接受。
> 架构边界（用户硬性约束）：
> - IFF 是**透明协议网关**：客户端用什么协议（OpenAI/Anthropic）就透传到对应端点，**不做协议翻译**。
> - **模型自动切换是 Agent 的职责**：IFF 不替 Agent 做模型/路由层的自动切换或 fallback；当本地模型宕机时返回清晰错误让 Agent 决策，绝不能悄悄把请求转到其他模型/云端。
> - IFF 负责把"不同模型的批量并发请求"支撑好。

---

## 需求清单（按用户指定优先级）

### R0. 🔴 修复：本地模型宕机后 `ensure_service` 无限重试循环（最高优先级）

**现状**：当 vLLM 因 OOM/崩溃导致健康检查失败后，watchdog 触发 auto-restart → 启动失败 → 清空 `active_services`。此后每个请求都会触发 `ensure_service` → `switch()` → 又失败 → 因为 `_last_switch` 只在**成功**时设置（`proxy_manager.py:348`），失败路径不设 cooldown → 下一个请求再次触发 → SWITCHING 状态阻塞并发请求 → **无限 503 风暴**。

**故障链**：
1. vLLM OOM → 崩溃 → `/health` 不可用。
2. Watchdog（每 30s）→ 3 次失败 → `profile_state=ERROR`；5 次失败 → auto-restart → `switch()` → `_deploy_model()` 失败。
3. `model_lifecycle.py:159-168`：deploy 失败清空全部 `active_services` + `profile_state=ERROR`。
4. 此后每个请求 → model 不在 `active_services` → `ensure_service()` 触发 `switch()`。
5. `ensure_service` 的 cooldown 检查 `time.time() - _last_switch < 10s` **永远通过**（因为 `_last_switch` 只在 switch 成功时设置，失败时不变）。
6. 每个请求 → switch 失败 → 返回 503 → 下个请求 → 循环。
7. 同时每次 `switch()` 设 `profile_state=SWITCHING` → SWITCHING guard 阻塞并发请求 → 503。

**要求**：`ensure_service` 的 switch **失败时也设置 `_last_switch`**，触发 10s cooldown；cooldown 期间的请求返回明确 503 + `Retry-After` 头，让 Agent 知道应退避。

**架构红线**：此处不涉及模型 fallback——IFF 只确保"不在同一个错误上无限循环"，Agent 收到 503 后自行决定是否切到云端。

**涉及**：`inferfabric/proxy_manager.py`（`ensure_service` 1 行 fix）+ `handler.py`/`chat_handlers.py`（cooldown 路径 503 补 `Retry-After` 头）。

### R1. request_log 补盲区（用户原指定最高优先级）
**现状**：`_handle_messages` 的 SWITCHING guard 503 early-return 路径、以及其它 409/503/404 早退路径不写 `request_log`，排障时丢记录。
**要求**：所有非 200 的早期返回（503/409/404/401 等）都写入 `RequestLog`，保证每个请求（无论成功失败）在 `request_log` 中都有记录。
**涉及**：`inferfabric/proxy/handler.py`（`_handle_messages` 的 5 个早退分支）+ `inferfabric/proxy/chat_handlers.py`（`handle_chat` 的 3 个早退分支）。

### R2. 云端转发退避重试
**现状**：`forwarder.forward_to_cloud` 是**单次**请求；云端 429/5xx/timeout 直接透传，无重试。这正是 baidu 间歇性 429/500/timeout 报错的直接解法。
**要求**：对云端转发增加 429/5xx/timeout 的指数退避重试（可配置次数与退避序列）。
**约束**：仅重试"可重试"的失败（429、5xx、连接/超时）；4xx（除 429 外）不重试。重试期间不能已向客户端发出响应头（流式开始后不可重试）。
**涉及**：`inferfabric/forwarder.py`（`forward_to_cloud`）。

### R3. 提高云端默认超时（60s → 600s）
**现状**：`ProviderConfig.timeout` 默认 60s；`cloud_provider.yaml` 里 baidu 未配置 timeout，实际用默认 60s。64k 长输出会在 60s 被掐断。
**要求**：把云端请求的默认超时提高到 600s（10 分钟），支撑长输出。允许 per-provider 通过 YAML `timeout` 覆盖。
**涉及**：`inferfabric/cloud_discovery.py`（`ProviderConfig.timeout` 默认值 + YAML 加载默认值 + preset 默认值）。

### R4. 增强 proxy 多模型批量请求与并发能力
**现状**：`DualGateLimiter` 的并发门是**全局** `threading.Semaphore(max_concurrent)`，所有模型共享一个并发池；多模型批量并发时互相抢槽。
**要求**：增强对"不同模型的批量并发请求"的支撑——把并发控制做成 **per-model**（每个模型独立并发池），或提供可配置的全局并发上限 + per-model 上限。让多模型批量请求可并行、不互相阻塞。
**涉及**：`inferfabric/ratelimit.py`（`DualGateLimiter` 并发门 per-model 化）+ 配置（`iff.yaml` 的 `rate_limit.max_concurrent`）。

### R5. 精确匹配响应缓存
**现状**：无缓存；agent 场景 system prompt 高度重复，每次都重新生成，浪费 token。
**要求**：增加**精确匹配**响应缓存：以 (model + 规范化请求体哈希) 为键，命中则直接返回缓存响应，省去重复生成。提供开关（`iff.yaml: cache.enabled`）与容量上限（LRU）。仅缓存非流式成功响应；流式响应在结束后可落缓存。
**涉及**：新增 `inferfabric/proxy/response_cache.py`（LRU 缓存）+ 在 `handler.py`/`chat_handlers.py` 命中缓存时短路返回 + 配置项。

### R6. 标准 /metrics（Prometheus 文本格式）+ 可选 OTel 导出
**现状**：`/api/metrics` 是自研 JSON 格式；无标准 Prometheus 文本端点。
**要求**：
- 暴露标准 **Prometheus 文本格式**的 `/metrics`（`text/plain; version=0.0.4`），指标含请求计数/耗时直方图、token 用量、错误率等。
- 提供**可选**的 OpenTelemetry 导出（OTLP HTTP）开关与配置；默认关闭，开启后导出 traces/metrics。
**涉及**：`inferfabric/proxy/metrics.py`（或新增 prometheus 端点）+ `inferfabric/telemetry.py`（OTel 导出）+ 配置。

### R7. 服务端模型升级：threaded → async + 多副本 + 最空闲/延迟路由（P2，已提高优先级）
**现状**：代理服务端是 `ThreadedHTTPServer`（`socketserver.ThreadingMixIn + http.server.HTTPServer`），单进程、每请求一线程；多副本与负载均衡路由尚未实现。
**要求**：
- 将 HTTP 服务端从单进程 threaded 升级为 **async**（uvicorn 或 aiohttp），以支撑多副本水平扩展。
- 支持**多副本**部署（同一模型多个副本）。
- 同模型多副本时提供**最空闲 / 延迟**路由策略（按各副本的负载/RTT 选择目标副本）。
**约束**：保持透明网关语义（不翻译协议、不自动切模型）；副本选择是"同模型多副本"的负载均衡，不是跨模型切换。
**涉及**：`inferfabric/proxy/handler.py`（`ThreadedHTTPServer` + `main()`）+ 新增 async server 模块 + 副本注册与健康探测 + 路由策略。

---

## 优先级与实施顺序（用户指定）

| 优先级 | 需求 | 说明 |
|---|---|---|
| **R0** 🔴 | **修复：ensure_service 无限重试循环** | vLLM 宕机后每请求触发一次失败 switch → 无限 503 风暴，1 行 fix |
| P0 原最高 | R1 request_log 补盲区 | 用户原指定最高优先级 |
| P0 | R2 云端退避重试 | 解决 baidu 429/500/timeout 报错 |
| P0 | R3 超时 60s→600s | 长输出不被掐断 |
| P1 | R4 多模型批量并发 | 增强 proxy 多模型批量与并发 |
| P1 | R5 精确匹配响应缓存 | agent system prompt 去重省 token |
| P1 | R6 标准 /metrics + OTel | 可观测对齐成熟网关 |
| P2（已提高优先级） | R7 服务端 threaded → async + 多副本 + 最空闲/延迟路由，排在 R4-R6 之后实施 | 用户明说"这个可以提高优先级" |

## 明确不做（用户不接受）
- 预算/花费管理（per-key/per-provider cost 核算 + 预算上限）
- 每 key 限流（per-key RPM/TPM）
- 云端熔断（provider cooldown）
- Guardrails（提示词注入 / PII 检测）
