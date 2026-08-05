# InferFabric — 本地 LLM 推理网关

> **版本**: v4.7.1
> **更新**: 2026-08-05
> **硬件**: RTX 5090D (32GB VRAM)
> **核心理念**: 模型即插件 — 一个 YAML 文件 = 一个可部署的模型

---

## 目录

- [概述](#概述)
- [快速开始](#快速开始)
- [核心概念](#核心概念)
- [云 Provider 预设](#云-provider-预设)
- [API Key 安全模型](#api-key-安全模型)
- [目录结构](#目录结构)
- [端口分配表](#端口分配表)
- [CLI 参考](#cli-参考)
- [模型配置格式](#模型配置格式)
- [OpenClaw / Claude Code 集成](#openclaw--claude-code-集成)
- [Proxy API 端点](#proxy-api-端点)
- [故障恢复](#故障恢复)
- [版本历史](#版本历史)

---

## 概述

InferFabric 是单卡 GPU 上的 LLM 推理网关，统一管理本地模型（vLLM/llama.cpp/Ollama/ComfyUI）和云端 Provider（百度千帆/DeepSeek/OpenAI/Anthropic 等）的生命周期、路由与发现。

**v4.7 核心能力**:

| 能力 | 说明 |
|------|------|
| 本地模型切换 | 三态 GPU 状态机（idle/exclusive/shared），模型即插件 |
| 云 Provider 预设 | 9 个预配置厂商，一键添加 + 自动模型发现 |
| API Key 安全 | 明文自动转 `${ENV_VAR}` 引用，密钥存 secrets.env (chmod 600) |
| 双引擎路由 | OpenAI `/v1/chat/completions` + Anthropic `/v1/messages` |
| Dashboard | 实时 GPU/模型/Provider 状态，预设卡片式添加 |
| 多后端支持 | vLLM、llama.cpp (CPU/CUDA 双构建)、Ollama、ComfyUI |

**版本演进**:

| v3.x (Profile) | v4.0+ (Model Plugin + Cloud) |
|-----------------|-------------------------------|
| `profiles.yaml` 单文件 | `models.d/` 目录, 一文件一模型 |
| 仅本地模型 | 本地 + 云端 Provider 统一管理 |
| GPU 锁二值(持有/不持有) | 三态(idle/exclusive/shared) |
| N 模型 × M 组合 = Profile 爆炸 | N 文件 + 云预设, 无组合 |
| API Key 明文存 YAML | `${ENV_VAR}` 引用 + secrets.env |

---

## 端口分配表

所有本地服务的端口固定分配，新增模型时**必须检查此表避免冲突**。

| 端口 | 模型 | 类型 | GPU Role | Conda 环境 |
|------|------|------|----------|------------|
| 8001 | qwen36-35b-vl | vllm | exclusive | qwen36-35b-vllm |
| 8003 | qwen36-27b-vl | vllm | shared | qwen36-27b-vllm-vl |
| 8004 | ovis-ocr2 | vllm | shared | ovis-vllm |
| 8005 | gemma4-31b-vl | vllm | exclusive | gemma-4-31b-vllm |
| 8002 | qwen3-vl-4b | vllm | shared | qwen3-vl-4b-vllm |
| 8188 | comfyui | comfyui | exclusive | comfyui |
| 8880 | tts-qwen3 | tts_server | none | tts-qwen3 |
| 8881 | sensevoice-small | asr_server | shared | sensevoice |
| 11434 | ollama-daemon | ollama_daemon | none | — |
| 11441 | bge-m3 | ollama_cpp | none | — |
| 11442 | bge-reranker-v2-m3 | ollama_cpp | none | — |

**端口分配规则**:
- vLLM GPU 模型: 8001-8099
- 专用服务 (ComfyUI): 8100-8999
- ASR/TTS 服务: 8880-8899
- Ollama/llama.cpp: 11400-11499
- Proxy: 8999 (固定)

---

## 快速开始

```bash
# 查看当前状态
iff status

# 列出可用模型
iff models

# 切换到 Qwen3.6-35B (独占模式)
iff switch qwen36-35b-vl

# 释放 GPU
iff switch idle

# 切换到 Qwen3.5-9B (共享模式)
iff switch qwen35-9b-vl

# 在共享模式下加入 TTS
iff switch tts-qwen3

# 停止单个共享服务
iff stop qwen35-9b-vl

# 强制重置
iff reset
```

---

## 核心概念

### 模型即插件

每个模型/服务由 `models.d/` 下的一个 YAML 文件定义。文件自带一切:

- 模型参数(路径、端口、conda 环境、vLLM 参数)
- 部署模式(`gpu_role: exclusive` / `gpu_role: shared` / `gpu_role: none`)
- 服务类型(`type: vllm` / `type: ollama_cpp` / `type: comfyui` / `type: tts_server`)
- 模型类型(`model_type: vl` / `embedding` / `omni` / `ocr` / `tts` / `aigc` / `infra`)

**增删模型 = 增删 YAML 文件**，零改动代码。

### 三态 GPU 状态机

```
                 switch(exclusive)
   idle ─────────────────────────────→ exclusive
    ↑                                    │
    │              switch(shared)         │   switch(idle)
    │         idle ──────→ shared ←──────│←───────┘
    │                          │
    │          switch(idle)    │
    └──────────────────────────┘

   ❌ exclusive → shared  : 必须先 idle
   ❌ shared → exclusive   : 必须先 idle
```

| 当前状态 | 允许操作 | 效果 |
|----------|----------|------|
| `idle` | `switch <exclusive_model>` | GPU 全锁 |
| `idle` | `switch <shared_model>` | GPU 共享锁 |
| `exclusive` | `switch idle` | 释放 GPU |
| `shared` | `switch <shared_model/service>` | 加入共享服务 |
| `shared` | `stop <model>` | 移除单个服务 |
| `shared` | `switch idle` | 停所有,释放 GPU |

### 双 llama.cpp 构建

| 构建类型 | 路径 | 用途 |
|----------|------|------|
| CPU-only | `~/llama-cpp/build/bin/llama-server` | `gpu_layers: 0` / `gpu_role: none` 模型 (如 bge-m3) |
| CUDA | `~/llama-cpp/build-cuda/bin/llama-server` | `gpu_layers != 0` / `gpu_role: shared` 模型 (如 TTS) |

CPU-only 构建避免 CUDA runtime 开销 (~100-300MB)，为 GPU 模型腾出 VRAM。

---

## 云 Provider 预设

### 预设厂商列表

| 预设 | 图标 | Base URL | ENV 变量 | 模型发现 |
|------|------|----------|----------|----------|
| 百度千帆 Coding Plan | 🟦 | `qianfan.baidubce.com/v2/coding` | `IFF_BAIDU_QIANFAN_KEY` | ❌ Spec |
| 火山方舟 | 🌋 | `ark.cn-beijing.volces.com/api/v3` | `IFF_VOLCENGINE_KEY` | ✅ |
| 阿里百炼 | 🟦 | `dashscope.aliyuncs.com/compatible-mode/v1` | `IFF_ALI_BAILIAN_KEY` | ✅ |
| DeepSeek 官方 | 🐋 | `api.deepseek.com/v1` | `IFF_DEEPSEEK_KEY` | ✅ |
| 智谱AI | 🐋 | `open.bigmodel.cn/api/paas/v4` | `IFF_ZHIPU_KEY` | ✅ |
| Moonshot (Kimi) | 🌙 | `api.moonshot.cn/v1` | `IFF_MOONSHOT_KEY` | ✅ |
| OpenAI | 🟢 | `api.openai.com/v1` | `IFF_OPENAI_KEY` | ✅ |
| Anthropic | 🟠 | `api.anthropic.com/v1` | `IFF_ANTHROPIC_KEY` | ❌ Spec |
| 自定义 / 中转站 | 🔗 | (用户填写) | (用户填写) | ✅ |

百度千帆和 Anthropic 使用 **Spec 模式**（预定义模型列表，避免不稳定的 auto-discovery）；其余厂商使用 **Discovery 模式**（自动发现可用模型）。

### 通过 API 添加 Provider

```bash
# 预设模式（推荐）
curl -X POST http://localhost:8999/admin/cloud/providers \
  -H "Content-Type: application/json" \
  -d '{"preset":"deepseek","api_key":"sk-xxx"}'

# 手动模式
curl -X POST http://localhost:8999/admin/cloud/providers \
  -H "Content-Type: application/json" \
  -d '{"name":"my-relay","api_key":"sk-xxx","openai_base":"https://relay.example.com/v1"}'
```

### 通过 Dashboard 添加

1. 打开 `http://localhost:8999/` → Cloud 标签页
2. 点击预设卡片（如 DeepSeek 🐋）
3. 输入 API Key → 点击「添加 & 发现」
4. 模型自动注册到路由表

---

## API Key 安全模型

```
┌──────────────────────────────────────────────────────┐
│  用户输入明文 Key                                      │
│       │                                              │
│       ▼                                              │
│  SecretsManager.write(env_var, plaintext_key)         │
│       │  → 写入 ~/.inferfabric/secrets.env (chmod 600)│
│       │  → YAML 只存 ${ENV_VAR} 引用                  │
│       ▼                                              │
│  cloud_provider.yaml: api_key: ${IFF_DEEPSEEK_KEY}   │
│  secrets.env:        IFF_DEEPSEEK_KEY=sk-actual-key  │
│       │                                              │
│       ▼  Proxy 启动时                                 │
│  _inject_secrets_env() → os.environ.setdefault()      │
│       │                                              │
│       ▼                                              │
│  ${VAR} 展开为实际值，用于 API 调用                      │
└──────────────────────────────────────────────────────┘
```

**安全保证**:
- ✅ YAML 中永不存储明文 Key（自动转换为 `${ENV_VAR}` 引用）
- ✅ `secrets.env` 权限 600，仅所有者可读写
- ✅ `secrets.env` 不入 git（`.gitignore` 排除）
- ✅ `_inject_secrets_env()` 使用 `os.environ.setdefault()`，不覆盖已有 ENV
- ✅ `SecretsManager.write()` 线程安全（`threading.Lock`）
- ✅ POST 添加 Provider 时，先写 secrets.env 再建内存配置（crash-safe）

---

## 目录结构

```
~/inferfabric/
├── models.d/                        # 模型配置目录(插件式)
│   ├── qwen36-35b-vl.yaml           # gpu_role: exclusive, type: vllm, model_type: vl
│   ├── qwen36-27b-vl.yaml           # gpu_role: exclusive, type: vllm, model_type: vl
│   ├── qwen35-9b-vl.yaml            # gpu_role: shared, type: vllm, model_type: vl
│   ├── gemma4-31b-vl.yaml           # gpu_role: exclusive, type: vllm, model_type: vl
│   ├── qwen25-omni-3b.yaml          # gpu_role: shared, type: ollama_cpp, model_type: omni
│   ├── tts-qwen3.yaml               # gpu_role: shared, type: tts_server, model_type: tts
│   ├── ovis-ocr2.yaml               # gpu_role: shared, type: vllm, model_type: ocr
│   ├── bge-m3.yaml                  # gpu_role: none, type: ollama_cpp, model_type: embedding
│   ├── qwen3-embedding-0.6b.yaml    # gpu_role: none, type: ollama_cpp, model_type: embedding
│   ├── comfyui.yaml                 # gpu_role: shared, type: comfyui, model_type: aigc
│   ├── ollama-daemon.yaml           # gpu_role: none, type: ollama_daemon, model_type: infra
│   └── model_affinity.yaml          # 静态路由亲和性配置
├── inferfabric/
│   ├── config.py                    # ModelConfig + load_models() + 常量
│   ├── state.py                     # GPUMode + validate_transition + StateDB
│   ├── gpu_lock.py                  # GPULock (flock)
│   ├── health.py                    # HTTP/GPU 健康检查
│   ├── process_manager.py           # vLLM + ComfyUI + Ollama/llama.cpp 进程管理
│   ├── cloud_discovery.py           # 云端模型发现 + SecretsManager + 预设
│   ├── cloud_presets.yaml           # 9 个云 Provider 预设配置
│   ├── manager.py                   # ModelManager (编排层)
│   ├── cli.py                       # CLI
│   ├── proxy/
│   │   ├── handler.py               # HTTP 路由 + Dashboard + Cloud API
│   │   ├── chat_handlers.py         # Chat/Messages 转发
│   │   └── request_logger.py        # 请求日志
│   ├── dashboard/                   # Dashboard 静态资源
│   └── preload.py                   # 模型预加载 (实验性)
├── scripts/
│   └── iff-recovery.sh              # 紧急恢复
└── tests/
```

---

## CLI 参考

> **CLI 完全独立于 proxy**。即使 proxy 挂掉，CLI 仍可直接操作。

### `iff status`

```
GPU Mode : 🔒 exclusive
Services : ['qwen36-27b']
  qwen36-27b: ✅
PIDs     : vLLM PID=12345
GPU      : 29140/32607 MiB used
```

### `iff models`

```
Available Models (12):
name                 gpu_role     type          model_type   description
-----------------------------------------------------------------------------------------
bge-m3               none         ollama_cpp    embedding    BGE-M3 Q4_K_M CPU embedding
bge-reranker-v2-m3   none         ollama_cpp    rerank       BGE-Reranker-V2-M3 Q8_0 CPU reranker
comfyui              shared       comfyui       aigc         ComfyUI 图像生成
gemma4-31b-vl        exclusive    vllm          vl           Gemma4-31B IT NVFP4 Dense
ollama-daemon        none         ollama_daemon infra        Ollama 守护进程
ovis-ocr2            shared       vllm          ocr          OvisOCR2 0.8B 文档 OCR
qwen25-omni-3b       shared       ollama_cpp    omni         Qwen2.5 Omni 3B GPU
qwen35-9b-vl         shared       vllm          vl           Qwen3.5-9B GPTQ-4bit
qwen36-27b-vl        exclusive    vllm          vl           Qwen3.6-27B NVFP4 + MTP
qwen36-35b-vl        exclusive    vllm          vl           Qwen3.6-35B A3B MoE NVFP4
sensevoice-small     shared       asr_server    asr          FunASR SenseVoice + Paraformer-zh
tts-qwen3            shared       tts_server    tts          Qwen3-TTS 1.7B CustomVoice
```

### `iff switch <model_name|idle>`

遵守三态规则。独占模型全锁 GPU，共享模型允许共存。

### `iff stop <model_name>`

停止单个共享服务。其他共享服务保留。

### `iff reset`

强制重置到 idle。杀死所有服务进程，清空状态。

### `iff reconcile`

状态对账：扫描所有模型端口健康状态，修正 state.db 与实际运行的差异。

---

## 模型配置格式

### vLLM 模型（独占）

```yaml
# models.d/qwen36-35b-vl.yaml
name: qwen36-35b-vl
description: "Qwen3.6-35B A3B MoE NVFP4 + MTP"
gpu_role: exclusive
model_type: vl
quantization: NVFP4

type: vllm
vllm:
  model_dir: Qwen3.6-35B-A3B-NVFP4-MTP
  served_name: vllm_qwen35b
  port: 8000
  conda_env: qwen36-35b
  max_model_len: 128000
  gpu_memory_utilization: 0.90
  kv_cache_dtype: fp8
  speculative_config: '{"method": "mtp", "num_speculative_tokens": 3}'
  extra_flags: >-
    --max-num-batched-tokens 8192
    --enable-prefix-caching
    --trust-remote-code
```

### Ollama/llama.cpp 模型（共享 / CPU）

```yaml
# models.d/qwen25-omni-3b.yaml
name: qwen25-omni-3b
description: "Qwen2.5 Omni 3B Q8_0 — Ollama.cpp GPU shared"
gpu_role: shared
model_type: omni
peak_vram_mb: 3800

type: ollama_cpp
ollama:
  model_path: qwen2.5-omni-3b:q8_0
  port: 8035
  gpu_layers: -1           # -1 = 全部层 offload 到 GPU
```

```yaml
# models.d/bge-m3.yaml
name: bge-m3
description: "BGE-M3 Q4_K_M — Ollama.cpp CPU embedding"
gpu_role: none
model_type: embedding

type: ollama_cpp
ollama:
  model_path: bge-m3:q4_k_m
  port: 8036
  gpu_layers: 0            # 纯 CPU，不占 VRAM
  extra_flags: --embedding
```

### TTS 服务

```yaml
# models.d/tts-qwen3.yaml
name: tts-qwen3
description: "Qwen3-TTS 1.7B CustomVoice"
gpu_role: shared
model_type: tts

type: tts_server
port: 8040
conda_env: tts-qwen3
```

### ComfyUI

```yaml
# models.d/comfyui.yaml
name: comfyui
description: "ComfyUI 图像生成"
gpu_role: shared
model_type: aigc

type: comfyui
conda_env: comfyui
port: 8188
working_dir: ~/ComfyUI
health_url: http://localhost:8188/system_stats
```

---

## OpenClaw / Claude Code 集成

InferFabric Proxy (`:8999`) 作为 OpenClaw 的统一后端，支持 OpenAI 和 Anthropic 两种协议格式的自动化路由。

### 路由架构

```
客户端 (OpenClaw / Claude Code / Codex)
        │
        ├── POST /v1/chat/completions ──→ 本地 vLLM / 云端 OpenAI-compatible
        │
        ├── POST /v1/messages ──→ 本地 vLLM / 百度千帆 Anthropic fallback
        │
        ├── POST /v1/embeddings ──→ 本地 bge-m3 (CPU llama.cpp)
        │
        └── GET  /v1/models ──→ 聚合本地 + 云端模型列表
```

### AUTO_SWITCH 行为

| 场景 | `AUTO_SWITCH=1` (默认) | `AUTO_SWITCH=0` |
|------|-------------------------|------------------|
| 请求不活跃的模型 | 自动切换模型，等待健康检查 | 返回 404 |
| 模型已在运行 | 直接转发 | 直接转发 |
| 切换进行中 | 返回 409 | 返回 409 |

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EDGE_PROXY_HOST` | `127.0.0.1` | 代理监听地址 |
| `EDGE_PROXY_PORT` | `8999` | 代理端口 |
| `EDGE_AUTO_SWITCH` | `1` | 是否自动切换模型 |
| `EDGE_HEALTH_CHECK` | `60` | 健康检查间隔（秒） |
| `IFF_ADMIN_TOKEN` | (空) | 管理端点鉴权 Token |
| `BAIDU_MESSAGES_BASE` | `https://qianfan.baidubce.com/anthropic/coding/v1` | Anthropic fallback |

---

## Proxy API 端点

### 核心路由

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Dashboard |
| GET | `/health` | Proxy 健康检查 |
| GET | `/status` | 完整状态 JSON |
| GET | `/models` | 可用模型列表 |
| POST | `/v1/chat/completions` | OpenAI Chat 转发 |
| POST | `/v1/messages` | Anthropic Messages 转发 |
| POST | `/v1/embeddings` | Embedding 请求转发 |

### 管理端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/switch` | 切换模型 `{"model": "qwen36-27b"}` |
| POST | `/stop` | 停止单个服务 `{"model": "comfyui"}` |
| POST | `/reset` | 强制重置 |
| POST | `/reconcile` | 状态对账 |
| GET | `/admin/cloud/presets` | 云 Provider 预设列表 |
| GET | `/admin/cloud/providers` | 已添加 Provider 列表 + 模型 + ENV 状态 |
| POST | `/admin/cloud/providers` | 添加 Provider (预设/手动) |
| DELETE | `/admin/cloud/providers` | 删除 Provider |
| POST | `/admin/cloud/discover` | 手动触发模型发现 |
| POST | `/admin/cloud/test` | 测试 Provider 连接 |

---

## 故障恢复

```bash
# 锁冲突
rm -f /tmp/inferfabric_gpu.lock && iff reconcile

# 进程卡死
iff reset

# GPU 显存不释放
~/inferfabric/scripts/iff-recovery.sh --full

# state.db 损坏
rm -f ~/.inferfabric/state.db && iff reconcile

# Proxy 无响应
iff status              # CLI 独立于 proxy
iff switch idle         # 强制释放
python3 -m inferfabric serve  # 重启 proxy
```

---

## 版本历史

| 版本 | 日期 | 关键变更 |
|------|------|----------|
| v1.0 | 2026-06-25 | bash-only switch_vllm.sh |
| v2.0 | 2026-06-27 | Python 重写, 8 个 bug 修复 |
| v3.0 | 2026-06-28 | 进程组管理、状态机、三态健康检查 |
| v3.1 | 2026-06-28 | 模块化拆分、ComfyUI 原生管理 |
| v3.2 | 2026-06-28 | Proxy 稳健重写、systemd watchdog |
| **v4.0** | **2026-06-28** | **模型即插件、三态 GPU 状态机、消除 Profile、models.d/ 目录** |
| v4.1 | 2026-07-01 | 双引擎负载均衡、流式管道修复 |
| v4.2 | 2026-07-02 | AICF 管线集成、Flux Dev 切换 |
| v4.3 | 2026-07-03 | CCR 架构 Anthropic Messages、模块化拆分 |
| v4.4 | 2026-07-04 | Stability+ — 线程安全锁、连接泄漏审计 |
| v4.5 | 2026-07-04 | Semaphore rate limiter、vLLM 过载保护 |
| v4.6 | 2026-07-15 | Cloud Discovery — 云端模型发现、Provider 管理、Dashboard |
| **v4.7** | **2026-08-04** | **Cloud Presets 预设厂商、API Key ENV 安全模型、SecretsManager、TTS 模型支持、llama.cpp CPU/CUDA 双构建** |

---

## 技术笔记

### TMA Patch（vLLM 兼容 RTX 5090D）

**根因**: `matmul_ogs.py` 中 `CC[0] > 9` 在 RTX 5090D (CC 12.0) 误启 TMA → OOM

**修复**: `CC[0] > 9` → `CC[0] > 9 and CC[0] < 12`

```bash
for env in qw36-27b-vllm qw35-9b-vllm gm4-26b-vllm; do
  FILE=~/miniconda3/envs/$env/lib/python3.11/site-packages/vllm/third_party/triton_kernels/matmul_ogs.py
  cp "$FILE" "$FILE.bak"
  sed -i 's/can_use_tma = can_use_tma and (torch.cuda.get_device_capability()\[0\] > 9 or bitwidth(w.dtype) != 4)/cc = torch.cuda.get_device_capability()\n    can_use_tma = can_use_tma and ((cc[0] > 9 and cc[0] < 12) or bitwidth(w.dtype) != 4)/' "$FILE"
done
```

**注意**: pip upgrade 会覆盖 patch，需重新打。vLLM 0.26+ 已修复此问题。
