# IFF Constitution

## 治理原则
- **绝对稳定优先**：IFF 是 AI 平台核心推理代理，proxy 挂 = 全链路挂
- **沙箱先行**：所有修改在沙箱完成 → pytest → 冒烟 → diff 审查 → 合入生产
- **增量收敛**：每个 PR 独立 spec → 独立验证 → 独立 commit
- **风险显式化**：每个变更必须声明风险爆炸链 + 缓解措施

## 质量门禁
- **678** pytest 全量通过（`tests/unit/ + tests/integration/`，2026-09-15 实测）
- `python3 -c "import inferfabric"` 启动冒烟
- 运行时冒烟：`GET /` 和 `GET /status` 返回 200
- Dashboard 每个面板必须有功能实现（不允许 placeholder）
- 失败数据不得污染成功指标的统计
- 版本号在 `__init__.py` 中更新

## 技术约束
- Python 3.10+
- vLLM 0.24（暂不升级）
- 不改 proxy 转发核心路径（PR-14 排除）
