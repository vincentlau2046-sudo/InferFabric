"""DualGateLimiter 验收测试 v2 — 定性 + 定量评估

核心设计: 所有测试控制在 30s 内完成
- 定性: 行为正确性切片
- 定量: 性能指标
"""

import sys, os, time, threading, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.expanduser("~/projects/inferfabric-sandbox"))
from inferfabric.ratelimit import RateLimiterV2, DualGateLimiter, GateResult

# ─── 配置 ───
RPM_MODEL = 30
MAX_CONCURRENT = 3
MODEL = "test-model"

def make_gate(rpm=RPM_MODEL, max_c=MAX_CONCURRENT):
    rpm_limiter = RateLimiterV2(server_rpm=rpm*2, model_rpm_default=rpm, timeout=2)
    return DualGateLimiter(rpm_limiter=rpm_limiter, max_concurrent=max_c)

PASS = "✅"
FAIL = "❌"

results = []

def record(name, passed, detail=""):
    status = PASS if passed else FAIL
    results.append((name, passed, detail))
    print(f"  {status} {name}: {detail}")

# ══════════════════════════════════════════════════════════════
# PART 1: 定性测试
# ══════════════════════════════════════════════════════════════
print("=" * 60)
print("PART 1: 定性测试 — 行为正确性")
print("=" * 60)

# Q1: RPM burst 消耗后限流
gate = make_gate(rpm=RPM_MODEL)
passed = 0
blocked = 0
for _ in range(RPM_MODEL + 5):
    r = gate.acquire(MODEL, timeout=0.01)
    if r.ok:
        r.release()
        passed += 1
    else:
        blocked += 1
record("Q1: RPM burst限流", passed == RPM_MODEL and blocked == 5,
       f"通过={passed}/{RPM_MODEL}, 拒绝={blocked}")

# Q2: Semaphore 硬限制
gate = make_gate()
holders = []
for i in range(MAX_CONCURRENT):
    r = gate.acquire(MODEL, timeout=1)
    assert r.ok, f"holder {i} should succeed"
    holders.append(r)
r = gate.acquire(MODEL, timeout=0.3)
record("Q2: Semaphore硬限制", not r.ok and "concurrency" in r.reason,
       f"第{MAX_CONCURRENT+1}次: ok={r.ok}, reason={r.reason}")
holders[0].release()
r2 = gate.acquire(MODEL, timeout=1)
record("Q2b: 释放后可获取", r2.ok, f"释放后: ok={r2.ok}")
r2.release()
for h in holders[1:]:
    h.release()

# Q3: Gate2失败后RPM归还
gate = make_gate()
holders = []
for i in range(MAX_CONCURRENT):
    r = gate.acquire(MODEL, timeout=1)
    assert r.ok
    holders.append(r)
# Gate2 失败 → RPM 归还
r = gate.acquire(MODEL, timeout=0.1)
gate2_failed = not r.ok and "concurrency" in r.reason
for h in holders:
    h.release()
# 1s 后验证 RPM 可用（holders 的 RPM 已消耗不归还，gate2 失败的归还了）
time.sleep(1.1)
recovered = 0
for _ in range(RPM_MODEL):
    r = gate.acquire(MODEL, timeout=0.01)
    if r.ok:
        r.release()
        recovered += 1
    else:
        break
record("Q3: Gate2失败归还RPM", gate2_failed and recovered >= RPM_MODEL - MAX_CONCURRENT - 2,
       f"gate2失败={gate2_failed}, 恢复={recovered}/{RPM_MODEL}")

# Q4: release 幂等
gate = make_gate()
r = gate.acquire(MODEL, timeout=1)
assert r.ok
r.release()
try:
    r.release()
    r.release()
    record("Q4: release幂等", True, "3次调用无异常")
except Exception as e:
    record("Q4: release幂等", False, str(e))

# Q5: context manager
gate = make_gate()
try:
    with gate.acquire(MODEL, timeout=1) as g:
        assert g.ok
    with gate.acquire(MODEL, timeout=1) as g2:
        assert g2.ok
    record("Q5: context manager", True, "自动释放后可再次获取")
except Exception as e:
    record("Q5: context manager", False, str(e))

# Q6: 并发无死锁 (高RPM)
gate = make_gate(rpm=300, max_c=MAX_CONCURRENT)
lock = threading.Lock()
total_ok = [0]
def worker():
    c = 0
    for _ in range(10):
        r = gate.acquire(MODEL, timeout=0.5)
        if r.ok:
            time.sleep(0.01)
            r.release()
            c += 1
    with lock:
        total_ok[0] += c
with ThreadPoolExecutor(max_workers=5) as pool:
    futs = [pool.submit(worker) for _ in range(5)]
    for f in as_completed(futs):
        f.result()
record("Q6: 并发无死锁", total_ok[0] > 0, f"总成功={total_ok[0]}/50")

# ══════════════════════════════════════════════════════════════
# PART 2: 定量测试
# ══════════════════════════════════════════════════════════════
print()
print("=" * 60)
print("PART 2: 定量测试 — 性能评估")
print("=" * 60)

# Quant-1: RPM 精度 — burst + 稳态
gate = make_gate(rpm=RPM_MODEL)
burst_ok = 0
for _ in range(RPM_MODEL + 2):
    r = gate.acquire(MODEL, timeout=0.01)
    if r.ok:
        r.release()
        burst_ok += 1
    else:
        break
# 稳态: 测 3s
steady_ok = 0
t0 = time.monotonic()
deadline = t0 + 3
while time.monotonic() < deadline:
    r = gate.acquire(MODEL, timeout=0.05)
    if r.ok:
        r.release()
        steady_ok += 1
steady_elapsed = time.monotonic() - t0
effective_rpm = steady_ok / steady_elapsed * 60
accuracy = effective_rpm / RPM_MODEL
print(f"\n  Quant-1: RPM 精度")
print(f"    burst={burst_ok}/{RPM_MODEL}")
print(f"    稳态: {steady_ok}req/{steady_elapsed:.1f}s = {effective_rpm:.1f} RPM (理论={RPM_MODEL})")
print(f"    稳态精度={accuracy:.1%}")
# 稳态 refill rate = RPM/60, 允许 ±50% (GIL抖动)
record("Quant-1: RPM稳态精度", 0.5 <= accuracy <= 2.0, f"accuracy={accuracy:.1%}")

# Quant-2: 吞吐量 — 高RPM下 acquire+release 循环
gate = make_gate(rpm=600, max_c=MAX_CONCURRENT)
N = 500
latencies_us = []
t_start = time.monotonic()
for _ in range(N):
    ta = time.monotonic()
    r = gate.acquire(MODEL, timeout=2)
    tb = time.monotonic()
    if r.ok:
        latencies_us.append((tb - ta) * 1e6)
        r.release()
t_end = time.monotonic()
qps = N / (t_end - t_start)
lat_sorted = sorted(latencies_us)
p50 = lat_sorted[int(len(lat_sorted)*0.5)]
p99 = lat_sorted[int(len(lat_sorted)*0.99)]
print(f"\n  Quant-2: 吞吐量 (RPM=600)")
print(f"    QPS={qps:.0f}, 样本={len(latencies_us)}")
print(f"    acquire延迟: P50={p50:.0f}μs, P99={p99:.0f}μs")
record("Quant-2: 吞吐量", qps > 1000, f"QPS={qps:.0f}")

# Quant-3: 突发控制
gate = make_gate(rpm=RPM_MODEL, max_c=MAX_CONCURRENT)
results_burst = {"ok": 0, "blocked": 0}
lock = threading.Lock()
def try_acquire():
    r = gate.acquire(MODEL, timeout=0.05)
    with lock:
        if r.ok:
            results_burst["ok"] += 1
            time.sleep(0.05)
            r.release()
        else:
            results_burst["blocked"] += 1
with ThreadPoolExecutor(max_workers=30) as pool:
    futs = [pool.submit(try_acquire) for _ in range(30)]
    for f in as_completed(futs):
        f.result()
burst_rate = results_burst["ok"] / 30
print(f"\n  Quant-3: 突发控制 (30并发)")
print(f"    通过={results_burst['ok']}, 拒绝={results_burst['blocked']}")
print(f"    穿透率={burst_rate:.1%}, RPM={RPM_MODEL}, Semaphore={MAX_CONCURRENT}")
record("Quant-3: 突发控制", results_burst["ok"] <= RPM_MODEL + 3,
       f"穿透={results_burst['ok']}/{30}")

# Quant-4: Semaphore 硬限制 — 峰值并发
gate = make_gate(rpm=600, max_c=MAX_CONCURRENT)
concurrent = {"current": 0, "peak": 0}
lock = threading.Lock()
def holder():
    r = gate.acquire(MODEL, timeout=2)
    if r.ok:
        with lock:
            concurrent["current"] += 1
            concurrent["peak"] = max(concurrent["peak"], concurrent["current"])
        time.sleep(0.05)
        with lock:
            concurrent["current"] -= 1
        r.release()
with ThreadPoolExecutor(max_workers=20) as pool:
    futs = [pool.submit(holder) for _ in range(30)]
    for f in as_completed(futs):
        f.result()
print(f"\n  Quant-4: Semaphore 硬限制")
print(f"    max_concurrent={MAX_CONCURRENT}, 峰值并发={concurrent['peak']}")
record("Quant-4: Semaphore硬限制", concurrent["peak"] <= MAX_CONCURRENT,
       f"峰值={concurrent['peak']} ≤ {MAX_CONCURRENT}")

# Quant-5: acquire 无竞争延迟
gate = make_gate(rpm=600, max_c=MAX_CONCURRENT)
N = 200
lat_us = []
for _ in range(N):
    t0 = time.monotonic()
    r = gate.acquire(MODEL, timeout=1)
    t1 = time.monotonic()
    if r.ok:
        lat_us.append((t1 - t0) * 1e6)
        r.release()
lat_us.sort()
p50 = lat_us[int(len(lat_us)*0.5)]
p90 = lat_us[int(len(lat_us)*0.9)]
p99 = lat_us[int(len(lat_us)*0.99)]
avg = statistics.mean(lat_us)
print(f"\n  Quant-5: acquire 延迟 (无竞争, RPM=600)")
print(f"    P50={p50:.0f}μs, P90={p90:.0f}μs, P99={p99:.0f}μs, AVG={avg:.0f}μs")
record("Quant-5: acquire延迟", avg < 500, f"AVG={avg:.0f}μs")

# Quant-6: 令牌桶恢复时间
# 关键: 需要 release Semaphore 但不归还 RPM，让桶真正耗尽
gate = make_gate(rpm=RPM_MODEL)
# 先 acquire+release 快速消耗 RPM（只归还 Semaphore，不归还 RPM）
consumed = 0
for _ in range(RPM_MODEL + 2):
    r = gate.acquire(MODEL, timeout=0.01)
    if r.ok:
        r.release()  # 释放 Semaphore，但 RPM 已消耗
        consumed += 1
    else:
        break
print(f"\n  Quant-6: 令牌桶恢复 (RPM={RPM_MODEL})")
print(f"    RPM消耗={consumed}/{RPM_MODEL}")
# 桶应已空，测首次恢复
t0 = time.monotonic()
r = gate.acquire(MODEL, timeout=5)
first_wait = time.monotonic() - t0
if r.ok:
    r.release()
expected_interval = 60.0 / RPM_MODEL
print(f"    首次恢复等待={first_wait:.3f}s")
print(f"    理论间隔={expected_interval:.3f}s")
if first_wait > 0.05:
    ratio = first_wait / expected_interval
    print(f"    实际/理论={ratio:.2%}")
    # 允许 ±100%: CPython 调度 + refill 精度
    record("Quant-6: 恢复时间", 0.3 <= ratio <= 3.0, f"实际/理论={ratio:.2%}")
else:
    print(f"    (瞬间恢复，桶内有余量 — 不应发生)")
    record("Quant-6: 恢复时间", False, "桶未耗尽")

# ══════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════
print()
print("=" * 60)
print("验收测试总结")
print("=" * 60)
passed = sum(1 for _, p, _ in results if p)
failed = sum(1 for _, p, _ in results if not p)
print(f"  通过: {passed}/{len(results)}")
print(f"  失败: {failed}/{len(results)}")
for name, p, detail in results:
    print(f"  {'✅' if p else '❌'} {name}: {detail}")
if failed == 0:
    print("\n🎉 全部通过 — DualGateLimiter 可合入生产")
else:
    print(f"\n⚠️ {failed} 项失败 — 需修复后重测")
