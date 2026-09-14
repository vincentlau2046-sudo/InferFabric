# v53 实施计划 v2（深入版）

> 对应需求：见同目录 `spec.md`。本文件是**深入实施方案**，每个需求包含现状分析→方案对比→选型理由→边界情况→测试策略→风险评估。
> 审核通过后按 **R0→P0→P1→P2** 顺序实施。
> 架构硬约束：透明网关（不翻译协议）；模型自动切换是 Agent 职责，IFF 不做 fallback；**零新增 Python 依赖**（当前唯一第三方依赖是 PyYAML）。

---

## 依赖基线

（同上，略）

---

## R0. 🔴 修复：本地模型宕机后 `ensure_service` 无限重试循环（最高优先级）

### 0.1 现状分析

**故障链（完整追踪）**：

```
vLLM OOM/崩溃
  → /health 不可用
    → Watchdog (每30s) 健康检查连续失败
      → 3 次失败 (90s):  watchdog.py:118 → profile_state=ERROR
      → 5 次失败 (150s): watchdog.py:105-115 → auto-restart
        → _restart_model() → reconcile() → switch(name)
          → manager.py:380: profile_state=SWITCHING
          → _deploy_model() 失败（vLLM 起不来）
            → model_lifecycle.py:133: profile_state=ERROR
            → model_lifecycle.py:159-168: active_services = [] (全部清空！)
  → 此后每个请求:
    → model 不在 active_services
      → ensure_service() 触发 auto-switch
        → proxy_manager.py:340: cooldown 检查 time.time() - _last_switch < 10s
        → _last_switch 仅在 switch 成功时设置 (line 348: if ok: self._last_switch = ...)
        → switch 失败: _last_switch 不变 ← BUG
        → cooldown 永远通过 → 无限循环
        → 每次 switch() 设 profile_state=SWITCHING → 阻塞并发请求
        → 每次返回 503
```

### 0.2 根因定位

`proxy_manager.py:340-351`（`ensure_service` 方法）：

```python
if time.time() - self._last_switch < self._cooldown:  # cooldown=10s
    return False                                          # ✅ cooldown 内跳过
result = self.mgr.switch(target)                          # 实际发起 switch
ok = result["status"] == "switched"
if ok:
    self._last_switch = time.time()   # ✅ 成功 → 记录时间
else:
    self.mgr.state.set("switching_target", "")
    return False                       # ❌ 失败 → _last_switch 未更新 → 下次仍然通过 cooldown
```

**关键**：`_cooldown=10`，`_last_switch=0.0`（初始值）。如果上次成功切换在 N 分钟前，`time.time() - _last_switch` 远大于 10s，cooldown 形同虚设。

### 0.3 架构红线

**不涉及模型 fallback**。修复的语义是：

- IFF 的角色：**不在同一个错误上无限循环**。开关失败后冷却 10s，期间返回清晰 503 + `Retry-After: 10`。
- Agent 的角色：**收到 503 后决策**（重试、退避、切到云端）。Agent 自主决定模型选择。

`resolve_route` 的行为不变：本地模型的 `served_name` 始终返回 `"local"`（不论 active 与否）。当模型宕机时，路由走到 `ensure_service` → cooldown → 503。**不 fallback 到 cloud**。

### 0.4 修复方案（2 处修改，总计约 6 行）

**修改 1**：`proxy_manager.py:349` — switch 失败时设置 `_last_switch`：

```python
# Before
else:
    self.mgr.state.set("switching_target", "")
    return False

# After
else:
    self._last_switch = time.time()    # ← 失败也记录 cooldown
    self.mgr.state.set("switching_target", "")
    return False
```

**修改 2**：`handler.py` + `chat_handlers.py` — cooldown 路径的 503 补 `Retry-After` 头，让 Agent 知道何时退避：

`handler.py`（auto-switch 失败 + model not active 两个 503 路径）和 `chat_handlers.py`（cannot switch 路径）的 `_send_json` 调用加上 `extra_headers={"Retry-After": "10"}`。

### 0.5 边界情况

| 场景 | 行为 |
|---|---|
| vLLM 宕机后第一个请求触发 switch → 失败 | `_last_switch` 更新 → 10s cooldown → 后续 10s 内请求直接 503+Retry-After |
| 10s cooldown 过期后请求 | 再次尝试 switch → 如仍失败 → 重置 cooldown → 再等 10s |
| vLLM 恢复（watchdog 或手动） | model 回到 active_services → `ensure_service` 直接返回 True（line 322） |
| 并发请求在 cooldown 内 | 不触发 switch，直接 503（不设 SWITCHING，不阻塞其他模型） |
| 并发请求在 switch 进行中 | `_switch_lock` 已被持有 → `acquire(timeout=0)` 返回 None → 409 "switch already in progress" |

### 0.6 测试

```python
# 单元测试：确保 model 不在 active_services 且 switch 失败 → _last_switch 被更新 → 10s 内 acquire 返回 False。
# 单元测试：cooldown 过期后 switch 又失败 → 返回 503。
# 集成测试：手动 stop vLLM → 发请求 → 断言 10s 内所有请求返回 503+Retry-After=10，不触发 switch 风暴。
# 集成测试：手动 start vLLM → 10s 后自动恢复。
```

### 0.7 风险评估

**风险等级：极低。** 1 行代码变更 + 503 补 Retry-After 头。不改变任何模型路由逻辑。最坏情况：cooldown 时间太短，vLLM 仍不能恢复时 Agent 会收到连续 503——这由 Agent 端的退避策略处理，属于正确行为。

当前 IFF 所有 `import` 均为 Python 标准库，唯一例外是 `yaml`（PyYAML，用于模型描述文件解析和 cloud_provider.yaml）。

本次增强**不引入任何新的第三方 Python 包**：
- 缓存用 `collections.OrderedDict + threading.Lock`（stdlib，20 行实现）。
- Prometheus 文本格式自研输出（stdlib，`prometheus_client` 仅作为可选增强，不强制依赖）。
- OTel 仅在用户显式开启时才 `import opentelemetry`，不增加默认依赖。
- async 升级用 `aiohttp`，**需新增依赖**，但这是 P2 的大改动，将在那时评估并确认。

---

## R1. request_log 补盲区（P0 最高优先）

### 1.1 现状分析

`_handle_messages`（handler.py:251）中的变量初始化：

```python
req_id = pm.new_request_id()          # 行 270
req_start = time.monotonic()          # 行 271
key_name = pm.auth.key_name(...)      # 行 272
self._req_id = req_id                 # 行 274
self._req_start = req_start           # 行 275
```

`RequestLog` dataclass 字段（request_logger.py:27-42）：
`req_id, key_name, model, status, ttft_ms, tokens_in, tokens_out, duration_ms, route, cloud_provider, error, timestamp, ts`

**已有 request_log 写的路径**：
- 行 293：auth 401 — ✅ 有 RequestLog。
- 行 399-408：cloud 成功 — ✅ 写 RequestLog。
- 行 446-455：cloud fallback — ✅ 写 RequestLog。
- 行 477-488：local 成功 — ✅ 写 RequestLog。

**缺失 request_log 的早退路径**（全部在 `_handle_messages` 和 `handle_chat` 中）：

| 文件 | 函数 | 行号 | 状态码 | 场景 |
|---|---|---|---|---|
| handler.py | `_handle_messages` | 325-330 | 503 | SWITCHING guard：请求的模型不是切换目标 |
| handler.py | `_handle_messages` | 352-353 | 409 | auto-switch 冲突（正在切换中） |
| handler.py | `_handle_messages` | 362-369 | 503 | auto-switch 失败（模型不健康） |
| handler.py | `_handle_messages` | 373-379 | 503 | 模型未激活且 AUTO_SWITCH=off |
| handler.py | `_handle_messages` | 458 | 503 | 无可用路由（无本地 LLM 也无云端匹配） |
| chat_handlers.py | `handle_chat` | 238-239 | 409 | switch already in progress |
| chat_handlers.py | `handle_chat` | 245-246 | 503 | Cannot switch (tri-state / manual stop) |
| chat_handlers.py | `handle_chat` | 277-278 | 404 | Unknown model |

### 1.2 设计方案

在每个早退分支的 `_send_json`（或 `handler._send_json`）调用之前，增加一行 `pm.logger.log(RequestLog(...))`调用。

关键设计决策：
- **model 字段**：使用 `original_model`（保留原始客户端请求的模型名）或 `requested_model`。`_handle_messages` 里有 `original_model = data.get("model", "")` 和 `requested_model = data.get("model", "")`（两者相同）；`handle_chat` 里用 `model` 变量。
- **route 字段**：所有早退路径都是"路由层拒绝"，设 `route="local"`（不涉及云端转发）。
- **error 字段**：用有意义的错误代码（`model_switching`、`switch_in_progress`、`auto_switch_failed`、`model_not_active`、`no_route`、`unknown_model`），便于 dashboard 按错误类型聚合。
- **duration_ms**：`(time.monotonic() - req_start) * 1000` 或 `(time.monotonic() - handler._req_start) * 1000`。

实施修改：
- `handler.py::_handle_messages`：在 5 个早退分支各插入 `pm.logger.log(RequestLog(...))`。
- `chat_handlers.py::handle_chat`：在 3 个早退分支各插入 `pm.logger.log(RequestLog(...))`。

### 1.3 测试

```python
# 单元测试：对每个早退分支发送请求，断言 request_log 有对应 status/error 记录。
# 集成测试：重启 IFF，通过 /api/request_log 查询确认 503/409/404 出现在日志中。
```

### 1.4 风险评估

**风险等级：极低。** 仅增加日志写入，不改变任何请求处理逻辑。`RequestLog` 写入是幂等的（JSONL append + SQLite buffer），失败不会抛出异常。

---

## R2. 云端转发退避重试（P0）

### 2.1 现状分析

`forward_to_cloud`（forwarder.py:173-273）是**单次**请求：

```python
def forward_to_cloud(handler, data, provider_cfg, cloud_model, protocol="openai", ...):
    body = json.dumps(data).encode("utf-8")
    req = Request(url, data=body, headers=headers, method="POST")
    resp = urlopen(req, timeout=provider_cfg.timeout)   # 单次，失败即返回
```

baidu-codingplan 云端间歇性返回 429（限频）、500（内部错误）或连接超时——直接透传给客户端导致"莫名其妙的 API error"。

已有的本地重试链（`forward_anthropic_local`，行 279-349）：
- 3 次尝试（`UPSTREAM_LOCAL_RETRIES=2`，即 1+2=3 次调用）
- 指数退避：`exponential_backoff(attempt)` → 0.5s, 1s, ……
- 重试条件：`should_retry_on_status`（5xx / 408 / 429）+ 连接异常（`ConnectionRefusedError` 等）

**云端转发缺失的正是同样的重试逻辑。**

### 2.2 重试策略设计

#### 2.2.1 可重试的错误

| 错误类型 | 可重试？ | 理由 |
|---|---|---|
| HTTP 429 Too Many Requests | ✅ | 临时限频，退避后通常恢复 |
| HTTP 5xx（500/502/503/504） | ✅ | 服务器内部错误，短暂故障后可恢复 |
| 连接超时 / 连接被拒 | ✅ | 网络抖动或服务临时不可用 |
| 其他 HTTP 4xx（400/401/403/404） | ❌ | 客户端错误，重试不会成功 |
| 流式响应中 error（mid-stream） | ❌ | 响应头已发给客户端，无法撤回 |

#### 2.2.2 重试次数与退避

沿用本地路径的配置：
- 重试次数：`iff.yaml: cloud_retry.max_retries`，默认 3（= `UPSTREAM_LOCAL_RETRIES` 的语义，共 1+2=3 次尝试）。
- 退避序列：`[0.5, 1.0, 2.0]` 秒（指数退避，与 `exponential_backoff` 一致）。

#### 2.2.3 流式响应的特殊处理

关键约束：**流式响应一旦调用 `pipe_stream_response` → 响应头已发出 → 不可撤回。**

重试逻辑：
```
for attempt in range(max_retries):
    try:
        resp = urlopen(req, timeout=...)
        if should_retry_on_status(resp.status) and attempt < max_retries:
            resp.close()   # 关闭连接，丢弃本次响应
            sleep(backoff)
            continue
        # status OK → 按原逻辑处理（流式 pipe，非流式 read）
        if was_stream:
            pipe_stream_response(handler, resp, sse_buf)
        else:
            handle_json_response(...)
        return CloudResult(200, ...)
    except (timeout, connection error) as e:
        if attempt < max_retries:
            sleep(backoff)
            continue
        raise  # 最终失败
```

关键：HTTP 状态码在响应头中，在读取 body 之前。所以检测到 429/5xx 时 body 还没读，安全关闭连接即可重试。

### 2.3 配置

```yaml
# iff.yaml（新增）
cloud_retry:
  max_retries: 3         # 共 3 次尝试（1 次初始 + 2 次重试）
  backoff_base: 0.5       # 退避基数（秒），序列为 base * 2^attempt
```

### 2.4 实施修改

- `forwarder.py`：在 `forward_to_cloud` 的 `urlopen` 调用外围加重试循环。
- `config.py`：新增 `CLOUD_RETRY_MAX_RETRIES` 默认常量（复用 `UPSTREAM_LOCAL_RETRIES` 风格）。

### 2.5 测试

```python
# 单元测试：mock urlopen 依次返回 429→200，断言重试 1 次后成功。
# 边界：mock 3 次全是 500，断言最终返回 500 且 CloudResult.error 不为 None。
# 边界：mock 返回 429，但 was_stream=True，断言不重试（响应头已发）。
# 实际上流式情况下检测到 429 时还没发响应头，可以重试。仅在 pipe_stream_response 内部出错时不可重试。
```

### 2.6 风险评估

**风险等级：低。** 重试逻辑仅重构 `forward_to_cloud` 的 try/except 块，对外接口（`CloudResult` 返回值）不变。注意点：重试会增加云端请求量，可能在 429 场景下加剧限频——应配合退避缓解。

---

## R3. 提高云端默认超时 60s → 600s（P0）

### 3.1 现状分析

`ProviderConfig.timeout` 的默认路径（cloud_discovery.py）：

| 位置 | 代码 | 当前默认值 |
|---|---|---|
| ProviderConfig dataclass | `timeout: int = 60` | 60s |
| YAML 加载（provider） | `timeout=pcfg.get("timeout", 60)` | 60s |
| YAML 加载（preset） | `timeout=pdata.get("timeout", 60)` | 60s |
| 实际使用（forward_to_cloud） | `urlopen(req, timeout=provider_cfg.timeout)` | 60s（来自 YAML 或默认） |

用户当前的 baidu-codingplan 提供商 YAML **没有配置 timeout 字段**（见 `~/.inferfabric/cloud_provider.yaml`），因此实际超时为 **60 秒**。64k token 长输出（约 2-4 分钟）会被 60s 掐断。

### 3.2 设计决策

将三处默认值从 60 改为 600：

- `ProviderConfig.timeout: int = 600`（dataclass 字段默认值）。
- `timeout=pcfg.get("timeout", 600)`（YAML 加载处）。
- `timeout=pdata.get("timeout", 600)`（preset 加载处）。

per-provider 仍可通过 YAML 的 `timeout` 字段覆盖为更短或更长的值。

### 3.3 实施修改

`cloud_discovery.py` 的 3 处修改（第 100 行、第 258 行、第 586 行附近）。

### 3.4 测试

```python
# 单元测试：构造未设 timeout 的 provider YAML，加载后 assert provider_cfg.timeout == 600。
# 单元测试：构造 timeout: 120 的 provider YAML，加载后 assert provider_cfg.timeout == 120。
```

### 3.5 风险评估

**风险等级：极低。** 纯默认值修改。副作用：600s 超时会长时间占用 worker 线程——在当前的 threaded server 模型下是唯一隐忧，待 R7 async 升级后完全消除。建议 P0 阶段暂时接受此风险。

---

## R4. per-model 并发池（P1）

### 4.1 现状分析

```python
# ratelimit.py: DualGateLimiter.__init__
self._concurrency = threading.Semaphore(max_concurrent)  # 全局信号量，所有模型共享
```

所有模型共享一个并发池。当调用方向多个不同模型同时发送批量请求时，它们互相争抢这个全局池：
- 模型 A 的请求可能阻塞模型 B（即使模型 B 的空闲容量充足）。
- `max_concurrent: auto` 从 vLLM 的 `max_num_seqs` 动态获取（当前活动模型的并发上限），但其他模型可能有自己的上限。

### 4.2 设计选择

把全局信号量改为 **per-model 信号量字典** + 可选的全局总上限：

```python
class DualGateLimiter:
    def __init__(self, ..., model_concurrent: dict[str, int] | int = 4, global_max: int | None = None):
        self._per_model: dict[str, threading.Semaphore] = {}
        self._model_limit = model_concurrent       # 每模型默认上限，或 {model: limit} dict
        self._global_sem = threading.Semaphore(global_max) if global_max else None
        self._lock = threading.Lock()
    
    def acquire(self, model: str, ...) -> GateResult:
        # 1. RPM 门（不变）
        # 2. 全局并发门（如果配置了 global_max）
        if self._global_sem:
            if not self._global_sem.acquire(timeout=...):
                return GateResult(ok=False, reason="global_concurrency_limit")
        # 3. per-model 并发门
        sem = self._get_or_create_sem(model)
        if not sem.acquire(timeout=...):
            if self._global_sem:
                self._global_sem.release()
            return GateResult(ok=False, reason=f"model_concurrency_limit:{model}")
        return GateResult(ok=True, ...)
```

配置（`iff.yaml`）：

```yaml
rate_limit:
  max_concurrent: auto       # 每模型并发池大小，auto=从 vLLM max_num_seqs 获取
  global_max_concurrent: 0   # 全局总上限，0=不限（仅 per-model 生效）
  model_concurrent:          # per-model 覆盖（可选）
    qwen38-27b: 4
    qwen36-35b-vl: 2
```

`max_concurrent: auto` 时，每个模型的并发池上限从 `model_obj.vllm.max_num_seqs` 获取（和当前行为一致，只是从全局共享变为 per-model 独立）。

### 4.3 实施修改

- `ratelimit.py::DualGateLimiter`：重构 `acquire`/`_release` 逻辑。
- `ratelimit.py::GateResult`：扩展 `_release` 以同时释放全局和 per-model 信号量。
- `config.py`：新增 `MODEL_CONCURRENT_DEFAULT` 常量和 `iff.yaml` 解析。

### 4.4 测试

```python
# 单元测试：并发 4 个模型 A 请求（per-model limit=2），前 2 通过后 2 阻塞，模型 B 请求不受影响。
# 集成测试：多模型批量请求，断言每个模型的 TTFT 不受其他模型负载影响。
```

### 4.5 风险评估

**风险等级：中。** 改动信号量逻辑，涉及 `acquire`/`release` 的对称性。GateResult 的 `__exit__` 需正确释放两层信号量。错误可能导致信号量泄漏（减少可用槽位）或死锁。充分单元测试 + 集成测试覆盖。

---

## R5. 精确匹配响应缓存（P1）— 深度分析

### 5.1 使用场景分析

用户目标：agent 场景 system prompt 高度重复。

在 Claude Code 的实际使用中，请求流量特征：
- **`/v1/messages`（Anthropic 协议）**：
  - System prompt + tools 定义在每个 conversation 中不变。
  - Messages 列表随对话增长（每轮 append user + assistant）。
  - 每个请求的完整 body 是唯一的（因为 messages 不同）。
- **`/v1/chat/completions`（OpenAI 协议）**：
  - 类似，system message + 对话历史。
  - Daemon bg-pty-host 进程可能用相同 system prompt 但不同 messages。

**精确匹配缓存的实际命中场景**（按价值排序）：

1. **多个 Agent 并行启动** — 每个 Agent 的第一个请求（system + initial user message）完全相同。此时第一次请求 miss（正常执行），后续相同请求 hit（直接返回缓存）。典型场景：同时启动多个 Claude Code 实例处理相同任务。
2. **Auto-compact 后重建上下文** — Claude Code 压缩对话后，重建的请求体在内容上可能与其他 session 相同。概率低但非零。
3. **工具调用重放** — 相同参数的 tool_use 可能在不同 session 中重复。

**命中率预估**：agent 工作负载下约 5-15%。每个命中的节省 = 完整生成过程（几百到几千 token），ROI 正向。

### 5.2 缓存键设计

键 = `sha256(f"{model}\n{canonical_json(body_without_stream)}\n")`

关键决策：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 包含 model | ✅ | 不同模型对同一 prompt 的响应不同，必须区分 |
| 包含完整 body | ✅ | "精确匹配"要求 |
| JSON 规范化 | ✅ `sort_keys=True` | 消除 key 顺序差异 |
| 排除 stream 参数 | ✅ | stream=true/false 对模型输出相同，归一化为同一缓存 |
| 排除 max_tokens | ❌（保留） | max_tokens 影响输出截断，不同截断长度不应共享缓存 |
| 排除 temperature | **策略决定** | 见 5.2.1 |

#### 5.2.1 temperature 处理策略

**推荐：默认仅缓存 `temperature=0` 的请求。**

理由：
- `temperature=0` 保证确定性输出（给定相同 prompt，输出总是相同）。缓存结果 **精确正确**。
- `temperature>0` 的输出是非确定的——缓存可能返回"错误"结果（与重试不同）。
- 对于 agent 使用场景，system prompt 通常搭配 `temperature=0`（要求确定性）。
- 提供 `cache.allow_nonzero_temp: true` 开关允许用户选择缓存非确定请求（有风险，但可能接受）。

**备选方案**：引入显式 `x-iff-cacheable: true` 请求头（类似 HTTP `Cache-Control`），让客户端声明该请求可安全缓存。这个方案更精确但增加客户端复杂度。优先级：v2 阶段再做。

### 5.3 实现方案对比

#### 方案 A：自建 OrderedDict LRU（推荐）

```python
from collections import OrderedDict
import threading

class LRUCache:
    """Thread-safe bounded LRU cache."""
    def __init__(self, maxsize: int = 1000):
        self._maxsize = maxsize
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
    
    def get(self, key: str):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)  # mark as recently used
                return self._cache[key]
        return None
    
    def put(self, key: str, value):
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = value
            if len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)  # evict oldest
```

**优点**：
- 零新增依赖（OrderedDict 是 Python 3.7+ 标准库）。
- 总计约 20 行代码，逻辑直接可验证，无隐性 bug 风险。
- 线程安全（lock 保护所有操作）。
- `get` + `move_to_end` + `popitem(last=False)` = 标准 LRU 语义，O(1) 操作。

**缺点**：
- 不持久化（重启即丢失）——对 agent 场景，每次重启是新的 context，丢失可接受。
- 不跨进程共享——IFF 是单进程部署，无此需求。

#### 方案 B：cachetools.LRUCache

```python
from cachetools import LRUCache
cache = LRUCache(maxsize=1000)
```

**优点**：工业级测试，数百万项目使用，线程安全（带 lock 的 wrapper）。
**缺点**：需 `pip install cachetools`（无其他依赖），**引入新第三方依赖**，违背零依赖原则。

#### 方案 C：diskcache / Redis

**diskcache**：持久化到 SQLite，跨重启保留缓存。
**Redis**：跨进程共享。

**评估**：两项都过重。IFF 是单机单进程部署；重启后 agent 状态清空，缓存无持久化意义。排除。

#### 方案 D：仅对 system prompt 做缓存（非精确匹配）

缓存键仅含 `(model + sha256(system_prompt))`，忽略 messages。命中时将缓存的 system prompt KV 返回给模型服务器（利用 vLLM 的自动前缀缓存）。

**评估**：这个方案更贴近真实的高命中率场景，但它**不是"精确匹配响应缓存"（它不缓存完整响应，而是依赖模型服务器的前缀缓存）**。且需要 IFF 理解 Anthropic/OpenAI 的 prompt 结构来提取 system prompt——这**涉及协议解析**，违背"透明网关不翻译协议"的架构边界。

**结论：不做。** 前缀缓存由 vLLM（`enable_prefix_caching=True`）自动完成，IFF 无需介入。

### 5.4 推荐方案

**方案 A（自建 OrderedDict LRU）** + temperature=0 策略。

选择理由：
1. 零新增依赖。
2. 代码量极小（20 行），逻辑可验证无隐性 bug。
3. Agent 场景实际命中率 5-15%，ROI 正向但不需要复杂的缓存基础设施。
4. 若未来需要持久化或跨进程共享，迁移到 cachetools/diskcache 是 trivial 的（接口兼容）。

不选的方案：
- **cachetools**：引入依赖但功能上与本方案等价，不值得。
- **前缀缓存**：违反透明网关架构边界。
- **语义缓存**：超出需求范围（用户要求精确匹配）。

### 5.5 缓存值结构

```python
cache_value = {
    "status": 200,
    "body": {...},       # 完整 JSON 响应体（与模型服务器返回一致）
    "usage": {...},      # token 用量（提示/完成）
    "cached_at": float,  # time.time()
}
```

响应头加 `X-IFF-Cache: hit`（或 `miss` 用于调试），不修改响应 body。

### 5.6 缓存准入条件

以下条件**全部满足**时才缓存：

1. `iff.yaml: cache.enabled == true`
2. 请求为 non-streaming（`stream == false` 或未设置）
3. 上游返回 status 200
4. `temperature == 0` 或 `cache.allow_nonzero_temp == true`

以下条件**全部满足**时才命中：

1. 缓存键精确匹配
2. 请求为 non-streaming（流式请求即使键匹配也不走缓存，因为流式响应不能从缓存构造）
3. 缓存条目未过期（可选 TTL，默认不过期，仅通过 LRU 淘汰）

### 5.7 缓存在请求流中的位置

```
request → auth → rate_limit → [cache lookup] → miss → forward → cache put → response
                                  ↓ hit
                              skip forward, return cached response
```

缓存查找在 auth 之后、rate_limit 之前（命中不应消耗并发槽）。在 `_handle_messages` 和 `handle_chat` 的 auth 检查之后插入。

### 5.8 实施修改

- 新增 `inferfabric/proxy/response_cache.py`：`LRUCache` 类 + `ResponseCache` 封装（cache key 生成、准入判断）。
- `config.py`：新增 `CACHE_ENABLED` / `CACHE_MAX_ENTRIES` 默认常量，`iff.yaml` 解析。
- `handler.py::_handle_messages`：auth 通过后、SWITCHING guard 之前插入缓存查找。
- `chat_handlers.py::handle_chat`：auth 通过后、auto-switch 之前插入缓存查找。

### 5.9 测试

```python
# 单元测试 LRUCache：插入超过 maxsize，断言最旧条目被淘汰。
# 单元测试 ResponseCache：相同 body 两次请求，第二次命中 X-IFF-Cache: hit。
# 单元测试 ResponseCache：stream=true 不缓存不命中。
# 单元测试 ResponseCache：temperature=0.7 不缓存（除非 allow_nonzero_temp 开启）。
# 单元测试 ResponseCache：不同 model 相同 prompt 不命中。
# 集成测试：重启 IFF，用 Claude Code 测试字段不破坏正常请求。
```

### 5.10 风险评估

**风险等级：低。** 缓存是无副作用的读-写通路：miss 时 fallthrough 到正常 forward 路径，hit 时短路返回。最坏情况：缓存返回 wrong response（仅当 `allow_nonzero_temp` 开启时可能发生），不会引起崩溃或丢失数据。

---

## R6. 标准 /metrics（Prometheus 文本格式）+ 可选 OTel 导出（P1）

### 6.1 现状分析

当前 IFF 的可观测端点：

| 端点 | 格式 | 内容 |
|---|---|---|
| `/api/metrics` | JSON | 自研聚合指标（请求数、token、TTFT 等） |
| `/vllm_metrics` | JSON | 实时 vLLM Prometheus 指标（经解析） |
| `/api/token-stats` | JSON | token 统计 |
| `/status` | JSON | GPU 模式 + 服务状态 |

**缺失的**：标准的 `/metrics` 端点（Prometheus 文本格式 `text/plain; version=0.0.4`），用于与 Prometheus/Grafana 等标准监控集成。

### 6.2 设计方案

#### 6.2.1 Prometheus 文本格式

**策略**：零依赖自研文本输出。

Prometheus 文本格式极其简单：
```
# HELP iff_requests_total Total requests served
# TYPE iff_requests_total counter
iff_requests_total{model="qwen38-27b",route="local",status="200"} 1234
iff_requests_total{model="deepseek-v4-flash",route="cloud",status="200"} 567
# HELP iff_request_duration_ms Request duration in milliseconds
# TYPE iff_request_duration_ms histogram
iff_request_duration_ms_bucket{le="100"} 50
iff_request_duration_ms_bucket{le="500"} 200
iff_request_duration_ms_bucket{le="1000"} 300
iff_request_duration_ms_bucket{le="+Inf"} 400
iff_request_duration_ms_sum 123456
iff_request_duration_ms_count 400
```

**实现方法**：
- 新增 `inferfabric/proxy/metrics_exporter.py`（命名避免与已有 `prometheus.py` 冲突——后者是 vLLM metrics 解析器）。
- 从 `RequestLogDB` + `TokenStatsCollector` 聚合指标（复用已有数据源）。
- 新增 GET 路由 `/metrics`。
- 指标列表：

| 指标名 | 类型 | Labels | 说明 |
|---|---|---|---|
| `iff_requests_total` | counter | model, route, status | 请求总数 |
| `iff_request_duration_ms` | histogram | model, route | 端到端延迟（含排队） |
| `iff_tokens_in_total` | counter | model | 输入 token 总量 |
| `iff_tokens_out_total` | counter | model | 输出 token 总量 |
| `iff_ttft_ms` | histogram | model | 首 token 延迟 |
| `iff_errors_total` | counter | model, error | 错误计数（含 401/429/500/503） |
| `iff_active_requests` | gauge | model | 当前在飞请求数 |

**备选方案**：引入 `prometheus_client` 库。
- 优点：标准实现，直方图 bucket 自动计算，线程安全，生态兼容。
- 缺点：新增依赖。用户已明确说"同意零依赖"。
- **决策**：零依赖自研实现。如果未来需要更丰富的指标（如自动汇总到 Pushgateway），可以可选导入 `prometheus_client`（`try: import ... except ImportError: fallback`）。

#### 6.2.2 OTel 导出

- 仅在 `iff.yaml: otel.enabled == true` 时才 `import opentelemetry`（惰性导入，不影响默认启动）。
- 导出协议：OTLP HTTP（gRPC 需要 `grpcio` 依赖，太重，只支持 HTTP）。
- 导出内容：traces（每个请求一个 span） + metrics（从上面的指标导出）。
- 配置：

```yaml
otel:
  enabled: false
  endpoint: ""             # OTLP HTTP endpoint（如 http://localhost:4318/v1/traces）
  service_name: "inferfabric"
```

### 6.3 实施修改

- 新增 `inferfabric/proxy/metrics_exporter.py`：Prometheus 文本格式构建器。
- `handler.py`：在 GET 路由表中新增 `/metrics`。
- `telemetry.py`：新增 `OtelExporter` 类（惰性导入 opentelemetry）。
- `config.py`：新增 `OTEL_ENABLED` / `OTEL_ENDPOINT` 配置。

### 6.4 测试

```python
# curl /metrics | grep iff_requests_total → 返回标准 Prometheus 文本。
# 单元测试：构造已知的 request_log 记录，生成 /metrics，断言计数和直方图正确。
```

### 6.5 风险评估

**风险等级：低。** /metrics 是只读端点，不修改任何请求路径。指标数据源自已有的 `RequestLogDB`，是可靠的数据源。

---

## R7. 服务端 async 化 + 多副本 + 最空闲/延迟路由（P2，已提高优先级）

### 7.1 现状分析

当前服务端：

```python
# handler.py:1439-1441
class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
```

- 每请求一个线程（无上限），高并发下线程数爆炸。
- 所有 I/O 都是阻塞的：`urlopen`（云端）、`HTTPConnection`（本地）、文件读写——阻塞时占用整个线程。
- `daemon_threads=True`：线程在主线程退出时被强制终止，可能导致正在处理的请求被截断。

### 7.2 框架选型

| 框架 | 优点 | 缺点 |
|---|---|---|
| **aiohttp** | 自带 HTTP server + client，协程 I/O，迁移路径短 | 生态小于 FastAPI，文档略少 |
| **uvicorn + Starlette** | 标准 ASGI，FastAPI 的底层，生态最好 | 需要 ASGI 包装层，路由迁移量大 |
| **uvicorn + FastAPI** | 自动 OpenAPI 文档，类型验证 | 太重，引入多个依赖（starlette/pydantic） |
| **trio + httpx** | 结构化并发，优雅 | 生态小，学习曲线 |

**推荐：aiohttp**。

理由：
1. 自带 `aiohttp.web`（HTTP server）和 `aiohttp.ClientSession`（async HTTP client），一石二鸟：
   - 服务端用 `aiohttp.web.Application + AppRunner + TCPSite` 替代 `ThreadedHTTPServer`。
   - 云端转发用 `aiohttp.ClientSession.post(url, ...)` 替代 `urllib.request.urlopen`。
2. 路由表（`_GET_ROUTES`/`_POST_ROUTES`/`_DELETE_ROUTES`）可自然映射到 aiohttp 的 `RouteTableDef`。
3. 迁移路径明确：路由处理函数逐个从同步改为 `async def`，`self._send_json` 改为直接的 `aiohttp.web.json_response`。

**不选 uvicorn/FastAPI 的理由**：
- IFF 是透明代理，不需要 ASGI 的中间件链、不需要 FastAPI 的类型验证和自动文档——这些都是"该有的功能但用不上"的负担。
- 引入 Starlette + uvicorn + pydantic 三四个依赖，违背尽量少的依赖原则。

### 7.3 多副本注册与健康探测

多副本场景：同一模型部署在多个 GPU/端口（用于故障转移和负载均衡）。当前 `models.d/*.yaml` 中每个模型只有一个 `port`。

**方案**：在模型 YAML 中支持 `replicas` 列表：

```yaml
# models.d/qwen38-27b-abliterated.yaml
engine: vllm
served_name: deepseek-v4-flash
replicas:
  - port: 8002
    weight: 1.0          # 权重（用于加权路由）
  - port: 8003
    weight: 1.0
```

副本健康探测复用现有 `watchdog` 机制（`ModelWatchdog` 已做 per-port 的 health check）。不健康的副本从路由池中暂时移除，恢复后自动加回。

### 7.4 路由策略

同模型多副本时，从副本池中选择一个目标：

| 策略 | 算法 | 场景 |
|---|---|---|
| `least_busy`（推荐默认） | 选择当前在飞请求数最少的副本 | 通用，适合请求耗时分布均匀的场景 |
| `latency` | 选择 P50 TTFT 最低的副本 | 对延迟敏感的场景 |
| `round_robin` | 轮流选择 | 副本性能完全一致时最简单，无状态追踪 |

**推荐默认 `least_busy`**。每个副本维护一个 `threading.Semaphore` 或 `asyncio.Semaphore`（per-replica 并发池），`least_busy` 就是选择剩余信号量最多的副本。

```python
async def select_replica(replicas: list[ReplicaInfo]) -> ReplicaInfo:
    """Select the replica with the most available capacity."""
    best = None
    max_available = -1
    for r in replicas:
        if not r.healthy:
            continue
        available = r.semaphore._value   # remaining permits
        if available > max_available:
            max_available = available
            best = r
    return best or replicas[0]  # fallback to first
```

### 7.5 迁移路径

**分三小步，每步独立可验证**：

1. **Async server（不改变路由逻辑）**：
   - 新建 `inferfabric/proxy/async_server.py`，用 aiohttp 重写 server 启动和请求调度。
   - `ProxyHandler` 的每个 `do_GET`/`do_POST` 改为 `async def handle_get(request)` / `async def handle_post(request)`。
   - 本地转发用 aiohttp client 替代 `http.client.HTTPConnection`。
   - 云端转发用 aiohttp client 替代 `urllib.request.urlopen`。
   - 启动方式：`iff start` 拉起 aiohttp server 替代 `ThreadedHTTPServer`。
   - 验证：所有已有测试通过，Claude Code 正常工作。

2. **多副本注册**：
   - `config.py` / `models.d/*.yaml` 支持 `replicas` 字段。
   - `ModelManager` 的 `get_model` 返回包含多副本信息的模型对象。
   - watchdog 对多副本做分别的健康探测。
   - 验证：启动 2 副本 qwen38-27b，`/status` 显示 2 个健康副本。

3. **负载均衡路由**：
   - 在 `_forward_local` 中实现副本选择（`least_busy`）。
   - 配置 `load_balance.strategy`。
   - 验证：并发压测，各副本负载均匀。

### 7.6 实施修改（范围）

- 新增 `inferfabric/proxy/async_server.py`：aiohttp server + route table + 请求处理。
- 新增 `inferfabric/proxy/async_forwarder.py`：aiohttp 版本的 `forward_to_cloud` / `forward_local`。
- 修改 `config.py`：解析 `replicas`、`load_balance` 配置。
- 修改 `ProxyManager` / `manager.py`：多副本模型注册。
- 保留 `handler.py` 中的路由表定义（`_GET_ROUTES` 等）供 async server 复用或迁移。
- 确保 `iff start` 可选择启动模式（threaded vs async），初期 async 作为 `iff start --async` 实验模式。

### 7.7 测试

```python
# 单元测试：aiohttp server 启动，/health 返回 200。
# 单元测试：多副本 least_busy 选择，并发 10 请求验证负载分布。
# 集成测试：Claude Code + async IFF，全量回归。
# 压测：100 并发请求到 2 副本，验证无连接超时、无死锁。
```

### 7.8 风险评估

**风险等级：高（最大改动）**。换服务端框架是全项目最大的架构变更：
- 路由、鉴权、限流、日志、缓存等所有横切关注点都需要在 async 上下文重新工作。
- aiohttp 的异常处理模型不同于同步代码（协程内的异常需要 `try/except` 在 `await` 边界上）。
- 限流器的 `threading.Semaphore` 需要改为 `asyncio.Semaphore`。
- **缓解**：分三小步做，每步独立验证；保留 threaded server 作为 fallback 模式。

---

## 配置总览（`iff.yaml` 新增字段）

```yaml
# ── 云端重试（R2）─────────────────────────
cloud_retry:
  max_retries: 3           # 共 3 次尝试
  backoff_base: 0.5         # 退避序列: 0.5s, 1s, 2s, ...

# ── 并发控制（R4）─────────────────────────
rate_limit:
  mode: observe
  server_rpm: 0
  model_rpm_default: 0
  max_concurrent: auto       # per-model 并发上限（auto=从 max_num_seqs）
  global_max_concurrent: 0  # 全局总上限, 0=不限
  timeout: 5

# ── 响应缓存（R5）─────────────────────────
cache:
  enabled: true
  max_entries: 1000
  allow_nonzero_temp: false   # true=也缓存 temperature>0（非确定，有风险）

# ── 指标（R6）────────────────────────────
otel:
  enabled: false
  endpoint: ""                # eg. http://localhost:4318
  service_name: "inferfabric"

# ── 负载均衡（R7）─────────────────────────
load_balance:
  strategy: least_busy        # least_busy | latency | round_robin
```

---

## 实施阶段与依赖关系

```
P0（3 项，可并行）
├── R1 request_log 补盲区  ← 用户指定最高优先级
├── R2 云端退避重试
└── R3 超时 60s→600s          修改项跟 R2 无冲突

P1（3 项，依赖于 P0 稳定运行后）
├── R4 per-model 并发池       改动 ratelimit.py，P0 稳定后再改
├── R5 响应缓存               改动 handler.py + chat_handlers.py，与 P0 耦合小
└── R6 /metrics + OTel        新增端点，不修改现有路径

P2（1 项，最后做）
└── R7 async 服务端升级        最大改动，需 P0+P1 全部稳定，分 3 小步
```

## 审核确认点

- [ ] R1–R7 的实施顺序 P0→P1→P2 确认？
- [ ] R2 重试 3 次 / 退避 0.5s,1s,2s 确认？
- [ ] R4 per-model 并发池 + 可选全局上限确认？
- [ ] R5 自建 OrderedDict LRU（零依赖）、仅缓存 temperature=0 确认？
- [ ] R6 零依赖自研 Prometheus 文本格式确认？
- [ ] R7 aiohttp（自带 http server + async client）确认？分 3 小步迁移确认？
