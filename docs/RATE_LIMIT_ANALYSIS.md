# InferFabric 代理层限流方案分析

## 问题根因

| 参数 | 值 |
|------|---|
| `max_num_seqs` | 8 |
| `gpu_memory_utilization` | 0.83 |
| `kv_cache_dtype` | fp8 |
| 安全并发上限 | **8** (等于 max_num_seqs) |

9+ 并发请求 → vLLM 调度器尝试将全部请求塞入 GPU 批处理 → KV cache 超过 83% VRAM 预算 → OOM 或强制驱逐（preemption）。

**当前代理层无任何限流**：`_handle_messages` 和 `_handle_chat` 直接透传到 vLLM，每个 HTTP 线程独立转发，无并发感知。

---

## 可用 vLLM 指标（/metrics 端点）

经实测，vLLM 暴露了以下限流关键指标：

| 指标名 | 类型 | 含义 | 限流价值 |
|--------|------|------|----------|
| `vllm:num_requests_running` | gauge | 当前运行中的请求数 | ⭐⭐⭐ 核心信号 |
| `vllm:num_requests_waiting` | gauge | 排队等待的请求数 | ⭐⭐⭐ 核心信号 |
| `vllm:num_requests_waiting{reason="capacity"}` | gauge | 因容量不足等待的请求数 | ⭐⭐ 细粒度 |
| `vllm:kv_cache_usage_perc` | gauge | KV cache 使用率 (0-1) | ⭐⭐ 滞后信号 |
| `vllm:num_gpu_cache_blocks` | gauge | GPU 上的 KV block 数 | ⭐ 辅助 |

---

## 方案评估

### 方案 A：并发连接数限制（Semaphore）

**原理**：在代理层用 `threading.Semaphore(max_concurrent)` 控制同时转发的请求数。

```python
# ProxyHandler 类级
_rate_limit = threading.Semaphore(8)  # 匹配 max_num_seqs

# _handle_messages / _handle_chat 入口
if not self._rate_limit.acquire(timeout=30):
    self._send_json({"error": "rate limit — server at capacity"}, 429)
    self._send_header("Retry-After", "30")
    return
try:
    # ... 转发逻辑 ...
finally:
    self._rate_limit.release()
```

| 维度 | 评估 |
|------|------|
| 实现复杂度 | ⭐ 极低（10 行代码） |
| 精度 | 中等 — 不知道 vLLM 内部实际占用 |
| 对 Claude Code 影响 | 26 个工具定义串行化，但同一 session 的请求会排队（timeout=30s 足够） |
| 对 OpenClaw 影响 | 多 session 并行 → 8 个并发 slots 分配给最先到达的 session |
| 流式请求 | ✅ semaphore release 在流结束后触发（finally），不影响 SSE 管道 |
| 缺点 | 静态值，不知道 vLLM 实际负载；长请求（长 context）占 slot 太久 |

### 方案 B：vLLM Queue 状态限流

**原理**：每次请求到达时，代理轮询 `vLLM/metrics` 获取 `num_requests_running + num_requests_waiting`，超过阈值则拒绝。

```python
def _can_accept(port: int, max_running: int = 8, max_queue: int = 4) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=2) as resp:
            text = resp.read().decode()
        running = 0
        waiting = 0
        for line in text.splitlines():
            if line.startswith("vllm:num_requests_running"):
                running = float(line.split()[-1])
            elif line.startswith("vllm:num_requests_waiting") and "by_reason" not in line:
                waiting = float(line.split()[-1])
        return (running + waiting) < max_running
    except Exception:
        return True  # 指标不可用时放行
```

| 维度 | 评估 |
|------|------|
| 实现复杂度 | ⭐⭐ 中等（需解析 Prometheus 文本，加缓存避免每请求 HTTP 开销） |
| 精度 | ⭐⭐⭐ 直接反映 vLLM 内部状态 |
| 对 Claude Code 影响 | 26 个工具请求 → 前 N 个进入，其余排队或 429 → 客户端重试 |
| 对 OpenClaw 影响 | 多 session 按到达顺序排队 → 先到先服务 |
| 流式请求 | ✅ 同方案 A |
| 缺点 | **TOCTOU 竞态**：查指标和发请求之间有窗口，极端情况下仍可能超限；每请求多一次 HTTP 到 vLLM |

### 方案 C：KV Cache 使用率限流

**原理**：监控 `vllm:kv_cache_usage_perc`，超过阈值（如 0.75）拒绝新请求。

| 维度 | 评估 |
|------|------|
| 实现复杂度 | ⭐⭐ 中等（解析 /metrics） |
| 精度 | ⭐ — 滞后信号。KV cache 满了才知道，但此时来不及了 |
| 问题 | KV cache 是**结果指标**（请求已经在跑后才体现），不适合作为**门控信号**。且 chunked-prefill 下，prefill 阶段的 cache 使用是渐进式的 |
| 结论 | ❌ 不适合做门控限流，适合作为健康监控告警 |

### 方案 D：令牌桶 / 滑动窗口

**原理**：固定速率发放令牌，请求消耗令牌后才被允许转发。

```python
class TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate          # 令牌发放速率 (req/s)
        self.capacity = capacity  # 桶容量
        self.tokens = capacity
        self.last_refill = time.time()

    def acquire(self, timeout=0) -> bool:
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        if timeout > 0:
            wait = (1 - self.tokens) / self.rate
            if wait <= timeout:
                time.sleep(wait)
                self._refill()
                self.tokens -= 1
                return True
        return False
```

| 维度 | 评估 |
|------|------|
| 实现复杂度 | ⭐⭐ 中等 |
| 精度 | 低 — 速率限流不适合突发场景 |
| 问题 | LLM 请求时延差异极大（100ms~300s），固定速率无法匹配 |
| 结论 | ❌ 不适合 LLM 代理。适用于传统 API 的 QPS 限流 |

---

## 推荐方案：A + B 混合（Semaphore + 动态调整）

### 设计

```
客户端
  │
  ▼
┌─────────────────────────────────────┐
│         Proxy :8999                │
│                                     │
│  ┌─ 1. Semaphore (静态上限) ─────┐ │
│  │  acquire(timeout=30)          │ │
│  │  fail → 429 + Retry-After    │ │
│  └──────────────────────────────┘ │ │
│                                     │
│  ┌─ 2. Metrics Gate (动态检查) ─┐ │
│  │  running + waiting >= max    │ │
│  │  → 排队或拒绝                  │ │
│  └──────────────────────────────┘ │ │
│                                     │
│  ▼ Forward to vLLM :8000            │
└─────────────────────────────────────┘
```

**两层保护**：
1. **Semaphore**：快速拒绝 — O(1) 判定，不依赖网络调用
2. **Metrics Gate**：精调 — 感知 vLLM 实际负载，防止 vLLM 内部排队过多

**关键参数**：
```python
# 从模型配置读取
MAX_CONCURRENT = model.vllm.max_num_seqs  # 8
MAX_QUEUE = 4                              # 允许 vLLM 内部排队 4 个
METRICS_CACHE_TTL = 2                    # /metrics 缓存 2s，避免每请求 HTTP
METRICS_POLL_THREAD = True              # 后台线程每 5s 拉一次 metrics
```

### 对 Claude Code 的影响

| 场景 | 行为 |
|------|------|
| 26 个工具定义并发调用 | Semaphore 放前 8 个，余 18 个排队 (30s timeout)。若 30s 内完成则放行，否则 429 |
| 单 session 工具调用 | 按工具依赖拓扑，实际并发通常 3-5 个（Claude 串行调 tool），不受影响 |
| 流式响应 | ✅ SSE 管道建立后 semaphore 已释放，后续 chunk 不受限 |

**关键观察**：Claude Code 的 26 个工具定义是**请求头**中的 `tools` 字段，不是 26 个独立 HTTP 请求。单次 `/v1/messages` 请求中携带 26 个 tool 定义 → **只占 1 个并发 slot**。

真正的并发性来自：
- 工具调用后，Claude Code 对**每个工具结果**发起独立请求（通常串行）
- OpenClaw 多 session 并行：不同 session 的请求同时到达

### 对 OpenClaw 的影响

| 场景 | 行为 |
|------|------|
| N 个 session 并行 | 8 个 slot 竞争 → 先到先服务 |
| 长 context 请求 | 占 slot 久 → 其他 session 排队更久 |
| 流式中断 | 客户端断开 → `finally` 释放 slot |

---

## 实现方案（具体代码结构）

### `inferfabric/ratelimit.py`（新建）

```python
import threading
import time
import urllib.request
import logging

log = logging.getLogger("inferfabric.ratelimit")

class ProxyRateLimiter:
    """两级限流：Semaphore 快速拒绝 + Metrics 精调。"""

    def __init__(self, max_concurrent: int, max_queue: int = 4):
        self._semaphore = threading.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self.max_queue = max_queue
        self._metrics_cache = {}  # port → (ts, running, waiting)
        self._cache_lock = threading.Lock()
        self._cache_ttl = 2.0

    def can_accept(self, port: int, timeout: float = 30.0) -> bool:
        """Check if we can accept a new request for the given vLLM port."""
        # Layer 1: Semaphore — O(1) fast path
        acquired = self._semaphore.acquire(timeout=timeout)
        if not acquired:
            log.warning("Semaphore full — rejecting (port %d)", port)
            return False

        # Layer 2: Metrics gate — check vLLM internal state
        running, waiting = self._get_queue_state(port)
        total = running + waiting
        if total >= self.max_concurrent + self.max_queue:
            self._semaphore.release()  # return the slot
            log.warning("vLLM queue full: running=%.0f waiting=%.0f — rejecting", running, waiting)
            return False

        # Passed both gates
        log.debug("Accepted: sem=ok, vllm running=%.0f waiting=%.0f", running, waiting)
        return True

    def release(self):
        """Release semaphore slot (call in finally block)."""
        self._semaphore.release()

    def _get_queue_state(self, port: int) -> tuple[int, int]:
        """Get (running, waiting) from vLLM metrics with caching."""
        now = time.time()
        with self._cache_lock:
            cached = self._metrics_cache.get(port)
            if cached and (now - cached[0]) < self._cache_ttl:
                return cached[1], cached[2]

        # Fetch fresh metrics
        try:
            url = f"http://127.0.0.1:{port}/metrics"
            with urllib.request.urlopen(url, timeout=2) as resp:
                text = resp.read().decode()
            running, waiting = 0, 0
            for line in text.splitlines():
                if line.startswith("vllm:num_requests_running"):
                    running = int(float(line.split()[-1]))
                elif line.startswith("vllm:num_requests_waiting") and "by_reason" not in line:
                    waiting = int(float(line.split()[-1]))
        except Exception:
            running, waiting = 0, 0  # metrics unavailable → trust semaphore only

        with self._cache_lock:
            self._metrics_cache[port] = (now, running, waiting)

        return running, waiting
```

### `proxy.py` 集成点

```python
# ProxyHandler 增加
from inferfabric.ratelimit import ProxyRateLimiter
_rate_limiter = ProxyRateLimiter(max_concurrent=8, max_queue=4)

# _handle_messages 入口
def _handle_messages(self, pm):
    # ... 读 body, 找到 active_llm ...
    if active_llm:
        if not self._rate_limiter.can_accept(active_llm.port, timeout=30):
            self._send_json({"error": "rate limit exceeded"}, 429)
            self.send_header("Retry-After", "30")
            return
        try:
            forwarder.forward_anthropic_local(...)
        finally:
            self._rate_limiter.release()
    else:
        # Baidu fallback — unlimited (cloud handles its own rate limits)
        forwarder.forward_to_baidu(...)

# _handle_chat 同理
```

---

## 实现复杂度汇总

| 方案 | 代码量 | 风险 | 精确度 | 推荐度 |
|------|--------|------|--------|--------|
| A: Semaphore | ~15 行 | 低 | 中 | ⭐⭐⭐⭐ |
| A+B: 混合 | ~80 行 | 低 | 高 | ⭐⭐⭐⭐⭐ |
| B: Metrics 门控 | ~50 行 | 中 (TOCTOU) | 高 | ⭐⭐⭐ |
| C: KV cache 监控 | ~40 行 | 中 | 低 | ⭐ |
| D: 令牌桶 | ~30 行 | 低 | 低 | ❌ |

---

## 配置建议

```yaml
# 加到 models.d/qwen36-27b.yaml
rate_limit:
  max_concurrent: 8        # 匹配 max_num_seqs
  max_queue: 4             # vLLM 允许的内部排队
  timeout: 30              # 客户端最长等待 (秒)
  metrics_cache_ttl: 2     # metrics 缓存时间 (秒)
  retry_after: 30          # 429 响应中的 Retry-After 头
```

---

## 边缘情况处理

| 场景 | 行为 |
|------|------|
| vLLM 未启动 | Metrics 不可用 → 降级为纯 Semaphore |
| 长 context (128K) | 占用 slot 数分钟 → 其他请求排队/429 |
| 客户端断开 | `BrokenPipeError` → `finally` 释放 slot |
| Baidu fallback | **不限流**（云端有独立限流） |
| Ollama 后端 | **不限流**（Ollama 内部调度 + CPU fallback） |
| 多模型共存 (shared mode) | 按端口独立限流 |

---

## 实施步骤

1. **P0**: 实现 Semaphore 限流（~15 行，零依赖）
2. **P1**: 加 Metrics 缓存线程（后台每 5s 拉 /metrics）
3. **P1**: 加 Metrics Gate 第二层检查
4. **P2**: 暴露限流状态到 Dashboard (`GET /rate-limit-status`)
5. **P2**: 支持 YAML 配置覆盖（`rate_limit` 段）

**P0 单独即可解决 OOM 问题** — 9 并发变 8 并发，直接消除越界。
