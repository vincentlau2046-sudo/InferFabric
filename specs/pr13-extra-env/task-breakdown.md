# PR-13 Task Breakdown

| Task | 内容 | 依赖 | 估时 |
|------|------|------|------|
| T1 | config.py: VLLMConfig 新增 extra_env + _PROTECTED_ENV_KEYS + ConfigError | — | 5min |
| T2 | config.py: load_models() 解析 extra_env + 校验 + str 转换 | T1 | 10min |
| T3 | process_manager.py: 删 DeepGemm 硬编码 + env.update(extra_env) + sleep 冲突检测 | T1 | 10min |
| T4 | model_lifecycle.py: start_vllm() 不再传 model_type | T3 | 5min |
| T5 | models.d/qwen36-27b-vl.yaml: 新增 extra_env | T1 | 2min |
| T6 | pytest 验证 + 启动冒烟 | T1-T5 | 10min |
