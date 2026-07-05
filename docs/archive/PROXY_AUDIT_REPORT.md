# InferFabric Proxy v4.0 — 交叉审计报告

**日期**: 2026-06-28
**审计方**: Nova (glm-5.1) + DeepSeek-V4-Pro
**目标**: `inferfabric/proxy.py` + 关联 `manager.py`, `state.py`, `config.py`

---

## 交叉验证矩阵

| # | 发现 | Nova | DeepSeek | 共识 | 最终严重度 |
|---|------|------|----------|------|-----------|
| 1 | `_upstream_pool` 无锁并发访问 | Critical | Critical | ✅ 双方一致 | **Critical** |
| 2 | GPULock 重入漏洞 | — | Critical | ⚠️ 需验证 | **Critical** (待验证) |
| 3 | StateDB.get() 无锁 | — | Critical | ⚠️ 需验证 | **High** (WAL 模式下可接受但需改进) |
| 4 | ensure_service 非阻塞锁丢弃请求 | High | High | ✅ 双方一致 | **High** |
| 5 | auto-switch 后不等待模型就绪 | High | — | ✅ Nova 独立发现 | **High** |
| 6 | 流式 chunked 编码问题 | High | High | ✅ 双方一致但角度不同 | **High** |
| 7 | 流式异常后发 JSON 错误体 | — | High | ✅ DeepSeek 独立发现 | **High** |
| 8 | load_models() 空 YAML 崩溃 | — | High | ✅ DeepSeek 独立发现 | **High** |
| 9 | _read_body() 无大小限制 | Medium | Medium | ✅ 双方一致 | **Medium** |
| 10 | manual_stop TTL 清理惰性 | Medium | — | ✅ Nova 独立发现 | **Medium** |
| 11 | upstream 连接池无过期 | Medium | Medium | ✅ 双方一致 | **Medium** |
| 12 | graceful shutdown 不等请求完成 | Medium | High | ⚠️ 分歧 | **Medium** (daemon_threads 可接受) |
| 13 | Content-Length=0 静默接受 | — | Medium | ✅ DeepSeek 独立发现 | **Low** |

---

## Top 5 必须修复（按优先级）

### 1. [Critical] _upstream_pool 无锁并发访问
**双方一致认定**

- `get_upstream()` 读写 `_upstream_pool` 无同步
- 多线程可能同时创建同端口连接 → 旧连接泄漏
- 两个线程拿到不同连接交替发送/读取 → 请求-响应错配

**修复**: 加 `threading.Lock`，或换 `urllib3.PoolManager`

### 2. [Critical] GPULock 重入漏洞
**DeepSeek 发现，Nova 遗漏**

- `switch()` acquire → `_switch_to_idle()` 内部再 acquire → finally release 清掉锁
- 外层 switch 继续执行时锁已释放，另一个 switch 可并发进入

**修复**: GPULock 改为可重入（计数器），或内层方法不单独管锁

### 3. [High] auto-switch 后不等待模型就绪
**Nova 发现，核心功能缺陷**

- `ensure_service()` 调 `switch()` 后立即返回 True
- `_handle_chat` 立即转发请求到可能还在加载的 vLLM
- 首次请求几乎必然失败

**修复**: `ensure_service()` 内 polling `/health` 直到 200（超时 180s）

### 4. [High] 流式异常后发 JSON 错误体（协议违规）
**DeepSeek 发现**

- 流式模式已发 response headers，异常后 `_send_json()` 再发 headers → 协议违规
- 客户端收到 corrupt 响应

**修复**: 流式异常只发 chunked 结束标记 + 关闭连接，不发新 JSON

### 5. [High] ensure_service 非阻塞锁丢弃请求
**双方一致**

- `_switch_lock.acquire(blocking=False)` → 锁被持有时直接丢弃
- cooldown 10s 期间所有新模型请求 503

**修复**: 改为 `acquire(timeout=30)`，或返回 503 + `Retry-After` header

---

## 次要修复（Medium/Low）

| # | 问题 | 修复 |
|---|------|------|
| 6 | StateDB.get() 无锁 | get() 内加 self._lock（读锁） |
| 7 | load_models() 空 YAML 崩溃 | safe_load 返回 None 时 continue + log.warning |
| 8 | _read_body() 无大小限制 | 加 10MB 上限，超限返回 413 |
| 9 | manual_stop TTL 惰性清理 | health_check 中周期清理 |
| 10 | upstream 连接无过期 | 加 last_used 时间戳，5min 过期 |
| 11 | Content-Length=0 静默接受 | 返回 400 Bad Request |

---

## 流式转发专项评估

Nova 和 DeepSeek 都指出了流式转发的问题，但角度不同：

- **Nova**: chunked 编码双层包装问题（SSE + chunked）
- **DeepSeek**: 异常后协议违规 + SSE 缓冲延迟

**实际影响评估**: OpenClaw 通过千帆 API 调用本地模型时，走的是非流式路径（`stream=False`）。流式路径仅在 OpenAI SDK 直接调用时触发。当前 OpenClaw 配置中本地模型在 fallback 末尾，流式场景极少。

**建议**: 短期修异常处理（不发 JSON），长期考虑换 aiohttp。

---

## 安全评估

- ✅ 绑定 127.0.0.1（loopback-only），已从 0.0.0.0 修正
- ✅ 无认证需求（loopback 场景）
- ⚠️ Content-Length 无上限 → OOM 风险（Medium）
- ✅ CORS `*` 在 loopback 场景可接受

---

## 运维评估

- ✅ systemd service 已配置，enabled，开机自启
- ✅ Watchdog 已配置（60s）
- ✅ 日志输出到 journal
- ⚠️ graceful shutdown 不等请求完成（Medium，daemon_threads 可接受）
- ✅ health_check 周期运行

---

## 总结

**整体评估**: Proxy 核心功能（路由、自动切换、manual stop 保护）可用，但并发安全和流式转发存在显著缺陷。单用户本地场景下（OpenClaw 单连接）这些问题不易触发，但作为 OpenClaw 的统一模型入口，健壮性必须提升。

**Top 3 必修**:
1. `_upstream_pool` 加锁（Critical，数据错乱风险）
2. auto-switch 后等待模型就绪（High，核心功能缺陷）
3. 流式异常处理修正（High，协议违规）
