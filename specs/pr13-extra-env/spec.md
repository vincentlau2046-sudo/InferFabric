# PR-13: `extra_env` 支持

## 业务需求
VLLMConfig 当前无法注入环境变量到 vLLM subprocess。模型特异 env（DeepGemm、HF_ENDPOINT 等）硬编码在 process_manager.py 中，违反"模型 YAML 自描述"原则。

## 变更范围
- `config.py`: VLLMConfig 新增 `extra_env: dict[str, str] = field(default_factory=dict)`
- `config.py`: load_models() 解析 YAML extra_env + 保护列表校验
- `process_manager.py`: start_vllm() 删 DeepGemm 硬编码，env.update(cfg.extra_env)
- `process_manager.py`: sleep_mode 冲突检测 + warning
- `model_lifecycle.py`: start_vllm(model.vllm) 不再传 model_type
- `models.d/qwen36-27b-vl.yaml`: 新增 extra_env

## 风险爆炸链
1. extra_env 覆盖 PATH → vllm bin 找不到 → 503
   - 缓解：硬保护列表 {"PATH","HOME","CONDA_DEFAULT_ENV"}，配置时 raise ConfigError
2. extra_env 含 VLLM_SERVER_DEV_MODE + sleep_mode 启用 → sleep 静默失效
   - 缓解：log.warning 明确告知
3. YAML 值类型错 → env 非字符串
   - 缓解：load_models() 阶段 str(v) 强转

## env 注入顺序
1. env = dict(os.environ)
2. 平台 defaults (PYTORCH_CUDA_ALLOC_CONF / VLLM_SERVER_DEV_MODE / PATH)
3. env.update(cfg.extra_env) — 最高优先级
4. subprocess.Popen(env=env)

## 验收标准
- [ ] VLLMConfig 无 extra_env → 默认空 dict，零侵入
- [ ] YAML 含 extra_env → 正确解析 + str 转换
- [ ] extra_env 含保护键 → ConfigError
- [ ] start_vllm env 包含 extra_env 键值
- [ ] DeepGemm 硬编码已删除
- [ ] sleep_mode 冲突时 log.warning
- [ ] 180 pytest 全量通过
- [ ] iff switch qwen36-27b-vl 日志可见 extra_env 注入
