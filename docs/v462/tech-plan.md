# IFF v4.6.2 SQLite 持久化数据层 — 技术方案 (Phase 4)

> 状态: DRAFT | 版本: 0.1 | 日期: 2026-08-01

## 1. 新表 Schema 设计

### 1.1 `request_log` 表

```sql
CREATE TABLE IF NOT EXISTS request_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id          TEXT    NOT NULL,
    key_name        TEXT    NOT NULL DEFAULT '',
    model           TEXT    NOT NULL,
    status          INTEGER NOT NULL,
    ttft_ms         REAL,                          -- NULL if not streaming/N/A
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    duration_ms     REAL    NOT NULL DEFAULT 0.0,
    route           TEXT    NOT NULL DEFAULT 'local',
    cloud_provider  TEXT,                          -- NULL for local requests
    error           TEXT,                          -- NULL for success
    timestamp       REAL    NOT NULL,              -- time.time() epoch seconds
    ts              TEXT    NOT NULL DEFAULT ''     -- ISO 8601 human-readable
);

CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_request_log_model ON request_log (model);
CREATE INDEX IF NOT EXISTS idx_request_log_route ON request_log (route);
```

**设计决策**：

| 决策 | 理由 |
|------|------|
| `timestamp` 用 REAL 而非 TEXT | 与 RequestLog.timestamp (float) 一致，范围查询数值比较快于字符串比较 |
| `ts` 保留为 TEXT | 人类可读性 + JSONL 兼容；非查询字段，无索引 |
| `ttft_ms`/`cloud_provider`/`error` 允许 NULL | 语义精确：NULL = 不适用，而非空字符串/0 的歧义 |
| `status` 用 INTEGER | HTTP 状态码，数值比较高效（< 400 判成功）|
| 无复合索引 | 单用户系统，查询模式简单；复合索引增加写入开销但收益微乎其微 |
| 无外键约束 | 单表，无关联关系 |

### 1.2 与现有表的关系

```
state.db
├── state            (不变) — KV 运行状态
├── history          (不变) — GPU 切换历史
├── usage_log        (废弃) — 空壳，不删除不写入
└── request_log      (新增) — 请求日志 + 指标数据源
```

## 2. StateDB 扩展方案

### 2.1 决策：复用现有连接，同库同文件

**理由**：
- `state.db` 已在 WAL 模式，支持并发读写
- 单用户系统无需分库 — 分库增加文件管理复杂度无收益
- `state`/`history` 表写入频率极低（秒级/分钟级），与 `request_log` 无锁竞争
- StateDB 已有线程安全锁（`threading.RLock`），可直接扩展

### 2.2 扩展方式

在 `StateDB._init()` 中追加 `request_log` 表创建和索引创建。新增方法：

```python
class StateDB:
    # ─── Request Log (v4.6.2) ──────────────────────────────

    def insert_request_log(self, entries: list[dict]):
        """批量插入 request_log 记录。调用者需将 RequestLog 转为 dict。

        entries: list of dicts with keys matching request_log columns (minus 'id').
        """
        with self._lock:
            c = self._conn()
            c.executemany(
                "INSERT INTO request_log "
                "(req_id, key_name, model, status, ttft_ms, tokens_in, tokens_out, "
                "duration_ms, route, cloud_provider, error, timestamp, ts) "
                "VALUES (:req_id, :key_name, :model, :status, :ttft_ms, :tokens_in, "
                ":tokens_out, :duration_ms, :route, :cloud_provider, :error, :timestamp, :ts)",
                entries,
            )
            c.commit()

    def query_request_log(self, since: float, until: float | None = None,
                          model: str | None = None, route: str | None = None,
                          limit: int = 100000) -> list[dict]:
        """查询 request_log，返回 dict 列表。用于 MetricsAggregator 回填。"""
        with self._lock:
            c = self._conn()
            sql = "SELECT * FROM request_log WHERE timestamp >= ?"
            params: list = [since]
            if until is not None:
                sql += " AND timestamp < ?"
                params.append(until)
            if model is not None:
                sql += " AND model = ?"
                params.append(model)
            if route is not None:
                sql += " AND route = ?"
                params.append(route)
            sql += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)
            rows = c.execute(sql, params).fetchall()
            cols = [d[0] for d in c.execute("SELECT * FROM request_log LIMIT 0").description]
            return [dict(zip(cols, row)) for row in rows]

    def prune_request_log(self, before: float) -> int:
        """删除 timestamp < before 的记录，返回删除行数。"""
        with self._lock:
            c = self._conn()
            cur = c.execute("DELETE FROM request_log WHERE timestamp < ?", (before,))
            c.commit()
            return cur.rowcount
```

### 2.3 锁竞争分析

StateDB 使用 `threading.RLock`，关键路径分析：

| 调用者 | 方法 | 频率 | 持锁时间 |
|--------|------|------|---------|
| RequestLogger flush | `insert_request_log` | ~0.2 次/s (50条/批, 10 RPS) | < 1ms (executemany) |
| MetricsAggregator 启动 | `query_request_log` | 1 次/启动 | < 50ms (10K 行) |
| 状态管理 | `get`/`set` | < 1 次/s | < 0.1ms |
| 历史记录 | `add_history` | < 0.01 次/s | < 0.1ms |

**结论**：锁竞争可忽略。最坏情况 MetricsAggregator 启动回填持锁 50ms，阻塞一次状态读取 < 50ms，用户无感知。

## 3. RequestLogger 改造方案

### 3.1 架构变更

```
Before:
  RequestLogger.log(entry)
    ├── write JSONL file (同步, 持锁)
    └── put entry to queue (异步)

After:
  RequestLogger.log(entry)
    ├── append to _buffer (内存, 无锁, atomic append)
    ├── put entry to queue (异步, 不变)
    └── [flush thread] periodically flush _buffer → SQLite
                      + optionally write JSONL
```

### 3.2 实现细节

```python
class RequestLogger:
    def __init__(self, log_dir: str | Path = "logs", enabled: bool = True,
                 on_log_queue: queue.Queue | None = None,
                 db: 'StateDB | None' = None,
                 jsonl_enabled: bool = True,
                 batch_size: int = 50,
                 flush_interval: float = 2.0):
        self._log_dir = Path(log_dir)
        self._enabled = enabled
        self._jsonl_enabled = jsonl_enabled
        self._db = db
        self._batch_size = batch_size
        self._flush_interval = flush_interval

        # JSONL state (unchanged)
        self._current_date: str = ""
        self._fd: IO | None = None
        self._lock = threading.Lock()

        # SQLite buffer
        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        self._flush_event = threading.Event()
        self._flush_thread: threading.Thread | None = None

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

        # 1. JSONL (if enabled)
        if self._jsonl_enabled:
            with self._lock:
                try:
                    today = date.today().isoformat()
                    if today != self._current_date:
                        self._rotate(today)
                    if self._fd:
                        line = json.dumps(asdict(entry), ensure_ascii=False)
                        self._fd.write(line + "\n")
                        self._fd.flush()
                except Exception as e:
                    log.warning("Failed to write JSONL: %s", e)

        # 2. SQLite buffer (lock-free append — GIL guarantees atomic list.append)
        if self._db:
            self._buffer.append({
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
            })
            # Fast-path: flush immediately if buffer full
            if len(self._buffer) >= self._batch_size:
                self._flush_event.set()

        # 3. Queue (unchanged)
        if self._on_log_queue is not None:
            try:
                self._on_log_queue.put_nowait(entry)
            except Exception:
                log.warning("Failed to enqueue for aggregator", exc_info=True)

    def _flush_loop(self):
        """后台线程：定期将缓冲区刷入 SQLite。"""
        while not self._flush_event.is_set() or self._buffer:
            self._flush_event.wait(timeout=self._flush_interval)
            self._flush_event.clear()
            self._do_flush()

    def _do_flush(self):
        """将缓冲区中的条目批量写入 SQLite + 执行数据清理。"""
        with self._buffer_lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()

        try:
            self._db.insert_request_log(batch)
            log.debug("Flushed %d request_log entries to SQLite", len(batch))
        except Exception as e:
            log.error("Failed to flush request_log: %s", e)
            # Re-insert failed batch at front (best-effort, accept data loss on persistent failure)
            with self._buffer_lock:
                self._buffer = batch + self._buffer

        # 惰性清理（1% 概率）
        if random.random() < 0.01:
            try:
                cutoff = time.time() - self._retention_seconds
                deleted = self._db.prune_request_log(cutoff)
                if deleted > 0:
                    log.info("Pruned %d request_log entries older than %d days",
                             deleted, self._retention_days)
            except Exception as e:
                log.warning("Prune failed: %s", e)

    def close(self):
        """关闭：强制 flush + 关闭 JSONL fd。"""
        if self._flush_thread:
            self._flush_event.set()
            self._flush_thread.join(timeout=5)
        self._do_flush()  # Final flush
        with self._lock:
            if self._fd:
                try:
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None
                self._current_date = ""
```

### 3.3 关键设计决策

| 决策 | 理由 |
|------|------|
| 缓冲区用 `list.append`（GIL 保护） | CPython list.append 是原子操作，无需显式锁；flush 时用 slice 复制 |
| `_buffer_lock` 仅保护 flush 时的 swap | 大部分 `log()` 调用无锁，仅 flush 线程持锁时短暂阻塞 |
| batch_size=50, flush_interval=2s | 10 RPS 下约 5 秒一批，延迟和吞吐的平衡点 |
| fast-path: buffer 满时立即 flush | 防止高突发时内存堆积 |
| flush 失败时回插 buffer | 最佳努力保留数据，避免静默丢弃 |
| JSONL 写入在 SQLite 缓冲之前 | JSONL 是同步写入，保持原有行为不变 |

## 4. MetricsAggregator 改造方案

### 4.1 启动回填

```python
class MetricsAggregator:
    def __init__(self, price_config: dict[str, CloudModelPrice] | None = None,
                 db: 'StateDB | None' = None,
                 replay_hours: float = 24.0):
        self._lock = threading.Lock()
        self._samples: collections.deque = collections.deque(maxlen=100000)
        self._price_config = price_config or {}
        self._db = db
        self._replay_hours = replay_hours

        if self._db and self._replay_hours > 0:
            self._replay_from_db()

    def _replay_from_db(self):
        """启动时从 SQLite 回填指定时间窗口的请求记录。"""
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

### 4.2 回填性能预估

| 场景 | 行数 | 查询时间 | 内存增量 |
|------|------|---------|---------|
| 轻量使用 (1 RPS × 24h) | ~86K 行 | ~100ms | ~15 MB |
| 中度使用 (10 RPS × 24h) | ~864K 行 | ~800ms | ~150 MB ⚠️ |
| 重度使用 (10 RPS × 7d) | ~6M 行 | ~5s | 超 deque 上限 |

**缓解**：deque maxlen=100000 自然截断。24h 回填在 10 RPS 下 ~864K 行超过 deque 上限，但 deque 会自动丢弃最旧的。实际效果：内存始终 ≤ 100K 条 × ~170B ≈ 17 MB。

**重要**：回填量 > deque maxlen 时，最旧数据被自动丢弃，等同于实际窗口小于 `replay_hours`。这是可接受的 — deque 的语义本就是"最近 N 条"。

### 4.3 AggregatorThread 不变

`AggregatorThread` 仍然从 queue 消费 → `record()` → 写入内存 deque。SQLite 写入由 RequestLogger 负责，Aggregator 不参与。

## 5. JSONL → SQLite 迁移策略

### 5.1 不做自动迁移

理由：
1. JSONL 文件可读性优于 SQLite — 人工 debug 场景仍可直接 `cat`/`jq`
2. 历史 JSONL 数据量小（432 行 × ~284B ≈ 123KB）— 不值得迁移
3. 迁移脚本引入额外复杂度和风险 — 可能破坏现有数据

### 5.2 双写过渡期

v4.6.2 发布后，JSONL 和 SQLite 将并行写入（`jsonl_enabled=True` 默认）。

**未来路径**：
- v4.7: 默认 `jsonl_enabled=False`，JSONL 变为 opt-in
- v5.0: 移除 JSONL 写入路径

### 5.3 手动迁移脚本（可选，不在发布范围内）

如需迁移历史 JSONL 到 SQLite，可提供 `scripts/jsonl_to_sqlite.py`：

```python
"""用法: python scripts/jsonl_to_sqlite.py [--db ~/.inferfabric/state.db] [--dir logs/]"""
```

此脚本为 best-effort 工具，不在 v4.6.2 发布范围内。

## 6. ProxyManager 集成变更

### 6.1 初始化流程变更

```python
# Before (v4.6.1):
self._agg_queue = _queue.Queue()
self.metrics = MetricsAggregator()
self._agg_thread = AggregatorThread(self.metrics, self._agg_queue)
self._agg_thread.start()
self.logger = RequestLogger(log_dir=IFF_DATA_DIR / "logs", enabled=True,
                             on_log_queue=self._agg_queue)

# After (v4.6.2):
self.state_db = StateDB(DEFAULT_STATE_DB)  # 复用已有 StateDB
self._agg_queue = _queue.Queue()
self.metrics = MetricsAggregator(db=self.state_db, replay_hours=24.0)
self._agg_thread = AggregatorThread(self.metrics, self._agg_queue)
self._agg_thread.start()
self.logger = RequestLogger(
    log_dir=IFF_DATA_DIR / "logs", enabled=True,
    on_log_queue=self._agg_queue,
    db=self.state_db,
    jsonl_enabled=self._config.get("access_log_jsonl", True),
    retention_days=self._config.get("request_log_retention_days", 90),
)
```

**注意**：`ProxyManager` 已通过 `Manager` 持有 `StateDB` 实例（`self.mgr.state`）。无需新建。

### 6.2 依赖注入路径

```
ProxyManager.__init__
  ├── mgr.state (StateDB, 已存在)
  ├── metrics = MetricsAggregator(db=mgr.state)
  └── logger = RequestLogger(db=mgr.state)
```

## 7. 性能评估

### 7.1 写入 QPS

| 指标 | 值 | 说明 |
|------|-----|------|
| 缓冲写入 | > 100,000 ops/s | 内存 list.append，GIL 保护 |
| SQLite 批量写入 | ~10,000 rows/s | executemany + WAL，单次 commit 50 行 |
| 实际写入频率 | ~0.2 次/s | 10 RPS / 50 条每批 = 0.2 batch/s |
| JSONL 写入 | ~1,000 行/s | 不变，fd.write + flush |

**结论**：10 RPS 下 SQLite 写入对系统零影响。即使突发 100 RPS，缓冲区可吸收（0.5s 积累 50 条后 flush）。

### 7.2 查询延迟

| 查询 | 行数 | 延迟 | 说明 |
|------|------|------|------|
| `/api/metrics?window=24h` (内存) | N/A | < 5ms | 不变，从 deque 聚合 |
| 启动回填 (24h) | 86K | ~100ms | SELECT + dict 构建 |
| 启动回填 (24h, 10 RPS) | 864K | ~800ms | 受 deque maxlen=100K 截断 |
| `prune_request_log` | 全表扫描 | < 50ms | 90 天数据量 < 50MB |

### 7.3 磁盘占用

| 指标 | 值 | 说明 |
|------|-----|------|
| 单行大小 | ~170 B | 13 列，平均行宽 |
| 1 天 (10 RPS) | ~150 MB | 864K × 170B |
| 90 天 (10 RPS) | ~13 GB ⚠️ | 需要关注 |
| 实际 (1 RPS avg) | ~1.3 GB | 90 天 |

**缓解**：
- 惰性清理保证 90 天自动回收
- 单用户 10 RPS 持续 90 天的极端场景很少见
- 如需更小占用，可调低 `request_log_retention_days`

### 7.4 WAL 文件大小

WAL 模式下，SQLite 会创建 `.db-wal` 和 `.db-shm` 文件。在正常 checkpoint 频率下：
- WAL 文件: ~1-5 MB（取决于写入频率和 checkpoint 间隔）
- SHM 文件: ~32 KB

**Checkpoint 策略**：SQLite WAL 默认在 WAL 文件达到 1000 页时自动 checkpoint。对于 IFF 的写入频率，这足够。

## 8. 风险评估 + 缓解措施

| # | 风险 | 概率 | 影响 | 缓解 |
|---|------|------|------|------|
| R1 | 缓冲区丢失 — 进程崩溃时未 flush 的数据丢失 | 中 | 低（最多 2s 数据） | WAL 模式 + 2s flush 间隔 = 最大丢失窗口 2s；可接受 |
| R2 | 锁竞争 — StateDB.RLock 在高写入时阻塞状态读取 | 低 | 低 | 批量写入持锁 < 1ms，状态读取频率 < 1/s |
| R3 | 磁盘空间 — 90 天数据量超预期 | 低 | 中 | 惰性清理 + 可配置保留天数 + 监控日志 |
| R4 | 回填阻塞启动 — 864K 行回填耗时 800ms | 低 | 低 | < 1s 可接受；若超时可通过 replay_hours 调整 |
| R5 | SQLite 损坏 — 断电/磁盘故障 | 极低 | 高 | WAL 模式抗崩溃；`PRAGMA integrity_check` 可在健康检查中执行 |
| R6 | 现有测试失败 — RequestLogger 构造函数签名变更 | 中 | 中 | 新参数全部可选 + 默认值；测试传入 db=None 时行为与旧版一致 |

## 9. 任务分解

### 9.1 原子任务列表

```
T1: StateDB — 新增 request_log 表 schema + 索引
    ├─ _init() 追加 CREATE TABLE + CREATE INDEX
    ├─ 废弃 usage_log 检测 + 日志
    └─ 依赖: 无

T2: StateDB — 新增 insert_request_log / query_request_log / prune_request_log
    ├─ 批量插入 (executemany)
    ├─ 范围查询 (timestamp + 可选 model/route 过滤)
    └─ 依赖: T1

T3: RequestLogger — 新增 SQLite 缓冲写入
    ├─ 构造函数新增 db/jsonl_enabled/batch_size/flush_interval 参数
    ├─ _buffer + _flush_loop + _do_flush
    ├─ close() 强制 flush
    └─ 依赖: T2

T4: RequestLogger — 惰性数据清理
    ├─ _do_flush 中 1% 概率调用 prune_request_log
    ├─ retention_days 可配置
    └─ 依赖: T2, T3

T5: MetricsAggregator — 启动回填
    ├─ 构造函数新增 db/replay_hours 参数
    ├─ _replay_from_db()
    └─ 依赖: T2

T6: ProxyManager — 集成接线
    ├─ 将 mgr.state 注入 RequestLogger 和 MetricsAggregator
    ├─ 配置项读取 (access_log_jsonl, request_log_retention_days)
    └─ 依赖: T3, T5

T7: 测试 — StateDB 新方法
    ├─ test_insert_request_log (批量插入 + 查询验证)
    ├─ test_query_request_log (时间窗口 + model/route 过滤)
    ├─ test_prune_request_log (清理验证)
    └─ 依赖: T2

T8: 测试 — RequestLogger SQLite 写入
    ├─ test_sqlite_write (log → flush → 查询验证)
    ├─ test_sqlite_batch (验证批量提交行为)
    ├─ test_jsonl_disabled (jsonl_enabled=False → 无 JSONL 有 SQLite)
    ├─ test_close_flush (close 后缓冲区已刷入)
    └─ 依赖: T3

T9: 测试 — MetricsAggregator 回填
    ├─ test_replay (预写数据 → 重启 → 指标正确)
    ├─ test_replay_disabled (replay_hours=0 → 无回填)
    └─ 依赖: T5

T10: 测试 — 回归验证
    ├─ 运行全量 180 pytest
    ├─ 修复任何兼容性问题
    └─ 依赖: T6, T7, T8, T9
```

### 9.2 依赖图

```
T1 ──→ T2 ──→ T3 ──→ T6
             ↗       ↑
        T2 ──→ T5 ──→ T6
             ↘
        T2 ──→ T4 (T3)

T2 ──→ T7
T3 ──→ T8
T5 ──→ T9
T6,T7,T8,T9 ──→ T10
```

### 9.3 执行顺序（关键路径）

```
Phase A (基础设施): T1 → T2
Phase B (核心实现): T3, T4, T5 (并行)
Phase C (集成): T6
Phase D (测试): T7, T8, T9 (并行)
Phase E (验证): T10
```

### 9.4 预估工时

| 任务 | 预估 | 说明 |
|------|------|------|
| T1 | 0.5h | 简单 DDL |
| T2 | 1h | 3 个方法，含参数化查询 |
| T3 | 2h | 缓冲区 + flush 线程 + 错误处理 |
| T4 | 0.5h | 简单逻辑 |
| T5 | 1h | 回填 + 边界处理 |
| T6 | 1h | 接线 + 配置读取 |
| T7 | 1h | 3 组测试 |
| T8 | 1h | 4 组测试 |
| T9 | 0.5h | 2 组测试 |
| T10 | 0.5h | 全量运行 |
| **总计** | **9h** | |

## 10. 配置项汇总

| 配置项 | 位置 | 类型 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `access_log_jsonl` | iff.yaml | bool | true | 是否同时写 JSONL 文件 |
| `request_log_retention_days` | iff.yaml | int | 90 | SQLite 请求日志保留天数 |

## 11. 未来扩展（v4.7+）

1. **默认关闭 JSONL** — `access_log_jsonl` 默认改为 false
2. **request_log 查询 API** — 新增 `/api/requests?since=...&model=...` 端点
3. **VACUUM 优化** — 定期 `VACUUM` 压缩数据库（prune 后碎片整理）
4. **Dashboard 历史图表** — 基于 SQLite 数据绘制历史趋势
5. **Token 用量追踪** — 基于 request_log 的按日/按模型聚合（替代 TokenStatsCollector 的独立 JSONL）
