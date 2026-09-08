# InferFabric 项目审计报告 — 性能 + 功能双视角

**日期**: 2026-09-08
**方式**: 双 agent 并行只读审计（性能 agent + 功能 agent）+ 主会话独立核查（测试基线 / 网关现场 / 仓库卫生）
**代码基线**: v5.6.5（commit 4dc92c0），工作区未提交改动为 qwen38/gemma4 的 `max_model_len` 回退

## 一、总体判断

- **功能面**：核心路由、三态状态机、云端预设、Dashboard 主流程完整可用；主要风险集中在**并发竞态**与**热重载失效**
- **性能面**：热路径存在**双重健康等待（最长阻塞 ~480s）**、**每请求新建连接（无连接池）**、**nvidia-smi 子进程风暴**、**前端多路高频轮询 + 全量重渲染**
- **测试面**：基线 **49 failed / 568 passed**（`pytest tests/ -q --ignore=tests/test_local.py`）；`test_local.py` 导入已删除的 `Profile` 类，在收集阶段 ImportError 中断整个套件

## 二、高严重度发现

| # | 视角 | 发现 | 位置 |
|---|------|------|------|
| H1 | 功能 | **SIGHUP 热重载中云端配置实际未重载**：`self._cloud.reload()` 缺必填参数 `config_path` → `TypeError` 被 except 吞掉，云端 provider 配置从未真正重载 | `config_reloader.py:71`（`cloud_discovery.py:360`） |
| M1 | 功能 | **切换竞态**：`current_mode` 在获取 GPU 锁（`manager.py:358`）之前读取（309 行），分支决策（368-378）用过期值，并发切换时先拿锁的线程可能按"仍是 IDLE"走 `_deploy_model`，与进行中切换在同端口双重部署 | `manager.py:309` |
| F1 | 性能 | **请求线程最长阻塞 ~480s**：auto-switch 路径 = `start_vllm` 内 `wait_http(300s)` + `ensure_service` 内 `_wait_healthy(180s)` 双重等待；503 报文 `retry_after=30` 与 180s 不匹配 | `chat_handlers.py:200-210`、`handler.py:330-354`、`proxy_manager.py:279-356`、`model_lifecycle.py:69-197`、`vllm.py:51-147`、`health.py:88-114` |
| F2 | 性能 | **每请求新建 HTTPConnection，无连接池**：高并发下 TIME_WAIT 累积、fd 线性消耗；`proxy_manager.py:363-369` 注释自证 "no pool" | `forwarder.py:440`、`proxy_manager.py:363-369` |
| T1 | 测试 | **`test_local.py` 导入已删除的 `Profile`/`load_profiles`**（`config.py` v4.2 拆包后移除），收集阶段 ImportError 中断全部测试 | `tests/test_local.py:15`（`config.py:827 行`，无 `Profile`） |

## 三、中严重度

| # | 视角 | 发现 | 位置 |
|---|------|------|------|
| M2 | 功能 | **reconcile 不持 GPU 锁**：HealthMonitor 每 60s 的 reconcile 可能在切换中途清掉 `switching_target` 或覆写 `active_services`（`gpu_state.py:223-277`） | `health_monitor.py:49-55/78-86`、`model_lifecycle.py:408` |
| M3 | 功能 | **GPULock.force_clear TOCTOU**：unlink 锁文件后，等待线程仍持旧 inode 的 flock，新进程 open 新文件 → 双持锁 | `gpu_lock.py:77-86`、调用方 `gpu_state.py:342` |
| M4 | 功能 | **force_reset 写遗留 KV 键**：v003/v005 已迁表，但 force_reset 仍写旧键，reconcile 只查表导致状态不一致 | `gpu_state.py:344-354` |
| F3 | 性能 | **JSONL 日志每请求同步写 + flush**（SQLite 路径已是 batch=50 + 后台 flush 线程，JSONL 未对齐） | `request_logger.py:141-144`（SQLite 好模式在 `request_logger.py:104-125`） |
| F4 | 性能 | **nvidia-smi/fuser 子进程风暴**：`_wait_gpu_idle` 每 3s 轮询一次 nvidia-smi（`handler.py:770-779`）；HealthMonitor 每 60s reconcile 又各调 `gpu_used_mb`/`gpu_total_mb`（`health_monitor.py:49-55`，内部 `health.py:32-49` 每次 fork 两个子进程）；`_wait_gpu_idle` 每秒最多 60 次 nvidia-smi（`base.py:126-156`）；`status()` 每次 2 个 nvidia-smi + N 次健康检查（N+1）（`manager.py:418-459`） |
| F5 | 性能 | **token_stats N+1**：N 个活跃端口触发 N+1 次 `mgr.status()` | `token_stats.py:289-301` |
| F6 | 性能 | **query_request_log 每次先取列名**：`SELECT * LIMIT 0` 一次额外往返 | `db.py:357`；调用方 `handler.py:629`（每 5s）、`handler.py:691` |
| F7 | 性能 | **仪表盘 5 路定时器（3s/5s/5s/10s/60s）+ 全量 innerHTML 重建** | `app.js:298/563/1219-1232`、`monitor.js:23/128-139/147`、`state.js:71-187`（ETag/304 已是好模式） |

## 四、低严重度

- **L1** `state.py:41` 表项 `("exclusive","exclusive"): False` 与 docstring（59 行 "✅ same-port swap"）矛盾，实际放行靠 `manager.py:314-316` 特判
- **L2** `db.py:379` `PRAGMA auto_vacuum` 是只读查询，prune 后并未真正 vacuum（`db.py:109` 连接池已设 INCREMENTAL）
- **L3** `ratelimit.py:300-307` `_release` 收 `rpm_held` 参数却只用 `sem_held`；observe 模式 300s 超时返回 `ok=True`（`ratelimit.py:283-288`）
- **L4** `cloud_discovery.py:554` 已做 `${VAR}` 展开，576 行 `startswith("${")` 判断几乎恒 False（死代码）
- **L5** `facade.py:332-333` `force_kill_all` 硬编码 `[8000,8001,8002]`，漏杀 8003/8004/8006/8008/8010/8011
- **L6** `db.py:338-352` `INSERT OR IGNORE` 对重复 `req_id` 静默丢弃
- **F8** `metrics_aggregator.py:146-215` 每次 `get_metrics` 做 5 次 O(n log n) 排序（deque 100000，`metrics_aggregator.py:27-36`）
- **F9** `chat_handlers.py:282-296` 重试退避在请求线程内 `time.sleep`（0.5/1/2s）；`forwarder.py:437-515`
- **F10** `token_stats.py:257-273` 每次调用同步读全量 JSON 状态文件
- **F11** `health_checker.py:28-35` 1s 粒度轮询至 300s

## 五、现场活证据（2026-09-08 会话实测）

- 本会话分类器模型 qwen38-27b-abliterated 的 auto-switch 反复 503：`{"error":"Auto-switch to qwen38-27b-abliterated failed, retry later","status":"switch_failed","retry_after":30}`
- `/status` 快照：`gpu_mode=exclusive`，active_services=[qwen38-27b-abliterated, bge-m3]；qwen38 服务 ✅ 健康（port 8002, PID 1963256），bge-m3 ✅（port 11441）；GPU 30830/32607 MB（94.55%）
- **503 根因链**：手动停止 TTL=600s、10s 冷却（`proxy_manager.py:48-50`）、三态违规/GPU 占用守卫（`manager.py:312-348`）、VRAM 预算守卫（`model_lifecycle.py:90-108`）、`_wait_healthy` 180s 超时（`proxy_manager.py:279-310`）
- 工作区未提交改动 `max_model_len` 163840→139264 缩减直接降低 KV 预算需求 → 503 属临时故障，与 `retry_after=30s` 后恢复的现象一致

## 六、快速胜利（改动小、收益大）

1. **nvidia-smi 5s TTL 缓存**（`health.py:32-49`）— 一处改动消解 F4 子进程风暴
2. **消除双重健康等待** + `retry_after` 对齐 180s（`proxy_manager.py:279-356`、`handler.py:350-351`、`chat_handlers.py:210`）
3. **token_stats N+1 合并**（`token_stats.py:289-301`）
4. **query_request_log 列名缓存**（`db.py:357`）
5. **仪表盘防抖 + 合并 5s 双定时器**（`app.js:1219-1232`、`monitor.js:23/147`）
6. **JSONL 后台化**（`request_logger.py:141-144`）

## 七、测试缺口

1. 503 auto-switch 失败路径回归（mock 手动停止 TTL / 10s 冷却 / VRAM 预算拒绝 / 180s 超时，断言 503 报文与 retry_after）
2. 阻塞时长断言（最坏 480s → 消除双重等待后下降）
3. SSE usage 提取（`chat_handlers.py:191-194` include_usage 注入 + `sse_buffer.py` 有界缓冲）
4. ETag/304 分支（`handler.py:722-727`）
5. 双门限流 observe 300s / reject 5s 429（`ratelimit.py:213-315`）
6. 看门狗自动重启（`watchdog.py:88-93` 120s stuck 守卫、`watchdog.py:105-115` 连续 5 次失败自动重启）
7. VRAM 预算守卫对新 `max_model_len` 的断言
8. 仪表盘轮询回归（DOM 有界、304 服务端行为、子进程次数）
9. 连接池回归（连接复用）
10. ConfigReloader 信号路径（H1 的 TypeError）无行为测试
11. 并发竞态（M1/M2）无测试
12. `force_clear` TOCTOU 无测试（现有 `test_robustness.py` 仅单线程）
13. `prune_request_log` 的 vacuum 行为无测试（L2）

## 八、测试基线（主会话独立核查）

- `pytest tests/ -q --ignore=tests/test_local.py`：**49 failed / 568 passed**（约 60s 跑完）
- 失败聚类：
  - 陈旧断言：`test_v45_comprehensive.py::test_version_format` 写死 `startswith("5.3")`，实际版本 5.6.5
  - 文件迁移：`test_robustness.py` 仍读拆包前的 `inferfabric/dashboard.py`（FileNotFoundError，10 处）
  - 行为差异：`test_v462_sqlite.py::test_insert_and_query_basic`（插入顺序断言 `iff-b != iff-a` 失败，SQLite 不保序）
  - TTS：`test_tts_server.py` 5 处 AttributeError/AssertionError（API 变了测试没跟上）
  - 进程 kill：`test_gap_phase1_process_kill.py` 5 处失败（fallback 逻辑变更）
- `tests/test_local.py` 在收集阶段 ImportError（导入已删除的 `Profile`、`load_profiles`）→ 中断整个套件

## 九、文档 vs 代码不一致

- 主 README API 表：`GET /reload-config` 实际是 **POST**（`handler.py:103`）；`/deploy`、`/pull` 同样只有 POST
- 主 README 端口表过期：写 `8001 | qwen38-27b-vl`，实际 `qwen38-27b-abliterated.yaml` 用 **8002**；`8002 | qwen3-vl-4b` 实际是 **8003**；漏掉 8006（muse-glimmer）、8008（qwen36-35b-vl）、8010/8011（P/D 分离）、11434（ollama-daemon）
- 主 README 只提 SIGHUP，代码还注册了 **SIGUSR1**（`config_reloader.py:40-41`）
- 主 README 宣称"状态永不漂移"，但 `state.py:194-198` 的 `gpu_mode` setter 仍持久化 KV，`force_reset` 仍写遗留键
- 主 README 版本史只写到 v5.5.1，实际 `__version__ = "5.6.5"`
- `iff` 启动器 docstring 还是 v3 "Profile Switcher" 文案（`iff list` 等旧命令），实际委托 v4+ 的 `inferfabric.cli.main()`
- `models.d/README.md:131` 验证命令写的是 `~/projects/inferfabric-sandbox/inferfabric/cli.py`（旧路径）
- 路由表 `"/admin/cloud/providers"` 出现 3 次（`handler.py` L84/L107/L113，dict 去重后等价，冗余）

## 十、仓库卫生

1. `inferfabric/__init__.py.merge`：git 跟踪的合并冲突残留（仅 `__version__ = "5.5.2"`），实际版本 5.6.5
2. 怪名文件 `"=,  font-size=')\npathlib.Path('iff-compare.svg').write_text(c)\nprint('Fixed SVG')\n"`：shell 引号事故产物，git 跟踪，工作区已删（`D` 状态）
3. `sandox/`：`sandbox/` 的拼写错误残留目录（16K，仅 web-dashboard 子目录）
4. `sandbox/`：整仓旧副本（5.2M），已被 .gitignore 忽略
5. `models.d/*.yaml.bak` × 2：旧模型（qwen36-27b-vl、qwen36-35b-vl）的备份
6. 未提交改动：`models.d/qwen38-27b-abliterated.yaml` 与 `models.d/gemma4-31b-vl.yaml`：`max_model_len` 163840→139264（上下文回退）；`models.d/muse-glimmer-vl.yaml`：仅补末尾换行；`inferfabric/forwarder.py` 的守卫改动已随 commit 40d231c/4dc92c0 提交

## 十一、值得保留的好模式

- `db.py:96-113`：连接池 WAL + `synchronous=NORMAL` + `busy_timeout=5000`
- `state.js:71-187`：ETag/304 + 乱序快照丢弃 + 批量渲染
- `monitor.js`：`visibilitychange` 暂停/恢复轮询
- `request_logger.py:104-125`：SQLite batch=50 + 后台 flush 线程
- `sse_buffer.py`：有界缓冲
- `watchdog.py:88-93` 120s stuck 守卫、`watchdog.py:105-115` 连续 5 次失败自动重启

## 十二、建议修复优先级

1. **先修 H1**（SIGHUP 云端重载失效）+ **T1**（test_local.py 阻塞套件）——低成本高收益
2. **M1/M2** 并发竞态 + **F1** 双重等待（与现场 503 直接相关）
3. 性能侧按"快速胜利"清单逐项落实（nvidia-smi 缓存、连接池、N+1 合并、前端防抖）
4. 测试侧：修复 49 个陈旧断言 + 补齐第七节测试缺口
