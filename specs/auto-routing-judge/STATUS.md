# IFF 自动调度裁判（auto-routing judge）Status

> 重大功能升级 · 从建议中心到 Laya 决策裁判

| Phase | 状态 | 日期 | 备注 |
|-------|------|------|------|
| P0 Intake | ✅ | 2026-09-26 | 用户提出「按性能统计 log 做启动重路由裁判」，Laya 事实核实（Apache 2.0 / 421M / typed-decisions） |
| P1 Explore | ✅ | 2026-09-26 | 三视角可行性分析（用户/技术/架构）获认可：观测层已齐，裁判=旁路建议者，执行走 tune.apply |
| P2 Specify | ✅ | 2026-09-26 | spec.md DRAFT 完成（含 §4.8 基线校准闭环） |
| P3 Design | ✅ | 2026-09-27 | 用户审阅通过——「行，就这样吧」（阈值从基线出发 + 三步校准闭环被采纳） |
| P4 Review | ✅ | 2026-09-27 | M0 设计审阅通过，里程碑 M0-M6 + 校准参数 + 示例系数已拍板 |
| P5 Implement | ⬜ | | M1 骨架 → M2 RuleJudge+Gate → M3 执行接线 |
| P6 Converge | ⬜ | | M4 数据积累（≥2 周） |
| P7 Laya | ⬜ | | M5 独立里程碑：LayaJudge 适配器 + held-out 对拍 |
| P8 Release | ⬜ | | 全量 pytest + 冒烟 + 合入生产 |

## 关键决策（已定）

1. **建议模式默认、auto 显式开启**——不侵略性自动机
2. **裁判永不写状态**：只产 Recommendation，执行唯一入口 `tune.apply`（继承五不变量 + 文件锁）
3. **规则先行，Laya 第二棒**：RuleJudge 兼作监督标签来源；Laya 仅在 held-out 验证优于规则召回后主裁
4. **不进入请求热路径**：旁路 asyncio 任务，60s 周期
5. **阈值从基线出发，不拍脑袋**：规则判定基于「相对基线的偏离系数」（baselines.yaml，§4.8）；任何规则须经「基线采集 → 业务推导 → dry-run 验证」三步转 `active` 后才可触发动作，此前为 `pending` 只记录不执行

## 待用户拍板

- [x] M0-M6 里程碑范围与顺序（含 M2 基线校准环节拆分）——2026-09-27 已拍板
- [x] 基线校准参数：`min_samples=200`、dry-run ≥7 天（scale_up 可延至 ≥14 天）——2026-09-27 已拍板
- [x] 业务推导示例系数：TTFT 2.0× / KV +15pct（兜底 90%）/ error +2σ / 低载 0.5×——2026-09-27 已拍板
- [x] auto 模式本期开放（仅 active 规则 + Gate 全条件，默认建议模式）——2026-09-27 已拍板

## 下一步（P5 Implement）

- M1 骨架：`inferfabric/scheduler/` + StateSnapshot + Recommendation + observer 聚合
- M2 基线校准：`iff calibrate`（采集 → 业务推导 → dry-run 回放）
- M3 RuleJudge + Gate → M4 执行接线 → M5 数据积累 → M6 Laya（对拍达标才主裁）