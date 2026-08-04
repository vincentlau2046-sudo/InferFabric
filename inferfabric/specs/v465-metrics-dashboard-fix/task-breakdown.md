# IFF v4.6.5 Task Breakdown

| Task | Bug | 依赖 | 优先级 | 预计改动 |
|------|-----|------|--------|---------|
| T-1 | M-1 | 无 | P0 | metrics_aggregator.py (1行→7行) |
| T-2 | M-2 | 无 | P0 | metrics_aggregator.py (2行条件) |
| T-3 | D-1 | 无 | P1 | handler.py + monitor.js |
| T-4 | D-2 | 无 | P2 | proxy_manager.py + metrics_aggregator.py |
| T-5 | Version | T1-T4 | P3 | __init__.py (1行) |

## 执行波次

**Wave 1**: T-1 + T-2 (并行, P0)
**Wave 2**: T-3 + T-4 (并行)
**Wave 3**: T-5 + 冒烟测试

## 验收标准映射

| Task | AC |
|------|-----|
| T-1 | AC-1, AC-2, AC-3 |
| T-2 | AC-4, AC-5, AC-6 |
| T-3 | AC-7, AC-8, AC-9, AC-10 |
| T-4 | AC-11, AC-12 |
| T-5 | __version__ == "4.6.5" |

## Review Protocol

每个 Task 完成后:
1. diff 沙箱 vs 生产 → 确认改动范围
2. 功能验证 → curl/API 测试
3. Review 通过 → 标记完成
4. Review 不通过 → 修复后重新 review
