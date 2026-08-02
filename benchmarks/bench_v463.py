#!/usr/bin/env python3
"""IFF v4.6.3 Model Benchmark — Context Window + Concurrency

Tests qwen35-9b, qwen36-27b-vl, gemma-4-31b across:
  - Input length scaling (short/medium/long context)
  - Concurrency (1/2/4)
  - Streaming vs non-streaming
  - Token extraction accuracy (SSELineBuffer)
"""
import sys
import os
import json
import time
import asyncio
import aiohttp
import statistics
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)

PROXY_URL = "http://localhost:8999"
DIRECT_BASES = {
    "qwen35-9b": "http://localhost:8002",
    "qwen36-27b-vl": "http://localhost:8003",
    "gemma-4-31B-it-NVFP4": "http://localhost:8005",
}

# Model specs from configs
MODEL_SPECS = {
    "qwen35-9b": {
        "served_name": "vllm_qw35_gptq",
        "max_model_len": 65536,
        "max_num_seqs": 4,
        "gpu_role": "shared",
    },
    "qwen36-27b-vl": {
        "served_name": "vllm_qwen27b_vl",
        "max_model_len": 131072,
        "max_num_seqs": 8,
        "gpu_role": "exclusive",
    },
    "gemma-4-31B-it-NVFP4": {
        "served_name": "gemma-4-31B-it-NVFP4",
        "max_model_len": 131072,
        "max_num_seqs": 4,
        "gpu_role": "exclusive",
    },
}

# Context lengths to test (token counts)
CTX_LENGTHS = {
    "short": 256,      # ~256 tokens input
    "medium": 2048,    # ~2K tokens
    "long": 8192,      # ~8K tokens
    "vlong": 32768,    # ~32K tokens (stress test)
}

OUTPUT_BUDGET = 256  # Keep output short to focus on input scaling
CONCURRENCY = [1, 2, 4]


def make_prompt(token_target: int) -> str:
    """Generate a prompt of approximately token_target tokens."""
    # Rough: 1 token ≈ 1.5 chars for English, ≈ 1 char for mixed
    # Use repeated paragraphs to hit target
    base = "The quick brown fox jumps over the lazy dog. " * 10  # ~90 tokens
    repeats = max(1, token_target // 90)
    context = base * repeats
    return f"Below is a long text for analysis. Please summarize the key points in 2-3 sentences.\n\n{context}"


async def bench_single(
    session: aiohttp.ClientSession,
    model_name: str,
    prompt: str,
    stream: bool = True,
    base_url: str = None,
) -> dict:
    """Run a single benchmark request."""
    url = (base_url or PROXY_URL) + "/v1/chat/completions"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": OUTPUT_BUDGET,
        "stream": stream,
        "stream_options": {"include_usage": True} if stream else {},
    }

    start = time.monotonic()
    ttft = None
    output_tokens = 0
    usage = {}

    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if stream:
                async for line in resp.content:
                    decoded = line.decode("utf-8", errors="ignore").strip()
                    if not decoded or not decoded.startswith("data:"):
                        continue
                    data_str = decoded[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        obj = json.loads(data_str)
                        choices = obj.get("choices", [])
                        # Handle both reasoning_content (Qwen3) and content
                        if choices:
                            delta = choices[0].get("delta", {})
                            has_content = delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")
                            if ttft is None and has_content:
                                ttft = (time.monotonic() - start) * 1000
                        u = obj.get("usage")
                        if u and u.get("completion_tokens", 0) > 0:
                            usage = u
                            output_tokens = u["completion_tokens"]
                    except json.JSONDecodeError:
                        pass
            else:
                result = await resp.json()
                ttft = (time.monotonic() - start) * 1000  # For non-stream, TTFT = total
                usage = result.get("usage", {})
                output_tokens = usage.get("completion_tokens", 0)

    except asyncio.TimeoutError:
        return {"error": "timeout", "duration_ms": 120000}
    except Exception as e:
        return {"error": str(e), "duration_ms": (time.monotonic() - start) * 1000}

    duration_ms = (time.monotonic() - start) * 1000
    tps = output_tokens / ((duration_ms - (ttft or 0)) / 1000) if (duration_ms > (ttft or 0)) else 0

    return {
        "ttft_ms": round(ttft, 1) if ttft else None,
        "duration_ms": round(duration_ms, 1),
        "output_tokens": output_tokens,
        "tps": round(tps, 1),
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
        "stream": stream,
    }


async def bench_concurrent(
    model_name: str,
    prompt: str,
    concurrency: int,
    stream: bool = True,
    base_url: str = None,
) -> list[dict]:
    """Run concurrent benchmark."""
    async with aiohttp.ClientSession() as session:
        tasks = [bench_single(session, model_name, prompt, stream, base_url) for _ in range(concurrency)]
        return await asyncio.gather(*tasks)


def fmt_ms(ms):
    if ms is None: return "—"
    if ms > 10000: return f"{ms/1000:.1f}s"
    return f"{ms:.0f}ms"


async def run_benchmark(model_key: str):
    """Run full benchmark for a model."""
    spec = MODEL_SPECS[model_key]
    served_name = spec["served_name"]
    base_url = DIRECT_BASES[model_key]
    
    # Check if model is running
    try:
        import urllib.request
        port = int(base_url.split(":")[-1])
        urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5)
    except:
        print(f"\n⚠️ {model_key} not running, skipping")
        return None

    print(f"\n{'='*60}")
    print(f"Benchmark: {model_key} ({served_name})")
    print(f"Max ctx: {spec['max_model_len']}, Max seqs: {spec['max_num_seqs']}")
    print(f"{'='*60}")

    all_results = {}

    # Test 1: Context length scaling (c=1, streaming)
    print(f"\n📊 Context Length Scaling (c=1, streaming):")
    print(f"  {'Context':<10} {'TTFT':<12} {'TPS':<10} {'E2E':<12} {'Tokens In':<12} {'Tokens Out':<12}")
    
    for ctx_name, token_count in CTX_LENGTHS.items():
        if token_count > spec["max_model_len"]:
            print(f"  {ctx_name:<10} SKIP (exceeds max_model_len)")
            continue
        prompt = make_prompt(token_count)
        results = await bench_concurrent(served_name, prompt, 1, stream=True, base_url=base_url)
        r = results[0]
        if r.get("error"):
            print(f"  {ctx_name:<10} ERROR: {r['error']}")
            all_results[ctx_name] = r
            continue
        
        print(f"  {ctx_name:<10} {fmt_ms(r.get('ttft_ms')):<12} {r.get('tps',0):<10} {fmt_ms(r.get('duration_ms')):<12} {r.get('tokens_in',0):<12} {r.get('tokens_out',0):<12}")
        all_results[f"ctx_{ctx_name}"] = r

    # Test 2: Concurrency scaling (short input)
    print(f"\n📊 Concurrency Scaling (short input, streaming):")
    print(f"  {'C':<4} {'TTFT p50':<12} {'TPS avg':<10} {'E2E p50':<12} {'TTFT p95':<12}")
    
    prompt = make_prompt(256)
    for c in CONCURRENCY:
        if c > spec["max_num_seqs"]:
            print(f"  c={c:<3} SKIP (exceeds max_num_seqs)")
            continue
        results = await bench_concurrent(served_name, prompt, c, stream=True, base_url=base_url)
        ttfts = [r.get("ttft_ms") for r in results if not r.get("error") and r.get("ttft_ms")]
        tps_list = [r.get("tps", 0) for r in results if not r.get("error")]
        durs = [r.get("duration_ms", 0) for r in results if not r.get("error")]
        
        if len(ttfts) >= 1:
            ttft_p50 = statistics.median(ttfts)
            ttft_p95 = sorted(ttfts)[int(0.95 * (len(ttfts) - 1))] if len(ttfts) > 1 else ttfts[0]
            tps_avg = statistics.mean(tps_list)
            dur_p50 = statistics.median(durs)
            print(f"  c={c:<3} {fmt_ms(ttft_p50):<12} {tps_avg:<10.1f} {fmt_ms(dur_p50):<12} {fmt_ms(ttft_p95):<12}")
            all_results[f"conc_{c}"] = {"ttft_p50": ttft_p50, "tps_avg": tps_avg, "dur_p50": dur_p50, "ttft_p95": ttft_p95}
        else:
            print(f"  c={c:<3} ALL FAILED")

    # Test 3: Streaming usage extraction accuracy
    print(f"\n📊 Streaming Usage Extraction (short input):")
    prompt = make_prompt(256)
    results_stream = await bench_concurrent(served_name, prompt, 1, stream=True, base_url=base_url)
    results_nostream = await bench_concurrent(served_name, prompt, 1, stream=False, base_url=base_url)
    
    r_s = results_stream[0]
    r_ns = results_nostream[0]
    print(f"  Stream:    in={r_s.get('tokens_in',0)}, out={r_s.get('tokens_out',0)}")
    print(f"  NonStream: in={r_ns.get('tokens_in',0)}, out={r_ns.get('tokens_out',0)}")
    
    # Compare token counts (should be similar for same prompt)
    diff_in = abs(r_s.get('tokens_in', 0) - r_ns.get('tokens_in', 0))
    usage_match = diff_in <= 5  # Allow small diff due to sampling
    print(f"  Usage match: {'✅' if usage_match else '❌'} (input diff: {diff_in})")
    all_results["usage_match"] = usage_match

    # Test 4: Through proxy vs direct
    print(f"\n📊 Proxy vs Direct Latency:")
    prompt = make_prompt(256)
    r_direct = await bench_concurrent(served_name, prompt, 1, stream=True, base_url=base_url)
    r_proxy = await bench_concurrent(served_name, prompt, 1, stream=True, base_url=PROXY_URL)
    
    d_dir = r_direct[0].get("duration_ms", 0)
    d_proxy = r_proxy[0].get("duration_ms", 0)
    overhead = ((d_proxy / d_dir) - 1) * 100 if d_dir > 0 else 0
    print(f"  Direct: {fmt_ms(d_dir)}, Proxy: {fmt_ms(d_proxy)}, Overhead: {overhead:+.1f}%")
    all_results["proxy_overhead_pct"] = round(overhead, 1)

    return all_results


async def main():
    # Only test currently running model(s)
    import urllib.request
    
    print("IFF v4.6.3 Model Benchmark")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()
    
    # Detect running models
    running = []
    for model_key, spec in MODEL_SPECS.items():
        port = int(DIRECT_BASES[model_key].split(":")[-1])
        try:
            urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3)
            running.append(model_key)
            print(f"✅ {model_key} (port {port}) — running")
        except:
            print(f"⚪ {model_key} (port {port}) — not running")
    
    print(f"\nRunning models: {running}")
    
    all_results = {}
    for model_key in running:
        result = await run_benchmark(model_key)
        if result:
            all_results[model_key] = result
    
    # Save results
    from pathlib import Path

    report_path = Path.home() / "inferfabric" / "bench_results" / f"v463-bench-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "version": "4.6.3",
        "results": all_results,
    }, indent=2, ensure_ascii=False, default=str))
    print(f"\n📄 Results saved to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
