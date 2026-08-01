# IFF v4.6.2 SQLite 持久化数据层 — 技术方案 (Phase 4)

> 状态: DRAFT v0.2 | 日期: 2026-08-01
> 审查: AtomCode Round 1 修正 — P0 并发竞态修复 + 拆库 + 索引修正

## 1. 数据库架构

### 1.1 独立数据库 `request_log.db`

**决策：拆库**。`request_log.db` 与 `state.db` 物理隔离。

| 维度 | 同库 (state.db) | 拆库 (request_log.db) |
|------|----------------|----------------------|
| 数据模式 | KV state + 时序日志混合 | 纯时序 append-only |
| WAL 影响 | request_log 高频写入影响 state 表 checkpoint | 物理隔离，互不干扰 |
| 维护 | vacuum/backup 需处理整个 DB | 可独立 vacuum/backup |
| 未来扩展 | 迁移到 DuckDB/Parquet 边界模糊 | 清晰数据边界 |
| 代价 | 无 | 多一个连接，单用户下可忽略 |

路径: `~/.inferfabric/request_log.db`

### 1.2 `request_log` 表 Schema

```sql
CREATE TABLE IF NOT EXISTS request_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id          TEXT    NOT NULL UNIQUE,
    key_name        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL,
    status          INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
    ttft_ms         REAL,
    tokens_in       INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out      INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    duration_ms     REAL    NOT NULL DEFAULT 0.0,
    route           TEXT    NOT NULL DEFAULT 'local',
    cloud_provider  TEXT,
    error           TEXT,
    timestamp       REAL    NOT NULL CHECK (timestamp > 0),
    ts              TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_request_log_model_ts ON request_log (model, timestamp);
```

**设计决策**：

| 决策 | 理由 |
|------|------|
| `req_id UNIQUE` | 防止 flush 失败回插/重试/上游重放导致重复行 |
| `INSERT OR IGNORE` | 配合 UNIQUE，静默跳过重复 |
| `CHECK (status BETWEEN 100 AND 599)` | HTTP 状态码语义约束，防脏数据 |
| `CHECK (tokens_in >= 0, tokens_out >= 0)` | 非负约束 |
| `CHECK (timestamp > 0)` | 防止 `timestamp=0` 脏行 |
| `timestamp` 用 REAL | 与 RequestLog.timestamp (float) 一致，范围查询高效 |
| `ts` 保留 TEXT | 人类可读，无索引 |
| `ttft_ms`/`cloud_provider`/`error` 允许 NULL | NULL = 不适用，语义精确 |
| `idx_request_log_timestamp` | 覆盖回填 + prune + 历史查询 |
| `idx_request_log_model_ts` 复合索引 | 覆盖按模型查时间窗口（最常见模式） |
| **无** `route` 单列索引 | 低基数列（仅 local/cloud），B-tree 无选择效益，反增写入开销 |

## 2. RequestLogDB 类（新增）

独立于 StateDB，管理 `request_log.db` 的连接。

```python
class RequestLogDB:
    """request_log.db 独立数据库管理器。线程安全。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init()

    def _init(self):
        """创建表 + 索引 + PRAGMA 设置。"""
        db_path = self._db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("""CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            req_id TEXT NOT NULL UNIQUE,
            key_name TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL,
            status INTEGER NOT NULL CHECK (status BETWEEN 100 AND 599),
            ttft_ms REAL CHECK (ttft_ms IS NULL OR ttft_ms >= 0),
            tokens_in INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
            tokens_out INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
            duration_ms REAL NOT NULL DEFAULT 0.0 CHECK (duration_ms >= 0),
            route TEXT NOT NULL DEFAULT 'local',
            cloud_provider TEXT,
            error TEXT,
            timestamp REAL NOT NULL CHECK (timestamp > 0),
            ts TEXT NOT NULL DEFAULT ''
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log (timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_log_model_ts ON request_log (model, timestamp)")
        conn.commit()
        self._conn = conn

        # auto_vacuum 检测：对已有 DB 无效，仅首次创建生效
        av = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if av != 2:  # 2 = INCREMENTAL
            log.warning("auto_vacuum=%d (expected INCREMENTAL=2); "
                        "fragment reclamation may not work on existing DB. "
                        "Recreate DB or run VACUUM to apply.", av)

    @property
    def _c(self) -> sqlite3.Connection:
        if self._conn is None:
            self._init()
        return self._conn

    def insert_request_log(self, entries: list[dict]):
        """批量插入，INSERT OR IGNORE 防重复。调用者需持 self._lock。"""
        with self._lock:
            self._c.executemany(
                "INSERT OR IGNORE INTO request_log "
                "(req_id, key_name, model, status, ttft_ms, tokens_in, tokens_out, "
                "duration_ms, route, cloud_provider, error, timestamp, ts) "
                "VALUES (:req_id, :key_name, :model, :status, :ttft_ms, :tokens_in, "
                ":tokens_out, :duration_ms, :route, :cloud_provider, :error, :timestamp, :ts)",
                entries,
            )
            self._c.commit()

    def query_request_log(self, since: float, until: float | None = None,
                          model: str | None = None, limit: int = 100000) -> list[dict]:
        """查询 request_log，返回 dict 列表。用于 MetricsAggregator 回填。"""
        with self._lock:
            sql = "SELECT * FROM request_log WHERE timestamp >= ?"
            params: list = [since]
            if until is not None:
                sql += " AND timestamp < ?"
                params.append(until)
            if model is not None:
                sql += " AND model = ?"
                params.append(model)
            sql += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)
            rows = self._c.execute(sql, params).fetchall()
            cols = [d[0] for d in self._c.execute("SELECT * FROM request_log LIMIT 0").description]
            return [dict(zip(cols, row)) for row in rows]

    def prune_request_log(self, before: float) -> int:
        """删除 timestamp < before 的记录，返回删除行数。执行 incremental_vacuum。"""
        with self._lock:
            cur = self._c.execute("DELETE FROM request_log WHERE timestamp < ?", (before,))
            self._c.commit()
            deleted = cur.rowcount
            if deleted > 0:
                try:
                    self._c.execute("PRAGMA incremental_vacuum")
                except Exception:
                    pass
            return deleted

    def checkpoint(self):
        """WAL checkpoint TRUNCATE — 在 close() 时调用。"""
        with self._lock:
            try:
                self._c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass

    def close(self):
        """关闭连接。先 checkpoint 再 close。"""
        with self._lock:
            self.checkpoint_unlocked()
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def checkpoint_unlocked(self):
        """内部 checkpoint，调用者需持锁。"""
        if self._conn:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
```

**锁竞争分析**：

| 调用者 | 方法 | 频率 | 持锁时间 |
|--------|------|------|---------|
| RequestLogger flush | `insert_request_log` | ~0.2 次/s | < 1ms |
| MetricsAggregator 启动 | `query_request_log` | 1 次/启动 | < 50ms |
| Prune | `prune_request_log` | ~0.002 次/s (1% × flush) | < 50ms |

与 StateDB 完全独立，零交叉锁。

## 3. RequestLogger 改造

### 3.1 缓冲区并发模型（Round 1 修正）

**旧方案（有竞态）**：`list.append` 无锁 + `_do_flush` 持 `_buffer_lock` swap。
**问题**：`[:]` + `clear()` 之间 append 的数据被 clear 丢弃。

**新方案（lock-protected swap）**：

```python
class RequestLogger:
    def __init__(self, log_dir="logs", enabled=True,
                 on_log_queue=None,
                 db: RequestLogDB | None = None,
                 jsonl_enabled: bool = True,
                 batch_size: int = 50,
                 flush_interval: float = 2.0,
                 retention_days: int = 90):
        ...
        self._buffer: list[dict] = []
        self._buf_lock = threading.Lock()  # 保护 buffer 的唯一锁
        self._flush_event = threading.Event()
        self._flush_thread = None
        self._retention_days = retention_days
        self._retention_seconds = retention_days * 86400

        if self._enabled and self._db:
            self._flush_thread = threading.Thread(
                target=self._flush_loop, daemon=True, name="reqlog-flush"
            )
            self._flush_thread.start()

    def log(self, entry: RequestLog):
        if not self._enabled:
            return
        if entry.timestamp == 0:
            entry.timestamp = time.time()
        if not entry.ts:
            entry.ts = datetime.now(tz=timezone.utc).isoformat()

        # 1. JSONL (if enabled, unchanged)
        if self._jsonl_enabled:
            self._write_jsonl(entry)

        # 2. SQLite buffer — 持锁 append
        if self._db:
            d = {
                "req_id": entry.req_id,
                "key_name": entry.key_name,
                "model": entry.model,
                "status": entry.status,
                "ttft_ms": entry.ttft_ms,
                "tokens_in": entry.tokens_in,
                "tokens_out": entry.tokens_out,
                "duration_ms": entry.duration_ms,
                "route": entry.route,
                "cloud_provider": entry.cloud_provider,
                "error": entry.error,
                "timestamp": entry.timestamp,
                "ts": entry.ts,
            }
            with self._buf_lock:
                self._buffer.append(d)
                should_flush = len(self._buffer) >= self._batch_size
            if should_flush:
                self._flush_event.set()

        # 3. Queue (unchanged)
        if self._on_log_queue is not None:
            try:
                self._on_log_queue.put_nowait(entry)
            except Exception:
                log.warning("Failed to enqueue for aggregator", exc_info=True)

    def _flush_loop(self):
        while True:
            self._flush_event.wait(timeout=self._flush_interval)
            self._flush_event.clear()
            self._do_flush()
            if not self._enabled:
                break
            # Lost-wakeup guard: flush 后 buffer 又满了则立即再 flush
            with self._buf_lock:
                if len(self._buffer) >= self._batch_size:
                    self._flush_event.set()

    def _do_flush(self):
        # 持锁 swap out buffer
        with self._buf_lock:
            if not self._buffer:
                return
            batch = self._buffer
            self._buffer = []  # 原子替换为空列表

        # 锁外执行 DB 写入（不阻塞 log()）
        try:
            self._db.insert_request_log(batch)
            log.debug("Flushed %d request_log entries to SQLite", len(batch))
        except Exception as e:
            log.error("Failed to flush request_log: %s", e)
            # 回插失败批次到 buffer 前端
            # 防止 buffer 无限膨胀（磁盘满场景）
            with self._buf_lock:
                if len(self._buffer) + len(batch) > 10000:
                    overflow = len(self._buffer) + len(batch) - 10000
                    log.error("Buffer overflow on flush failure: discarding %d oldest entries",
                             overflow)
                    batch = batch[overflow:]
                self._buffer = batch + self._buffer

        # 惰性清理（1% 概率）
        if random.random() < 0.01:
            self._maybe_prune()

    def _maybe_prune(self):
        try:
            cutoff = time.time() - self._retention_seconds
            deleted = self._db.prune_request_log(cutoff)
            if deleted > 0:
                log.info("Pruned %d request_log entries older than %d days",
                         deleted, self._retention_days)
        except Exception as e:
            log.warning("Prune failed: %s", e)

    def close(self):
        if self._flush_thread:
            self._enabled = False
            self._flush_event.set()
            self._flush_thread.join(timeout=10)
            if self._flush_thread.is_alive():
                log.error("Flush thread did not terminate in 10s, "
                          "in-memory buffer may be lost")
        self._do_flush()  # Final flush — flush thread 已退出，安全
        if self._db:
            try:
                self._db.checkpoint()
                self._db.close()
            except Exception:
                pass
        # JSONL close (unchanged)
        with self._lock:
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
```

**关键修正**：
1. `log()` 中 append **持 `_buf_lock`** — 消除竞态
2. `_do_flush()` 中 swap `self._buffer = []` **在锁内** — 新 append 进入新列表，旧 batch 被 flush 线程持有
3. DB 写入 `insert_request_log` **在锁外** — 不阻塞 `log()`
4. 持 `_buf_lock` 时间极短（append 一条 dict / swap 一个 list 引用）— < 1μs

### 3.2 性能分析

| 操作 | 锁持有时间 | 频率 |
|------|-----------|------|
| `log()` 中 append | < 1μs | 每次 log 调用 |
| `_do_flush()` 中 swap | < 1μs | 每 2s 一次 |
| `insert_request_log` | < 1ms (锁外) | 每 2s 一次 |

**结论**：`log()` 调用几乎无阻塞（持锁 < 1μs），性能影响可忽略。

## 4. MetricsAggregator 改造

### 4.1 启动回填

```python
class MetricsAggregator:
    def __init__(self, price_config=None, db: RequestLogDB | None = None,
                 replay_hours: float = 24.0):
        self._lock = threading.Lock()
        self._samples: collections.deque = collections.deque(maxlen=100000)
        self._price_config = price_config or {}
        self._db = db
        self._replay_hours = replay_hours

        if self._db and self._replay_hours > 0:
            self._replay_from_db()

    def _replay_from_db(self):
        since = time.time() - self._replay_hours * 3600
        try:
            rows = self._db.query_request_log(since=since)
            with self._lock:
                for row in rows:
                    self._samples.append({
                        "model": row["model"],
                        "status": row["status"],
                        "error": row["error"],
                        "ttft_ms": row["ttft_ms"],
                        "duration_ms": row["duration_ms"],
                        "tokens_in": row["tokens_in"],
                        "tokens_out": row["tokens_out"],
                        "route": row["route"],
                        "cloud_provider": row["cloud_provider"],
                        "timestamp": row["timestamp"],
                    })
            log.info("MetricsAggregator replayed %d rows from SQLite (last %.0fh)",
                     len(rows), self._replay_hours)
        except Exception as e:
            log.warning("MetricsAggregator replay failed: %s", e)
```

### 4.2 回填性能

| 场景 | 行数 | 查询时间 | deque 实际容量 |
|------|------|---------|--------------|
| 1 RPS × 24h | ~86K | ~100ms | 86K |
| 10 RPS × 24h | ~864K | ~800ms | 100K (maxlen 截断) |

deque maxlen=100000 自然截断，内存始终 ≤ 17MB。

## 5. ProxyManager 集成

```python
# After (v4.6.2):
self._reqlog_db = RequestLogDB(DEFAULT_REQUEST_LOG_DB)  # 新建独立 DB
self._agg_queue = _queue.Queue()
self.metrics = MetricsAggregator(db=self._reqlog_db, replay_hours=24.0)
self._agg_thread = AggregatorThread(self.metrics, self._agg_queue)
self._agg_thread.start()
self.logger = RequestLogger(
    log_dir=IFF_DATA_DIR / "logs", enabled=True,
    on_log_queue=self._agg_queue,
    db=self._reqlog_db,
    jsonl_enabled=self._config.get("access_log_jsonl", True),
    retention_days=self._config.get("request_log_retention_days", 90),
)
```

**StateDB 完全不变** — 不新增方法，不新增连接。

## 6. 遗留 usage_log 表

`state.py` 代码中**从未创建过 `usage_log` 表**。该表仅存在于旧版 `state.db` 文件中。

处理方式：不删除、不迁移、不查询。如未来需检测，在 StateDB._init() 中记录 INFO 日志。

## 7. 性能评估

| 指标 | 值 | 说明 |
|------|-----|------|
| log() 持锁时间 | < 1μs | dict append |
| SQLite 批量写入 | ~10,000 rows/s | executemany + WAL |
| 实际写入频率 | ~0.2 batch/s | 10 RPS / 50 条每批 |
| /api/metrics | < 5ms | 内存 deque 聚合，不变 |
| 启动回填 24h | < 1s | 受 deque maxlen 截断 |
| 单行磁盘 | ~170 B | 13 列 |
| 1 RPS × 90 天 | ~1.3 GB | 含 auto_vacuum 回收 |

## 8. 风险评估

| # | 风险 | 概率 | 影响 | 缓解 |
|---|------|------|------|------|
| R1 | 进程崩溃丢失未 flush 数据 | 中 | 低（≤2s） | flush_interval=2s，WAL 模式 |
| R2 | req_id 重复 | 极低 | 无 | UNIQUE + INSERT OR IGNORE |
| R3 | 磁盘空间超预期 | 低 | 中 | 惰性清理 + auto_vacuum + 可配置保留天数 |
| R4 | 启动回填阻塞 | 低 | 低 | < 1s，deque maxlen 自然截断 |
| R5 | DB 损坏 | 极低 | 高 | WAL 抗崩溃 + PRAGMA integrity_check |
| R6 | 现有测试失败 | 中 | 中 | 新参数全部可选 + 默认值 |

## 9. 任务分解

```
T1: RequestLogDB 类 — 独立 DB + request_log 表 + PRAGMA
    ├─ __init__, _init, insert_request_log, query_request_log, prune_request_log
    ├─ checkpoint, close
    └─ 依赖: 无

T2: RequestLogger 改造 — SQLite 缓冲写入
    ├─ 新增 db/jsonl_enabled/batch_size/flush_interval/retention_days 参数
    ├─ lock-protected buffer + flush thread
    ├─ close() 强制 flush + DB checkpoint
    └─ 依赖: T1

T3: MetricsAggregator 改造 — 启动回填
    ├─ 新增 db/replay_hours 参数
    ├─ _replay_from_db()
    └─ 依赖: T1

T4: ProxyManager 集成
    ├─ 创建 RequestLogDB 实例
    ├─ 注入 RequestLogger 和 MetricsAggregator
    ├─ 配置项读取
    └─ 依赖: T2, T3

T5: 测试 — RequestLogDB
    ├─ test_create_table (表 + 索引 + PRAGMA 验证)
    ├─ test_insert_query (批量插入 + 查询验证)
    ├─ test_insert_or_ignore (重复 req_id 静默跳过)
    ├─ test_prune (清理 + incremental_vacuum)
    ├─ test_checkpoint (WAL checkpoint)
    └─ 依赖: T1

T6: 测试 — RequestLogger SQLite
    ├─ test_sqlite_write (log → flush → 查询验证)
    ├─ test_concurrent_log_flush (并发 log + flush 无丢数据)
    ├─ test_jsonl_disabled (仅 SQLite，无 JSONL)
    ├─ test_close_flush (close 后缓冲区已刷入)
    ├─ test_insert_or_ignore (flush 失败回插不重复)
    └─ 依赖: T2

T7: 测试 — MetricsAggregator 回填
    ├─ test_replay (预写数据 → 重启 → 指标正确)
    ├─ test_replay_disabled (replay_hours=0 → 无回填)
    └─ 依赖: T3

T8: 测试 — 回归验证
    ├─ 运行全量 180 pytest
    └─ 依赖: T4, T5, T6, T7
```

**依赖图**：

```
T1 ──→ T2 ──→ T4
  │            ↑
  └──→ T3 ──→ T4

T1 ──→ T5
T2 ──→ T6
T3 ──→ T7
T4,T5,T6,T7 ──→ T8
```

**执行顺序**：

```
Phase A: T1 (RequestLogDB)
Phase B: T2, T3 (并行)
Phase C: T4 (集成)
Phase D: T5, T6, T7 (并行)
Phase E: T8 (回归)
```

## 10. 配置项

| 配置项 | 位置 | 类型 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `access_log_jsonl` | iff.yaml | bool | true | 是否同时写 JSONL |
| `request_log_retention_days` | iff.yaml | int | 90 | SQLite 保留天数 |
