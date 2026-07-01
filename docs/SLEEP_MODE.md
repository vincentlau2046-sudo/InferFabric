# Sleep Mode Implementation — 2026-06-30

## 已完成

### 1. Dashboard vLLM 性能监控
- 性能面板：KV Cache / Waiting / Preempt / TTFT / Throughput 五指标
- Prometheus text 格式解析器
- 60s 自动刷新，阈值告警（KV>90% 红，Preempt>0 红）
- 服务卡片改造：行布局（名称 + 端口 + 模式标签 + 状态）

### 2. 服务按钮改造
- LLM 服务：running → [释放][休眠]，sleeping → [释放][唤醒]，stopped → [启动]
- ComfyUI：running → [释放]，stopped → [启动]
- 所有按钮通过 proxy API 后端支持

### 3. Sleep Mode 代码框架
- L2 sleep（权重丢弃，VRAM 释放）
- 互斥锁：同时只允许一个模型 sleep
- 状态机：sleep 不影响 GPU 模式定义
- CLI：`edge-llm sleep/wake`
- Dashboard + proxy API 端到端

### 4. KV Offloading 配置
- Qwen36-27B: `--kv-offloading-size 16GB`（因 VRAM 未启用）
- Qwen35-9B: `--kv-offloading-size 8GB`（因 VRAM 未启用）

## 阻塞问题

**vLLM 0.23.0 L2 sleep wake_up 有 bug**，三种不同错误：
1. `CUDA Error: invalid argument at cumem_allocator.cpp:145`
2. `'list' object has no attribute 'zero_'`
3. 进程挂死
- 官方三步（wake_up → reload_weights → reset_prefix_cache）均无法正常恢复

**VRAM 限制**：
- Qwen36-27B @128K + CUDA 图 + sleep cumem ≈ **超出 32GB**
- 降 0.80 仍差 ~500MB
- 去 CUDA graphs 可放但损失推理性能

## 代码改动用文件

| 文件 | 改动内容 |
|------|---------|
| `models.d/qwen36-27b.yaml` | gpu_memory_utilization 0.90，sleep_mode 已清除 |
| `models.d/qwen35-9b.yaml` | gpu_memory_utilization 0.38 + KV offloading 8GB |
| `config.py` | SleepModeConfig dataclass |
| `process_manager.py` | sleep_vllm/wake_vllm + 启动注入 |
| `state.py` | sleep_state 持久化 |
| `manager.py` | sleep_model/wake_model + 状态机 + status 增强 |
| `cli.py` | sleep/wake 命令 |
| `dashboard.py` | 性能面板 + 服务卡片 4/2 按钮 |
| `proxy.py` | /vllm_metrics + /sleep + /wake 端点 |

## 后续方向

1. **升级 vLLM** → 0.24+ 可能有修复（查找 `/wake_from_sleep` 等新 API）
2. **重新评估 VRAM** → 升级后先测 Qwen35（9B, VRAM 充裕）
3. **激活 sleep_mode** → 在目标模型 YAML 加 `sleep_mode: enabled: true` 即可