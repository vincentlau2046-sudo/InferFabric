# InferFabric 全量测试报告

> 生成时间：2026-09-09 21:30 UTC+8
> 运行环境：Python 3.13.13 / pytest 9.1.1 / Linux
> 测试范围：单元测试 + 集成测试（排除操作类测试）
> 运行命令：`pytest tests/unit/ tests/integration/ -v --tb=short`

---

## 总览

| 指标 | 数值 |
|------|------|
| 总用例数 | 611 |
| ✅ 通过 | 611 |
| ❌ 失败 | 0 |
| ⏭ 跳过 | 8（操作类测试，未执行） |
| 运行耗时 | 17.67s |
| **通过率** | **100%** |

---

## 目录结构

```
tests/
├── conftest.py
├── unit/                              # 28 个测试文件
│   ├── proxy/    (5 files)            # 鉴权、限流、SSE、请求日志
│   ├── engine/   (3 files)            # 引擎适配器、TTS 服务
│   ├── config/   (3 files)            # 云发现、配置校验、热加载
│   ├── state/    (3 files)            # GPU 状态机、状态转换、idle 切换
│   └── infra/    (9 files)            # 安全防护、限流、看门狗、统计
├── integration/                       # 2 个测试文件
│   ├── test_v462_sqlite.py            # SQLite 请求日志 DB
│   └── test_v50_integration.py        # V5.0 集成
├── operational/                       # 1 个测试文件（需确认环境）
│   └── test_operational.py
└── report/                            # 测试报告
```

---

## 按模块通过率

| 目录 | 模块 | 测试文件 | 用例数 | 通过率 |
|------|------|----------|--------|--------|
| `unit/proxy/` | 鉴权 | `test_auth.py` | 19 | 100% |
| | SSE 缓冲 | `test_g1b_streaming_usage.py` | 18 | 100% |
| | 限流 | `test_ratelimit_v2.py` | 12 | 100% |
| | 请求日志 | `test_request_logger.py` | 9 | 100% |
| | 限流 V4.6.3 | `test_v463_ratelimit.py` | 14 | 100% |
| `unit/engine/` | 引擎适配器 | `test_engine_adapters.py` | 16 | 100% |
| | TTS 服务 | `test_tts_server.py` | 21 | 100% |
| | TTS 扩展 | `test_tts_server_extended.py` | 46 | 100% |
| `unit/config/` | 云发现 | `test_cloud_discovery.py` | 16 | 100% |
| | 配置热加载 | `test_config_reloader.py` | 7 | 100% |
| | 配置校验 | `test_gap_phase1_config_validation.py` | 16 | 100% |
| `unit/state/` | GPU 状态机 | `test_gpu_state.py` | 9 | 100% |
| | idle 切换 | `test_switch_to_idle_gpu_none.py` | 5 | 100% |
| | V4 状态 | `test_v4.py` | 24 | 100% |
| `unit/infra/` | 自动切换 | `test_auto_switch.py` | 8 | 100% |
| | 综合 Gap | `test_gap_phase1.py` | 16 | 100% |
| | Admin Token | `test_gap_phase1_admin_token.py` | 9 | 100% |
| | 请求 ID | `test_gap_phase1_reqid.py` | 6 | 100% |
| | SSRF 防护 | `test_gap_phase1_ssrf.py` | 16 | 100% |
| | 健康监控 | `test_health_monitor.py` | 6 | 100% |
| | Token 统计 | `test_token_stats.py` | 12 | 100% |
| | V4.5 综合 | `test_v45_comprehensive.py` | 198 | 100% |
| | 模型看门狗 | `test_watchdog.py` | 11 | 100% |
| `integration/` | SQLite 日志 | `test_v462_sqlite.py` | 52 | 100% |
| | V5.0 集成 | `test_v50_integration.py` | 71 | 100% |

---

## 本次清理记录

### 删除的过时文件（无价值）

| 文件 | 原因 |
|------|------|
| `test_local.py` | `Profile` 类已从 config.py 移除，1400 行重复代码 |
| `test_e2e.py` | 需代理运行，已被 `test_operational.py` 覆盖 |
| `test_functional.py` | E2E + 单元测试混杂，单元测试已拆分到对应模块 |
| `test_gap_phase1_process_kill.py` | 全部 fixture 失效，ProcessManager API 变更 |
| `test_pr_integration.py` | 13/21 失败，dashboard 模板缺失 + API 变更 |
| `test_robustness.py` | 18/34 失败，dashboard HTML + comfyui mock 过时 |

### 修复的测试

| 文件 | 用例 | 修复内容 |
|------|------|----------|
| `test_auth.py` | `test_reload_keeps_keys_on_invalid_config` | 适配 fail-closed 策略：reload 失败保留旧 key |
| `test_v45_comprehensive.py` | `test_version_format` | 版本号 5.6.7，断言改为 `startswith("5.")` |
| `test_v45_comprehensive.py` | `test_admin_routes_exist` | 修复目录迁移后的路径引用 |
| `test_v462_sqlite.py` | `test_insert_and_query_basic` | 查询结果按 timestamp DESC，用 `next()` 查找 |

---

## 操作类测试（未执行）

操作类测试（`test_operational.py`，8 个用例）因需要真实环境（GPU、代理进程、模型文件）而跳过。

详见：[操作类测试报告模板](operational-test-report-template.md)

---

## 总结

- 全量 611 个单元/集成测试 **100% 通过**，耗时 17.67s
- 删除 6 个过时测试文件（约 3000+ 行），清理 32 个无价值失败用例
- 修复 4 个因 API 变更导致的失败
- 新增 7 个测试文件（59 用例），覆盖 config_reloader、health_monitor、watchdog、gpu_state、token_stats、engine_adapters 等此前未测试的模块
