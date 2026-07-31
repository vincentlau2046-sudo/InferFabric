# IFF v4.6.1 Spec — Metrics & Hardening

**日期**: 2026-07-31
**基线**: v4.6.0 (commit a61914e)
**审查**: AtomCode GLM-5.2 + Codex GLM-5.1 交叉验证

---

## 目标

v4.6.0 已实现 API Gateway 四模块 (auth/log/rate-limiter/cloud)。v4.6.1 补齐三层缺失：
1. 数据先记全 (G-1a)
2. 限流语义正确 (PR-E 二级嵌套门)
3. 可观测闭环 (G-2 聚合 + G-3 Dashboard)

---

## PR Specs

### G-1a: RequestLog 数据补全 (non-streaming)

**问题**: cloud 路由日志在请求开始前写 (status=0)，请求结果未记录。29 条 access log tokens 全为 0。

**变更**:
1. `forwarder.py` — `forward_to_cloud()` non-streaming 路径：解析上游响应 `usage` 字段，返回 `(usage_dict, ttft_ms, duration_ms)` 给调用方
2. `chat_handlers.py` — cloud 路由：删除请求前的占位日志，改为在 `forward_to_cloud()` 返回后写完整日志
3. `chat_handlers.py` — local non-streaming：补 TTFT (`_forward_request` 非流式分支设 `_ttft_ms`)
4. 上游无 `usage` 字段 → graceful fallback 到 0，不抛 KeyError

**不包含**: streaming usage 提取 (G-1b，推迟)

**验证**:
- cloud non-streaming 请求后 access log `tokens_in/tokens_out/ttft_ms/duration_ms` 非零
- 上游不返 usage 时 tokens 为 0，请求不 500
- local non-streaming TTFT 非零

**风险**: 低 — 日志写入在 finally 块，失败不 500

---

### PR-E: RateLimiterV2 集成 (二级嵌套门)

**问题**: `_RateLimiter` (Semaphore) 是并发数限流，`RateLimiterV2` (TokenBucket) 是 RPM 限流，语义正交。直接替换会导致 burst=60 瞬间涌入 vLLM，KV cache OOM。

**设计**: 二级嵌套门
```
请求 → [RPM TokenBucket 软门] → [并发 Semaphore 硬门] → vLLM
         限每分钟总量              限同时在飞数
```

**变更**:
1. `ratelimit.py` — 新增 `DualGateLimiter` 类：先 `RateLimiterV2.acquire()` (RPM)，再 `_RateLimiter.acquire()` (并发)
2. `proxy_manager.py` — 初始化 `DualGateLimiter`，注册已知模型桶
3. `chat_handlers.py` — `_get_model_rate_limiter()` → `pm.dual_gate.acquire(model_name)` / `pm.dual_gate.release(model_name)`
4. 删除旧的 `_MODEL_RATE_LIMITERS` / `_get_model_rate_limiter` (确认无其他 import 后)
5. **cloud 路由不加限流**

**验证**:
- burst > max_num_seqs 时并发 Semaphore 正确拦截
- RPM 超限时返回 429
- cloud 请求不经限流门

**风险**: 中 — 限流逻辑改动影响请求准入，需充分测试 429 + 正常放行

---

### G-2: MetricsAggregator

**依赖**: G-1a

**设计**:
- 内存滑动窗口聚合，1h/24h/7d/全部
- 指标：请求计数(success/fail) / token 计数 / 延迟分位(p50/p95/p99) / 费用估算
- **queue.Queue 解耦**: RequestLogger.log() 写完 JSONL 后把 RequestLog 入队列，aggregator 后台线程消费
- **费用持久化**: 复用 access JSONL 的 tokens 字段 + price 配置，重启后回放重建窗口
- `cloud_provider.yaml` 增加 `price_input/price_output` (¥/1M tokens)
- 新增 `/api/metrics` 端点
- 端点 try/except 兜底，异常不 500 主服务

**滑动窗口实现**: 全量样本 + 现算分位 (单用户 QPS 低)

**验证**:
- 请求后 `/api/metrics` 返回正确计数和分位
- aggregator 出错不影响主请求
- 重启后从 JSONL 回放费用窗口

---

### PR-F: Provider 持久化 (原子写入)

**变更**:
1. `cloud_discovery.py` — `save_config()`: write-to-temp → `yaml.safe_load` 校验 → `os.replace()` 原子替换
2. `handler.py` — POST/DELETE 路由成功后调用 `save_config()`
3. `save_config` 加 `threading.Lock`，与 `reload` 互斥
4. `_load_config` 的 `except Exception` 静默吞错改为 `log.error` + 状态标志
5. 用 `ruamel.yaml` 保留注释 (接受部分行内注释丢失)

**验证**:
- 添加/删除 provider → 重启 IFF → 确认持久化
- 进程在写入中途被 kill → YAML 不损坏

---

### G-3a: Dashboard 拆分 (字节级等价)

**变更**:
- `dashboard.py` (1774 行单文件) → `dashboard/` 目录
- fragments 用 `Path(__file__).parent` 加载，**启动时缓存到内存**
- fragment 加载失败 graceful degradation (最小可用 HTML)
- 输出与重构前字节级等价

### G-3b: Monitor 7 面板重做

7 面板: 请求概览 / Token 用量 / 延迟分布 / 费用概览 / 请求日志 / vLLM 性能(保留) / GPU 实时(保留)

---

## 不做的事

- ❌ cloud 路由限流 (单用户 + 云端自有配额)
- ❌ API Key 级统计
- ❌ PostgreSQL/Redis 依赖
- ❌ 实时 WebSocket 推送
- ❌ 多租户/团队管理
- ❌ 预算限制/告警
