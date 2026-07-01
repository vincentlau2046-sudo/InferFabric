# edge-llm 技术笔记

## KV Offload 配置 (2026-07-01)

### 参数
- `--kv-offloading-backend native --kv-offloading-size 8`

### 已知限制
- **expandable_segments 冲突**: `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 与 KV offload NIXL/Mooncake IB memory 冲突
  - 解决: `process_manager.py` 检测 `--kv-offloading-size` 时自动跳过 `expandable_segments`
- **sleep_mode 不可用**: cumem 分配器导致 CUDA graph 从 0.04 GiB 膨胀到 5.69 GiB → OOM
- **lmcache 未安装**: `--kv-offloading-backend lmcache` 需要 lmcache 包
- **有效 backend**: `native`（vLLM 内置）

### 参考配置 (qwen36-27b)
```yaml
gpu_memory_utilization: 0.90
extra_flags: >-
  --kv-offloading-backend native
  --kv-offloading-size 8
```

## TMA Patch (2026-07-01)

### 根因
`matmul_ogs.py` 中 `can_use_tma` 条件 `CC[0] > 9` 在 RTX 5090 D (CC 12.0) 误启 TMA → descriptor buffer 膨胀 → OOM

### 修复
`CC[0] > 9` → `CC[0] > 9 and CC[0] < 12`，排除消费级 Blackwell (CC 12.0)

### Patch 命令
```bash
for env in qw36-27b-vllm qw35-9b-vllm gm4-26b-vllm; do
  FILE=~/miniconda3/envs/$env/lib/python3.11/site-packages/vllm/third_party/triton_kernels/matmul_ogs.py
  cp "$FILE" "$FILE.bak"
  sed -i 's/can_use_tma = can_use_tma and (torch.cuda.get_device_capability()\[0\] > 9 or bitwidth(w.dtype) != 4)/cc = torch.cuda.get_device_capability()\n    can_use_tma = can_use_tma and ((cc[0] > 9 and cc[0] < 12) or bitwidth(w.dtype) != 4)/' "$FILE"
done
```

### 注意
- `VLLM_USE_TMA=0` 不存在，patch 是唯一方案
- pip upgrade 会覆盖，需重新打
