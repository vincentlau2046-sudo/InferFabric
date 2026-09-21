# InferFabric — 单卡 AI 推理操作系统

> **把你的 GPU 工作站变成一台统一推理服务器。模型即插件，本地+云端统一，一个 API 管所有。**
>
> 单卡 GPU 推理操作系统 · 模型即插件 · 三态 GPU 状态机 · 8 引擎适配器 · macOS Dashboard · 9 云端预设 · 双协议路由

---

## 定位

**InferFabric 是面向单卡 GPU 工作站的个人 AI 推理操作系统。**

它不是 API 网关（有状态——管理进程生命周期），不是 vLLM 包装器（多引擎适配器抽象），不是本地推理工具（统一管理本地+云端+多模态）。它把你的 GPU 从零散的推理环境变成一个**可编程的统一推理服务**。

核心差异化：

| 维度 | InferFabric | 替代方案 |
|---|---|---|
| 单卡多模型 | 三态 GPU 状态机（idle/exclusive/shared），自动切换，永不 OOM | 手动停旧启新，或一个模型占死 GPU |
| API 入口 | OpenAI + Anthropic 双协议同一端口 `:8999` | 每个引擎一个端口、一种格式 |
| 模型定义 | 一个 YAML = 一个模型（零代码） | 写启动脚本、配 conda env |
| 推理引擎 | 9 种适配器：vLLM/NInfer/SGLang/Ollama/Ollama.cpp/ComfyUI/TTS/ASR/OllamaDaemon | 一种引擎一套工具链 |
| 云端代理 | 9 个预设 + 自动发现 + 同一 API 路由 | 每个云商一套 SDK/Key 管理 |
| 治理 | 重试/熔断/缓存/限流/超时/负载均衡/异常采集/全链路日志 | 无（直连引擎） |
| 管理界面 | macOS 风格 Dashboard（监控/切换/聊天/部署/OpenAPI 阅览） | 终端 curl 或 YAML 编辑 |

**设计哲学**：吃自己的狗粮。InferFabric 把本地模型暴露为 Anthropic 兼容 API——你可以把 `ANTHROPIC_BASE_URL` 指回本地 `localhost:8999`，用 Claude Code 驱动开发同时验证推理网关的兼容性和性能。反馈闭环即产品验证。**自 5.8.0 起是一条命令加 `--async` 即可启用生产级 aiohttp 异步引擎。**

---

## What It Solves

**The Problem**: Running multiple LLM models on a single GPU is painful. Model switching is manual. Cloud API keys are scattered in config files. Each client needs its own backend configuration.

**InferFabric solves this**:
- **Model switching without OOM** — Three-state GPU (idle/exclusive/shared) with safe transitions and health checks
- **One API, any model** — Every model—local vLLM/NInfer, local ollama.cpp, or cloud OpenAI/Anthropic—is accessed through the same `/v1/chat/completions` or `/v1/messages` endpoint
- **API keys never in plaintext** — `${ENV_VAR}` auto-conversion, secrets stored in `chmod 600` file
- **Dashboard, not YAML editing** — macOS sidebar dashboard for model switching, monitoring, chat testing, and cloud provider management
- **Dual-mode proxy** — Threaded (stdlib, default) or async (aiohttp, `--async`), both with zero forwarding-core change

---

## Architecture

### 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  CLI (./iff)                     Dashboard (:8999/)             │
├─────────────────────────────────────────────────────────────────┤
│  Proxy (:8999) — OpenAI + Anthropic 双协议                       │
│  ├─ 线程模式 (http.server, 默认)                                  │
│  └─ 异步模式 (aiohttp, --async, v5.8.0+ PR-19)                  │
│  ├─ Auth      → API key 验证                                    │
│  ├─ Cache     → temperature=0 响应缓存 (cachetools LRU)          │
│  ├─ RateLimit → DualGateLimiter (RPM + 并发)                     │
│  ├─ Anomaly   → AnomalyCollector 环形缓冲 (R9)                   │
│  ├─ Telemetry → RequestLog + Prometheus /metrics (R6)           │
│  └─ Replicas  → ReplicaSelector least_busy/round_robin (R7)    │
├─────────────────────────────────────────────────────────────────┤
│  Engine Adapter Layer — 模型类型无关的启动/停止/健康检查           │
│  ┌────────┬──────┬────────┬──────┬──────┬──────┬─────┬─────┐   │
│  │ vLLM   │NInfer│ SGLang │Ollama│ cpp  │Comfy │ TTS │ ASR │   │
│  └────────┴──────┴────────┴──────┴──────┴──────┴─────┴─────┘   │
├─────────────────────────────────────────────────────────────────┤
│  Process Managers — Docker / conda / subprocess 生命周期         │
├─────────────────────────────────────────────────────────────────┤
│  GPU 状态机 — idle → exclusive/shared → idle                    │
│  HealthMonitor — 去耦合健康检查 + 状态修正                       │
│  Watchdog — 模型异常自动重启                                     │
└─────────────────────────────────────────────────────────────────┘
```

### 引擎适配器（Engine Adapter）

每个 `type` 映射一个 `engine_adapter/*.py`，在 `__init__.py` 中注册：

| 引擎类型 | 适配器 | 启动方式 | 典型用途 |
|---------|--------|---------|----------|
| `vllm` | `vllm.py` | conda env + subprocess | 大模型推理（LLM/VL） |
| `ninfer` | `ninfer.py` | Docker container | NInfer 优化推理（NVFP4） |
| `sglang` | `sglang.py` | Docker/conda | SGLang 推理（RadixAttention） |
| `ollama` | `ollama.py` | HTTP to Ollama daemon | Ollama 模型 |
| `ollama_cpp` | `ollama_cpp.py` | subprocess (GGUF) | CPU 嵌入/重排序 |
| `comfyui` | `comfyui.py` | conda env + subprocess | 图像生成 |
| `tts_server` | `tts.py` | conda env + subprocess | TTS 语音合成 |
| `asr_server` | `asr.py` | subprocess | ASR 语音识别 |

每个适配器实现 `EngineAdapter` 接口（`base.py`）：
```python
def start(self, model: ModelConfig) -> int: ...
def stop(self, model: ModelConfig) -> bool: ...
def health_check(self, model: ModelConfig) -> bool: ...
def validate(self, config: dict) -> list[str]: ...
```

### 三态 GPU 状态机

```
idle ─→ exclusive   (one heavy model, full GPU)
  │
  └──→ shared       (many small models, coexist)
         │
         └──→ idle   (return to idle anytime)
```

Local models operate in one of three GPU modes. The gateway enforces safe transitions—you can't accidentally start two exclusive models.

The state machine is computed from actual service processes rather than a persisted flag, so **state never drifts**. HealthMonitor decoupled from state reconciliation eliminates race conditions.

### 代理路由链

所有请求到达 `:8999` 后经过统一的路由链：

1. **Auth** — API key 验证（YAML 密钥文件）
2. **Cache** — ResponseCache 精确匹配（`temperature=0` 且非流式）
3. **SWITCHING Guard** — 切换中模型 → 503 + Retry-After
4. **Local routing** — `find_model_by_served_name()` → 活跃模型直接转发，非活跃 auto-switch
5. **Multi-replica** — ReplicaSelector (`least_busy` / `round_robin`)
6. **Cloud routing** — CloudDiscovery → 零协议透明代理
7. **Unknown model** — 显式 404 + AnomalyEvent（无静默回退）

上游路径归一化（v5.8.0+，由 YAML `type` 字段自动驱动）：

| 入站路径 | 归一化目标 | 说明 |
|---------|-----------|------|
| `/v1/chat/completions` | `/v1/chat/completions` | OpenAI 标准 |
| `/v1/completions` | → `/v1/chat/completions` | 别名 |
| `/api/chat` | → `/v1/chat/completions` | Ollama 兼容别名 |
| `/api/generate` | → `/v1/chat/completions` | Ollama 兼容别名 |
| `/v1/messages` | `/v1/messages` | Anthropic 标准 |

### 双引擎模式（v5.8.0+）

| | 线程模式（默认） | 异步模式（`--async`） |
|---|---|---|
| 底层 | `http.server.ThreadedHTTPServer` | aiohttp 3.14.3 (`_deps/`) |
| 并发模型 | 每请求一线程 | 事件循环 + ThreadPoolExecutor(32) |
| 流式 SSE | 手工 chunked 分帧 | StreamResponse 原生分帧 |
| Connection | `Connection: close` | keep-alive（aiohttp 自管） |
| 回退 | — | 不传 `--async` 即回退线程版 |

---

## 模型即插件（YAML 插件系统）

添加模型 = 在 `models.d/` 放一个 YAML 文件。零代码。零配置键。

### 通用字段

```yaml
name: my-model               # ✅ 必填，必须与文件名（不含扩展名）一致
description: "..."           # 可选，人类可读描述
type: vllm                   # ✅ 必填，引擎类型（见下表）
gpu_role: exclusive          # exclusive | shared | none
model_type: llm              # llm | vl | omni | ocr | aigc | embedding | rerank | infra | tts | asr
quantization: NVFP4          # 量化格式字符串
peak_vram_mb: 30000          # 峰值显存，用于 OOM 保护
typical_vram_pct: 0          # 典型显存占比（0-100，comfyui 用）
modality: text-vision        # 可选，推导自 model_type
replicas: []                 # R7: 多副本端口列表
startup_timeout: 300         # 启动超时秒数
```

### 引擎类型 YAML 参考

#### vLLM

```yaml
type: vllm
gpu_role: exclusive
vllm:
  model_dir: my-model-name       # models/ 下的目录名
  served_name: my-model          # API 路由用的模型名
  port: 8005                     # 监听端口
  conda_env: my-conda-env        # conda 环境名
  gpu_memory_utilization: 0.92   # GPU 显存利用率
  max_model_len: 131072          # 最大上下文长度
  max_num_seqs: 4                # 并行请求数
  kv_cache_dtype: fp8            # KV 缓存精度
  kv_offloading_size: 0          # KV offload 大小（GB）
  extra_flags: >-                # 额外 vLLM 参数
    --enable-prefix-caching
    --enable-chunked-prefill
  extra_env:                     # 额外环境变量
    FLASHINFER_DISABLE_VERSION_CHECK: "1"
  model_id: my-model             # Docker multi-model 时的模型 ID
```

#### NInfer

```yaml
type: ninfer
gpu_role: exclusive
ninfer:
  served_name: Qwen38-27B-TXT
  weight_path: ~/models/ninfer-nvfp4/model.ninfer  # 权重路径
  docker_image: ninfer:auto                          # Docker 镜像
  port: 8007
  container_name: iff-ninfer-my-model
  max_context: 204800           # 最大上下文
  kv_capacity: 0                # KV 容量（0 = auto）
  kv_dtype: nvfp4               # KV 缓存精度
  weight_precision: NVFP4       # 权重精度
  max_concurrency: 4            # 最大并发
  default_max_tokens: 32000     # 默认最大输出 Token
  prefill_chunk: 4096           # Prefill 分块大小
  pending_timeout_ms: 600000    # 请求排队超时
  enable_mtp: true              # MTP 投机解码
  draft_tokens: 4               # 草案 Token 数
  enable_lm_head_draft: false   # LM Head Draft
  startup_timeout: 120
```

#### SGLang

```yaml
type: sglang
gpu_role: exclusive
sglang:
  model_dir: Muse-Glimmer-NVFP4
  served_name: muse-glimmer
  port: 8006
  conda_env: ""                 # 空 = 用 Docker
  docker_image: lmsysorg/sglang:dev-muse-glimmer
  mem_fraction: 0.90            # GPU 显存比例
  context_length: 163840        # 最大上下文
  max_running_requests: 8       # 最大并行请求
  cpu_offload_gb: 16            # CPU offload
  enable_lmcache: true          # 启用 LMCache
  extra_env:
    SGLANG_DISABLE_CUDA_GRAPH: "0"
```

#### Ollama

```yaml
type: ollama
gpu_role: shared
ollama:
  model_name: llama3.2          # Ollama 模型名
  port: 11434                   # Ollama 守护进程端口
```

> Ollama 需要先启动 `ollama_daemon` 基础设施服务。

#### Ollama.cpp

```yaml
type: ollama_cpp
gpu_role: none                  # CPU-only
ollama_cpp:
  model_path: ~/models/gguf/model.gguf  # GGUF 文件路径
  port: 11441
  threads: 8
  context_size: 8192
  gpu_layers: 0                 # GPU 层数（0 = 纯 CPU）
```

#### ComfyUI

```yaml
type: comfyui
gpu_role: shared
conda_env: comfyui              # 引擎通用字段
port: 8188
working_dir: ~/ComfyUI
health_url: http://localhost:8188/system_stats
health_check_timeout: 180
extra_flags: --enable-manager
```

> ComfyUI 不使用嵌套的 engine-specific 字典，字段在顶层。

#### TTS Server

```yaml
type: tts_server
gpu_role: shared
tts_server:
  conda_env: qwen3-tts
  port: 8880
  working_dir: ~/services/TTS-Server
  health_url: http://localhost:8880/health
  health_check_timeout: 180
  start_cmd: python -m api.main
  extra_env:
    TTS_BACKEND: official
    TTS_LOAD_ALL_MODELS: "true"
```

#### ASR Server

```yaml
type: asr_server
gpu_role: shared
asr_server:
  conda_env: sensevoice
  port: 8881
  working_dir: ~/services/funasr-asr
  health_url: http://localhost:8881/health
  health_check_timeout: 120
  start_cmd: funasr-server --model sensevoice --device cuda --port 8881
  extra_env:
    MODELSCOPE_CACHE: ~/models/funasr
```

#### Ollama Daemon（基础设施）

```yaml
type: ollama_daemon
gpu_role: none
ollama_daemon:
  port: 11434
  health_url: http://localhost:11434
  data_dir: ~/.ollama
```

> 这是一个基础设施服务，不消耗 GPU 资源，为 `type: ollama` 模型提供后端。

---

## Cloud Provider Presets

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

## Dashboard (v5.5.x)

A macOS-inspired sidebar dashboard for model management, monitoring, and chat testing:

| | |
|:---:|:---:|
| **Overview — GPU metrics + model cards** | **Cloud providers management** |
| ![Dashboard Overview](docs/screenshots/00-dashboard-overview.png) | ![Cloud Providers](docs/screenshots/02-cloud-providers.png) |
| **Chat inference panel** | **Metrics & monitoring** |
| ![Chat Panel](docs/screenshots/03-chat-panel.png) | ![Metrics](docs/screenshots/04-metrics.png) |
| **GPU status & vLLM performance** | |
| ![GPU Status](docs/screenshots/05-gpu-status.png) | |

**Features**:
- Sidebar navigation (推理/监控/云端/部署/Chat)
- Live 4-metric bar: GPU memory · GPU load · System memory · CPU load
- Model card grid with macOS-icon-box layout, status badges, start/stop controls
- vLLM performance panels: token throughput, latency distribution, KV cache usage
- Token usage charts with time-series visualization
- Cloud provider management: CRUD, auto-discover, connection test
- Chat inference panel with local + cloud model support, streaming responses
- Dark mode, typography hierarchy, WCAG AA contrast
- OpenAPI spec viewer (📖 link in top bar)

---

## 安装

### 依赖

InferFabric 是单文件 Python 应用，运行时依赖 `aiohttp` + `cachetools`（及 aiohttp 生态的传递依赖）。`requirements.txt` 已 pin 全部版本。

```bash
git clone https://github.com/vincentlau2046-sudo/InferFabric.git
cd InferFabric
pip install -r requirements.txt
```

> **本地开发（vendored 依赖）：** 仓库根目录的 `_deps/`（未提交到 git）是 vendored 依赖快照。若存在，`proxy_manager` 启动时会优先把它插到 `sys.path` 前面，**无需 pip install**。克隆获取不到 `_deps/`，请用上面的 `pip install -r requirements.txt`。

### Python 与 GPU 环境

- **Python 3.10+**
- **vLLM 0.24**（不要升级——适配器针对此版本调优）
- NVIDIA GPU + CUDA（推理引擎自身依赖，非 InferFabric 直接依赖）
- 各引擎按需安装：vLLM / NInfer / SGLang / Ollama / ComfyUI 等

### 启动

```bash
./iff status                                    # CLI（线程模式，stdlib http.server）
python3 -m inferfabric.proxy.handler --async    # 生产异步引擎（aiohttp, :8999）
# Dashboard: http://localhost:8999
```

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

# Start with async engine (v5.8.0+)
python3 -m inferfabric.proxy.handler --async
```

---

## API Endpoints

### Core

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | macOS Dashboard |
| `GET` | `/health` | Simple health `{"status":"ok"}` |
| `GET` | `/status` | GPU state, active services, health |
| `GET` | `/models` | All configured models |
| `GET` | `/v1/models` | OpenAI-compatible model list |
| `GET` | `/system` | System info (CPU, RAM, uptime) |
| `GET` | `/api/snapshot` | Full state snapshot (state + models + history + token stats) |
| `GET` | `/api/metrics` | Aggregated request metrics (JSON, 24h window) |
| `GET` | `/metrics` | Prometheus text format (R6) |
| `GET` | `/api/request_log` | Request log history (R1) |
| `GET` | `/api/token-stats` | Historical token usage |
| `GET` | `/api/token-curve` | Token curve data |
| `GET` | `/api/anomalies` | Structured anomaly events (R9) |
| `GET` | `/engine_metrics` | Engine-level metrics (with `?model=`) |
| `GET` | `/watchdog_status` | Watchdog fail counts + running state |
| `GET` | `/history` | Switch history (last 30) |
| `GET` | `/api/openapi.json` | OpenAPI 3.1 specification |
| `GET` | `/vllm_metrics` | vLLM-specific metrics |

### Inference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat |
| `POST` | `/v1/completions` | → alias for `/v1/chat/completions` |
| `POST` | `/v1/messages` | Anthropic-compatible messages |
| `POST` | `/api/chat` | → alias for `/v1/chat/completions` |
| `POST` | `/api/generate` | → alias for `/v1/chat/completions` |
| `POST` | `/v1/embeddings` | Embedding requests |
| `POST` | `/v1/rerank` | Reranking requests |

### Control (admin: X-Admin-Token or localhost)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/switch` | Switch model `{"model":"gemma4-31b-vl"}` |
| `POST` | `/stop` | Stop a shared service |
| `POST` | `/reset` | Force reset to idle |
| `POST` | `/reconcile` | Fix state.db vs reality |
| `POST` | `/reload-config` | SIGHUP hot-reload (config + cloud) |
| `POST` | `/sleep` | L2 sleep: discard weights, rapid wake |
| `POST` | `/wake` | Wake a sleeping model |
| `POST` | `/deploy` | Deploy a new model |
| `POST` | `/pull` | Pull a remote model |
| `POST` | `/admin/cache/toggle` | Toggle response cache on/off |
| `POST` | `/admin/gpu-clear` | Clear GPU CUDA state |

### Cloud Admin (admin)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/cloud/presets` | Provider presets list |
| `GET` | `/admin/cloud/providers` | List providers |
| `POST` | `/admin/cloud/providers` | Add provider |
| `DELETE` | `/admin/cloud/providers` | Remove provider |
| `POST` | `/admin/cloud/reload` | Reload cloud config from disk |
| `POST` | `/admin/cloud/discover` | Run discovery now |
| `POST` | `/admin/cloud/test` | Test provider connection |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EDGE_PROXY_HOST` | `127.0.0.1` | Proxy bind address |
| `EDGE_PROXY_PORT` | `8999` | Proxy listen port |
| `EDGE_AUTO_SWITCH` | `0` | Auto-switch on request (1 = enable) |
| `EDGE_HEALTH_CHECK` | `60` | Health check interval (seconds) |
| `EDGE_ASYNC_WORKERS` | `32` | Async mode executor threads (PR-19) |
| `IFF_ADMIN_TOKEN` | `""` | Admin route auth token (empty = localhost-only) |
| `IFF_CACHE_ENABLED` | (YAML) | Override cache enabled (via env) |
| `IFF_DATA_DIR` | `~/.inferfabric` | Data directory (state.db, logs, secrets, cloud_provider.yaml) |
| `NOTIFY_SOCKET` | — | systemd sd_notify socket path |
| `CLOUD_PROVIDER_KEY_*` | — | Per-provider API key env vars (auto-detected) |

---

## Port Map（当前部署）

| Port | Service | Engine | GPU Role |
|------|---------|--------|----------|
| 8001 | qwen38-27b-vl | vLLM | exclusive |
| 8002 | qwen3-vl-4b | vLLM | shared |
| 8003 | qwen3-vl-4b-prefill | vLLM | shared |
| 8004 | ovis-ocr2 | vLLM | shared |
| 8005 | gemma4-31b-vl | vLLM | exclusive |
| 8006 | muse-glimmer-vl | SGLang | exclusive |
| 8007 | Qwen38-27B-TXT | NInfer | exclusive |
| 8008 | qwen36-35b-vl | vLLM | exclusive |
| 8188 | comfyui | ComfyUI | shared |
| 8880 | tts-qwen3 | TTS | shared |
| 8881 | asr-sensevoice | ASR | shared |
| 11434 | ollama-daemon | Ollama | none |
| 11441 | bge-m3 | Ollama.cpp | none |
| 11442 | bge-reranker-v2-m3 | Ollama.cpp | none |
| **8999** | **Proxy** | **HTTP** | **—** |

---

## CLI Commands

```bash
./iff status              # GPU state + active services
./iff models              # List all models in models.d/
./iff switch <model|idle> # Switch model (auto-starts if stopped)
./iff stop <model>        # Stop a shared service
./iff reset               # Force reset to idle
./iff reconcile           # Fix state.db vs reality
./iff history             # Switch history
./iff pull <url>          # Pre-download model
./iff list-downloaded     # List downloaded models
./iff sleep <model>       # L2 sleep: discard weights, wake in ~3-6s
./iff wake <model>        # Wake a sleeping model
```

---

## Recovery

```bash
./iff reset                          # Force to idle
./iff reconcile                      # Fix state.db
bash scripts/iff-recovery.sh --full  # Nuclear: SIGKILL all + nvidia-smi -gpu-reset
```

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| v4.0 | 2026-06 | Model plugin architecture, three-state GPU |
| v4.6 | 2026-07 | Cloud discovery, provider management, Dashboard |
| v5.4.0 | 2026-08 | macOS Dashboard: sidebar, chat, 12 SVG icons, dark mode |
| v5.5.0 | 2026-08 | GPU state computed property (no drift), HealthMonitor decoupled, SIGHUP ConfigReloader |
| v5.5.1 | 2026-08 | OpenAPI 3.1.0 specification (37 endpoints, shared schemas) |
| v5.6.7 | 2026-09 | R0: `ensure_service` cooldown fix |
| v5.6.8 | 2026-09 | **Gateway Hardening**: R1-R10, Prometheus /metrics, AnomalyCollector, silent fallback removal |
| **v5.8.0** | **2026-09** | **PR-19: Production-grade aiohttp async edge (`--async`)** — hybrid executor model, 7 stream routes with incremental SSE pump, 30+ buffered routes with full header propagation, chunked request body support, 100MB client_max_size, EADDRINUSE retry, systemd sd_notify, C extension wheel rebuild (2.7x perf), dead route cleanup, **path normalization fix**: `/v1/completions`/`/api/chat`/`/api/generate` aliased to `/v1/chat/completions` (engine-type-driven via YAML). |

---

## Hardware

- **GPU**: NVIDIA GeForce RTX 5090D, 32 GB GDDR7, 512-bit, 1792 GB/s, Blackwell (SM 12.0)
- **RAM**: 64 GB DDR5
- **OS**: Ubuntu 25.04, Python 3.12+

---

[InferFabric](https://github.com/vincentlau2046-sudo/InferFabric) · MIT License