# Gemma4-31B-VL vs Qwen3.8-27B-Abliterated — 深度配置分析与优化报告

> **报告日期**: 2026-08-30
> **分析范围**: YAML 配置解析、运行日志异常检测、性能数据分析、参数优化建议
> **硬件平台**: NVIDIA GeForce RTX 5090 D (32,607 MiB VRAM) | 62 GiB RAM | 63 GiB Swap
> **推理引擎**: vLLM 0.26.0
> **约束前提**: 上下文长度 (max_model_len=262144) 不变

---

## 1. 环境总览

**GPU**: NVIDIA GeForce RTX 5090 D — VRAM: 32,607 MiB ≈ 31.8 GiB
**基线占用**: ~2,067 MiB (Xorg/桌面), 约 **30 GiB 可用**
**系统内存**: 62 GiB (可用 ~50 GiB)
**Swap**: 63 GiB (已用 1.3 GiB)

## 2. 模型配置总览

### Gemma4-31B-VL

| 参数 | 值 |
|------|-----|
| 模型 | Huihui Gemma4-31B IT Abliterated v2 NVFP4 Dense |
| 架构 | Gemma4ForConditionalGeneration |
| 端口 | 8005, Conda: gemma-4-31b |
| gpu_memory_utilization | **0.92** |
| max_model_len | **262144** (256K) |
| max_num_seqs | 8 |
| kv_cache_dtype | fp8 |
| 注意力后端 | **TRITON_ATTN** (强制 — 不支持 FA4) |
| CUDA Graph | PIECEWISE [1,2,4,8] |
| KV offload | native, 16 GiB buffer |
| 启动用时 | ~54s |

### Qwen38-27B-Abliterated

| 参数 | 值 |
|------|-----|
| 模型 | Huihui Qwen3.8-27B-abliterated-NVFP4 |
| 架构 | Qwen3_5ForConditionalGeneration (Mamba+Attention) |
| 端口 | 8002, Conda: Qwen3.8-27B-VL |
| gpu_memory_utilization | **0.93** |
| max_model_len | **262144** (256K) |
| max_num_seqs | 4 |
| max_num_batched_tokens | 4096 |
| kv_cache_dtype | fp8 |
| 注意力后端 | **FlashInfer** |
| CUDA Graph | PIECEWISE [1,2,4] |
| KV offload | native, 16 GiB buffer |
| startup_timeout | 540s |
| 启动用时 | ~49s |

## 3. 检出异常清单

### 🔴 高优先级

**#1 — FlashInfer autotune 缓存作废 (Qwen38)**
- 日志: "was created in a different environment (cudnn_version: saved=unknown vs current=91002)"
- 影响: 每次启动都会重新 autotune (3-5s)，且因为 cuDNN 不匹配**不保存结果**
- 根因: 清除过 ~/.cache/vllm 或在不同 cuDNN 环境下运行过 vLLM
- 修复: `rm -rf ~/.cache/vllm/flashinfer_autotune_cache/0.6.13/120f/bee4d080d6ebe81e43b2426a99c8e9d19f5841fb8f1cc4fd43ebec83e33661e5/`

**#2 — KV 缓存不足 (Gemma4)**
- 日志: "GPU KV cache: 8.4 GiB vs needed ~12.3 GiB. Overflow blocks will be evicted to CPU"
- 影响: 256K 上下文无法完全放在 GPU, 频繁 offload 增加延迟
- KV 并发容量仅 0.69x — 一个 256K 请求都放不下
- 修复: 提升 gpu_memory_utilization 到 0.95

**#3 — KV 缓存临界 (Qwen38)**
- 日志: "GPU KV cache: 8.3 GiB vs needed ~8.3 GiB"
- 刚好够一个完整的 256K 上下文, 无余量 — 稍有波动即触发 offload

**#4 — 推理期 Triton JIT 编译**
- Qwen38: 5个内核 (slot_mapping, copy_page_indices, batc memcpy, causal_conv1d_update, gated_delta_rule)
- Gemma4: 2个内核 (kernl_unified_attention, reduce_segments)
- 影响: 首次遇到未覆盖形状时延迟飙升到秒级
- 修复: 增大 CUDA Grph capture sizes / 增加 warmups

### 🟡 中优先级

| # | 异常 | 模型 | 说明 |
|---|------|------|------|
| 5 | FA4 不可用 | Gemma4 | 异构头强制 Trion |
| 6 | NVFP4 并行层精度 | 两模型 | q/k/v_proj 独立 scale |
| 7 | V2 Runner 不支持 thinking_budget | 两模型 | 可用 VLLM_USE_V2 绕过 |
| 8 | prefix cache hit rate: 8% | Qwen38 | 几乎没有重复 prefix —考虑关闭|

## 4. 性能数据

### Token 统计 (08-19 ~ 08-29)

| 模型 | Prompt Tokens | Generation Tokens | 比率 |
|------|--------------|------------------|------|
| **Qwen38** | **209,049,769** | **1,3,647** | **90:1** |
| **Gemma4** | **4,531,467** | **846,147** | **5.4:1** |

- Qwen38 是主力模型，处理 ~97% 的总 prompt 量
- Qwen38 峰值日 (08-25): **86.3M prompt tokens**, 比例 **195:1** — 极长上下文+短输出
- Gemma4 的生成吞吐 (137-177 tok/s) 显著优于 Qwen38 (25-60 tok/s)

## 5. 优化建议

### 🔴 强烈建议 (立即实施)

#### 建议 1: 提升 gpu_memory_utilization → 0.95

**修改两模型 YAML**:
```diff
- gpu_memory_utilization: 0.92   # Gemma4
+ gpu_memory_utilization: 0.95
```
```diff
- gpu_memory_utilization: 0.93   # Qwen38
+ gpu_memory_utilization: 0.95
```

**预期**: KV 缓存扩大 10-15%, offoad 减少, TTFT 降低

#### 建议 2: 处理 FlashInfer autotune 缓存冲突

```bash
rm -rf ~/.cache/vllm/flashinfer_autotune_cache/0.6.13/120f/bee4d080d6ebe81e43b2426a99c8e9d19f5841fb8f1cc4fd43ebec83e33661e5/
```

#### 建议 3: 增大 CUDA Grp capture sizes

**Gemma4**: `"cudgraph_capture_szes": [1, 2, 4, 8, 16], "cudgraph_num_of_warmups": 1`
**Qwen38**: 保持当前 [1,2,4]

### 🟡 可以考虑

#### 建议 4: 移除 Qwen38 的 prefix caching

当前命中率仅 8%, 移除可节省少量显存/计算开销

#### 建议 5: 降低 Qwen38 的 startup_timeout 到 480

启动约 49s, 540s 的阈值可以降低

## 6. 引用参考

1. vLLM Perormance Tuning Gide: https://dos.vllm.ai/en/latest/features/performance_tuing.html
2. vLLM Compilation Config: https://dos.vllm.ai/en/latest/serving/compatibility.tml#omplation-cnfig
3. vLLM KV Cache Offoading: https://dos.vll.ai/en/latest/eatures/kv_foading.html
4. vLLM CUDA Grph: https://docs.vlm.ai/en/latest/eatures/cuda_gaph.html
5. Gema 4 Tech Report — Google DeepMind, 2025
6. Qwen 3.5 — Alibaba Cloud, 2026
7. NVPF4 Qantization (Neural Maic): https://euralmagic.com/blog/vlm-nvf4/
8. FlashInfer v0.6.13: https://flashinfer.ai/
