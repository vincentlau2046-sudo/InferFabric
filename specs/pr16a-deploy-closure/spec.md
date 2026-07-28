# PR-16a: deploy ↔ auto_deploy 闭环修复

## 问题
1. `auto_deploy()` YAML 已存在时返回 error → deploy handler 显示失败，应返回 already_configured
2. `auto_deploy()` 写 YAML 后 `load_models(models_dir)` 只刷新模块缓存，未刷新 `manager._models` → switch 找不到新模型
3. YAML 写入非原子 → kill 后可能半写损坏
4. 并发 auto_deploy 同一模型 → 文件交错损坏

## 变更范围
- `model_discovery.py`: auto_deploy() 幂等性 + 原子写入 + 并发锁
- `manager.py`: 新增 reload_models() 方法 + auto_deploy 调用后刷新 self._models
- `handler.py`: _handle_deploy() 对 already_configured 返回友好响应

## 风险
- deploy 路径改动影响所有 deploy 调用 → 必须走沙箱全流程
- reload_models 与 switch 并发 → GPU lock 保护
- YAML 写入 → 原子 rename
