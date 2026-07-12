# models.d — 模型配置文件目录

## 目录用途

`models.d/` 存放所有模型的 YAML 配置文件。每个文件描述一个模型的类型、资源需求、运行时参数等。IFF 启动时自动扫描此目录，加载所有 `.yaml` 文件作为可用模型。

旧架构的 `profiles.yaml` 已被此目录取代。每个模型是自描述的（self-describing plugin），不再需要中心化的 profile 定义。

---

## YAML 模板规范

### 通用字段（所有模型类型）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `name` | ✅ | string | 模型唯一标识符，同时也是文件名（不含扩展名） |
| `description` | ✅ | string | 模型描述，显示在 `iff models` 输出中 |
| `type` | ✅ | string | 模型类型: `vllm` / `comfyui` / `ollama` / `ollama_cpp` / `ollama_daemon` / `alias_map` |
| `gpu_role` | ✅ | string | GPU 角色: `exclusive` / `shared` / `none` |
| `model_type` | 否 | string | 模型类别: `llm` / `vl` / `embedding` / `aigc` |
| `modality` | 否 | string | 输入输出模态: `text` / `text-vision` / `multimodal` / `embedding` / `aigc` |
| `quantization` | 否 | string | 量化格式: `NVFP4` / `GPTQ-4bit` / `Q4_K_M` / `Q8_0` / `Q4_0` |
| `typical_vram_pct` | 否 | float | 典型显存占用百分比（仅 shared 模型） |

### 类型特有字段

#### vllm
用于 vLLM 推理引擎。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `vllm.model_dir` | ✅ | string | 模型目录名（相对于 `~/models/`） |
| `vllm.served_name` | ✅ | string | vLLM serve 注册名 |
| `vllm.port` | 否 | int | 服务端口（默认池分配） |
| `vllm.gpu_memory_utilization` | 否 | float | GPU 显存利用率 (0.0–1.0) |
| `vllm.max_model_len` | 否 | int | 最大上下文长度 |
| `vllm.extra_args` | 否 | list[string] | 额外 vLLM 启动参数 |
| `vllm.enforce_eager` | 否 | bool | 是否强制 eager 模式 |
| `vllm.distributed_executor_backend` | 否 | string | 分布式后端: `ray` / `mp` |

#### comfyui
用于 ComfyUI 图像生成。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `conda_env` | ✅ | string | Conda 环境名 |
| `port` | ✅ | int | Web 服务端口 |
| `working_dir` | 否 | string | ComfyUI 工作目录（默认 `~/ComfyUI`） |

#### ollama
用于 Ollama 端模型推理。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ollama.model_ref` | ✅ | string | Ollama 模型引用名 |

#### ollama_cpp
用于 Ollama.cpp 直接推理（CPU/GPU）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ollama_cpp.model_path` | ✅ | string | GGUF 模型文件路径 |
| `ollama_cpp.port` | ✅ | int | 服务端口 |
| `ollama_cpp.n_gpu_layers` | 否 | int | GPU 卸载层数 |
| `ollama_cpp.extra_args` | 否 | list[string] | 额外启动参数 |

#### ollama_daemon
用于 Ollama 守护进程（基础设施）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ollama_daemon.port` | ✅ | int | 服务端口 |
| `ollama_daemon.health_url` | ✅ | string | 健康检查 URL |
| `ollama_daemon.data_dir` | 否 | string | 数据目录（默认 `~/.ollama`） |

#### alias_map
用于模型别名映射。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `aliases` | ✅ | map[string,string] | 别名 → 模型名映射 |

---

## 命名约定

- 文件名: `{model-name}.yaml`，全小写，短横线分隔
- `name` 字段值必须与文件名（不含扩展名）一致
- 别名文件: `aliases.yaml`（固定名称）

---

## 现有模型列表

| 文件名 | 类型 | 用途 |
|--------|------|------|
| `aliases.yaml` | alias_map | 模型别名映射（fast → qwen25-omni-3b, powerful → qwen36-27b-vl 等） |
| `bge-m3.yaml` | ollama_cpp | BGE-M3 Q4_K_M 嵌入模型（CPU，多语言） |
| `comfyui.yaml` | comfyui | ComfyUI 图像生成服务（shared GPU） |
| `gemma-4-31B-it-NVFP4.yaml` | vllm | Gemma4-31B IT NVFP4 Dense 文本推理 |
| `ollama-daemon.yaml` | ollama_daemon | Ollama 守护进程（基础设施） |
| `phi3-mini.yaml` | ollama | Phi-3 Mini 3.8B 文本推理（shared GPU） |
| `qwen25-omni-3b.yaml` | ollama_cpp | Qwen2.5 Omni 3B Q8_0 多模态推理（CPU） |
| `qwen3-embedding-0.6b.yaml` | ollama_cpp | Qwen3 Embedding 0.6B 嵌入模型（CPU，多语言） |
| `qwen35-9b.yaml` | vllm | Qwen3.5-9B GPTQ-4bit 视觉语言模型（shared GPU） |
| `qwen36-27b-vl.yaml` | vllm | Qwen3.6-27B NVFP4 + MTP 视觉语言模型（exclusive GPU） |
| `qwen36-35b.yaml` | vllm | Qwen3.6-35B A3B MoE NVFP4 视觉语言模型（exclusive GPU） |

---

## 新增模型流程

1. **创建 YAML 文件**  
   在 `models.d/` 下创建 `{model-name}.yaml`，遵循上述模板规范。

2. **填写必填字段**  
   `name`、`description`、`type`、`gpu_role` 为所有模型必填。

3. **填写类型特有字段**  
   根据 `type` 类型添加对应字段块（`vllm:` / `comfyui:` / `ollama:` 等）。

4. **验证语法**  
   ```bash
   python3 -c "import yaml; yaml.safe_load(open('models.d/{model-name}.yaml'))"
   ```

5. **注册到版本控制**  
   `git add models.d/{model-name}.yaml`

6. **测试**  
   ```bash
   iff switch {model-name}
   iff status
   ```

> **注意**: 不需要修改任何 Python 代码。模型配置完全由 YAML 文件声明。IFF 启动时自动扫描 `models.d/` 目录。