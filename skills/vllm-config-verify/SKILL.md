---
name: "vllm-config-verify"
description: "Verify vLLM serve flags and KV-cache capacity before changing vLLM config. Use before editing any vLLM model config or startup command."
---

# vLLM config verification

Verify a proposed vLLM config against the installed vLLM and the live service, so invalid flag values or OOM risk are caught before a restart.

## Steps

1. Baseline the running service (never assume):
   - `ps aux | grep "vllm serve"` → record the exact live command line.
   - `curl -s :<port>/metrics | grep cache_config_info` → read `num_gpu_blocks`, `kv_cache_size_tokens`, `kv_cache_usage_perc`.
   - `nvidia-smi --query-gpu=memory.total,memory.used,memory.free --format=csv` → actual VRAM headroom.

2. Validate every flag's legal values against the installed vLLM (do not rely on memory):
   - dtype flags: read the `MambaDType` and `CacheDType` `Literal` definitions in `site-packages/vllm/config/cache.py`. Example: `MambaDType = Literal["auto","float32","float16","bfloat16"]` — so `--mamba-ssm-cache-dtype fp8` is rejected by argparse.
   - Parser flags: list registered names:
     - Tool parsers: `python -c "from vllm.tool_parsers import ToolParserManager; print(ToolParserManager.list_registered())"`
     - Reasoning parsers: `python -c "from vllm.reasoning import ReasoningParserManager; print(ReasoningParserManager.list_registered())"`
   - When unsure whether a value is accepted, simulate the argparse `choices` for that flag and confirm the value is in the set (a rejected value raises `invalid choice`).

3. Cross-check model facts from the checkpoint's `config.json`:
   - `max_position_embeddings` must be ≥ `--max-model-len`.
   - If `--speculative-config` uses `method=mtp`, confirm the MTP weight file (e.g. `model-mtp-bf16.safetensors`) exists in the model dir.

4. Report only the deltas vs the live config (which flags changed and why), and flag any value the installed vLLM would reject.

5. Cross-system context-window sync (IFF ↔ OpenClaw):
   - A change to `max_model_len` in an IFF `models.d` yaml must be mirrored in OpenClaw: update `contextWindow` for that served model, or the client-side limit drifts out of sync.
   - The `inferfabric` provider lives ONLY in per-agent files `~/.openclaw/agents/{main,kamera,ava}/agent/models.json` → `providers.inferfabric.models[]`; it is NOT in the global `~/.openclaw/openclaw.json` (which holds only other providers).
   - `max_model_len` change → set `contextWindow` to the new value in ALL THREE agent files; leave `maxTokens` untouched.
   - Output-cap (`max_tokens`) change → set `maxTokens` in ALL THREE agent files; leave `contextWindow` untouched.
   - Edit via a Python `json.load` → modify → `json.dump` script (never hand-edit JSON), then re-read the files to verify the new value.
   - If the model is not listed under the OpenClaw `inferfabric` provider (e.g. gemma4), no OpenClaw-side change is needed.

## Verification

- The proposed command parses without `invalid choice` for every flag.
- KV headroom: the longest-context request (≈ `max-model-len` tokens) must fit within `kv_cache_size_tokens`; for concurrency, `max_num_seqs` requests at context length must fit the pool (add MTP draft tokens on top).
- After restart, `nvidia-smi` shows no OOM kill.

## Config authoring notes (IFF `models.d` yaml)

- **YAML `>-` folded-block pitfall**: a line starting with `#` inside `extra_flags: >-` is FOLDED into the string, not treated as a comment. Verify with `yaml.safe_load` and assert `'#' not in flags`. Put `#` comments on their own line OUTSIDE the folded block (above the key), never inside it.
- **VRAM budgeting principle**: `max-num-batched-tokens` drives *activation* memory (scales ~linearly with the value), not total VRAM (that is set by `gpu_memory_utilization`). With `--enable-prefix-caching` + `kv_offloading` enabled, most prefill is cache-hits, so a smaller batched-tokens value (e.g. 2048 vs 8192) is safe and lowers peak VRAM.
- **MTP (`--speculative-config`)**: the draft-model + extra forward for `num_speculative_tokens` is drawn FROM the `gpu_memory_utilization` budget, not extra VRAM. Raising `num_speculative_tokens` adds startup time (extra draft weights) — raise `startup_timeout` accordingly.

## Post-hoc diagnosis (model won't start, or dies on first inference)

Trace from logs, never assume. Sources, in order:

1. `~/.inferfabric/state.db` `history` table (`timestamp, from_profile, to_profile, duration, status`): `status=error` with `duration` ≈ the model's `startup_timeout` (e.g. 565s/553s vs 540s) = startup phase itself timed out.
2. proxy.log `[inferfabric]` manager lines: `Starting vLLM cmd:` shows the exact flags in effect (e.g. `max-model-len` varying 114688/168000/131072/163840 across restarts); `Stopping vLLM`, `vLLM started: PID=...`, `SIGTERM timeout → SIGKILL` show the stop/start lifecycle.
3. proxy.log `[inferfabric.forwarder]`: `timed out` / `Connection reset by peer` / `Broken pipe` on a real request = the target vLLM process died mid-inference.
4. vLLM model log (`~/.inferfabric/logs/vllm_<conda_env>.log`): JIT-compile warnings on the first request (`_copy_page_indices_kernel`, `fused_recurrent_gated_delta_rule_packed_decode_kernel`) = first-request latency spikes; `Available KV cache memory` + `Maximum concurrency` lines show VRAM headroom.
5. `dmesg -T | grep -iE "out of memory|oom|killed process"`: empty = process-level crash, not a kernel OOM-kill.

Confirmed root-cause pattern (qwen38-27b, 32GB GPU): raising `gpu_memory_utilization` 0.92→0.93 + raising `--max-num-batched-tokens` 4096→8192 + removing `--speculative-config` (MTP) → startup exceeds `startup_timeout: 540`, and the first inference request (JIT spike + 16GB CPU KV offload) crashes the vLLM process. Restoring the baseline (0.92 / 4096 / MTP) fixes both symptoms.
