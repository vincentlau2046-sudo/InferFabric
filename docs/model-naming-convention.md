# IFF 模型命名规范

**生效日期**: 2026-08-04 (v4.7.0)
**适用范围**: `models.d/*.yaml` 的 `name` 和 `served_name` 字段

---

## 命名格式

```
{vendor}{version}-{params}-{capability}
```

### 各段规则

| 段 | 规则 | 示例 |
|---|---|---|
| vendor | 小写字母，系列名 | `qwen`, `gemma`, `llama`, `deepseek`, `bge`, `ovis` |
| version | 版本号合并，不加点号 | `35` (Qwen3.5), `36` (Qwen3.6), `4` (Gemma4) |
| params | 数字+`b` | `9b`, `27b`, `31b`, `70b`, `0.6b`, `3b` |
| capability | **必填**，`-` 分隔 | `vl`, `omni`, `tts`, `aigc`, `ocr`, `embed`, `infra` |

### 能力标签

| 标签 | 含义 |
|------|------|
| `vl` | 视觉语言（Vision-Language） |
| `omni` | 全模态（文本+图像+音频） |
| `tts` | 文本转语音 |
| `aigc` | AI 生成内容（图像/视频工作流） |
| `ocr` | 光学字符识别 |
| `embed` | 文本嵌入 |
| `infra` | 基础设施服务（非推理） |

### 全部小写，单词间用 `-` 连接

## name 与 served_name

**`name` == `served_name`**，统一一个名字，不搞两套。`load_models()` 强制校验，不符则拒绝加载。

## 量化/微调变体

量化方式和微调信息**不编入模型名**，放在 yaml 的 `quantization` / `description` 字段：

```yaml
name: gemma4-31b-vl
description: "Huihui Gemma4-31B IT Abliterated v2 NVFP4 Dense"
quantization: NVFP4
```

同模型多量化版本共存时，用目录区分（`models.d/gemma4-31b-vl-nvfp4.yaml`），name 仍为 `gemma4-31b-vl`。不可同时部署两个同名模型。

## 当前模型

| 文件名 | name (= served_name) | 类型 | 说明 |
|--------|---------------------|------|------|
| `qwen35-9b-vl.yaml` | `qwen35-9b-vl` | vllm | Qwen3.5-9B VL GPTQ |
| `qwen36-27b-vl.yaml` | `qwen36-27b-vl` | vllm | Qwen3.6-27B VL NVFP4 |
| `qwen36-35b-vl.yaml` | `qwen36-35b-vl` | vllm | Qwen3.6-35B A3B MoE VL NVFP4 |
| `gemma4-31b-vl.yaml` | `gemma4-31b-vl` | vllm | Gemma4-31B VL NVFP4 |
| `qwen25-omni-3b.yaml` | `qwen25-omni-3b` | vllm | Qwen2.5-Omni-3B |
| `qwen3-embedding-0.6b.yaml` | `qwen3-embedding-0.6b` | vllm | Qwen3-Embedding-0.6B |
| `tts-qwen3.yaml` | `tts-qwen3` | tts_server | Qwen3-TTS 1.7B |
| `bge-m3.yaml` | `bge-m3` | vllm | BGE-M3 嵌入 |
| `ovis-ocr2.yaml` | `ovis-ocr2` | vllm | Ovis2-OCR |
| `comfyui.yaml` | `comfyui` | comfyui | ComfyUI 工作流引擎 |
| `ollama-daemon.yaml` | `ollama-daemon` | ollama | Ollama 守护进程 |

## 例外

以下模型因命名模式与主规则不完全一致，保留现有命名：

| 模型 | 原因 |
|------|------|
| `bge-m3` | BAAI 官方命名，无版本号，嵌入式模型不适用 `{vendor}{version}` 模式 |
| `ovis-ocr2` | 第三方独立命名模式，ocr2 为版本+能力的组合 |

新增模型必须遵循主规则，不再新增例外。
