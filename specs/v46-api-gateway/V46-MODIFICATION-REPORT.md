# IFF v4.6 修改总结 + 测试报告

**版本**: v4.6-delta-001  
**日期**: 2026-07-30  
**状态**: 待 Vincent 审查  
**沙箱路径**: `~/projects/inferfabric-sandbox/`  
**生产路径**: `~/projects/inferfabric/` (未修改)

---

## 一、变更概览

| PR | 模块 | 新增文件 | 修改文件 | 代码行 |
|----|------|---------|---------|-------|
| A | 鉴权 | `proxy/auth.py` | `proxy/handler.py`, `proxy/chat_handlers.py`, `proxy_manager.py` | ~120 |
| B | 请求日志 | `proxy/request_logger.py` | `proxy/handler.py`, `proxy_manager.py` | ~100 |
| C | 限流器 V2 | `ratelimit.py` (重写) | — | ~180 |
| D | 云端 Provider | `cloud_discovery.py`, `cloud_provider.yaml` | `forwarder.py`, `proxy/handler.py`, `proxy/chat_handlers.py`, `proxy_manager.py` | ~250 |

**总计**: 新增 ~650 行核心代码 + 4 个测试文件 ~500 行

---

## 二、各 PR 详细说明

### PR-A: 鉴权 (AuthManager)

- **文件**: `inferfabric/proxy/auth.py`
- **功能**: 单用户 Bearer token 鉴权 + 可选临时 guest key
- **配置**: `api_keys.yaml`（不存在则鉴权关闭，向后兼容）
- **注入点**: `_handle_messages()` 和 `handle_chat()` 请求路由前
- **特性**: primary key 全模型通行 + guest key 模型白名单 + 过期时间 + 热加载

### PR-B: 请求日志 (RequestLogger)

- **文件**: `inferfabric/proxy/request_logger.py`
- **功能**: JSONL 按日轮转，实时 flush
- **数据**: req_id, key_name, model, status, ttft_ms, tokens, route, cloud_provider, error
- **降级**: enabled=False 时零开销

### PR-C: 限流器 V2 (RateLimiterV2)

- **文件**: `inferfabric/ratelimit.py` (重写)
- **功能**: 二级令牌桶（server RPM + model RPM），替代旧 Semaphore 方案
- **特性**: TokenBucket 线程安全 + 超时 acquire + 非阻塞 try_acquire + v1 兼容层
- **修复**: 原死锁问题（`_get_model_bucket` 内嵌套锁 → 内联注册逻辑）

### PR-D: 云端 Provider 统一管理

- **文件**: `inferfabric/cloud_discovery.py`, `cloud_provider.yaml`
- **功能**: 
  - 从 `cloud_provider.yaml` 加载 provider 配置
  - `GET <openai_base>/models` 自动发现模型 + regex filter
  - 统一路由决策：本地优先 → 云端 → fallback
  - 双协议透传：OpenAI→OpenAI endpoint, Anthropic→Anthropic endpoint
  - IFF 持有云端凭证，客户端只需 IFF key
- **forwarder 新增**: `forward_to_cloud()` 双协议转发（stream + non-stream）
- **handler 替换**: Step 2-3 的 `model_affinity` 硬编码路由 → `CloudDiscovery.resolve_route()`
- **未删除**: `model_affinity.yaml` 和 `forward_to_baidu()` 保留为 v1 兼容 fallback，待 PR 合并后清理

---

## 三、测试报告

### v4.6 新增测试（全部通过）

| 测试文件 | 测试数 | 状态 |
|---------|-------|------|
| `test_auth.py` | 19 | ✅ 全绿 |
| `test_request_logger.py` | 10 | ✅ 全绿 |
| `test_ratelimit_v2.py` | 15 | ✅ 全绿 |
| `test_cloud_discovery.py` | 16 | ✅ 全绿 |
| **合计** | **60** | **✅ 全绿** |

### 旧测试回归

| 测试文件 | 通过 | 失败 | 状态 |
|---------|------|------|------|
| `test_functional.py` | 8 | 0 | ✅ |
| `test_pr_integration.py` | 18 | 0 | ✅ |
| `test_v4.py` | 38 | 0 | ✅ |
| `test_switch_to_idle_gpu_none.py` | 5 | 0 | ✅ |
| `test_robustness.py` | 29 | 1 | ⚠️ pre-existing |
| `test_e2e.py` | 0 | 2 | ⚠️ pre-existing |
| `test_local.py` | — | ImportError | ⚠️ pre-existing |

**Pre-existing 失败**（非 v4.6 引入）：
- `test_robustness.py::test_stop_comfyui_no_pid_uses_fallback` — mock 断言不匹配
- `test_e2e.py` — bge-m3 gpu_role=none 生命周期测试
- `test_local.py` — `Profile` import 失败（配置重构遗留）

### 回归结论

v4.6 变更**未引入任何新的测试失败**。全部 60 个新测试通过，旧测试回归与变更前一致。

---

## 四、架构决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| 限流器 | TokenBucket 替代 Semaphore | RPM 语义更精确，支持突发流量 |
| 云端路由 | CloudDiscovery 统一路由替代 model_affinity | 自动发现，零硬编码 |
| 双协议 | 透传，不做转换 | 单用户场景无转换需求，减少出错面 |
| Handler async 桥接 | 暂未实现（v2 TokenBucket 是同步的） | PR-C 改用同步 TokenBucket 避免复杂度，async 桥接留给后续 |
| v1 兼容 | 保留 `_RateLimiter` / `forward_to_baidu()` / `model_affinity` | 渐进迁移，避免一次性删除导致风险 |

---

## 五、待办 / 遗留项

1. **`cloud_provider.yaml` 中 `api_key` 环境变量展开** — 当前 YAML 不支持 `${VAR}` 语法，需启动脚本或 config.py 中做替换
2. **`/v1/models` 端点合并** — 当前未合并本地 + 云端模型列表返回
3. **TTFT 采集** — `chat_handlers.py` SSE 首 token 时间戳采集未实现（需改造 `_forward_request`）
4. **AtomCode 交叉审查** — provider 暂时 401，恢复后补审
5. **性能基准** — 未跑 c=4 并发 benchmark（v4.3 vs v4.6 对比）
6. **v1 兼容清理** — PR 合并后删除 `_RateLimiter` / `forward_to_baidu()` / `model_affinity.yaml`

---

## 六、冻结合同

**v4.6 变更未合入生产**。所有修改仅在 `~/projects/inferfabric-sandbox/` 目录。待 Vincent 审查通过后执行合并。
