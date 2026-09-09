# InferFabric 测试套件

## 目录结构

```
tests/
├── conftest.py                              # 公共 markers / fixtures
├── README.md                                # 本文件
│
├── unit/                                    # ── 单元测试（纯 mock，无外部依赖）──
│   ├── proxy/                               #   代理层
│   │   ├── test_auth.py                     #     鉴权：primary/guest key、过期、热加载
│   │   ├── test_sse_buffer.py               #     SSE 流式 usage 提取（OpenAI/Anthropic）
│   │   ├── test_ratelimit.py                #     令牌桶 & 二级限流器（TokenBucket/RateLimiterV2）
│   │   ├── test_ratelimit_gate.py           #     限流门控：observe/reject 模式、RPM=0、并发
│   │   └── test_request_logger.py           #     请求日志：JSONL 写入、按日轮转
│   │
│   ├── engine/                              #   引擎层
│   │   ├── test_engine_adapters.py          #     引擎适配器注册表、路由、validate/sleep
│   │   ├── test_tts_config.py               #     TTS 服务配置解析（TTSConfig、load_models）
│   │   └── test_tts_lifecycle.py            #     TTS 服务生命周期（端口清理、force kill、孤儿 PID）
│   │
│   ├── config/                              #   配置层
│   │   ├── test_cloud_discovery.py          #     云服务发现：配置加载、模型发现、路由决策
│   │   ├── test_config_reloader.py          #     配置热加载：SIGHUP/SIGUSR1、cooldown 防抖
│   │   └── test_config_validation.py        #     iff.yaml 配置校验（rate_limit 各字段）
│   │
│   ├── state/                               #   状态层
│   │   ├── test_gpu_state.py                #     GPU 状态机：模式推导、orphan PID、reconcile
│   │   ├── test_idle_switch.py              #     idle 切换保留 gpu_role:none 服务
│   │   └── test_model_config.py             #     模型配置 & 状态转换（models.d、GPUMode、CLI）
│   │
│   └── infra/                               #   基础设施
│       ├── test_auto_switch.py              #     自动切换默认值、EADDRINUSE 重试、端口守卫
│       ├── test_admin_token.py              #     Admin token 安全：恒定时间比较、fail-fast
│       ├── test_req_id.py                   #     请求 ID 线程安全、无碰撞
│       ├── test_ssrf.py                     #     SSRF 防护：私有 IP、DNS rebinding、协议检查
│       ├── test_security_misc.py            #     安全杂项：req_id/pkill/SSRF/admin/SSE 综合
│       ├── test_health_monitor.py           #     后台健康监控：manual_stop 清理、reconcile
│       ├── test_watchdog.py                 #     模型看门狗：故障计数、自动重启、阈值告警
│       ├── test_token_stats.py              #     Token 统计采集：增量计算、聚合、过期清理
│       └── test_core_comprehensive.py       #     核心模块综合：GPUMode/StateDB/Auth/限流/云发现等
│
├── integration/                             # ── 集成测试（多组件交互，临时文件/DB）──
│   ├── test_request_log_db.py               #   SQLite 请求日志持久化（CRUD/prune/并发/回放）
│   └── test_engine_lifecycle.py             #   引擎生命周期（IFFDB/StateDB/TelemetryHub/适配器）
│
├── operational/                             # ── 操作测试（⚠️ 需真实环境 + 再确认）──
│   └── test_operational.py                  #   模型上下线、GPU 切换、代理路由、并发安全
│
└── report/                                  # ── 测试报告 ──
    ├── README.md
    ├── full-test-report-2026-09-09.md
    └── operational-test-report-template.md
```

---

## 快速运行

```bash
# 单元测试（推荐 CI）
pytest tests/unit/ -v --tb=short

# 集成测试
pytest tests/integration/ -v --tb=short

# 单元 + 集成
pytest tests/unit/ tests/integration/ -v --tb=short

# 操作类测试（⚠️ 需确认环境安全）
IFF_OPERATIONAL_CONFIRM=1 pytest tests/operational/ -v --tb=short
```

---

## 测试文件说明

### unit/proxy/ — 代理层

| 文件 | 测试对象 | 用例数 | 说明 |
|------|----------|--------|------|
| `test_auth.py` | `AuthManager` | 19 | Primary/guest key 校验、Bearer 前缀、过期、热加载 fail-closed |
| `test_sse_buffer.py` | `SSELineBuffer` | 18 | 行缓冲、usage 提取、跨边界、CRLF、Anthropic 格式 |
| `test_ratelimit.py` | `TokenBucket` / `RateLimiterV2` | 12 | burst 限制、令牌补充、server/model 级限流、并发 |
| `test_ratelimit_gate.py` | `DualGateLimiter` | 14 | RPM=0 不限流、observe/reject 模式、Semaphore 并发、stream_options 注入 |
| `test_request_logger.py` | `RequestLogger` | 9 | JSONL 写入、按日轮转、cloud route、error 记录 |

### unit/engine/ — 引擎层

| 文件 | 测试对象 | 用例数 | 说明 |
|------|----------|--------|------|
| `test_engine_adapters.py` | `EngineAdapter` 子类 | 16 | 7 种引擎注册表、get_adapter 路由、validate_config、sleep/wake |
| `test_tts_config.py` | `TTSConfig` / `ModelConfig` | 21 | TTS 配置解析、extra_env 保护、启动命令构建 |
| `test_tts_lifecycle.py` | `ModelLifecycle` / `ProcessManager` | 46 | TTS 端口清理、force_kill、孤儿 PID 检测、deploy 失败回滚 |

### unit/config/ — 配置层

| 文件 | 测试对象 | 用例数 | 说明 |
|------|----------|--------|------|
| `test_cloud_discovery.py` | `CloudDiscovery` | 16 | 配置加载、模型发现（filter/anthropic/不可达）、路由决策 |
| `test_config_reloader.py` | `ConfigReloader` | 7 | SIGHUP/SIGUSR1 注册、cooldown 防抖、异常容错 |
| `test_config_validation.py` | `_validate_runtime_config` | 16 | rate_limit 各字段类型/范围校验、优雅降级 |

### unit/state/ — 状态层

| 文件 | 测试对象 | 用例数 | 说明 |
|------|----------|--------|------|
| `test_gpu_state.py` | `GpuStateMachine` | 9 | 模式推导、端口扫描、orphan PID、reconcile、force_reset |
| `test_idle_switch.py` | `_switch_to_idle` 逻辑 | 5 | gpu:none 服务保留、全 GPU 清空、未知服务跳过 |
| `test_model_config.py` | `load_models` / `StateDB` / `GPUMode` | 24 | models.d 加载、状态转换、CLI 命令、代理路由 |

### unit/infra/ — 基础设施

| 文件 | 测试对象 | 用例数 | 说明 |
|------|----------|--------|------|
| `test_auto_switch.py` | `AUTO_SWITCH` / `_create_server` | 8 | 默认关闭、EADDRINUSE 重试、端口占用守卫 |
| `test_admin_token.py` | `_check_admin` / `_validate_admin_token_safety` | 9 | hmac 恒定时间比较、fail-fast、正确/错误 token |
| `test_req_id.py` | `new_request_id` | 6 | 格式验证、单调递增、100 线程零碰撞 |
| `test_ssrf.py` | `_validate_cloud_test_url` | 16 | 私有 IP/DNS rebinding/协议/白名单/解析失败拦截 |
| `test_security_misc.py` | 多模块综合 | 16 | req_id/pkill/SSRF/admin/config 校验/SSE buffer |
| `test_health_monitor.py` | `HealthMonitor` | 6 | 线程生命周期、manual_stop 清理、reconcile、health_check |
| `test_watchdog.py` | `ModelWatchdog` | 11 | 故障计数、alert/restart 阈值、auto_restart 开关 |
| `test_token_stats.py` | `TokenStatsCollector` | 12 | 增量计算、聚合、过期清理、采集线程 |
| `test_core_comprehensive.py` | 多模块综合 | 198 | GPUMode/StateDB/Auth/限流/云发现/Forwarder/ProxyManager/Dashboard |

### integration/ — 集成测试

| 文件 | 测试对象 | 用例数 | 说明 |
|------|----------|--------|------|
| `test_request_log_db.py` | `RequestLogDB` / `RequestLogger` | 52 | SQLite CRUD、prune、checkpoint、并发写入、MetricsAggregator 回放 |
| `test_engine_lifecycle.py` | `IFFDB` / `StateDB` / `TelemetryHub` / 适配器 | 71 | 数据架构、引擎适配器注册、ModelLifecycle sleep/wake |

### operational/ — 操作测试

| 文件 | 测试对象 | 用例数 | 说明 |
|------|----------|--------|------|
| `test_operational.py` | 运行中的代理 + GPU | 8 | ⚠️ 模型上下线、GPU 切换、API 可用性、并发安全 |

---

## ⚠️ 操作类测试须知

> 操作类测试会操作真实环境，**必须经过再确认后才能运行**：
>
> - 模型上线 / 下线（启动/停止推理进程）
> - GPU 模式切换（idle ↔ exclusive ↔ shared）
> - 向代理发送 switch / stop HTTP 请求
> - 强制终止进程（SIGTERM / SIGKILL）
>
> **运行前确认：**
> 1. 当前无其他重要推理任务在运行
> 2. GPU 显存充足，不会影响其他服务
> 3. 代理服务已启动（`iff serve`）
> 4. 已备份当前 GPU 状态
>
> ```bash
> IFF_OPERATIONAL_CONFIRM=1 pytest tests/operational/ -v --tb=short
> ```

---

## CI/CD 集成

```yaml
- name: Unit Tests
  run: pytest tests/unit/ -v --tb=short --cov=inferfabric

- name: Integration Tests
  run: pytest tests/integration/ -v --tb=short

- name: Operational Tests (manual)
  if: github.event_name == 'workflow_dispatch'
  run: IFF_OPERATIONAL_CONFIRM=1 pytest tests/operational/ -v --tb=short
```
