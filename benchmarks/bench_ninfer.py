#!/usr/bin/env python3
"""NInfer Qwen38-27B-TXT benchmark.

Matrix: input length [1K, 10K, 100K tokens] x concurrency [1, 2, 4, 8].

Each request uses a UNIQUE synthetic prompt (distinct random seed) so prefix
cache hits do not flatter the numbers. Output capped at 512 tokens.

Metrics per request (from server `timings` + client wall clock):
  prompt_n / prompt_ms         -> prefill rate
  predicted_n / predicted_ms   -> decode rate (excl. queue)
  draft_n / draft_n_accepted   -> MTP acceptance
  client wall                  -> includes engine queue wait
  ttft_est = queue_wait + prompt_ms/1000

Usage: python3 bench_ninfer.py [--base http://localhost:8007] [--out DIR]
"""
import json, os, random, statistics, subprocess, sys, threading, time, urllib.request
from datetime import datetime

BASE = "http://localhost:8007"
MODEL = "Qwen38-27B-TXT"
MAX_OUT = 512
INPUTS = [1024, 10240, 102400]
CONCS = [1, 2, 4, 8]
OVERHEAD_S = 0.05

WORDS = """the model layer attention head token tensor core fp4 bf16 kernel cuda
graph prefill decode batch context cache page allocator gpu vram memory
throughput latency queue scheduler stream chunk split merge fuse quantize
calibrate scale amax block weight bias norm residual gate projection rotary
position embedding vocab sampler temperature logits softmax greedy draft
speculative accept reject mamba ssm state recurrence linear quadratic cost
gradient checkpoint flash attention head dim rope theta vision patch
merger projector temporal spatial window slide anchor slot host pinned
transfer copy async sync lock mutex thread worker pool drain flush evict
replace compact defrag allocate free release reserve budget limit cap max
""".split()


def gpu_vram_info() -> dict:
    """Query GPU VRAM via nvidia-smi. Returns {used_mb, total_mb} or None on failure."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=10, capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = [x.strip() for x in r.stdout.strip().split(",")]
            if len(parts) >= 2:
                return {"used_mb": int(parts[0]), "total_mb": int(parts[1])}
    except Exception:
        pass
    return None

def make_sentence(rng):
    n = rng.randint(8, 15)
    words = [rng.choice(WORDS) for _ in range(n)]
    s = " ".join(words)
    if rng.random() < 0.5:
        s += f" {rng.randint(1, 9999)}"
    return s + "."

def make_prompt(rng, target_chars):
    chunks, chars, section = [], 0, 0
    while chars < target_chars:
        section += 1
        head = f"## Section {section}\n"
        paras = []
        for _ in range(rng.randint(4, 7)):
            sent_n = rng.randint(4, 9)
            paras.append(" ".join(make_sentence(rng) for _ in range(sent_n)))
        chunk = head + "\n\n".join(paras) + "\n\n"
        chunks.append(chunk)
        chars += len(chunk)
    return "".join(chunks)[:target_chars]

def chat_nonstream(prompt, max_tokens=MAX_OUT, timeout=1200):
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    wall = time.perf_counter() - t0
    u, t = data.get("usage", {}), data.get("timings", {})
    prompt_ms = t.get("prompt_ms", 0.0)
    pred_ms = t.get("predicted_ms", 0.0)
    queue = max(0.0, wall - (prompt_ms + pred_ms) / 1000.0 - OVERHEAD_S)
    return {
        "wall_s": wall, "queue_s": queue,
        "ttft_est_s": queue + prompt_ms / 1000.0,
        "prompt_tokens": u.get("prompt_tokens", 0),
        "completion_tokens": u.get("completion_tokens", 0),
        "reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
        "prefill_tps": (t.get("prompt_n", 0) / (prompt_ms / 1000.0)) if prompt_ms > 0 else None,
        "decode_tps": (t.get("predicted_n", 0) / (pred_ms / 1000.0)) if pred_ms > 0 else None,
        "mtp_accept_rate": (t.get("draft_n_accepted", 0) / t.get("draft_n", 0)) if t.get("draft_n", 0) > 0 else None,
        "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
    }

def calibrate():
    rng = random.Random(42)
    probe = make_prompt(rng, 30000)
    res = chat_nonstream(probe, max_tokens=1)
    return res["prompt_tokens"] / len(probe), res["prompt_tokens"]

def run_cell(conc, target_tokens, tok_per_char):
    target_chars = int(target_tokens / max(tok_per_char, 1e-9))
    prompts = [make_prompt(random.Random(1000 * conc + i + 7), target_chars) for i in range(conc)]
    results, errors = [None] * conc, [None] * conc
    def worker(i):
        try:
            results[i] = chat_nonstream(prompts[i])
        except Exception as e:
            errors[i] = str(e)
    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(conc)]
    for t in threads: t.start()
    for t in threads: t.join()
    wall = time.perf_counter() - t0
    # Snapshot GPU VRAM after the run cell completes
    vram = gpu_vram_info()
    ok = [r for r in results if r]
    def agg(key):
        vals = [r[key] for r in ok if r.get(key) is not None]
        return {"mean": round(statistics.fmean(vals), 2), "min": round(min(vals), 2), "max": round(max(vals), 2)} if vals else None
    return {
        "concurrency": conc, "target_input_tokens": target_tokens,
        "actual_prompt_tokens": [r["prompt_tokens"] for r in ok],
        "wall_s": round(wall, 2), "success": len(ok),
        "failed": [e for e in errors if e],
        "vram": vram,
        "ttft_est_s": agg("ttft_est_s"),
        "prefill_tps": agg("prefill_tps"),
        "decode_tps": agg("decode_tps"),
        "mtp_accept_rate": agg("mtp_accept_rate"),
        "queue_s": agg("queue_s"),
        "completion_tokens_total": sum(r["completion_tokens"] for r in ok),
        "out_throughput_tps": round(sum(r["completion_tokens"] for r in ok) / wall, 2) if wall > 0 else None,
        "in_throughput_tps": round(sum(r["prompt_tokens"] for r in ok) / wall, 2) if wall > 0 else None,
    }

def main():
    global BASE
    out_dir = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "bench_results"
    os.makedirs(out_dir, exist_ok=True)
    tok_per_char, probe_tokens = calibrate()
    print(f"== calib: {probe_tokens}t / 30k chars = {tok_per_char:.4f} tok/char", flush=True)
    meta = {"ts": datetime.now().isoformat(timespec="seconds"), "model": MODEL, "tok_per_char": tok_per_char}
    cells = []
    for inp in INPUTS:
        for conc in CONCS:
            print(f"  > in={inp} conc={conc} ...", end=" ", flush=True)
            c = run_cell(conc, inp, tok_per_char)
            c["stage"] = f"in={inp} conc={conc}"
            cells.append(c)
            print(f"wall={c['wall_s']}s dec={c['decode_tps']}", flush=True)
            time.sleep(2)
    out = {"meta": meta, "cells": cells}
    path = os.path.join(out_dir, "ninfer-bench.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"== saved: {path}", flush=True)
    lines = ["# NInfer Qwen38-27B-TXT Benchmark", "",
             "| in_tok | con | wall_s | TTFT(s) | pf_tps | dec_tps | MTP | q_s | out_tps | in_tps | VRAM(MB) |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in cells:
        def g(d, k):
            return d[k] if d else "-"
        vram_str = f"{c['vram']['used_mb']}/{c['vram']['total_mb']}" if c.get('vram') else "-"
        lines.append(f"| {c['target_input_tokens']} | {c['concurrency']} | {c['wall_s']} | "
                     f"{g(c['ttft_est_s'],'mean')} | {g(c['prefill_tps'],'mean')} | "
                     f"{g(c['decode_tps'],'mean')} | {g(c['mtp_accept_rate'],'mean')} | "
                     f"{g(c['queue_s'],'mean')} | {c['out_throughput_tps']} | {c['in_throughput_tps']} | "
                     f"{vram_str} |")
    md = os.path.join(out_dir, "ninfer-bench.md")
    with open(md, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"== saved: {md}", flush=True)

if __name__ == "__main__":
    main()
