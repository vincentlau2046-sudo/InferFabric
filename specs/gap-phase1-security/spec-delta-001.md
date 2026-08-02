# IFF GAP Phase 1 — 安全 & 数据完整性 — 功能规格

> 状态: APPROVED | 日期: 2026-08-02
> 来源: GAP 审计报告 (iff-gap-list-2026-08-02.md)
> 增量编号: delta-001

## 1. 问题陈述

v4.6.5 的 GAP 审计发现 5 项 P0 级安全与数据完整性问题，必须修复后才能进入后续架构重构。

| ID | 问题 | 严重度 | 影响 |
|----|------|--------|------|
| P0-1 | `_req_counter` 竞态 → req_id 碰撞 → 日志静默丢失 | P0 | 数据完整性 |
| P0-2 | `_pkill_vllm_fallback` 杀死所有 vLLM（不区分 IFF vs 其他） | P0 | 破坏性范围不可控 |
| P0-3 | SSRF: `/admin/cloud/test` 任意 URL 出站 | P0 | 安全边界突破 |
| P0-4 | `_check_admin` 用 `==` 比较 token（时序攻击） | P0 | 安全边界突破 |
| P0-6 | `iff.yaml` 无 schema 校验 → 坏值首请求崩 | P0 | 启动时 fail-open |

## 2. 目标

修复上述 5 项 P0，使 IFF 达到"安全基线"标准：
- **D-1**: req_id 生成线程安全 + 碰撞概率降至可忽略
- **D-2**: 进程终止精确到 IFF 管理的进程，不误杀
- **D-3**: cloud test 端点防止 SSRF
- **D-4**: admin token 常数时间比较 + 未设 token 非 localhost 时 fail-fast
- **D-5**: iff.yaml 启动时 schema 校验 + 坏值 fail-fast

## 3. 功能规格

### D-1: 线程安全 req_id

**现状**: `handler.py:109` — `self._req_counter += 1` 非原子 + `uuid4().hex[:4]` 仅 65536 种

**修改**:
1. 替换 `_req_counter` 为 `itertools.count()`（CPython 中 `__next__` 是 C 实现，GIL 下原子）
2. req_id 格式改为 `f"{next(counter):08x}-{uuid4().hex[:8]}"`（32 位 hex + 2^32 种 counter + 2^32 种 random）
3. 保留向后兼容：已有的 `req_id` 字段是文本，格式变更不影响 SQLite schema

**验收标准**:
- Given 100 并发请求, When 同时分配 req_id, Then 无碰撞
- Given request_log.db 有旧格式记录, When 查询, Then 新旧格式共存无报错

### D-2: 进程终止精确化

**现状**: `process_manager.py:275-303` — fallback 用 `pkill -9 -f "vllm serve"` 全局扫

**修改**:
1. 删除 `_pkill_vllm_fallback` 中的 `pkill -f` 全局模式
2. 增强主路径：`_stop_vllm` 使用 PGID kill（已有），增加 PID 文件二次验证
3. fallback 改为：读 PID 文件 → `kill -9 <pid>` → 等 → 读 PGID → `kill -9 -<pgid>`
4. 最终兜底：`fuser <port>/tcp` 精确到端口（仅杀占用该端口的进程）

**验收标准**:
- Given IFF 启动了 vLLM + 用户手动启动了另一个 vLLM, When IFF stop_vllm, Then 只杀 IFF 的 vLLM
- Given vLLM 进程逃逸 PGID, When stop_vllm fallback, Then 通过 PID 文件精确终止

### D-3: SSRF 防护

**现状**: `handler.py:765-787` — 用户提交 URL 直接 `urlopen`

**修改**:
1. 新增 `_validate_cloud_test_url(url)` 方法：
   - 解析 URL，仅允许 `https` scheme
   - 拒绝 RFC 1918 / 链路本地 / 元数据 IP（127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, ::1, fc00::/7）
   - URL host 必须在已注册 provider 的 `openai_base`/`anthropic_base` 白名单中
2. `api_key` 不从请求体取，强制用已注册 provider 的 key
3. 校验失败返回 400 + actionable 错误信息

**验收标准**:
- Given 用户提交 `http://127.0.0.1:6379`, When cloud test, Then 400 "Private IP not allowed"
- Given 用户提交非白名单域名, When cloud test, Then 400 "Host not in allowed list"
- Given 用户提交白名单内 URL, When cloud test, Then 正常执行

### D-4: Admin token 常数时间比较

**现状**: `handler.py:636` — `token == _ADMIN_TOKEN`

**修改**:
1. 替换为 `hmac.compare_digest(token, _ADMIN_TOKEN)`
2. 启动时检查：若 `IFF_ADMIN_TOKEN` 未设且 `PROXY_HOST != "127.0.0.1"` 且 `PROXY_HOST != "localhost"` → fail-fast 拒绝启动 + actionable 错误信息
3. 未设 token 且 localhost 绑定时：日志 WARN（不改当前行为，控制面仍开放）

**验收标准**:
- Given IFF_ADMIN_TOKEN=xxx, When 错误 token 请求, Then 403
- Given IFF_ADMIN_TOKEN 未设且 PROXY_HOST=0.0.0.0, When 启动, Then 拒绝启动 + 明确错误
- Given IFF_ADMIN_TOKEN 未设且 PROXY_HOST=127.0.0.1, When 启动, Then 启动成功 + WARN 日志

### D-5: iff.yaml schema 校验

**现状**: `proxy_manager.py:62-80` — 仅 `isinstance dict` 检查

**修改**:
1. 新增 `_validate_runtime_config(config: dict)` 方法：
   - `rate_limit.mode`: 必须是 `"observe"` 或 `"reject"`
   - `rate_limit.timeout`: 必须 int > 0
   - `rate_limit.server_rpm`: 必须 int ≥ 0
   - `rate_limit.model_rpm_default`: 必须 int ≥ 0
   - `rate_limit.max_concurrent`: 必须是 `"auto"` 或 int > 0
   - `access_log_jsonl`: 必须是 bool
   - `request_log_retention_days`: 必须 int > 0
2. 校验失败 → `ConfigError` → 启动拒绝 + actionable 错误（含字段名、期望类型、实际值）
3. 校验在 `_load_runtime_config` 中调用，在 `DualGateLimiter` 构造之前

**验收标准**:
- Given `timeout: "abc"`, When 启动, Then ConfigError + "rate_limit.timeout: expected int > 0, got str 'abc'"
- Given `mode: "invalid"`, When 启动, Then ConfigError + "rate_limit.mode: must be 'observe' or 'reject'"
- Given 有效配置, When 启动, Then 正常

## 4. 风险评估

| 变更 | 风险爆炸链 | 缓解 |
|------|-----------|------|
| D-1 req_id 格式变更 | 旧日志查询/前端解析依赖 4 hex 格式 | 新格式兼容文本字段，无 schema 依赖 |
| D-2 进程终止重构 | fallback 路径失败 → vLLM 残留 | PID 文件 + fuser 多层兜底 |
| D-3 SSRF 白名单 | 白名单过严 → 正常 cloud test 失败 | 白名单基于已注册 provider，动态生成 |
| D-4 token fail-fast | 用户未设 token + 改了 host → 启动失败 | 明确错误信息 + localhost 默认仍开放 |
| D-5 schema 校验 | 旧配置含非标字段 → 启动失败 | 只校验已知字段，未知字段忽略 |

## 5. 不改什么

- 不改 proxy 转发核心路径
- 不改 StateDB schema
- 不改 cloud_discovery 的后台轮询逻辑（P0-5 属于 Phase 2）
- 不改 watchdog / GPU 切换逻辑（P0-7/P0-8 属于 Phase 2）
