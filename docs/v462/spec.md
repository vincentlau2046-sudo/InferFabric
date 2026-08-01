# IFF v4.6.2 SQLite 持久化数据层 — 功能规格 (Phase 2)

> 状态: DRAFT | 版本: 0.1 | 日期: 2026-08-01

## 1. 问题陈述

当前 IFF 存在三条互不连通的数据管道：

| 管道 | 介质 | 写入者 | 查询者 | 问题 |
|------|------|--------|--------|------|
| `state.db` | SQLite | StateDB | Dashboard/API | `usage_log` 表空壳从未写入 |
| `access-*.jsonl` | JSONL 文件 | RequestLogger | 人工 debug | 无结构化查询能力 |
| MetricsAggregator | 内存 deque | AggregatorThread | `/api/metrics` | 重启即丢失 |

后果：
1. **重启后指标丢失** — Dashboard 需等请求重新积累才能显示数据
2. **无历史查询** — 无法回答"昨天 P99 TTFT 是多少"
3. **数据孤岛** — 三个管道互不关联，无法联合查询
4. **usage_log 空壳** — 表存在但 0 行，schema 也无法承载 RequestLog 全量字段

## 2. 目标

以 SQLite 为**唯一持久化数据层**，消除碎片化：

- **G-1**: RequestLog 写入 SQLite（JSONL 可选保留作为 debug 副本）
- **G-2**: MetricsAggregator 启动时从 SQLite 回填指定时间窗口
- **G-3**: 废弃 `usage_log` 表，统一到新的 `request_log` 表
- **G-4**: 所有写入事务安全，崩溃后零数据丢失
- **G-5**: 不破坏现有功能 — 180 pytest 全量通过

## 3. 功能规格

### F-1: `request_log` 表

新增 SQLite 表 `request_log`，承载所有请求结构化数据。

**字段映射**（RequestLog dataclass → SQL 列）：

| RequestLog 字段 | SQL 列名 | 类型 | 约束 | 说明 |
|-----------------|----------|------|------|------|
| — | `id` | INTEGER | PK AUTOINCREMENT | 自增主键 |
| `req_id` | `req_id` | TEXT | NOT NULL | 请求唯一标识 |
| `key_name` | `key_name` | TEXT | NOT NULL DEFAULT '' | API key 名称 |
| `model` | `model` | TEXT | NOT NULL | 模型名 |
| `status` | `status` | INTEGER | NOT NULL | HTTP 状态码 |
| `ttft_ms` | `ttft_ms` | REAL | | 首 token 延迟 (ms)，NULL=流式未完成/不适用 |
| `tokens_in` | `tokens_in` | INTEGER | NOT NULL DEFAULT 0 | 输入 token 数 |
| `tokens_out` | `tokens_out` | INTEGER | NOT NULL DEFAULT 0 | 输出 token 数 |
| `duration_ms` | `duration_ms` | REAL | NOT NULL DEFAULT 0 | 总耗时 (ms) |
| `route` | `route` | TEXT | NOT NULL DEFAULT 'local' | 'local' / 'cloud' |
| `cloud_provider` | `cloud_provider` | TEXT | | 云端 provider，NULL=本地请求 |
| `error` | `error` | TEXT | | 错误信息，NULL=成功 |
| `timestamp` | `timestamp` | REAL | NOT NULL | `time.time()` 格式 |
| `ts` | `ts` | TEXT | NOT NULL DEFAULT '' | ISO 8601 格式 |

**索引**：

| 索引名 | 列 | 类型 | 用途 |
|--------|-----|------|------|
| `idx_request_log_timestamp` | `timestamp` | B-tree | 时间窗口查询（MetricsAggregator 回填 + 历史查询）|
| `idx_request_log_model` | `model` | B-tree | 按模型分组聚合 |
| `idx_request_log_route` | `route` | B-tree | 按路由类型过滤 |

### F-2: RequestLogger 改造

**F-2.1**: `RequestLogger.log()` 在写 JSONL 的同时，将 `RequestLog` 插入 `request_log` 表。

**F-2.2**: 写入方式 — 批量提交（batch commit）：
- 维护内存缓冲区，满 N 条（默认 50）或超时 T 秒（默认 2s）时批量 INSERT + COMMIT
- 每次 `log()` 调用立即进入缓冲区，不阻塞调用方
- 后台 flush 线程负责定时刷盘
- 进程退出时（`close()`）强制 flush 剩余

**F-2.3**: JSONL 写入变为可选 — `iff.yaml` 新增 `access_log_jsonl: true`（默认 true），false 时不写 JSONL 文件但仍然写 SQLite。

**F-2.4**: `RequestLogger.__init__` 新增 `db: StateDB` 参数，接收已初始化的 StateDB 实例。

### F-3: MetricsAggregator 改造

**F-3.1**: 启动时从 `request_log` 表回填最近 W 小时（默认 24h）的数据到内存 deque。

**F-3.2**: 回填逻辑：
1. 查询 `SELECT * FROM request_log WHERE timestamp >= ? ORDER BY timestamp ASC`
2. 将每行转换为 dict，append 到 `_samples` deque
3. 回填在 AggregatorThread 启动前同步完成，确保 `/api/metrics` 从第一个请求起就有数据

**F-3.3**: 回填窗口可配置 — `MetricsAggregator.__init__` 新增 `replay_hours: float = 24.0` 参数。设为 0 禁用回填（纯内存模式，向后兼容测试）。

**F-3.4**: `record()` 方法不变 — 仍然从 queue 接收 RequestLog 写入内存 deque。SQLite 写入由 RequestLogger 负责，Aggregator 只读回填。

### F-4: usage_log 表废弃

**F-4.1**: `_init()` 中不再创建 `usage_log` 表（新 DB）。已有 `usage_log` 表不删除、不迁移（0 行数据无价值）。

**F-4.2**: 在 `_init()` 中检测 `usage_log` 表是否存在，如存在则记录 INFO 日志"legacy usage_log table detected (0 rows), superseded by request_log"。

### F-5: 数据生命周期

**F-5.1**: `request_log` 表自动清理 — 保留最近 90 天数据，更早的行在 flush 时惰性删除。

**F-5.2**: 清理策略 — 每次 flush 时以 1% 概率触发 `DELETE FROM request_log WHERE timestamp < ?`（90 天前）。避免每次 flush 都扫描。

**F-5.3**: 清理阈值可配置 — `iff.yaml` 新增 `request_log_retention_days: 90`。

## 4. 验收标准

### AC-1: RequestLog 写入 SQLite

```
Given IFF 正在运行且 access_log: true
When 发送一个 chat completions 请求并完成
Then state.db 的 request_log 表中存在一行记录，字段与 RequestLog dataclass 一致
And JSONL 文件同步写入（当 access_log_jsonl: true）
```

### AC-2: MetricsAggregator 启动回填

```
Given state.db 中有过去 24h 的 request_log 记录（N 条）
When IFF 重启后 MetricsAggregator 初始化完成
Then /api/metrics?window=24h 返回基于 N 条记录的聚合结果
And total_requests == N
```

### AC-3: 崩溃恢复

```
Given IFF 运行中，缓冲区有 K 条未 flush 的 RequestLog（K < batch_size）
When IFF 进程被 SIGKILL 强杀
Then 重启后 state.db 中 request_log 包含这些记录（因为 WAL 模式 + 合理的 flush 频率）
And 最多丢失最近 T 秒内的数据（T = flush_interval，默认 2s）
```

### AC-4: 性能无退化

```
Given IFF 处理 10 RPS 持续请求
When 单次 log() 调用
Then 耗时 < 1ms（缓冲写入，非同步 INSERT）
And /api/metrics 查询耗时 < 100ms（24h 窗口，~10K 行）
```

### AC-5: 向后兼容

```
Given 现有 180 个 pytest
When 运行完整测试套件
Then 全部通过，0 failure
```

### AC-6: JSONL 可选

```
Given iff.yaml 中 access_log_jsonl: false
When 发送请求
Then state.db request_log 表有记录
And logs/ 目录无新 JSONL 文件
```

### AC-7: 数据自动清理

```
Given state.db 中有 120 天前的 request_log 记录
When flush 触发清理（1% 概率）
Then 90 天前的记录被删除
And 30-90 天内的记录保留
```

## 5. 向后兼容性要求

| 维度 | 要求 | 实现方式 |
|------|------|---------|
| API 兼容 | `/api/metrics` 返回格式不变 | MetricsAggregator.get_metrics() 签名不变 |
| 数据兼容 | 现有 state.db 不破坏 | 新增表，不修改现有 state/history 表 schema |
| 配置兼容 | 新配置项有默认值 | `access_log_jsonl` 默认 true，`request_log_retention_days` 默认 90 |
| 测试兼容 | 现有测试无需修改 | RequestLogger 构造函数新参数可选；MetricsAggregator replay_hours=0 禁用回填 |
| JSONL 兼容 | 现有 JSONL 文件不受影响 | 新增 SQLite 写入路径，JSONL 路径不变 |
| 进程兼容 | 无需手动迁移 | 首次启动自动创建 request_log 表 |

## 6. 非目标 (Out of Scope)

- 不做 RequestLog schema 扩展（如添加 user_id、session_id 等新字段）
- 不做 JSONL → SQLite 的历史数据迁移工具（JSONL 仍可读，无需迁移）
- 不做 Dashboard UI 改造（API 数据格式不变）
- 不做分布式/多实例支持（IFF 是单用户系统）
- 不替换 StateDB 的 state/history 表功能
