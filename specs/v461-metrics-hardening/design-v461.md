# IFF v4.6.1 Design — Metrics & Hardening

**日期**: 2026-07-31
**基线**: spec-v461.md
**修订**: 双 Agent review 交叉验证后

---

## G-1a: Non-streaming RequestLog 补全

### 调用链改造

**当前** (chat_handlers.py:213-221):
```python
pm.logger.log(RequestLog(model=..., status=0, route=f"cloud:...", ...))  # 请求前写
forwarder.forward_to_cloud(handler, data, provider_cfg, cloud_model, ...)
return  # 无请求结果记录
```

**目标**:
```python
result = forwarder.forward_to_cloud(handler, data, provider_cfg, cloud_model, ...)
# result = CloudResult(usage={...}, ttft_ms=..., duration_ms=..., status=200)
pm.logger.log(RequestLog(
    model=model, status=result.status, route=f"cloud:{provider_name}",
    tokens_in=result.usage.get("prompt_tokens", 0),
    tokens_out=result.usage.get("completion_tokens", 0),
    ttft_ms=result.ttft_ms, duration_ms=result.duration_ms,
    ...
))
```

### forward_to_cloud 返回值

新增 `CloudResult` dataclass:
```python
@dataclass
class CloudResult:
    status: int = 200
    usage: dict = field(default_factory=dict)  # {prompt_tokens, completion_tokens}
    ttft_ms: float | None = None
    duration_ms: float = 0.0
    error: str | None = None
```

- non-streaming: 从 `json.loads(response_body)["usage"]` 提取
- streaming: 返回 `CloudResult(status=200)` (usage 留给 G-1b)
- 上游无 usage → `usage={}` → tokens fallback 到 0

### Local non-streaming TTFT

`chat_handlers.py` `_forward_request` 非流式分支，在收到上游首个响应字节时设 `handler._ttft_ms`。

---

## PR-E: DualGateLimiter

### 架构

```
                    ┌─────────────────────┐
  请求 ──────────→  │  DualGateLimiter     │
                    │                      │
                    │  1. RPM TokenBucket  │ ← 软门：限每分钟总量
                    │     acquire()        │
                    │     ↓                │
                    │  2. Semaphore        │ ← 硬门：限同时在飞数
                    │     acquire()        │
                    │     ↓                │
                    │  → 放行到 vLLM       │
                    │                      │
                    │  release():          │
                    │  Semaphore.release() │
                    │  (RPM 不需 release)  │
                    └─────────────────────┘
```

### 实现

```python
class DualGateLimiter:
    def __init__(self, rpm_limiter: RateLimiterV2, max_concurrent: int = 6):
        self._rpm = rpm_limiter
        self._concurrency = threading.Semaphore(max_concurrent)

    def acquire(self, model: str, timeout: float = 30) -> tuple[bool, str]:
        # Gate 1: RPM
        ok, reason = self._rpm.acquire(model, timeout)
        if not ok:
            return False, f"rpm_limit: {reason}"
        # Gate 2: Concurrency
        acquired = self._concurrency.acquire(timeout=timeout)
        if not acquired:
            return False, "concurrency_limit"
        return True, ""

    def release(self, model: str):
        self._concurrency.release()
```

### 初始化

`ProxyManager.__init__`:
```python
self.dual_gate = DualGateLimiter(
    rpm_limiter=RateLimiterV2(server_rpm=..., model_rpm_default=...),
    max_concurrent=6
)
```

### handler 改造

```python
# 旧:
limiter = _get_model_rate_limiter(pm, model)
if not limiter.acquire():
    ...429...
try:
    ...forward...
finally:
    limiter.release()

# 新:
ok, reason = pm.dual_gate.acquire(model, timeout=30)
if not ok:
    pm.logger.log(RequestLog(..., status=429, error=reason, ...))
    return handler._send_json({"error": f"Rate limited: {reason}"}, 429)
try:
    ...forward...
finally:
    pm.dual_gate.release(model)
```

---

## G-2: MetricsAggregator

### 架构

```
RequestLogger.log()  ──→  JSONL 文件 (持久化)
        │
        └──→  queue.Queue  ──→  AggregatorThread  ──→  MetricsAggregator (内存)
                                                        │
                                               /api/metrics ← Dashboard 轮询
```

### 关键设计决策

1. **Queue 解耦**: RequestLogger 写完 JSONL 后，`queue.put(entry)` 入队。aggregator 后台线程消费。主路径零额外锁等待。
2. **费用持久化**: 不单独持久化。重启时从 JSONL 回放 tokens × price 重建窗口。
3. **滑动窗口**: 全量样本列表 + `statistics.quantiles` 现算。单用户 QPS < 10/min，7d ≈ 100K 样本，内存 < 50MB。
4. **故障隔离**: aggregator 推送 try/except 吞掉异常；`/api/metrics` 端点 try/except 兜底。

### AggregatorThread

```python
class AggregatorThread(threading.Thread):
    def __init__(self, aggregator: MetricsAggregator, queue: queue.Queue):
        super().__init__(daemon=True)
        self._agg = aggregator
        self._q = queue

    def run(self):
        while True:
            entry = self._q.get()
            try:
                self._agg.record(entry)
            except Exception:
                log.warning("aggregator record failed", exc_info=True)
```

---

## PR-F: 原子写入

```python
def save_config(self):
    with self._lock:  # threading.Lock, 与 reload 互斥
        tmp = self._config_path.with_suffix('.yaml.tmp')
        with open(tmp, 'w') as f:
            self._yaml_dump(self._serialize(), f)
        # 校验
        with open(tmp) as f:
            yaml.safe_load(f)  # 解析失败抛异常，不 rename
        os.replace(tmp, self._config_path)  # POSIX 原子
```

---

## G-3: Dashboard 拆分策略

### 两步走

1. **G-3a**: 机械拆分，输出字节级等价。启动时 `get_html()` 一次性加载+缓存所有 fragments 和 JS 到内存。
2. **G-3b**: 新增 monitor 面板，读取 `/api/metrics`。

### 资源加载

```python
# dashboard/__init__.py
_cached_html: str | None = None

def get_html() -> str:
    global _cached_html
    if _cached_html is not None:
        return _cached_html
    try:
        base = (Path(__file__).parent / "base.html").read_text()
        for frag_name in ["inference", "monitor", "deploy", "cloud"]:
            frag = Path(__file__).parent / "fragments" / f"{frag_name}.html"
            base = base.replace(f"<!-- FRAGMENT:{frag_name} -->", frag.read_text())
        for js_name in ["app", "inference", "monitor", "deploy", "cloud"]:
            js = Path(__file__).parent / "js" / f"{js_name}.js"
            base = base.replace(f"<!-- JS:{js_name} -->", js.read_text())
        _cached_html = base
        return base
    except Exception as e:
        log.error("Dashboard fragment load failed: %s", e)
        return _fallback_html()
```
