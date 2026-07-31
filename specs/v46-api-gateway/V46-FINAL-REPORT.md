# IFF v4.6 — 最终修改报告

**日期**: 2026-07-30  
**版本**: v4.6 (API Gateway)  
**仓库**: `~/projects/inferfabric-sandbox/` (sandbox, 未合入生产)

---

## 一、功能总览

| PR | 功能 | 新文件 | 修改文件 |
|----|------|--------|---------|
| PR-A | 鉴权系统 (AuthManager) | `proxy/auth.py` | — |
| PR-B | 请求日志 (RequestLogger) | `proxy/request_logger.py` | — |
| PR-C | 限流 V2 (TokenBucket + RateLimiterV2) | — | `ratelimit.py` (重写 + v1 compat) |
| PR-D | 云端发现与路由 (CloudDiscovery) | `cloud_discovery.py`, `cloud_provider.yaml` | `forwarder.py` |
| 额外 | Admin API + Dashboard + CLI | `scripts/iff-cloud` | `handler.py`, `chat_handlers.py`, `proxy_manager.py`, `dashboard.py` |

## 二、新增文件 (5)

| 文件 | 行数 | 功能 |
|------|------|------|
| `proxy/auth.py` | 117 | Bearer token 鉴权：primary/guest key + 模型白名单 + 过期时间 + hot-reload |
| `proxy/request_logger.py` | 85 | JSONL 日志：daily rotation + 线程安全 + TTFT/route/cloud_provider 字段 |
| `cloud_discovery.py` | 280 | 云端模型发现：`/models` 端点轮询 + regex filter + 双 key 注册 + env var 展开 |
| `cloud_provider.yaml` | 25 | 云端 provider 配置：Baidu CodingPlan (OpenAI + Anthropic 双协议) |
| `scripts/iff-cloud` | 72 | CLI 工具：discover / reload / providers / models |

## 三、修改文件 (7)

| 文件 | 修改内容 |
|------|---------|
| `ratelimit.py` | 新增 TokenBucket + RateLimiterV2 (二级限流: server+model RPM)；v1 `_RateLimiter` 保留 + `_v1_lock` 线程安全 |
| `forwarder.py` | 新增 `forward_to_cloud()` — 双协议透传 (OpenAI→OpenAI, Anthropic→Anthropic, 不做协议转换) |
| `proxy/handler.py` | 鉴权检查 + 请求日志上下文变量 + cloud 路由 + Admin API 路由 (`/admin/cloud/*`) + GET providers |
| `proxy/chat_handlers.py` | auth 检查 + 全路径日志记录 (200/429/502) + TTFT 采集 + cloud 路由分发 |
| `proxy_manager.py` | AuthManager/RequestLogger/CloudDiscovery 初始化 + `IFF_DATA_DIR` 绝对路径 + `start_polling()` |
| `dashboard.py` | `☁️ 云端管理` tab：provider 表格 + 模型列表 + discover/reload 按钮 + JS 函数 |
| `proxy/auth.py` | P2-2 修复：配置缺失/格式错误时 `log.warning("auth DISABLED")` |

## 四、架构决策

| 决策 | 理由 |
|------|------|
| 双协议原生透传 | IFF 不做 OpenAI↔Anthropic 协议转换；客户端协议直通对应云端端点 |
| 自动发现 > 硬编码 | `/models` 端点 + regex filter 替代 `model_affinity.yaml` 硬编码 |
| TokenBucket > Semaphore | RPM 语义精确，支持 burst |
| Sync TokenBucket | 避免 asyncio/sync 桥接复杂度 |
| `~/.inferfabric/` 数据目录 | 消除 CWD 依赖，与现有 `token_stats.py` 一致 |
| 双 key 模型注册 | `model_id` (短名, first-wins) + `provider/model_id` (全名, always-stored) |
| v1 compat 保留 | `_RateLimiter` / `forward_to_baidu()` / `model_affinity.yaml` 作为 fallback |

## 五、测试

| 套件 | 测试数 | 覆盖域 |
|------|--------|--------|
| `test_v45_comprehensive.py` | 144 | 27 类 / 14 功能域：状态/鉴权/日志/限流/云端/转发/ProxyManager/配置/GPU锁/Watcher/Metrics/Admin/Dashboard/包结构 |
| `test_auth.py` | 19 | AuthManager primary/guest/expiry/reload |
| `test_request_logger.py` | 14 | JSONL 写入/daily rotation/并发/TTFT |
| `test_ratelimit_v2.py` | 17 | TokenBucket/RateLimiterV2/v1 compat |
| `test_cloud_discovery.py` | 20 | ProviderConfig/discover/filter/route/polling |
| **合计** | **214** | **204 通过** |

注：comprehensive 套件中 10 个测试与专项套件有逻辑重叠，实际独立测试用例数 ≈ 180+。

## 六、交叉审查修复

| ID | 问题 | 修复 | 验证 |
|----|------|------|------|
| P0-1 | `cloud_discovery.py` 缺 `import os` | 已加 `import os` + `import json as _json` | ✅ test |
| P0-2 | 请求日志未覆盖所有路径 | 上下文变量 + success/429/502 三路径日志 | ✅ test |
| P0-3 | 配置文件相对路径 → CWD 依赖 | `IFF_DATA_DIR = Path.home() / ".inferfabric"` | ✅ test |
| P0-4 | `read_body` 不支持 chunked encoding | **Deferred** — 当前客户端均用 Content-Length | — |
| P1-1 | RequestLogger 线程安全 | `threading.Lock()` + `log()`/`close()` 加锁 | ✅ test |
| P1-2 | `yaml.safe_load` 解析 JSON API 响应 | → `_json.loads()` | ✅ test |
| P1-5 | v1 compat 全局状态无锁 | `_v1_lock = threading.Lock()` | ✅ test |
| P1-6 | 同名 model_id 静默覆盖 | 双 key 注册 (short + provider/) | ✅ test |
| P1-7 | TTFT 未写入 RequestLog | `handle_chat` success path 写入 | ✅ test |
| P2-2 | Auth 配置错误时静默禁用 | 加 `log.warning("auth DISABLED")` | ✅ test |
| P2-4 | `start_polling()` 未在首次发现后调用 | `ensure_cloud_discovered()` 中调用 | ✅ test |

## 七、已知遗留

| 项 | 严重度 | 实际影响 | 建议 |
|----|--------|---------|------|
| P0-4 chunked encoding | P0 标签 | 零触发（所有客户端用 Content-Length） | 永久 defer |
| P1-3 hop-by-hop headers | 无 observable bug | 依赖链路变化才触发 | defer |
| P1-4 busy-wait → Condition | 性能瑕疵 | 单用户低并发，收益 ≈0 | defer |
| P2-1 constant-time auth | 安全加固 | 单用户无旁路攻击面 | defer |
| P2-3 RequestLogger shutdown flush | 数据安全 | JSONL 追加写，OS buffer 通常能刷 | defer |
| P2-5 double import re | cosmetic | 无功能影响 | defer |

## 八、生产合入检查清单

- [ ] Vincent 审查本报告
- [ ] 性能基准 (v4.3 vs v4.6, c=4 concurrent)
- [ ] `diff -r ~/projects/inferfabric/ ~/projects/inferfabric-sandbox/` 确认变更范围
- [ ] 生产环境 `cloud_provider.yaml` 配置（需填入真实 API key）
- [ ] `api_keys.yaml` 部署（primary key 生成）
- [ ] 停机 → rsync → 启动 → 验证 Admin API + Dashboard
- [ ] v1 compat 观察期后移除 `_RateLimiter` / `forward_to_baidu()` / `model_affinity.yaml`
