# InferFabric 业务流程图

> v5.6.8 — 网关加固完成后全链路文档
> 包含：整体架构、本地模型调用流程、云端模型调用流程、异常事件流

---

## 1. 整体架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          Client (Claude Code / API)                     │
│                     POST /v1/messages 或 /v1/chat/completions            │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                    InferFabric Proxy (127.0.0.1:8999)                   │
│                                                                          │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────────────┐ │
│  │  AuthManager  │   │  ProxyManager│   │    CloudDiscovery            │ │
│  │  (API keys)   │──▶│  (routing)   │──▶│  (baidu/deepseek/etc.)      │ │
│  └──────────────┘   │              │   └──────────────────────────────┘ │
│                     │  ┌──────────────────────────┐                    │ │
│                     │  │  DualGateLimiter          │                    │ │
│                     │  │  • RPM token bucket       │                    │ │
│                     │  │  • per-model Semaphore    │                    │ │
│                     │  │  • optional global max    │                    │ │
│                     │  └──────────────────────────┘                    │ │
│                     │  ┌──────────────────────────┐                    │ │
│                     │  │  ResponseCache (R5)      │                    │ │
│                     │  │  • cachetools LRU        │                    │ │
│                     │  │  • temp=0, non-stream    │                    │ │
│                     │  └──────────────────────────┘                    │ │
│                     │  ┌──────────────────────────┐                    │ │
│                     │  │  ReplicaSelector (R7)    │                    │ │
│                     │  │  • least_busy selection  │                    │ │
│                     │  │  • per-replica health    │                    │ │
│                     │  └──────────────────────────┘                    │ │
│                     │  ┌──────────────────────────┐                    │ │
│                     │  │  AnomalyCollector (R9)   │                    │ │
│                     │  │  • ring buffer 500 evts  │                    │ │
│                     │  │  • GET /api/anomalies    │                    │ │
│                     │  └──────────────────────────┘                    │ │
│  ┌──────────────┐   │  ┌──────────────────────────┐                    │ │
│  │ RequestLogger │   │  │  HealthMonitor + Watchdog │                   │ │
│  │  (SQLite)    │   │  │  • per-replica health   │                    │ │
│  └──────────────┘   │  │  • auto-restart on fail │                    │ │
│                     │  └──────────────────────────┘                    │ │
│                     └───────────────────────────────────────────────────│
│                                                                          │
│  GET /metrics → Prometheus 文本格式 (R6)                                 │
│  GET /api/anomalies → 结构化异常事件 JSON (R9)                          │
│  GET /api/request_log → 请求日志历史 (R1)                               │
│  GET /dashboard → Web UI (R10: 含"异常"tab)                             │
│                                                                          │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
┌─────────────────┐ ┌───────────┐ ┌──────────────────────┐
│  本地 vLLM       │ │  SGLang    │ │  云端 Provider       │
│  127.0.0.1:8002  │ │  :8006    │ │  api.baidu.com/v1    │
│  qwen38-27b      │ │ gemma4    │ │  deepseek.com/v1     │
│  (replicas支持)  │ │ (docker)  │ │  (零协议转换透传)     │
└─────────────────┘ └───────────┘ └──────────────────────┘
```

---

## 2. 请求路由决策树

```
                        ┌──────────────┐
                        │  请求进入      │
                        │  model="xxx"  │
                        └──────┬───────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │  Auth 检查            │
                    │  pm.auth.check()      │
                    └──────┬───────┬───────┘
                           │ 通过   │ 失败
                           ▼        ▼ 401
                    ┌──────────────────────┐
                    │  R5: 缓存查找         │
                    │  ResponseCache.get()  │
                    └──────┬───────┬───────┘
                     命中   │       │ 未命中
                           ▼       ▼
                      直接返回   ┌──────────────────────────┐
                      (200)     │  SWITCHING 守卫           │
                                │  profile_state == SWITCH? │
                                └──────┬───────┬────────────┘
                                      YES     │ NO
                                        │     │
                                        ▼     ▼
                              ┌──────────────────────┐
                              │  放行云模型 /         │
                              │  503 (其他本地模型)   │
                              └──────────────────────┘

                              继续 → 本地/云路由判定
```

---

## 3. 本地模型调用流程

```
                    ┌──────────────────────────────────────┐
                    │  find_model_by_served_name(model)    │
                    │  → 找到 ModelConfig                  │
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  model.name in active_services?      │
                    └──────┬──────────────┬────────────────┘
                        YES│              │NO
                           ▼              ▼
                    ┌────────────┐ ┌──────────────────────────┐
                    │ 直接转发    │ │ AUTO_SWITCH=ON?          │
                    │            │ └──────┬──────────┬────────┘
                    │ select_port │     YES│          │NO
                    │ (or .port)  │       │          │
                    │             │       ▼          ▼
                    │ forward     │ ┌──────────┐ ┌──────────┐
                    │ → release   │ │ensure_    │ │503       │
                    │             │ │service()  │ │not_active│
                    └────────────┘ │           │ │+Retry-After:10│
                                   │ switch()  │ └──────────┘
                                   │  失败     │
                                   └──┬───┬────┘
                                    成功│   │失败
                                       │   │
                                       ▼   ▼
                                ┌──────────┐ ┌───────────────┐
                                │forward   │ │503 switch_    │
                                │local     │ │failed +       │
                                │200       │ │Retry-After:10 │
                                │          │ │R0: cooldown   │
                                │→ cache   │ │for 10s        │
                                │→ release │ │+ anomaly      │
                                └──────────┘ └───────────────┘

                本地转发细节:
                ┌────────────────────────────────────────────┐
                │  _forward_local(pm, data, ..., model_obj)  │
                │  ├─ gate = dual_gate.acquire(model)        │
                │  │    • RPM token check                    │
                │  │    • per-model Semaphore acquire        │
                │  │    • optional global max Semaphore      │
                │  ├─ R7: 多副本端口选择                      │
                │  │    • model_obj.replicas? → select_port()│
                │  │    • least_busy / round_robin           │
                │  ├─ forward_anthropic_local(或 vLLM)       │
                │  │    • retry 3次 (0.5s/1s/2s backoff)    │
                │  │    • pipe_stream_response / send_json   │
                │  ├─ R5: 非流式成功 → cache put             │
                │  ├─ gate.release()                         │
                │  └─ R7: release_port() (多副本时)          │
                └────────────────────────────────────────────┘
```

---

## 4. 云端模型调用流程

```
                    ┌──────────────────────────────────────┐
                    │  find_model_by_served_name(model)    │
                    │  → None (本地无此 served_name)        │
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  resolve_route(model, local_models)  │
                    │  → "cloud:baidu-codingplan"          │
                    │  (short_name in cloud_models?)       │
                    └──────┬──────────────┬────────────────┘
                       cloud│              │None
                           ▼              ▼
                    ┌──────────────┐  ┌──────────────────────┐
                    │ forward_to_   │  │ R8: 404 + anomaly   │
                    │ cloud()      │  │ "Unknown model"     │
                    │              │  └──────────────────────┘
                    │ 协议透传      │
                    │ protocol=    │
                    │ anthropic/   │
                    │ openai       │
                    │              │
                    │ R3: timeout  │
                    │ 60s→600s    │
                    │              │
                    │ R2: 重试循环  │
                    │ 3 attempts   │
                    │ 0.5s/1s/2s  │
                    └──────┬───────┘
                           │
                           ▼
                ┌──────────────────────┐
                │  云端 Provider        │
                │  baidu-codingplan     │
                │  api.baidu.com/v1     │
                │  (Anthropic/OpenAI)  │
                └──────────────────────┘

                云端转发细节:
                ┌────────────────────────────────────────────┐
                │  forward_to_cloud(protocol="anthropic")    │
                │  ├─ 构造请求: url + x-api-key              │
                │  ├─ R2: for attempt in range(3):           │
                │  │    ├─ urlopen(req, timeout=600)         │
                │  │    ├─ 429/5xx → backoff → retry        │
                │  │    └─ 4xx (非429) → 不重试             │
                │  ├─ 流式: pipe_stream_response             │
                │  ├─ 非流式: send_json                      │
                │  └─ CloudResult → RequestLog               │
                └────────────────────────────────────────────┘
```

---

## 5. 异常事件流 (R9+R10)

```
                  各采集点
    ┌─────────────┬─────────────┬─────────────┐
    │ 路由层       │ 模型层       │ 配置层       │ 云端层
    │ SWITCHING   │ auto-switch │ AUTO_SWITCH │ retry exhaust
    │ guard       │ failed      │ =off        │ 5xx/timeout
    │ unknown     │ cooldown    │ config miss │ 501 unsupported
    │ model       │ manual stop │             │
    └──────┬──────┴──────┬──────┴──────┬──────┘
           │             │             │
           ▼             ▼             ▼
    ┌──────────────────────────────────────────────┐
    │          AnomalyCollector (R9)               │
    │  • AnomalyEvent{category, severity, model,   │
    │    message, status_code, possible_cause}     │
    │  • 环形缓冲 500 条                            │
    │  • 线程安全 Lock                              │
    └──────────────────┬───────────────────────────┘
                       │
                       ▼
    ┌──────────────────────────────────────────────┐
    │  GET /api/anomalies                           │
    │  ?category=routing&severity=warning&limit=100│
    │  → JSON {events: [...], count: N}            │
    └──────────────────┬───────────────────────────┘
                       │
                       ▼
    ┌──────────────────────────────────────────────┐
    │  Dashboard "异常" tab (R10)                  │
    │  • 实时事件列表（10s 轮询）                   │
    │  • severity 着色：🟢🟡🔴⚫                  │
    │  • 按 category / severity 筛选               │
    │  • possible_cause 直接渲染                    │
    └──────────────────────────────────────────────┘
```

---

## 6. 响应缓存流程 (R5)

```
                    ┌──────────────────────┐
                    │  Auth 通过后          │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │  cache enabled?      │──否──→ 正常转发
                    │  stream=false?       │
                    │  temperature=0?      │
                    └──────────┬───────────┘
                             是│
                    ┌──────────▼───────────┐
                    │  ResponseCache.get()  │
                    │  key=sha256(model+   │
                    │  canonical_json(body))│
                    └──────┬───────────────┘
                     命中   │       │ 未命中
                           ▼       ▼
                    ┌──────────┐ ┌──────────────────┐
                    │ 直接返回  │ │ 正常转发          │
                    │ 200      │ │ → 成功后 cache put│
                    │ X-IFF-   │ │ (仅 non-stream,  │
                    │ Cache:hit│ │  temp=0, 200)    │
                    └──────────┘ └──────────────────┘
```

---

## 7. 指标与可观测 (R6)

```
                    ┌────────────────────────────────┐
                    │  GET /metrics                  │
                    │  Prometheus text/plain 格式    │
                    └────────────────┬───────────────┘
                                     │
                    ┌────────────────▼───────────────┐
                    │  PrometheusMetricsExporter      │
                    │  ┌──────────────────────────┐  │
                    │  │ iff_requests_total        │  │
                    │  │   {model, route, status}  │  │
                    │  │ iff_errors_total          │  │
                    │  │   {model, error}          │  │
                    │  │ iff_tokens_in_total       │  │
                    │  │   {model}                │  │
                    │  │ iff_tokens_out_total      │  │
                    │  │   {model}                │  │
                    │  │ iff_anomalies_total       │  │
                    │  │   {category, severity}    │  │
                    │  │ iff_uptime_seconds        │  │
                    │  └──────────────────────────┘  │
                    │  数据源: RequestLogDB +        │
                    │         AnomalyCollector       │
                    └────────────────────────────────┘
```

---

## 8. 全项目需求矩阵

| 需求 | 分类 | 说明 | 状态 |
|------|------|------|------|
| R0 | 基础修复 | ensure_service cooldown fix | ✅ v5.6.7 |
| R1 | 可观测 | request_log 补盲区（8 个早退路径） | ✅ v5.6.8 |
| R2 | 云端增强 | 云端转发退避重试（3 次，0.5s/1s/2s） | ✅ v5.6.8 |
| R3 | 云端增强 | 默认超时 60s→600s | ✅ v5.6.8 |
| R4 | 能力增强 | per-model 并发池 + 可选全局上限 | ✅ v5.6.8 |
| R5 | 能力增强 | 精确匹配响应缓存（cachetools LRU） | ✅ v5.6.8 |
| R6 | 可观测 | Prometheus /metrics 文本格式 | ✅ v5.6.8 |
| R7 | 架构升级 | async aiohttp server + 多副本 + 负载均衡 | ✅ v5.6.8 |
| R8 | 行为修正 | 削除 Step 6/7 静默 fallback | ✅ v5.6.8 |
| R9 | 可观测 | AnomalyCollector + /api/anomalies | ✅ v5.6.8 |
| R10 | 可观测 | Dashboard 异常看板 tab | ✅ v5.6.8 |