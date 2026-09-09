# InferFabric 操作类测试报告

> ⚠️ 本报告为模板，需在真实环境执行操作类测试后手动填写。
>
> 运行命令：
> ```bash
> IFF_OPERATIONAL_CONFIRM=1 pytest tests/test_operational.py -v --tb=short
> ```

---

## 测试环境

| 项目 | 值 |
|------|---|
| 日期 | YYYY-MM-DD |
| 代理地址 | http://localhost:8999 |
| GPU 型号 | |
| GPU 显存 | |
| 可用模型 | |
| 操作人员 | |

---

## 执行前确认清单

- [ ] 确认当前无其他重要推理任务在运行
- [ ] 确认 GPU 显存充足
- [ ] 确认代理服务已启动（`iff serve`）
- [ ] 确认模型配置文件正确
- [ ] 已备份当前 GPU 状态

---

## 测试结果

| # | 测试用例 | 场景 | 结果 | 耗时 | 备注 |
|---|----------|------|------|------|------|
| 1 | `test_shared_model_lifecycle` | 共享模型 启动→停止 | ⬜ | | |
| 2 | `test_exclusive_model_lifecycle` | 排他模型 启动→idle | ⬜ | | |
| 3 | `test_double_release_safety` | 重复释放安全性 | ⬜ | | |
| 4 | `test_exclusive_to_shared_rejected` | 排他→共享 拒绝 | ⬜ | | |
| 5 | `test_status_endpoint` | /status API | ⬜ | | |
| 6 | `test_v1_models_endpoint` | /v1/models API | ⬜ | | |
| 7 | `test_switch_idle_idempotent` | 幂等切换 idle | ⬜ | | |
| 8 | `test_dashboard_accessible` | Dashboard 可访问 | ⬜ | | |

---

## 统计

| 指标 | 数值 |
|------|------|
| 总用例数 | 8 |
| ✅ 通过 | |
| ❌ 失败 | |
| 运行耗时 | |

---

## 问题记录

| # | 用例 | 问题描述 | 建议 |
|---|------|----------|------|
| | | | |
