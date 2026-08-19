# InferFabric — LLM Inference Gateway

> **Model as Plugin. One YAML, one model. Local + Cloud, unified.**
>
> 单卡 GPU 推理网关 · 模型即插件 · 三态 GPU 状态机 · macOS Dashboard · 9 云端预设

---

## What It Solves

**The Problem**: Running multiple LLM models on a single GPU is painful. Model switching is manual. Cloud API keys are scattered in config files. Each client needs its own backend configuration.

**InferFabric solves this**:

- **Model switching without OOM** — Three-state GPU (idle/exclusive/shared) with safe transitions and health checks
- **One API, any model** — Every model—local vLLM, local llama.cpp, or cloud OpenAI/Anthropic—is accessed through the same `/v1/chat/completions` endpoint
- **API keys never in plaintext** — `${ENV_VAR}` auto-conversion, secrets stored in `chmod 600` file
- **Dashboard, not YAML editing** — macOS sidebar dashboard for model switching, monitoring, chat testing, and cloud provider management
- **Auto-switch on demand** — Request a model and IFF boots it if needed; no manual intervention
>
> macOS Dashboard · Three-state GPU · OpenAI/Anthropic dual-protocol routing · 9 cloud presets

---

## What is InferFabric?

InferFabric is a single-GPU LLM inference gateway. It treats every model—local or cloud—as a pluggable unit, managing their lifecycle, routing, and discovery behind a unified OpenAI-compatible API.

```
┌──────────────────────────────────────────────────────┐
│  OpenClaw · Claude Code · Codex · AtomCode            │
│  OpenAI / Anthropic protocol                         │
└────────────────────┬─────────────────────────────────┘
                     │  :8999
┌────────────────────▼─────────────────────────────────┐
│              InferFabric Proxy                        │
│  ┌──────────────┐  ┌───────────┐  ┌───────────────┐  │
│  │ Local Router │  │ Cloud Mgr │  │ macOS Dashboard│  │
│  │ vLLM/SGLang  │  │ 9 Presets │  │ Sidebar + Chat │  │
│  │ Ollama/Comfy │  │ Auto-Disc │  │ Live Metrics   │  │
│  └──────────────┘  └───────────┘  └───────────────┘  │
└────────────────────┬─────────────────────────────────┘
        │                            │
   ┌────▼────┐                ┌──────▼──────┐
   │  vLLM   │                │ 百度千帆 🟦  │
   │  SGLang  │                │ DeepSeek 🐋 │
   │  Ollama  │                │ OpenAI  🟢  │
   │  ComfyUI │                │ Anthropic🟠 │
   └─────────┘                └─────────────┘
     Local Models              Cloud Providers
```

---

## Architecture

### Three-State GPU Model

```
idle ─→ exclusive   (one heavy model, full GPU)
  │                                    
  └──→ shared       (many small models, coexist)
         │                              
         └──→ idle   (return to idle anytime)
```

Local models operate in one of three GPU modes. The gateway enforces safe transitions—you can't accidentally start two exclusive models.

### Model as Plugin

Add a model = drop a YAML file. No code. No profile keys.

```yaml
# models.d/gemma4-31b-vl.yaml
name: gemma4-31b-vl
gpu_role: exclusive
model_type: vl
type: vllm
vllm:
  port: 8005
  conda_env: gemma-4-31b-vllm
  max_model_len: 131072
  gpu_memory_utilization: 0.90
```

### Dual-Protocol Routing

All requests arrive as standard OpenAI `/v1/chat/completions`. The gateway:

1. Checks if the model is a local service → routes to vLLM/SGLang port
2. Checks cloud provider route → proxies through provider API (OpenAI/Anthropic compatible)
3. If AUTO_SWITCH is on and the model is configured but stopped → starts it automatically

No client configuration changes needed. You ask for a model, InferFabric figures out where it lives.

### Cloud Provider Presets

9 pre-configured cloud providers with one-click setup:

| Provider | Discovery | Protocol |
|----------|-----------|----------|
| 百度千帆 Coding Plan | Spec | OpenAI + Anthropic |
| 火山方舟 | Auto | OpenAI |
| 阿里百炼 | Auto | OpenAI |
| DeepSeek | Auto | OpenAI |
| 智谱AI | Auto | OpenAI |
| Moonshot (Kimi) | Auto | OpenAI |
| OpenAI | Auto | OpenAI |
| Anthropic | Spec | Anthropic |
| Custom Relay | Manual | OpenAI + Anthropic |

API Keys are never stored in plaintext—automatically converted to `${ENV_VAR}` references and persisted in a `chmod 600` secrets file.

---

## Dashboard (v5.4.0)

A macOS-inspired sidebar dashboard for model management, monitoring, and chat testing:

```
┌──────┬──────────────────────────────────────────┐
│  🚀  │  GPU 显存  GPU 负载  系统内存  CPU 负载   │  ← Sticky metrics
│      │──────────────────────────────────────────│
│ 推理  │  ┌─ 独占模型 ───── 3 ─────────────────┐  │
│      │  │ ┌──┐ gemma4  [独占]                 │  │
│      │  │ │🔥│ ✅ running :8008               │  │
│ 监控  │  │ └──┘ NVFP4  128K  vLLM  [释放]    │  │
│      │  └─────────────────────────────────────┘  │
│ 部署  │                                           │
│      │  ┌─ Chat ──────────────────────────────┐  │
│ 云端  │  │ 模型 ▼  Max  Temperature    [Send]  │  │
│      │  │ 💬 iMessage-style chat bubbles      │  │
│      │  └─────────────────────────────────────┘  │
└──────┴──────────────────────────────────────────┘
```

**Features**: Sidebar navigation, live 4-metric bar, model card grid with icon boxes, vLLM performance panels, token usage charts, cloud provider management, dark mode, chat inference panel with local + cloud model support.

---

## Quick Start

```bash
# View all models
iff status

# Switch to a model (auto-start if stopped)
iff switch gemma4-31b-vl

# Return to idle
iff switch idle

# Dashboard at http://localhost:8999
```

---

## API Endpoints

### Core

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | macOS Dashboard |
| `GET`  | `/status` | GPU state, active services, health |
| `GET`  | `/models` | All configured models |
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat |
| `POST` | `/v1/messages` | Anthropic-compatible messages |
| `POST` | `/v1/embeddings` | Embedding requests |

### Control

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/switch` | Switch model `{"model":"gemma4-31b-vl"}` |
| `POST` | `/stop` | Stop a shared service |
| `POST` | `/reset` | Force reset to idle |
| `POST` | `/reconcile` | Fix state.db vs reality |

### Cloud Admin

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/admin/cloud/presets` | Provider presets |
| `GET/POST/DELETE` | `/admin/cloud/providers` | Manage providers |
| `POST` | `/admin/cloud/discover` | Discover models |
| `POST` | `/admin/cloud/test` | Test connection |

---

## Port Map

| Port | Service | Type |
|------|---------|------|
| 8001 | qwen38-27b-vl | vLLM |
| 8005 | gemma4-31b-vl | vLLM |
| 8002 | qwen3-vl-4b | vLLM |
| 8004 | ovis-ocr2 | vLLM |
| 8188 | comfyui | ComfyUI |
| 8880 | tts-qwen3 | TTS |
| 8881 | sensevoice-small | ASR |
| 11441 | bge-m3 | ollama.cpp |
| 11442 | bge-reranker | ollama.cpp |
| 8999 | **Proxy** | HTTP |

---

## Recovery

```bash
iff reset                          # Force idle
iff reconcile                      # Fix state.db
~/inferfabric/scripts/iff-recovery.sh --full  # Nuclear
```

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| v4.0 | 2026-06 | Model plugin architecture, three-state GPU |
| v4.6 | 2026-07 | Cloud discovery, provider management, Dashboard |
| v4.7 | 2026-08 | Cloud presets, API key security, TTS support |
| **v5.4.0** | **2026-08** | **macOS Dashboard: sidebar, chat, 12 SVG icons, dark mode** |

---

## Hardware

- **GPU**: NVIDIA GeForce RTX 5090D, 32 GB GDDR7
- **RAM**: 64 GB DDR5
- **OS**: Ubuntu 25.04, Python 3.12+

---

[InferFabric](https://github.com/vincentlau2046-sudo/InferFabric) · MIT License