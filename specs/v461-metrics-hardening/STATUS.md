# STATUS: v4.6.1 Metrics & Hardening

| Phase | 状态 | 完成时间 | 备注 |
|-------|------|---------|------|
| P0 Constitution | ✅ | 2026-07-31 | 继承 v4.6 + 新增：二级限流门、原子写入、queue 解耦 |
| P1 Context | ✅ | 2026-07-31 | 双 Agent review (AtomCode GLM-5.2 + Codex GLM-5.1) |
| P2 Specify | ✅ | 2026-07-31 | spec-v461.md |
| P3 Clarify | ✅ | 2026-07-31 | Vincent 确认：cloud 不限流、生产可靠性优先 |
| P4 Design | ✅ | 2026-07-31 | design-v461.md (review 修订版) |
| P5 Analyze | ⬜ | — | — |
| P6 Tasks | ⬜ | — | — |
| P7 Implement | ⬜ | — | Phase 1: G-1a + PR-E |
| P8 Converge | ⬜ | — | — |
| P9 Release | ⬜ | — | — |

## PR 清单

| PR | 内容 | 优先级 | Phase | 状态 |
|----|------|--------|-------|------|
| G-1a | RequestLog 数据补全 (non-streaming) | P0 | 1 | ⬜ |
| PR-E | RateLimiterV2 集成 (二级嵌套门) | P0 | 1 | ⬜ |
| G-1b | RequestLog 数据补全 (streaming) | P1 | 推迟 | 📦 goal |
| PR-E2 | 连接池 keep-alive | P2 | 推迟 | ⬜ |
| G-2 | MetricsAggregator (queue 解耦+费用持久化) | P0 | 2 | ⬜ |
| PR-F | Provider 持久化 (原子写入) | 中 | 2 | ⬜ |
| G-3a | Dashboard 拆分 (字节级等价) | P1 | 3 | ⬜ |
| G-3b | Monitor 7 面板重做 | P1 | 3 | ⬜ |
| G-4 | Prometheus /metrics 导出 | P2 | 推迟 | 📦 goal |

## 关键修订（vs 初版计划）

1. **PR-E**: Semaphore 并发限流 + TokenBucket RPM 双门嵌套，非替换
2. **G-1**: 拆为 G-1a (non-streaming) + G-1b (streaming, 推迟)
3. **G-2**: queue.Queue 解耦 aggregator，避免嵌套锁死锁；费用从 JSONL 回放
4. **PR-F**: os.replace() 原子写入，替代 .bak + 校验
5. **G-3**: 拆为 G-3a (拆分等价) + G-3b (monitor 重做)；启动时缓存 fragments
6. **PR-E2**: 从 PR-E 拆出，forwarder 异步化独立评估

## 测试覆盖

基线: v4.6.0 — 216 tests passing

| 新增测试 | 覆盖 PR | 状态 |
|----------|---------|------|
| test_g1a_log_completion.py | G-1a | ⬜ |
| test_pr_e_dual_gate.py | PR-E | ⬜ |
| test_g2_aggregator.py | G-2 | ⬜ |
| test_pr_f_atomic_write.py | PR-F | ⬜ |
