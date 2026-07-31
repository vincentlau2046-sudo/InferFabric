# Task Breakdown: v4.6 API Gateway

**Version**: delta-001
**Date**: 2026-07-30

## Wave 1: 基础设施（可并行）

### Task 1: PR-A 鉴权
- **Files**: 新增 `inferfabric/proxy/auth.py`, 修改 `inferfabric/proxy/handler.py`, `inferfabric/proxy_manager.py`
- **Steps**:
  1. 创建 `AuthManager` 类 + `_KeyEntry` dataclass
  2. 实现 `api_keys.yaml` 加载 + 校验逻辑
  3. handler.py `do_POST` 注入鉴权检查点
  4. ProxyManager 持有 AuthManager 实例
- **Test**: `tests/test_auth.py` — key 校验/过期/模型白名单/空配置
- **Verify**: `python3 -c "from inferfabric.proxy.auth import AuthManager; print('ok')"`

### Task 2: PR-B 日志
- **Files**: 新增 `inferfabric/proxy/request_logger.py`, 修改 `inferfabric/proxy/handler.py`, `inferfabric/proxy/chat_handlers.py`
- **Steps**:
  1. 创建 `RequestLog` dataclass + `RequestLogger` 类
  2. JSONL 按日轮转 + 实时 flush
  3. handler.py 注入 req_id 生成 + 日志记录
  4. chat_handlers.py SSE 首 token 时间戳采集
- **Test**: `tests/test_request_logger.py` — JSONL 写入/轮转/disabled
- **Verify**: `python3 -c "from inferfabric.proxy.request_logger import RequestLogger; print('ok')"`

### Task 3: PR-C 限流器重构
- **Files**: 重写 `inferfabric/ratelimit.py`, 修改 `inferfabric/proxy/handler.py`, `inferfabric/forwarder.py`
- **Steps**:
  1. 创建 `AsyncTokenBucket` + `RateLimiterV2`
  2. handler.py asyncio 桥接（`run_coroutine_threadsafe`）
  3. forwarder.py aiohttp TCPConnector keepalive
  4. 启动时预创建所有 model bucket
  5. 删除旧 `_MODEL_RATE_LIMITERS` / `clear()` / `_VLLM_RATE_LIMITER`
- **Test**: `tests/test_ratelimit_v2.py` — 令牌桶 refill/acquire/release/并发
- **Verify**: `python3 -c "from inferfabric.ratelimit import RateLimiterV2; print('ok')"`

## Wave 2: 云端路由（依赖 Wave 1 的 A+B）

### Task 4: PR-D 云端 Provider 配置 + 发现
- **Files**: 新增 `cloud_provider.yaml`, 新增 `inferfabric/cloud_discovery.py`, 修改 `inferfabric/proxy_manager.py`, `inferfabric/config.py`
- **Steps**:
  1. 定义 `CloudProviderConfig` + `CloudModel` 数据结构
  2. 实现 `cloud_provider.yaml` 加载
  3. 实现 `CloudDiscovery.discover_all()` — GET /models + filter
  4. ProxyManager 启动时调用发现，合入 `_cloud_models`
  5. 可选定时轮询（interval>0 时）
- **Test**: `tests/test_cloud_discovery.py` — 发现/filter/双协议标记
- **Verify**: `python3 -c "from inferfabric.cloud_discovery import CloudDiscovery; print('ok')"`

### Task 5: PR-D 统一路由 + 双协议转发
- **Files**: 修改 `inferfabric/proxy/handler.py`, `inferfabric/forwarder.py`, 删除 `model_affinity.yaml`
- **Steps**:
  1. handler.py 路由决策替换 Step 2-3
  2. forwarder.py 新增 `forward_to_cloud()` 双协议透传
  3. IFF 持有凭证，客户端只需 IFF key
  4. /v1/models 合并返回本地+云端模型
  5. 删除 `model_affinity.yaml` 和硬编码路由
  6. /v1/chat/completions 云端回退路径
- **Test**: `tests/test_cloud_routing.py` — 路由决策/双协议/凭证持有/fallback
- **Verify**: `python3 -m pytest tests/test_cloud_routing.py -q`

## Wave 3: 集成验证

### Task 6: 全量测试
- **Scope**: 单元 + 集成 + 性能
- **Steps**:
  1. 修复已知 12 个过期测试
  2. 新增集成测试（mock HTTP server 模拟 vLLM + 云端）
  3. 性能 benchmark（c=4 并发，对比 v4.3 vs v4.6）
  4. 冒烟检查清单（SCPM Phase 7→8 Gate）
- **Verify**: 全量 pytest 通过 + benchmark 报告

## 依赖图

```
Wave 1 (并行):
  Task 1 (PR-A) ──┐
  Task 2 (PR-B) ──┤
  Task 3 (PR-C) ──┘
                    │
Wave 2 (串行):      ▼
  Task 4 (PR-D 配置+发现) → Task 5 (PR-D 路由+转发)
                                        │
Wave 3:                                ▼
  Task 6 (全量测试)
```
