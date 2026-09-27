# IFF 自动调度裁判（auto-routing judge）Status

> 重大功能升级 · 从建议中心到 Laya 决策裁判

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P0 Intake | ✅ | 2026-09-26 | 用户提出「按性能统计 log 做启动重路由裁判」，Laya 事实核实（Apache 2.0 / 421M / typed-decisions） |
| P1 Explore | ✅ | 2026-09-26 | 三视角可行性分析（用户/技术/架构）获认可：观测层已齐，裁判=旁路建议者，执行走 tune.apply |
| P2 Specify | 🔶 | 2026-09-26 | spec.md DRAFT 待用户审阅 |
| P3 Design | ⬜ | | 模块图 / 接口签名 / rules.yaml schema 细化 |
| P4 Review | ⬜ | | 用户审阅 spec，拍板里程碑范围 |
| P5 Implement | ⬜ | | M1 骨架 → M2 RuleJudge+Gate → M3 执行接线 |
| P6 Converge | ⬜ | | M4 数据积累（≥2 周） |
| P7 Laya | ⬜ | | M5 独立里程碑：LayaJudge 适配器 + held-out 对拍 |
| P8 Release | ⬜ | | 全量 pytest + 冒烟 + 合入生产 |

## 关键决策（已定）

1. **建议模式默认、auto 显式开启**——不侵略性自动机
2. **裁判永不写状态**：只产 Recommendation，执行唯一入口 `tune.apply`（继承五不变量 + 文件锁）
3. **规则先行，Laya 第二棒**：RuleJudge 兼作监督标签来源；Laya 仅在 held-out 验证优于规则召回后主裁
4. **不进入请求热路径**：旁路 asyncio 任务，60s 周期

## 待用户拍板

- [ ] M0-M5 里程碑范围与顺序
- [ ] 规则初始阈值（TTFT 300ms / KV 80%-90% / 冷却 600s 等）
- [ ] auto 模式是否本期开放（还是 P0 仅建议模式，auto 后置）