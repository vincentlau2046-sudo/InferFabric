# Design: v4.6 API Gateway

**Version**: delta-001
**Date**: 2026-07-30
**Depends on**: spec-delta-001.md

## 架构变更

### 当前架构 (v4.3)

```
Client → ProxyHandler (BaseHTTPRequestHandler, 同步)
           → _handle_messages / _handle_chat
             → ratelimit._get_model_rate_limiter (threading.Semaphore)
             → forwarder.forward_anthropic_local / forward_to_baidu
               → HTTPConnection / urllib
```

### 目标架构 (v4.6)

```
Client → ProxyHandler (BaseHTTPRequestHandler, 同步入口 + async 桥接)
           → [PR-A] AuthManager.check(bearer_token, model)
           → [PR-C] RateLimiterV2.acquire(model) [asyncio]
           → [PR-B] RequestLogger.log(entry) [JSONL]
           → Router.resolve(model)
             → 本地? → forwarder.forward_local [asyncio + aiohttp TCPConnector]
             → 云端? → forwarder.forward_cloud(provider, protocol) [双协议透传]
```

## 模块设计

### PR-A: AuthManager

**文件**: `inferfabric/proxy/auth.py` (~80 行)

```
AuthManager
  ├── __init__(config_path: Path)
  │     → 读取 api_keys.yaml
  │     → 构建 _key_map: dict[str, _KeyEntry]  (O(1) lookup)
  ├── enabled: bool  (property)
  ├── check(bearer_token: str, model: str) → tuple[bool, str]
  │     → key_map lookup → expired? → model allowed?
  └── reload()  → 重新读取 yaml
```

**配置格式** (`api_keys.yaml`):
```yaml
primary: "sk-iff-<hash>"
guests:
  - key: "***"
    name: "测试用"
    models: ["qwen35-9b"]
    expires: "2026-08-30T00:00:00+08:00"
```

**handler.py 注入点**: `do_POST` → 协议路由后 → `_forward_local` 前

### PR-B: RequestLogger

**文件**: `inferfabric/proxy/request_logger.py` (~70 行)

```
RequestLogger
  ├── __init__(log_dir: Path, enabled: bool)
  ├── log(entry: RequestLog)
  │     → 按日轮转 JSONL
  │     → 实时 flush
  └── _rotate(new_date: str)
```

**RequestLog dataclass**:
```python
@dataclass
class RequestLog:
    req_id: str
    key_name: str
    model: str
    status: int
    ttft_ms: float | None
    tokens_in: int
    tokens_out: int
    duration_ms: float
    route: str              # "local" | "cloud"
    cloud_provider: str | None  # "baidu-codingplan" | None
    error: str | None
    ts: str
```

**TTFT 采集**: `chat_handlers.py` SSE 管道中，首 `choices[0].delta.content` 到达时记录时间戳

### PR-C: RateLimiterV2

**文件**: `inferfabric/ratelimit.py` 重写 (~120 行)

```
AsyncTokenBucket
  ├── __init__(capacity, refill_per_sec)
  ├── async acquire(tokens=1, timeout=30.0) → bool
  └── _refill()

RateLimiterV2
  ├── __init__(models_config: dict[str, int])
  │     → server_bucket = AsyncTokenBucket(sum(max_seqs))
  │     → model_buckets = {name: AsyncTokenBucket(max_seqs)}
  ├── async acquire(model, timeout=30.0) → bool
  │     → model bucket first → server bucket second
  │     → server 失败则释放 model bucket
  └── async release(model)
```

**handler async 桥接**:
```python
# handler.py _forward_local 改造
def _forward_local(self, pm, data, auth_header, model_obj, original_model):
    loop = asyncio.new_event_loop()
    try:
        acquired = loop.run_until_complete(
            pm.limiter.acquire(model_name, timeout=30.0)
        )
        if not acquired:
            self._send_json({"error": "rate_limit"}, 429)
            return
        try:
            forwarder.forward_anthropic_local(...)
        finally:
            loop.run_until_complete(pm.limiter.release(model_name))
    finally:
        loop.close()
```

**forwarder 连接池**:
```python
# forwarder.py
_session = None

async def _get_session():
    global _session
    if _session is None:
        _session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=100, keepalive_timeout=30)
        )
    return _session
```

### PR-D: Cloud Provider 管理

**文件**: `cloud_provider.yaml` (新增) + `inferfabric/forwarder.py` 重构 + `inferfabric/proxy_manager.py` 发现逻辑

**cloud_provider.yaml**:
```yaml
providers:
  baidu-codingplan:
    api_key: "***"
    openai_base: "https://qianfan.baidubce.com/v2/coding"
    anthropic_base: "https://qianfan.baidubce.com/anthropic/coding/v1"
    timeout: 60
    enabled: true
    discovery:
      enabled: true
      endpoint: "/models"
      interval: 3600
      filter:
        include_pattern: "^(deepseek|glm|qwen3\\.5).*"
    routing:
      default: "cloud_only"

local_fallback:
  default: true
```

**发现引擎**:
```
CloudDiscovery
  ├── __init__(provider_configs)
  ├── discover_all() → dict[str, CloudModel]
  │     → 对每个 enabled provider: GET <openai_base>/models
  │     → 应用 filter
  │     → 构建 CloudModel 列表
  └── start_polling(interval) → asyncio background task
```

**CloudModel**:
```python
@dataclass
class CloudModel:
    model_id: str
    provider: str
    openai_available: bool
    anthropic_available: bool
    discovered_at: float
```

**路由决策** (替换 handler.py Step 2-3):
```python
def _resolve_route(self, model_name, cloud_models, local_models):
    if model_name in local_models:
        return Route(type="local", model=local_models[model_name])
    if model_name in cloud_models:
        cm = cloud_models[model_name]
        return Route(type="cloud", provider=cm.provider, model=cm)
    return None
```

**统一转发**:
```python
def forward_to_cloud(handler, data, provider_cfg, cloud_model, protocol):
    if protocol == "anthropic":
        if not cloud_model.anthropic_available:
            return handler._send_json({"error": "provider does not support Anthropic protocol"}, 501)
        url = f"{provider_cfg.anthropic_base}/messages"
        headers = {"x-api-key": provider_cfg.api_key, "Content-Type": "application/json"}
    elif protocol == "openai":
        if not cloud_model.openai_available:
            return handler._send_json({"error": "provider does not support OpenAI protocol"}, 501)
        url = f"{provider_cfg.openai_base}/chat/completions"
        headers = {"Authorization": f"Bearer {provider_cfg.api_key}", "Content-Type": "application/json"}
    # ... 请求发送
```

**删除项**:
- `model_affinity.yaml` → 被 `cloud_provider.yaml` 替代
- `forwarder.forward_to_baidu()` → 被 `forward_to_cloud()` 替代
- handler.py 中硬编码的 `baidu` 路由逻辑 → 被统一路由决策替代
- handler.py 中硬编码的 `qianfan-code-latest` → 被 `/models` 发现替代

## 测试策略

### 单元测试（沙箱环境，不涉及生产配置）

| PR | 测试文件 | 测试项 |
|----|---------|--------|
| A | `test_auth.py` | key 校验/过期/模型白名单/空配置 |
| B | `test_request_logger.py` | JSONL 写入/轮转/TTFT 采集/disabled |
| C | `test_ratelimit_v2.py` | 令牌桶 refill/acquire/release/并发 |
| D | `test_cloud_discovery.py` | 发现/filter/路由决策/双协议 |

### 集成测试（沙箱环境）

- 沙箱配置指向沙箱端口，不启动真实 vLLM
- mock HTTP server 模拟 vLLM 和云端端点
- 验证完整请求链路

### 性能测试（沙箱环境）

- c=4 并发 benchmark，对比 v4.3 vs v4.6 的 TTFT/TPS
- 验证 PR-C 的事件循环不再被阻塞
