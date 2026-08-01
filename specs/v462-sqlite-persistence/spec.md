# IFF v4.6.2 SQLite 持久化数据层 — 功能规格 (Phase 2)

> 状态: DRAFT v0.2 | 日期: 2026-08-01
> 审查: AtomCode Round 1 → 修正

## 1. 问题陈述

当前 IFF 存在三条互不连通的数据管道：

| 管道 | 介质 | 写入者 | 查询者 | 问题 |
|------|------|--------|--------|------|
| `state.db` | SQLite | StateDB | Dashboard/API | `usage_log` 表存在于旧 DB 文件但代码从未创建/写入 |
| `access-*.jsonl` | JSONL 文件 | RequestLogger | 人工 debug | 无结构化查询能力 |
| MetricsAggregator | 内存 deque | AggregatorThread | `/api/metrics` | 重启即丢失 |

后果：
1. **重启后指标丢失** — Dashboard 需等请求重新积累才能显示数据
2. **无历史查询** — 无法回答"昨天 P99 TTFT 是多少"
3. **数据孤岛** — 三个管道互不关联，无法联合查询

## 2. 目标

以 SQLite 为**唯一持久化数据层**，消除碎片化：

- **G-1**: RequestLog 写入独立 SQLite 数据库（`request_log.db`）
- **G-2**: MetricsAggregator 启动时从 SQLite 回填指定时间窗口
- **G-3**: 统一到 `request_log` 表；旧 `state.db` 中可能存在的遗留 `usage_log` 表检测并忽略
- **G-4**: 所有写入事务安全，崩溃后最大丢失窗口 ≤ 2s
- **G-5**: 不破坏现有功能 — 180 pytest 全量通过

## 3. 功能规格

### F-1: 独立数据库 `request_log.db`

**设计决策：拆库而非同库**。理由：

| 维度 | 同库 (state.db) | 拆库 (request_log.db) |
|------|----------------|----------------------|
| 数据模式 | KV state + 时序日志混合 | 纯时序 append-only |
| WAL 影响 | request_log 高频写入影响 state 表 checkpoint | 物理隔离，互不干扰 |
| 维护 | vacuum/backup 需处理整个 DB | 可独立 vacuum/backup |
| 未来扩展 | 迁移到 DuckDB/Parquet 边界模糊 | 清晰的数据边界 |
| 代价 | 无 | 多一个连接 + 一套锁，单用户下可忽略 |

`request_log.db` 位于 `~/.inferfabric/request_log.db`，独立于 `state.db`。

### F-2: `request_log` 表

```sql
CREATE TABLE IF NOT EXISTS request_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id          TEXT    NOT NULL UNIQUE,           -- 唯一约束，防重复
    key_name        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL,
    status          INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
    ttft_ms         REAL CHECK (ttft_ms IS NULL OR ttft_ms >= 0),
    tokens_in       INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out      INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    duration_ms     REAL    NOT NULL DEFAULT 0.0 CHECK (duration_ms >= 0),
    route           TEXT    NOT NULL DEFAULT 'local',  -- 'local' | 'cloud'
    cloud_provider  TEXT,                              -- NULL = 本地请求
    error           TEXT,                              -- NULL = 成功
    timestamp       REAL    NOT NULL CHECK (timestamp > 0),
    ts              TEXT    NOT NULL DEFAULT ''
);

-- 时间窗口查询（回填 + 历史查询 + prune）
CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log (timestamp);

-- 按模型查时间窗口（最常见查询模式）
CREATE INDEX IF NOT EXISTS idx_request_log_model_ts ON request_log (model, timestamp);
```

**索引决策**：
- `idx_request_log_timestamp`: 覆盖回填查询 `WHERE timestamp >= ? ORDER BY timestamp ASC` 和 prune `DELETE WHERE timestamp < ?`
- `idx_request_log_model_ts`: 覆盖按模型过滤的时间窗口查询（未来 `/api/requests?model=X&since=Y`）
- **不建** `route` 单列索引：route 只有 'local'/'cloud' 两个值，B-tree 对低基数列无选择效益，反增写入开销

### F-3: RequestLogger 改造

**F-3.1**: 新增 `RequestLogDB` 类，管理独立 SQLite 连接。

**F-3.2**: `RequestLogger.log()` 在写 JSONL 的同时，将 entry 追加到内存 deque 缓冲区。后台 flush 线程定期批量 INSERT。

**F-3.3**: 缓冲区并发模型 — **deque + lock-protected swap**：

```
log() 路径（持 _buf_lock）:
    with _buf_lock:
        _buffer.append(entry_dict)

flush() 路径（持 _buf_lock swap out）:
    with _buf_lock:
        batch = _buffer
        _buffer = []      # 原子替换，旧引用被 flush 线程持有
    db.insert_request_log(batch)  # 锁外执行，不阻塞 log()
```

**关键**：`log()` 和 `flush()` 都在 `_buf_lock` 下操作 buffer，消除竞态。swap 后 flush 在锁外执行 DB 写入，不阻塞 `log()`。

**F-3.4**: 插入策略 — `INSERT OR IGNORE`（基于 `req_id` 唯一约束），防止 flush 失败回插或重试导致重复行。

**F-3.5**: JSONL 写入变为可选 — `iff.yaml` 新增 `access_log_jsonl: true`（默认 true）。

**F-3.6**: batch_size=50, flush_interval=2s。buffer 满 50 条时立即触发 flush event。

### F-4: MetricsAggregator 改造

**F-4.1**: 启动时从 `request_log` 表回填最近 W 小时（默认 24h）的数据到内存 deque。

**F-4.2**: 回填逻辑：
1. `SELECT * FROM request_log WHERE timestamp >= ? ORDER BY timestamp ASC`
2. 逐行转 dict，append 到 `_samples` deque
3. deque maxlen=100000 自然截断旧数据

**F-4.3**: 回填窗口可配置 — `replay_hours: float = 24.0`。设为 0 禁用回填（向后兼容测试）。

**F-4.4**: `record()` 方法不变 — 从 queue 接收 RequestLog 写入内存 deque。SQLite 写入由 RequestLogger 负责。

### F-5: 遗留 usage_log 表处理

**F-5.1**: `state.py` 代码中**从未创建过 `usage_log` 表**。该表仅存在于旧版 `state.db` 文件中（可能由早期手动操作创建）。

**F-5.2**: 新代码不创建、不写入、不查询 `usage_log` 表。如检测到该表存在，记录 INFO 日志。

### F-6: 数据生命周期

**F-6.1**: 自动清理保留最近 N 天数据（默认 90 天），更早的行在 flush 时惰性删除。

**F-6.2**: 清理策略 — 每次 flush 时以 1% 概率触发 `DELETE FROM request_log WHERE timestamp < ?`。

**F-6.3**: 清理阈值可配置 — `iff.yaml` 新增 `request_log_retention_days: 90`。

### F-7: WAL 与碎片管理

**F-7.1**: `request_log.db` 使用 WAL 模式 + `PRAGMA synchronous=NORMAL` + `PRAGMA auto_vacuum = INCREMENTAL`。

**F-7.2**: `_init()` 中检测 `PRAGMA auto_vacuum` 返回值，非 INCREMENTAL 时 log.warning（已有 DB 无法更改此设置）。

**F-7.3**: prune 后执行 `PRAGMA incremental_vacuum` 回收空间。

**F-7.4**: 显式设置 `PRAGMA wal_autocheckpoint = 1000`。

**F-7.5**: `close()` 时执行 `PRAGMA wal_checkpoint(TRUNCATE)` 确保 WAL 文件收缩。

**F-7.6**: 单实例假设 — 多进程同时写入 `request_log.db` 不受支持，会导致 SQLITE_BUSY 错误。

## 4. 验收标准

### AC-1: RequestLog 写入 SQLite
```
Given IFF 正在运行且 access_log: true
When 发送一个 chat completions 请求并完成
Then request_log.db 的 request_log 表中存在一行记录
And req_id 唯一，字段与 RequestLog dataclass 一致
```

### AC-2: MetricsAggregator 启动回填
```
Given request_log.db 中有过去 24h 的 request_log 记录（N 条）
When IFF 重启后 MetricsAggregator 初始化完成
Then /api/metrics?window=24h 返回基于 N 条记录的聚合结果
```

### AC-3: 崩溃恢复
```
Given IFF 运行中，缓冲区有 K 条未 flush 的 RequestLog
When IFF 进程被 SIGKILL 强杀
Then 重启后 request_log.db 包含已 flush 的记录
And 最多丢失最近 2s 内的 buffer 数据（flush interval）
And 无重复行（INSERT OR IGNORE + req_id UNIQUE）
```

### AC-4: 性能无退化
```
Given IFF 处理 10 RPS 持续请求
When 单次 log() 调用
Then 耗时 < 1ms
And /api/metrics 查询耗时 < 100ms
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
Then request_log.db 有记录，logs/ 目录无新 JSONL 文件
```

### AC-7: 数据自动清理
```
Given request_log.db 中有 120 天前的记录
When flush 触发清理（1% 概率）
Then 90 天前的记录被删除，90 天内保留
```

## 5. 向后兼容性要求

| 维度 | 要求 | 实现方式 |
|------|------|---------|
| API 兼容 | `/api/metrics` 返回格式不变 | MetricsAggregator.get_metrics() 签名不变 |
| 数据兼容 | 现有 state.db 不破坏 | 新增独立 request_log.db，不修改 state.db |
| 配置兼容 | 新配置项有默认值 | 所有新配置项有合理默认值 |
| 测试兼容 | 现有测试无需修改 | 新参数全部可选 + 默认值；db=None 时行为与旧版一致 |
| JSONL 兼容 | 现有 JSONL 文件不受影响 | JSONL 写入路径不变，默认仍开启 |
| 进程兼容 | 无需手动迁移 | 首次启动自动创建 request_log.db + request_log 表 |

## 6. 非目标 (Out of Scope)

- 不做 RequestLog schema 扩展（如 user_id、session_id）
- 不做 JSONL → SQLite 历史数据迁移工具
- 不做 Dashboard UI 改造
- 不做分布式/多实例支持
- 不替换 StateDB 的 state/history 表功能
