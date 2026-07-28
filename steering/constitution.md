# IFF Constitution

## 治理原则
- **绝对稳定优先**：IFF 是 AI 平台核心推理代理，proxy 挂 = 全链路挂
- **沙箱先行**：所有修改在沙箱完成 → pytest → 冒烟 → diff 审查 → 合入生产
- **增量收敛**：每个 PR 独立 spec → 独立验证 → 独立 commit
- **风险显式化**：每个变更必须声明风险爆炸链 + 缓解措施

## 质量门禁
- 180 pytest 全量通过
- `python3 -c "import inferfabric"` 启动冒烟
- AtomCode GLM-5.2 交叉 review
- 运行时冒烟：核心 API 端点 200

## 技术约束
- Python 3.10+
- vLLM 0.24（暂不升级）
- 不改 proxy 转发核心路径（PR-14 排除）
