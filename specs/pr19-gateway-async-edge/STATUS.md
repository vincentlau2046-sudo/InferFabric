# PR-19 Gateway Async Edge Status

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P0 Intake | ✅ | 2026-09-16 | 用户拍板方案 C（拆 PR-19/PR-20） |
| P1 Explore | ✅ | 2026-09-16 | proxy I/O / forwarder / 治理约束三路探索 |
| P2 Specify | ✅ | 2026-09-16 | spec.md |
| P4 Design | ✅ | 2026-09-16 | 混合执行模型 + _WfileSink 解帧泵送 |
| P7 Implement | ✅ | 2026-09-16 | async_server.py 重写 + 14 新测试 + 死路由删除 + 版本 5.8.0 |
| P8 Converge | ✅ | 2026-09-16 | 745 pytest 全绿；启动冒烟 + 流式 e2e + chunked 请求体 + SIGTERM 关停 |
| P9 Release | ✅ | 2026-09-16 | cp313 wheel 重建 + diff 审查 + 合入生产；生产 745 pytest + --async 启动冒烟（隔离 18999）+ SIGTERM 优雅关停；commit 5.8.0 |

## 测试基线

- 变更前：731 passed（unit + integration，2026-09-16）
- 变更后：745 passed（+14 新增 test_async_server.py）

## cp313 wheel 重建性能记录

100 事件 × 1KB SSE 服务端处理（aiohttp 本地回环）：
- 纯 Python 模式（无 C 扩展）：total 0.41ms
- cp313 C 模式：total 0.15ms（**约 2.7× 提升**）

复现：`scripts/rebuild-deps.sh`（含 C 扩展加载验证；回滚 = 删 .so 自动回退纯 Python）

## 后续（PR-20，另发）

- 转发核心 async 化（共享 ClientSession）+ 删线程入口 + 修订宪法 PR-14 条款
