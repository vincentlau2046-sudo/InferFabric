# v462-sqlite-persistence AtomCode 审查报告

**审查者**: AtomCode GLM-5.2
**日期**: 2026-08-01
**审查范围**: spec.md + tech-plan.md

---

## 关键发现

### 🔴 P0: 并发安全缺陷 — `_buffer.append` 与 `_buffer.clear()` 竞态

tech-plan §3.2 声称"lock-free append — GIL guarantees atomic list.append"，这是**错误的**。

`list.append` 单次调用是原子的，但 `_do_flush` 中的 `batch = self._buffer[:]` + `self._buffer.clear()` 是**复合操作**，二者之间 `log()` 的 `append` 可能插入，被 `clear()` 丢弃 → **数据丢失**。

**修复**: `log()` 中的 append 也必须在 `_buffer_lock` 下。或者用 `collections.deque` + `rotate` 无锁 swap 模式。

### 🟡 P1: `req_id` 缺唯一约束

flush 失败回插、重试、上游重放都可能产生重复行。崩溃恢复语义无法保证"零重复"。

**修复**: `UNIQUE(req_id)` 索引 + `INSERT OR IGNORE` 容错。

### 🟡 P1: 索引选择问题

1. **缺失复合索引** — `idx_request_log_model_ts (model, timestamp)` 覆盖最常见查询模式
2. **`idx_request_log_route` 冗余** — route 只有 'local'/'cloud' 两个值，B-tree 对低基数列无选择效益，删除

### 🟡 P1: 拆库方案更优

将 `request_log` 拆到独立 DB 文件（`request_log.db`）比同库更合理：
- append-only 时序数据 vs KV state 访问模式完全不同
- 独立文件可单独 backup/vacuum/压缩
- 避免 WAL checkpoint 互相干扰
- 未来迁移到 DuckDB/Parquet 边界更清晰

**代价**: 两个连接、两套锁，单用户下可忽略。

### 🟢 P2: Schema 约束不足

- `status` 缺 `CHECK (status BETWEEN 100 AND 599)`
- `tokens_in/tokens_out` 缺 `CHECK >= 0`
- `timestamp` 入口校验缺 `> 0`

### 🟢 P2: WAL/碎片管理

- 建议显式 `PRAGMA wal_autocheckpoint = 1000`
- 建议 `PRAGMA auto_vacuum = INCREMENTAL` + prune 后 `PRAGMA incremental_vacuum`
- `close()` 时执行 `PRAGMA wal_checkpoint(TRUNCATE)`

### 🔵 事实修正

`usage_log` 表**在 state.py 代码中根本不存在**。它只存在于旧版 state.db 文件中（可能是手动创建的）。spec 中"废弃 usage_log"的叙述需要修正为"检测并忽略遗留 usage_log 表"。

---

## 审查通过条件

1. [ ] 修复 P0 并发竞态（_buffer.append 必须 lock 或换 deque）
2. [ ] 补 req_id 唯一约束
3. [ ] 修正索引策略（加复合索引、删 route 单列索引）
4. [ ] 评估拆库方案并记录排除/采纳理由
5. [ ] 修正 usage_log 事实错误
6. [ ] 补 Schema CHECK 约束
7. [ ] 补 WAL/碎片管理策略
