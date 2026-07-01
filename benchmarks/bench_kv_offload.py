#!/usr/bin/env python3
"""Qwen3.6-27B KV Offload Performance Benchmark v2.

Robust error handling, graceful degradation on failures.
"""

import json
import time
import statistics
import urllib.request
import urllib.error
import concurrent.futures
import sys
from pathlib import Path

VLLM_URL = "http://localhost:8000"
MODEL = "vllm_qwen27b"
MAX_TOKENS = 2048
TEMPERATURE = 0.0

FILLER = ("人工智能（AI）是计算机科学的分支，致力于创建执行通常需要人类智能的任务的系统。"
          "深度学习是AI的核心技术，依赖于神经网络。大型语言模型（LLM）通过海量文本训练，"
          "学会了理解和生成人类语言。量化技术（如NVFP4）和KV缓存卸载可以减少内存占用。")


def make_prompt(tokens: int) -> str:
    """Generate ~tokens-length Chinese prompt."""
    chars = int(tokens * 1.4)
    p = "请详细分析以下文本的主题、结构和关键观点，并给出你的见解：\n\n"
    while len(p) < chars:
        p += FILLER + "\n"
    return p[:chars]


def api_call(prompt, max_tokens=MAX_TOKENS, stream=False, timeout=300):
    """Call vLLM API. Returns (result_dict, error_str)."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": TEMPERATURE,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{VLLM_URL}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if stream:
                return _parse_stream(resp), None
            else:
                data = json.loads(resp.read().decode())
                usage = data.get("usage", {})
                return {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }, None
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()[:200] if e.fp else ""
        return None, f"HTTP {e.code}: {err_body}"
    except Exception as e:
        return None, str(e)


def _parse_stream(resp):
    """Parse streaming response using usage field for accurate token count."""
    ttft = None
    first_chunk_t = None
    chunk_count = 0
    t0 = time.perf_counter()
    last_usage = None

    for line in resp:
        line = line.decode().strip()
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
            # Get usage from final chunk if available
            usage = chunk.get("usage")
            if usage:
                last_usage = usage
            choices = chunk.get("choices", [])
            delta = choices[0].get("delta", {}) if choices else {}
            t_now = time.perf_counter()
            if delta.get("reasoning") or delta.get("content"):
                chunk_count += 1
                if ttft is None:
                    ttft = t_now - t0
        except json.JSONDecodeError:
            continue

    total_time = time.perf_counter() - t0

    # Use completion_tokens from usage if available (accurate with MTP)
    # Otherwise estimate from chunk count (less accurate but fallback)
    if last_usage and last_usage.get("completion_tokens", 0) > 0:
        total_output = last_usage["completion_tokens"]
        # Estimate reasoning vs content from usage details if available
        content_tokens = total_output  # approximate
        reasoning_tokens = 0
    else:
        # Fallback: chunk count × avg_tokens_per_chunk (MTP ≈ 3)
        total_output = chunk_count * 3  # rough estimate for MTP
        content_tokens = total_output
        reasoning_tokens = 0

    # TPOT: time between first content and last chunk
    tpot = None
    if chunk_count >= 3 and ttft:
        # Estimate: (total_time - ttft) / total_output gives avg inter-token time
        output_time = total_time - ttft
        tpot = output_time / total_output if total_output > 0 else None

    return {
        "ttft_s": round(ttft, 4) if ttft else None,
        "tpot_ms": round(tpot * 1000, 2) if tpot else None,
        "completion_tokens": total_output,
        "total_output_tokens": total_output,
        "chunk_count": chunk_count,
        "total_time_s": round(total_time, 3),
        "throughput_tok_s": round(total_output / total_time, 2) if total_time > 0 else 0,
    }


def bench_basic():
    """TTFT + throughput across prompt lengths."""
    print("\n" + "="*70)
    print("TEST 1: TTFT + Throughput vs Prompt Length (streaming)")
    print("="*70)
    print(f"{'Prompt':>8} | {'TTFT':>8} | {'TPOT':>8} | {'Tput':>10} | {'Reason':>7} | {'Content':>7} | {'Total':>6}")
    print("-"*70)

    results = []
    for target in [128, 512, 1024, 4096, 16384, 65536]:
        prompt = make_prompt(target)
        r, err = api_call(prompt, max_tokens=MAX_TOKENS, stream=True, timeout=300)
        if err:
            print(f"{target:>8} | ERROR: {err[:50]}")
            results.append({"target": target, "error": err})
            continue

        ttft = f"{r['ttft_s']:.2f}s" if r['ttft_s'] else "N/A"
        tpot = f"{r['tpot_ms']:.1f}ms" if r['tpot_ms'] else "N/A"
        print(f"{target:>8} | {ttft:>8} | {tpot:>8} | {r['throughput_tok_s']:>8.1f} t/s | "
              f"chunks={r.get('chunk_count','?'):>5} tokens={r.get('completion_tokens','?'):>5}")
        r["target"] = target
        results.append(r)

        # Check health after long prompts
        if target >= 65536:
            try:
                urllib.request.urlopen(f"{VLLM_URL}/health", timeout=5).read()
            except:
                print("  ⚠️ vLLM unhealthy after long prompt, stopping")
                break

    return results


def bench_concurrency():
    """Concurrent request performance."""
    print("\n" + "="*70)
    print("TEST 2: Concurrency Scaling (1K prompt, 256 output)")
    print("="*70)
    print(f"{'N':>3} | {'Agg Tput':>10} | {'PerReq':>10} | {'Wall':>8} | {'AvgWait':>8}")
    print("-"*50)

    prompt = make_prompt(1024)
    results = {}

    for n in [1, 2, 4, 8]:
        t0 = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(api_call, prompt, max_tokens=256, stream=False) for _ in range(n)]
            responses = [f.result() for f in concurrent.futures.as_completed(futures)]
        wall = time.perf_counter() - t0

        valid = [(r, e) for r, e in responses if r and not e and r.get("completion_tokens", 0) > 0]
        if not valid:
            print(f"{n:>3} | All failed")
            continue

        total_comp = sum(r["completion_tokens"] for r, _ in valid)
        agg_tput = total_comp / wall
        avg_wait = statistics.mean(r.get("total_tokens", 0) / max(r.get("wall_time_s", wall), 0.01) for r, _ in valid if r.get("total_tokens", 0) > 0)

        results[n] = {
            "aggregate_tput": round(agg_tput, 1),
            "total_wall_s": round(wall, 1),
            "total_completion": total_comp,
        }
        print(f"{n:>3} | {agg_tput:>8.1f} t/s | {total_comp/n:>8.0f} avg | {wall:>6.1f}s | {wall/n:>6.1f}s")

        # Health check between concurrency levels
        try:
            urllib.request.urlopen(f"{VLLM_URL}/health", timeout=5).read()
        except:
            print("  ⚠️ vLLM unhealthy, stopping")
            break

    return results


def bench_long_context():
    """Long context performance — KV offload's primary value."""
    print("\n" + "="*70)
    print("TEST 3: Long Context (16K → 128K) — KV Offload Sweet Spot")
    print("="*70)
    print(f"{'Prompt':>8} | {'TTFT':>8} | {'Prefill':>8} | {'Tput':>10} | {'Status':>10}")
    print("-"*60)

    results = []
    for target in [16384, 32768, 65536, 131072]:
        prompt = make_prompt(target)
        r, err = api_call(prompt, max_tokens=256, stream=True, timeout=600)

        if err:
            print(f"{target:>8} | ERROR: {err[:40]}")
            results.append({"target": target, "error": err})
            # If OOM at this length, note it
            if "500" in err or "OOM" in err.upper() or "out of memory" in err.lower():
                print(f"  → Context limit reached at ~{target} tokens")
                results.append({"target": target, "limit": True})
            break

        ttft = r['ttft_s'] or 0
        # Estimate prefill time ≈ TTFT for first generation
        prefill_rate = round(target / ttft, 0) if ttft > 0 else 0
        print(f"{target:>8} | {ttft:>6.2f}s | {prefill_rate:>6.0f} t/s | {r['throughput_tok_s']:>8.1f} t/s | {'OK':>10}")
        r["target"] = target
        r["prefill_rate_tok_s"] = prefill_rate
        results.append(r)

        # Health check
        try:
            urllib.request.urlopen(f"{VLLM_URL}/health", timeout=5).read()
        except:
            print(f"  ⚠️ vLLM unhealthy after {target} tokens")
            break

    return results


def bench_prefix_caching():
    """Prefix caching hit test."""
    print("\n" + "="*70)
    print("TEST 4: Prefix Caching (repeated system prompt)")
    print("="*70)

    sys_prompt = "你是一个专业的AI助手。" + FILLER * 5
    results = {}

    for i, query in enumerate(["请总结上面的文本。", "上面的文本有哪些关键数字？"], 1):
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": query},
            ],
            "max_tokens": 128, "temperature": 0.0,
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{VLLM_URL}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"},
        )
        try:
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
            t1 = time.perf_counter()

            usage = data.get("usage", {})
            prompt_tok = usage.get("prompt_tokens", 0)
            cached = 0
            # Try to get cached tokens from various vLLM response formats
            details = usage.get("prompt_tokens_details", {})
            if isinstance(details, dict):
                cached = details.get("cached_tokens", 0)

            results[f"req_{i}"] = {
                "prompt_tokens": prompt_tok,
                "cached_tokens": cached,
                "cache_hit_pct": f"{cached/prompt_tok*100:.1f}%" if prompt_tok > 0 and cached > 0 else "0%",
                "completion_tokens": usage.get("completion_tokens", 0),
                "wall_s": round(t1 - t0, 3),
            }
            r = results[f"req_{i}"]
            print(f"  Req {i}: prompt={r['prompt_tokens']} cached={r['cached_tokens']} "
                  f"hit={r['cache_hit_pct']} wall={r['wall_s']:.2f}s")
        except Exception as e:
            print(f"  Req {i}: ERROR {e}")
            results[f"req_{i}"] = {"error": str(e)}

    return results


def get_gpu():
    import subprocess
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        parts = r.stdout.strip().split(",")
        return {"used_mb": int(parts[0]), "total_mb": int(parts[1])}
    except:
        return {}


def get_vllm_kv():
    """Get KV cache block counts from vLLM metrics."""
    try:
        with urllib.request.urlopen(f"{VLLM_URL}/metrics", timeout=5) as resp:
            text = resp.read().decode()
        gpu_blocks = cpu_blocks = 0
        for line in text.splitlines():
            if line.startswith("vllm:num_gpu_cache_blocks"):
                gpu_blocks = float(line.split()[-1])
            elif line.startswith("vllm:num_cpu_cache_blocks"):
                cpu_blocks = float(line.split()[-1])
        return {"gpu_blocks": gpu_blocks, "cpu_blocks": cpu_blocks}
    except:
        return {}


def main():
    print("Qwen3.6-27B KV Offload Performance Benchmark v2")
    print("="*70)

    # Health check
    try:
        urllib.request.urlopen(f"{VLLM_URL}/health", timeout=5).read()
        print("vLLM healthy ✓")
    except:
        print("vLLM not running! Start: edge-llm switch qwen36-27b")
        return

    gpu = get_gpu()
    kv = get_vllm_kv()
    print(f"GPU: {gpu.get('used_mb', '?')}/{gpu.get('total_mb', '?')} MiB")
    print(f"KV blocks: GPU={kv.get('gpu_blocks', 0):.0f} CPU={kv.get('cpu_blocks', 0):.0f}")
    print(f"Config: max_model_len=171000, kv_offload=native 8GB, gpu_util=0.90")

    all_results = {
        "config": {"model": MODEL, "kv_offload": "native 8GB", "max_model_len": 171000},
        "baseline_gpu": gpu,
        "baseline_kv": kv,
    }

    # Run tests
    all_results["basic"] = bench_basic()
    all_results["concurrency"] = bench_concurrency()
    all_results["long_context"] = bench_long_context()
    all_results["prefix_caching"] = bench_prefix_caching()
    all_results["final_gpu"] = get_gpu()
    all_results["final_kv"] = get_vllm_kv()

    # Save
    out = Path(__file__).parent.parent / "bench_results" / f"kv_offload_{int(time.time())}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(all_results, indent=2, ensure_ascii=False, default=str))
    print(f"\nResults saved: {out}")

    # Summary
    print("\n" + "="*70)
    print("SUMMARY: KV Offload Performance")
    print("="*70)

    basic = all_results.get("basic", [])
    if basic:
        print("\n📊 TTFT vs Context Length:")
        for r in basic:
            if "error" not in r:
                ttft = f"{r['ttft_s']:.3f}s" if r.get('ttft_s') else "N/A"
                print(f"  {r.get('target', '?'):>6} tok → TTFT={ttft:>10} | Tput={r.get('throughput_tok_s', 0):.1f} tok/s "
                      f"(reason={r.get('reasoning_tokens', 0)} + content={r.get('content_tokens', 0)})")

    conc = all_results.get("concurrency", {})
    if conc:
        print("\n📊 Concurrency:")
        for n, r in sorted(conc.items()):
            if isinstance(r, dict) and "error" not in r:
                print(f"  N={n}: Agg={r.get('aggregate_tput', '?')} tok/s | Wall={r.get('total_wall_s', '?')}s")

    long = all_results.get("long_context", [])
    if long:
        print("\n📊 Long Context (KV Offload Value):")
        for r in long:
            if "error" not in r:
                print(f"  {r.get('target', '?'):>6} tok → TTFT={r.get('ttft_s', 'N/A')}s | "
                      f"Prefill={r.get('prefill_rate_tok_s', '?')} tok/s | "
                      f"Decode={r.get('throughput_tok_s', '?')} tok/s")

    print("\n📊 Memory:")
    print(f"  Before: {gpu.get('used_mb', '?')} MiB | After: {all_results.get('final_gpu', {}).get('used_mb', '?')} MiB")
    kv_after = all_results.get("final_kv", {})
    print(f"  KV blocks: GPU={kv_after.get('gpu_blocks', 0):.0f} CPU={kv_after.get('cpu_blocks', 0):.0f}")


if __name__ == "__main__":
    main()
