# GAP Phase 1 — 任务分解

> 日期: 2026-08-02 | 增量: delta-001
> 关联: spec-delta-001.md + tech-plan-delta-001.md

## 任务列表

| # | 任务 | D项 | 文件 | 依赖 | 风险 | 委派 |
|---|------|-----|------|------|------|------|
| T-1 | req_id 线程安全 + 碰撞修复 | D-1 | handler.py | 无 | 低 | Claude Code |
| T-2 | 进程终止精确化 | D-2 | process_manager.py | 无 | 中（fallback 路径） | Claude Code |
| T-3 | SSRF 防护 | D-3 | handler.py | 无 | 低 | Claude Code |
| T-4 | Admin token 常数时间 + fail-fast | D-4 | handler.py, proxy_manager.py | 无 | 低 | Claude Code |
| T-5 | iff.yaml schema 校验 | D-5 | proxy_manager.py, config.py | 无 | 低 | Claude Code |
| T-6 | 全量测试 + 回归 | All | tests/ | T-1~T-5 | — | Nova |
| T-7 | AtomCode 交叉 review | All | All | T-6 | — | AtomCode |

## 执行波次

### Wave 1: 独立任务并行（T-1, T-3, T-4, T-5）
四个任务互不依赖，修改不同函数/方法，可并行。

### Wave 2: 进程终止重构（T-2）
D-2 修改 process_manager.py 的 fallback 路径，与 Wave 1 文件不重叠，但逻辑复杂度较高，单独执行便于 review。

### Wave 3: 测试 & 收敛（T-6, T-7）
全量 pytest + AtomCode review。

## 执行策略

- Wave 1 用 Claude Code 并行执行 T-1/T-3/T-4/T-5（四个独立 prompt）
- Wave 2 用 Claude Code 单独执行 T-2（需要更仔细的 fallback 路径设计）
- Wave 3 Nova 执行 pytest + AtomCode review
- 每个 Wave 完成后 git commit，保留回退点
