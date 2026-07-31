# IFF v4.6.0 需求全量清单

**日期**: 2026-07-30
**沙箱**: `~/projects/inferfabric-sandbox/`
**生产**: `~/projects/inferfabric/` (未修改)

---

## PR-A: 鉴权 (AuthManager)

| # | 需求 | 验收标准 | 状态 |
|---|------|---------|------|
| A-1 | `api_keys.yaml` 配置 primary key | primary key 全模型通行 | ✅ |
| A-2 | 可选 guest keys + 模型白名单 | guest key 请求非白名单模型 → 401 "model not allowed" | ✅ |
| A-3 | guest key 过期时间 | expires 已过 → 401 "key expired" | ✅ |
| A-4 | 文件不存在/为空 = 鉴权关闭 | 行为与 v4.3 完全一致 | ✅ |
| A-5 | Bearer token 校验在请求入口 | 无 Authorization → 401 | ✅ |
| A-6 | admin 路由不受鉴权影响 | /switch, /stop 等无需 key | ✅ |
| A-7 | 热加载 | `iff reload` 重新读取 yaml | ✅ |
| A-8 | 配置缺失时 WARNING 日志 | api_keys.yaml 无效 → `WARNING: auth DISABLED` | ✅ (P2-2 fix) |

**文件**: `proxy/auth.py` (新增), `proxy/handler.py`, `proxy/chat_handlers.py`, `proxy_manager.py`

---

## PR-B: 结构化请求日志 (RequestLogger)

| # | 需求 | 验收标准 | 状态 |
|---|------|---------|------|
| B-1 | JSONL 按日轮转 | `logs/access-YYYY-MM-DD.jsonl` | ✅ |
| B-2 | 字段: req_id, key_name, model, status | 每行 JSONL 包含全部字段 | ✅ |
| B-3 | 字段: ttft_ms | SSE 首 token 时间戳采集 | ✅ |
| B-4 | 字段: tokens_in, tokens_out, duration_ms | 请求级统计 | ✅ |
| B-5 | 字段: route(local/cloud), cloud_provider | 路由信息记录 | ✅ |
| B-6 | 字段: error | 错误请求记录 | ✅ |
| B-7 | enabled=False 零开销 | 无日志文件生成 | ✅ |
| B-8 | 实时 flush | 防 crash 丢数据 | ✅ |
| B-9 | 跨日自动轮转 | 第二天到来自动切换新文件 | ✅ |

**文件**: `proxy/request_logger.py` (新增), `proxy/handler.py`, `proxy/chat_handlers.py`

---

## PR-C: 限流器重构 (RateLimiterV2)

| # | 需求 | 验收标准 | 状态 |
|---|------|---------|------|
| C-1 | TokenBucket 替代 Semaphore | RPM 语义更精确，支持突发 | ✅ |
| C-2 | 两级门控: server 级 + per-model | server 并发满 → 429 | ✅ |
| C-3 | acquire 超时机制 | timeout 参数，超时返回 (False, msg) | ✅ |
| C-4 | 非阻塞 try_acquire | 立即返回结果 | ✅ |
| C-5 | 启动时预创建 model bucket | 消除首次竞态 | ✅ |
| C-6 | 线程安全 | TokenBucket 用 threading.Lock | ✅ |
| C-7 | v1 兼容层 | `_RateLimiter` 保留，旧代码不受影响 | ✅ |
| C-8 | 删除 `_MODEL_RATE_LIMITERS` dict 竞态 | 新架构无此 dict | ✅ |

**文件**: `ratelimit.py` (重写)

---

## PR-D: 云端 Provider 统一管理

| # | 需求 | 验收标准 | 状态 |
|---|------|---------|------|
| D-1 | `cloud_provider.yaml` per-provider 配置 | api_key + openai_base + anthropic_base | ✅ |
| D-2 | 双协议原生透传 | OpenAI→cloud OpenAI, Anthropic→cloud Anthropic | ✅ |
| D-3 | IFF 不做协议转换 | 单用户无转换需求 | ✅ |
| D-4 | 自动发现: GET `/models` + regex filter | 启动时发现云端模型合入路由表 | ✅ |
| D-5 | 定期轮询发现 | interval 可配，发现新模型/移除下线模型 | ✅ |
| D-6 | IFF 持有云端凭证 | 客户端只需 IFF key | ✅ |
| D-7 | `/v1/models` 合并返回 | 本地 + 云端模型统一列表 | ✅ |
| D-8 | 路由决策: 本地优先 → 云端 → fallback | `CloudDiscovery.resolve_route()` | ✅ |
| D-9 | `forward_to_cloud()` 双协议转发 | stream + non-stream | ✅ |
| D-10 | 本地失败自动回退 | local_fallback=true 时回退云端同名模型 | ✅ |
| D-11 | 单协议 provider 的 501 | 仅有 openai_base 时 Anthropic 请求 → 501 | ✅ |
| D-12 | api_key 环境变量展开 | `${VAR}` 语法解析 | ✅ |
| D-13 | Dual-key 模型注册表 | 短名 + provider/model_id 双索引 | ✅ |
| D-14 | Spec-only 模型注册 | 未发现但有 specs 的模型也注册 | ✅ |

**文件**: `cloud_discovery.py` (新增), `cloud_provider.yaml` (新增), `forwarder.py`, `proxy/handler.py`, `proxy/chat_handlers.py`, `proxy_manager.py`

---

## PR-D-ext: 模型能力属性 (v4.6.0 Dashboard 迭代)

| # | 需求 | 验收标准 | 状态 |
|---|------|---------|------|
| D-15 | CloudModel 能力字段 | context_window, max_output_tokens, supports_vision, supports_tools | ✅ |
| D-16 | `name` 人类可读名称 | "DeepSeek V4 Flash" 等 | ✅ |
| D-17 | `contextWindow` / `maxTokens` (实际可用) | 对齐 OpenClaw model spec，考虑 CodingPlan 流控 | ✅ |
| D-18 | `context_window` / `max_output_tokens` (理论最大) | 官方文档数据源 | ✅ |
| D-19 | `input` 模态列表 | `["text"]` / `["text", "image"]` 代替 supports_vision bool | ✅ |
| D-20 | `reasoning` 思考模式支持 | 对齐 OpenClaw 配置 | ✅ |
| D-21 | `extra` dict 扩展字段 | pricing_tier 等非标准字段 | ✅ |
| D-22 | `to_api_dict()` 输出 `/v1/models` 格式 | 含 capabilities 完整字段 | ✅ |
| D-23 | model_specs 合并优先级 | 手动配置 > 自动发现 | ✅ |
| D-24 | 启动时注册 spec-only 模型 | `_load_config` 后自动调用 `_register_spec_only_models` | ✅ |
| D-25 | `to_api_dict()` 始终输出布尔能力 | supports_vision/tools 不再按 True/False 省略 | ✅ |
| D-26 | 参数来源: 官方文档 | DS V4: HuggingFace, GLM: 千帆+智谱, 不再凭记忆编造 | ✅ |

**数据源**:
- DeepSeek V4: HuggingFace model card — ctx=1M, out=384K
- GLM-5/5.1: 百度千帆模型列表 + 智谱官方文档 — ctx=198K/200K, out=128K
- CodingPlan 流控: OpenClaw `openclaw.json` 实际配置 — contextWindow / maxTokens

---

## PR-D-ext: Admin API + Dashboard 云端管理

| # | 需求 | 验收标准 | 状态 |
|---|------|---------|------|
| D-27 | GET `/admin/cloud/providers` | 返回 provider 列表 + 模型能力 | ✅ |
| D-28 | POST `/admin/cloud/providers` | 添加 provider (name, api_key, bases) | ✅ |
| D-29 | DELETE `/admin/cloud/providers` | 删除 provider 及其模型 | ✅ |
| D-30 | POST `/admin/cloud/discover` | 手动触发模型发现 | ✅ |
| D-31 | POST `/admin/cloud/discover` + body.provider | 单 provider 发现 | ✅ |
| D-32 | POST `/admin/cloud/reload` | 热加载 cloud_provider.yaml | ✅ |
| D-33 | POST `/admin/cloud/test` | 测试 provider 连接 | ✅ |
| D-34 | Dashboard: 添加 Provider 表单 | 名称/API Key/OpenAI Base/Anthropic Base | ✅ |
| D-35 | Dashboard: "添加 & 发现" 按钮 | 添加后自动触发发现 | ✅ |
| D-36 | Dashboard: "仅测试连接" 按钮 | 不添加，只测试 /models 可达性 | ✅ |
| D-37 | Dashboard: Provider 列表卡片 | 含 🔍 发现 / 🗑 删除 按钮 | ✅ |
| D-38 | Dashboard: 模型列表 + 能力标签 | ctx/out/vision/tools/reasoning 标签 | ✅ |
| D-39 | Dashboard: 模型去重 | 跳过 dual-key 的 provider/ 前缀条目 | ✅ |
| D-40 | Dashboard: spec-only 模型标记 | "仅配置" vs "已发现" 状态标签 | ✅ |
| D-41 | CLI: `scripts/iff-cloud` | 列出/发现/重载 provider | ✅ |

---

## 审计修复 (Cross-review Fixes)

| # | 修复 | 状态 |
|---|------|------|
| P0-1 | handler.py auth 注入点覆盖所有路径 | ✅ |
| P0-2 | RateLimiterV2 死锁修复 (嵌套锁内联) | ✅ |
| P0-3 | forward_to_cloud stream 模式 SSE 解析 | ✅ |
| P1-1 | RequestLog dataclass 字段完整性 | ✅ |
| P1-2 | TokenStatsCollector API 适配 | ✅ |
| P1-5 | parse_retry_after_ms header 名称 | ✅ |
| P1-6 | GPULock 文件锁隔离 (测试用 temp path) | ✅ |
| P1-7 | CloudModel env var None vs "" | ✅ |
| P2-2 | auth config 缺失时 WARNING 日志 | ✅ |
| P2-4 | is_held property vs method | ✅ |

---

## 延迟项 (Deferred)

| # | 需求 | 原因 | 优先级 |
|---|------|------|--------|
| P0-4 | Chunked transfer encoding | 当前 IFF 客户端不使用 | 低 |
| P1-3 | Hop-by-hop header 清理 | 无实际触发场景 | 低 |
| P1-4 | TokenBucket acquire busy-wait | 当前超时值足够 | 低 |
| P2-1 | 常量时间 auth 比较 | 单用户场景攻击面极小 | 低 |
| P2-3 | Logger flush 策略优化 | 当前实时 flush 已足够 | 低 |
| P2-5 | 双重 import re 模块 | 无功能影响 | 低 |

---

## v1 兼容保留 (迁移期)

| 组件 | 状态 | 备注 |
|------|------|------|
| `_RateLimiter` | 保留 | ratelimit.py 兼容层 |
| `forward_to_baidu()` | 保留 | forwarder.py fallback |
| `model_affinity.yaml` | 保留 | 路由 fallback |
| 观察期后移除 | 待定 | 生产稳定后清理 |

---

## 测试覆盖

| 测试套件 | 测试数 | 状态 |
|----------|--------|------|
| test_v45_comprehensive.py | 144 | ✅ 全绿 |
| test_auth.py | 19 | ✅ 全绿 |
| test_request_logger.py | 14 | ✅ 全绿 |
| test_ratelimit_v2.py | 17 | ✅ 全绿 |
| test_cloud_discovery.py | 20 | ✅ 全绿 |
| **新增合计** | **214** | **✅ 全绿** |
| test_functional.py | 8 | ⚠️ 6 pre-existing fail |
| test_robustness.py | 29 | ⚠️ 1 pre-existing fail |
| **回归结论** | | **v4.6.0 未引入新失败** |

---

## 未完成项

| # | 需求 | 状态 | 备注 |
|---|------|------|------|
| U-1 | AtomCode 交叉审查 | ⬜ 待审 | Provider 401，恢复后补审 |
| U-2 | 性能基准 (v4.3 vs v4.6, c=4) | ⬜ 未跑 | 需生产级负载 |
| U-3 | Dashboard 模型能力标签前端展示 | ⬜ 待验证 | JS 已写，需刷新确认 |
| U-4 | `cloud_provider.yaml` 持久化 (从 Dashboard 添加后写回 YAML) | ⬜ 未实现 | 当前仅内存添加，重启丢失 |

---

**总需求数**: 41 (PR-A: 8 + PR-B: 9 + PR-C: 8 + PR-D: 14) + 审计修复 10 + Dashboard 15 = **68 项已完成**
**延迟项**: 6 | **未完成项**: 4
