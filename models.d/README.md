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
| `type` | ✅ | string | 模型类型: `vllm` / `sglang` / `ninfer` / `comfyui` / `ollama_cpp` / `ollama_daemon` / `tts_server` / `asr_server` / `alias_map` |
| `gpu_role` | ✅ | string | GPU 角色: `exclusive` / `shared` / `none` |
| `model_type` | 否 | string | 模型类别: `llm` / `vl` / `embedding` / `aigc` |
| `modality` | 否 | string | 输入输出模态: `text` / `text-vision` / `multimodal` / `embedding` / `aigc` |
| `quantization` | 否 | string | 量化格式: `NVFP4` / `GPTQ-4bit` / `Q4_K_M` / `Q8_0` / `Q4_0` |
| `peak_vram_mb` | 否 | int | 峰值显存 (MB)，用于 GPU 调度估算 |

### 类型特有字段

#### vllm
用于 vLLM 推理引擎。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `vllm.model_dir` | ✅ | string | 模型目录名（相对于 `~/models/`） |
| `vllm.served_name` | ✅ | string | vLLM serve 注册名 |
| `vllm.port` | ✅ | int | 服务端口 |
| `vllm.gpu_memory_utilization` | ✅ | float | GPU 显存利用率 (0.0–1.0) |
| `vllm.max_model_len` | 否 | int | 最大上下文长度 |
| `vllm.max_num_seqs` | 否 | int | 最大并发序列数 |
| `vllm.kv_cache_dtype` | 否 | string | KV cache 数据类型（`auto` / `fp8` / `fp8_e4m3`） |
| `vllm.conda_env` | ✅ | string | Conda 环境名 |
| `vllm.startup_timeout` | 否 | int | 健康检查超时秒数（0 = 使用全局默认值） |
| `vllm.extra_env` | 否 | dict | 注入子进程的环境变量（最高优先级） |
| `vllm.extra_flags` | 否 | string | 额外 vLLM 启动参数（以空格分隔，经 shlex.split 追加到命令行） |

#### comfyui
用于 ComfyUI 图像生成。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `comfyui.port` | ✅ | int | Web 服务端口 |
| `comfyui.working_dir` | 否 | string | ComfyUI 工作目录（默认 `~/ComfyUI`） |

#### ninfer
用于 NInfer Docker 推理引擎（Blackwell NVFP4 专属，支持 VL/多模态）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ninfer.weight_path` | ✅ | string | `.ninfer` artifact 路径 |
| `ninfer.docker_image` | ✅ | string | 镜像名（VL 版需用含 `--vision` 支持的镜像，如 `ninfer:vl`） |
| `ninfer.served_name` | 否 | string | proxy 路由名，缺省用模型名 |
| `ninfer.model_id` | 否 | string | 覆盖 artifact identity（`--model-id`） |
| `ninfer.port` | ✅ | int | 服务端口（查端口登记表取唯一端口） |
| `ninfer.container_name` | 否 | string | Docker 容器名 |
| `ninfer.max_context` | 否 | int | 单请求最大上下文（tokens） |
| `ninfer.kv_capacity` | 否 | int | KV cache 池（tokens）；`0` = auto |
| `ninfer.kv_dtype` | 否 | string | KV 精度: `int8` / `fp8` / `nvfp4` / `bf16` |
| `ninfer.vision` | 否 | bool | 多模态 VL：`true` 时追加 `--vision` 启用视觉塔+media（默认 `false`） |
| `ninfer.enable_mtp` | 否 | bool | 启用 MTP 投机解码（默认 `true`） |
| `ninfer.draft_tokens` | 否 | int | MTP draft token 数 |
| `ninfer.media_cache_mib` | 否 | int | media 复用缓存 MiB；`0` 关闭跨请求复用（默认 `1024`） |
| `ninfer.media_live_mib` | 否 | int | media 在线 BF16 patch 上限 MiB；视觉下 minimum≈384MiB、不可为 0（默认 `2048`） |

> ⚠️ **多模态模型 `vision: true` 时，顶到显卡显存上限，端口务必避开 CPU/桌面占用的空间**；用 `ninfer:vl` 这类带视觉支持的镜像。

> 💡 **KV 池调优**：`kv_capacity=0`(auto) 会留 1024MiB 自动头寸 + 空闲余量，池较小但最稳。若需更高 KV，可压 `media_live_mib`（视觉下不低于 ~384MiB）+ 设**显式** `kv_capacity`（如 420000）。显式值会吃满可用（`weights 后可用`≈10.2GiB/卡上，边界≈434K tokens；桌面/远程客户端如 ToDesk 占用会直接压低此上限 → `Engine runtime reservation requires ... available` FATAL ）。显式值太高会启动失败，需 ≤ 上限并留少量余量。参见 [timeline]。

#### ollama_daemon
用于 Ollama 守护进程（基础设施）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ollama_daemon.port` | ✅ | int | Ollama API 端口 |

#### ollama_cpp
用于 Ollama.cpp 直接推理（CPU/GPU）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ollama_cpp.port` | ✅ | int | OpenAI-compatible API 端口 |

#### tts_server
用于 TTS 语音合成服务（OpenAI-compatible `/v1/audio/speech`）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `tts.port` | ✅ | int | API 端口 |
| `tts.conda_env` | ✅ | string | Conda 环境名 |

#### asr_server
用于 ASR 语音识别服务（OpenAI-compatible `/v1/audio/transcriptions`）。

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `asr.port` | ✅ | int | API 端口 |
| `asr.conda_env` | ✅ | string | Conda 环境名 |

---

## 命名约定

- 文件名: `{model-name}.yaml`，全小写，短横线分隔
- `name` 字段值必须与文件名（不含扩展名）一致
- 别名文件: `model_affinity.yaml`（固定名称，云端模型路由）

---

## 端口登记表

**端口分配原则**：
- `8000-8009`: vLLM / NInfer 推理服务（LLM / VL）
- `8188`: ComfyUI
- `8800-8899`: TTS / ASR
- `11000-11999`: Ollama 基础设施

⚠️ **每次新增/删除/修改任意 YAML（含端口、字段、模板）都必须同步更新本表及本文档**，防端口冲突与文档漂移。详见下方「新增/删除 YAML 流程」的强制规则。

| 端口 | 文件名 | served_name | 类型 | GPU 角色 | 描述 |
|------|--------|-------------|------|----------|------|
| 8002 | `Qwen38-27B-VL.yaml` | `Qwen38-27B-VL` | vllm | exclusive | Qwen3.8-27B Abliterated NVFP4 VL |
| 8003 | `qwen3-vl-4b.yaml` | `qwen3-vl-4b` | vllm | shared | Qwen3-VL-4B GPTQ W4A16（AICF 质检） |
| 8004 | `ovis-ocr2.yaml` | `ovis-ocr2` | vllm | shared | OvisOC2 0.8B 端到端文档OCR |
| 8005 | `gemma4-31b-vl.yaml` | `gemma4-31b-vl` | vllm | exclusive | Gemma4-31B IT NVFP4 Dense VL |
| 8006 | `muse-glimmer-vl.yaml` | `muse-glimmer` | vllm | exclusive | Meta Muse Glimmer 30B NVFP4 VL （SGLang） |
| 8007 | `Qwen38-27B-TXT.yaml` | `Qwen38-27B-TXT` | ninfer | exclusive | Qwen3.8-27B NVFP4 NInfer（纯文本，MTP） |
| 8008 | `qwen36-35b-vl.yaml` | `qwen36-35b-vl` | vllm | exclusive | Qwen3.6-35B A3B MoE NVFP4 VL |
| 8009 | `NI-Qwen38-27B-VL.yaml` | `NI-Qwen38-27B-VL` | ninfer | exclusive | Qwen3.8-27B NVFP4 NInfer 多模态 VL（视觉塔 + NVFP4 KV） |
| 8188 | `comfyui.yaml` | — | comfyui | shared | ComfyUI 图像生成 |
| 8880 | `tts-qwen3.yaml` | — | tts_server | shared | Qwen3-TTS 1.7B 语音合成 |
| 8881 | `asr-sensevoice.yaml` | — | asr_server | shared | ASR SenseVoice-Small 中文语音识别 |
| 11434 | `ollama-daemon.yaml` | — | ollama_daemon | — | Ollama 守护进程 |
| 11441 | `bge-m3.yaml` | — | ollama_cpp | none | BGE-M3 Q4_K_M 嵌入（CPU） |
| 11442 | `bge-reranker-v2-m3.yaml` | — | ollama_cpp | none | BGE-Reranker-v2-m3 Q8_0 重排序（CPU） |

**注释**：
- `model_affinity.yaml` 为云模型路由配置，不占用端口。
- GPU 角色：`exclusive` = 独占 GPU、`shared` = 共享 GPU、`none` = CPU 运行。
- `qwen3-vl-4b-prefill` / `qwen3-vl-4b-decode`（P/D 分离，端口 8010/8011）已归档至 `archive/*.yaml.bak`（2026-09-21，暂不启用 PD 分离；如需恢复：还原文件名回 `models.d/` 并在本表加回两行）。

---

## 新增/删除 YAML 流程

> **📌 强制规则（必须遵守）：任何对 `models.d/*.yaml` 或本文档"模板规范 / 端口登记表"的更改，都必须同步更新本 README，否则视为配置无效。** 否则会导致端口冲突 / 文档漂移（历史教训：`8002` 行曾保留旧文件名，`Qwen38-27B-TXT` 曾漏登）。

1. **新增**：创建 `{model-name}.yaml`，选唯一端口（查上表），填入必填字段。
2. **删除**：移文件到 `archive/` 子目录（保留回溯可能），而非直接 `rm`。
3. **修改任何 YAML**：无论是改字段、改类型、改端口，均须同步——
   - 若端口变更 → 更新**端口登记表**对应行；
   - 若新增/改模板字段 → 更新**YAML 模板规范**对应类型区块；
   - 每次变更 → 在**更新时间线**追加一行记录（日期 + 变更内容）。
4. **验证**：`python3 ~/projects/inferfabric-sandbox/inferfabric/cli.py models`，确认新模型在列表中出现，且端口无冲突。

> **注意**：模型配置完全由 YAML 文件声明，IFF 启动时自动扫描 `models.d/` 目录。本文档（本 README）是该目录配置的**唯一权威登记**——YAML 与本文档不一致即视为配置漂移，需要修正。git 提交时建议将对应 YAML 与本文档改动放在同一 commit 便于审查。

---

## 更新时间线

- 2026-09-22: 新增 `NI-Qwen38-27B-VL.yaml`（NInfer 多模态 VL，端口 8009）。补登 8007 `Qwen38-27B-TXT`；修正 8002 行文件/served_name 为 `Qwen38-27B-VL`；模板 type 增补 `sglang`/`ninfer` 并新增 ninfer 字段说明。
- 2026-09-22: `NI-Qwen38-27B-VL.yaml` 调显存寻更多 KV：压 `media_cache_mib: 0` + `media_live_mib: 1024`（default 是 1024/2048），并将 `kv_capacity` 由 `0`(auto) 改为显式 `420000`（auto 只到 ~378K；显式上限实测 ≈434K/卡上、ToDesk 占用时）。NInferConfig 新增 `media_cache_mib`/`media_live_mib` 字段（config.py 代码改动 → 需重启 proxy）。
- 2026-08-23: 创建端口登记表。清理旧版中已删除模型的记录（aliases.yaml、phi3-mini.yaml、qwen25-omni-3b.yaml、qwen3-embedding-0.6b.yaml、qwen35-9b.yaml、qwen36-27b-vl.yaml）。迁入全部 12 个活跃模型的端口映射。
