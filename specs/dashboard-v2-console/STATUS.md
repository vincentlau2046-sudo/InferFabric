# Dashboard v2 Console 重设计 状态

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| 设计 | ✅ | 2026-09-19 | 用户确认：专业监控台方向 / 方案 A 零构建（vanilla + ECharts vendor）/ 双主题（dark 主力 + light）/ 六 TAB 全重做 / LRU 等网关控制归推理 TAB、监控 TAB 纯遥测 |
| spec | ✅ | 2026-09-19 | spec.md |
| P1 实施 | ⬜ | — | 设计令牌 + Shell + GPU 遥测带 + 总览（先静态原型过目） |
| P2 实施 | ⬜ | — | ECharts + 监控 TAB（双调色板 CVD 验证） |
| P3 实施 | ⬜ | — | 推理 TAB + 网关控制卡 |
| P4 实施 | ⬜ | — | 部署 / 云端 / 异常 |
| P5 收口 | ⬜ | — | empty state / light 走查 / 全量 pytest + 冒烟 |

## 测试基线

- 变更前：实施时记录（`python3 -m pytest tests/unit/ tests/integration/`）
