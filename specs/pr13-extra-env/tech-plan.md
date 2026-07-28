# PR-13 Tech Plan

## 改动文件与顺序

| # | 文件 | 改动 | 依赖 |
|---|------|------|------|
| 1 | `config.py` | VLLMConfig 新增 extra_env 字段 + 保护列表常量 | — |
| 2 | `config.py` | load_models() 解析 extra_env + 校验 | #1 |
| 3 | `process_manager.py` | start_vllm() 删 L116-118 + env.update(cfg.extra_env) + sleep 冲突检测 | #1 |
| 4 | `model_lifecycle.py` | _start_model() 不再传 model_type | #3 |
| 5 | `models.d/qwen36-27b-vl.yaml` | 新增 extra_env: { VLLM_USE_DEEP_GEMM: "0" } | #1 |

## 关键代码

### config.py — 保护列表
```python
_PROTECTED_ENV_KEYS = frozenset({"PATH", "HOME", "CONDA_DEFAULT_ENV"})
```

### config.py — VLLMConfig
```python
extra_env: dict[str, str] = field(default_factory=dict)
```

### config.py — load_models() extra_env 解析
```python
extra_env = vllm_raw.pop("extra_env", {}) or {}
if not isinstance(extra_env, dict):
    extra_env = {}
# 校验保护键
for k in extra_env:
    if k in _PROTECTED_ENV_KEYS:
        raise ConfigError(f"extra_env key '{k}' is protected and cannot be overridden")
    extra_env[k] = str(extra_env[k])  # 强转
vllm_cfg = VLLMConfig(**vllm_raw, extra_env=extra_env)
```

### process_manager.py — start_vllm() env 注入
```python
# 删 L116-118:
#   if model_type == "vl":
#       env["VLLM_USE_DEEP_GEMM"] = "0"

# 在 env 构建末尾（Popen 之前）:
for k, v in cfg.extra_env.items():
    env[k] = v
    log.debug("extra_env: %s=%s", k, v)

# sleep_mode 冲突检测:
if cfg.sleep_mode and cfg.sleep_mode.enabled:
    if "VLLM_SERVER_DEV_MODE" in cfg.extra_env:
        log.warning("extra_env overrides VLLM_SERVER_DEV_MODE for %s — sleep mode may not work", cfg.served_name)
```

### model_lifecycle.py — 简化调用
```python
# 原: return self._proc.start_vllm(model.vllm, model.model_type)
# 新: return self._proc.start_vllm(model.vllm)
```
