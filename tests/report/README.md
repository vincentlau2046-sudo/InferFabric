# InferFabric 测试报告目录

## 说明

本目录存放 InferFabric 项目的测试报告，按类型分为：

| 文件 | 说明 |
|------|------|
| `full-test-report-2026-09-09.md` | 全量单元/集成测试报告（不含操作类测试） |
| `operational-test-report-template.md` | 操作类测试报告模板（需手动填写） |

## 生成方式

```bash
# 生成全量测试报告（排除操作类测试）
pytest tests/ -m "not operational" --ignore=tests/test_local.py -v --tb=short 2>&1 | tee tests/report/raw-output.log

# 仅运行操作类测试（⚠️ 需确认环境安全后手动执行）
IFF_OPERATIONAL_CONFIRM=1 pytest tests/test_operational.py -v --tb=short 2>&1 | tee tests/report/operational-output.log
```

## 注意事项

- 测试报告为静态快照，反映生成时刻的测试状态
- 操作类测试报告需在真实环境执行后手动填写
- 失败用例需结合 `--tb=long` 查看完整堆栈
