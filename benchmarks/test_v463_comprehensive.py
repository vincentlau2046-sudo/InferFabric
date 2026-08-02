#!/usr/bin/env python3
"""IFF v4.6.3 Comprehensive Test Suite

Tests:
  1. Module functional tests (CLI/Proxy/GPU State/Health/Rate Limit/DB)
  2. Metrics API consistency
  3. Dashboard data vs spec verification
  4. Performance monitoring coverage
  5. Streaming usage extraction (SSELineBuffer)
"""
import sys
import os
import json
import time
import sqlite3
import threading
import http.client
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)

PROXY = "localhost:8999"
IFF_DIR = Path.home() / "inferfabric"
DB_PATH = Path.home() / ".inferfabric" / "request_log.db"
STATE_DB = Path.home() / ".inferfabric" / "state.db"

results = {"pass": 0, "fail": 0, "skip": 0, "issues": []}

def test(name, condition, detail=""):
    if condition:
        results["pass"] += 1
        print(f"  ✅ {name}")
    else:
        results["fail"] += 1
        print(f"  ❌ {name}: {detail}")
        results["issues"].append(f"{name}: {detail}")

def skip(name, reason=""):
    results["skip"] += 1
    print(f"  ⏭️ {name}: {reason}")

def api_get(path):
    """GET request to proxy API"""
    try:
        conn = http.client.HTTPConnection(PROXY, timeout=10)
        conn.request("GET", path)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return data, resp.status
    except Exception as e:
        return {"error": str(e)}, 0

# ─── Test 1: Core Module Functional Tests ─────────────────────────
print("\n" + "="*60)
print("TEST 1: Core Module Functional Tests")
print("="*60)

# 1.1 Health endpoint
data, status = api_get("/health")
test("Health endpoint returns 200", status == 200)
test("Health has status=ok", data.get("status") == "ok")
test("Health has gpu_mode", "gpu_mode" in data)

# 1.2 System info
data, status = api_get("/system")
test("System info returns 200", status == 200)
test("System has version", data.get("version") == "4.6.3")
test("System has CPU info", "cpu_percent" in data and "cpu_cores" in data)
test("System has RAM info", "ram_total_gb" in data and "ram_used_gb" in data)
test("System has GPU info", "gpu_util_pct" in data and "gpu_power_w" in data)

# 1.3 Status endpoint
data, status = api_get("/status")
test("Status returns 200", status == 200)
test("Status has gpu_mode", "gpu_mode" in data)
test("Status has active_services", "active_services" in data)
test("Status has services_health", "services_health" in data)

# 1.4 Models endpoint
data, status = api_get("/models")
test("Models returns 200", status == 200)
test("Models is a list", isinstance(data, list))
test("Models includes bge-m3", any(m.get("name") == "bge-m3" for m in data) if isinstance(data, list) else False)
test("Models includes qwen35-9b", any(m.get("name") == "qwen35-9b" for m in data) if isinstance(data, list) else False)

# 1.5 v1/models endpoint (OpenAI compat)
data, status = api_get("/v1/models")
test("v1/models returns 200", status == 200)

# ─── Test 2: Metrics API Consistency ──────────────────────────────
print("\n" + "="*60)
print("TEST 2: Metrics API Consistency")
print("="*60)

for window in ["1h", "24h", "7d", "all"]:
    data, status = api_get(f"/api/metrics?window={window}")
    test(f"Metrics {window} returns 200", status == 200)
    
    # Bug: Empty window returns {"window": "1h", "total": 0} - missing fields
    has_total_requests = "total_requests" in data
    has_success = "success" in data
    has_fail = "fail" in data
    has_models = "models" in data
    
    if data.get("total") == 0 and not has_total_requests:
        test(f"Metrics {window} has full schema (BUG: empty returns truncated)", False,
             "Empty window returns {window, total} instead of full schema")
    else:
        test(f"Metrics {window} has total_requests", has_total_requests)
        test(f"Metrics {window} has success/fail", has_success and has_fail)
        test(f"Metrics {window} has models dict", has_models)

# Test 24h window with actual data
data, status = api_get("/api/metrics?window=24h")
if data.get("total_requests", 0) > 0:
    test("24h has success_rate", "success_rate" in data)
    test("24h has cost_yuan", "cost_yuan" in data)
    
    for model_name, mdata in data.get("models", {}).items():
        test(f"Model {model_name} has requests", "requests" in mdata)
        test(f"Model {model_name} has tokens_in/out", "tokens_in" in mdata and "tokens_out" in mdata)
        test(f"Model {model_name} has ttft p50/p95/p99", 
             "ttft_p50" in mdata and "ttft_p95" in mdata and "ttft_p99" in mdata)
        test(f"Model {model_name} has duration p50/p95",
             "duration_p50" in mdata and "duration_p95" in mdata)

# ─── Test 3: Dashboard Data vs PR Spec ────────────────────────────
print("\n" + "="*60)
print("TEST 3: Dashboard Data vs PR Spec")
print("="*60)

# G-3b spec: 7 panels
# Panel 1: Overview (total, success, fail, rate) ✅
# Panel 2: Token table (model, requests, input, output) ✅
# Panel 3: Latency (model, TTFT p50/p95/p99, E2E p50/p95) ✅
# Panel 4: Cost (total + breakdown by model) ✅
# Panel 5: Request log — PLACEHOLDER (未实现)
# Panel 6: vLLM performance (retained) ✅
# Panel 7: GPU real-time (retained) ✅

monitor_js = (IFF_DIR / "inferfabric/dashboard/js/monitor.js").read_text()

test("Dashboard has renderOverview", "renderOverview" in monitor_js)
test("Dashboard has renderTokens", "renderTokens" in monitor_js)
test("Dashboard has renderLatency", "renderLatency" in monitor_js)
test("Dashboard has renderCost", "renderCost" in monitor_js)

# Check request log panel implementation
has_request_log_api = "/api/request_log" in monitor_js or "loadRequestLog" in monitor_js
test("Request log panel has API call", has_request_log_api)

# Check the placeholder text
has_placeholder = "请求日志需 access log API" in monitor_js or "后续实现" in monitor_js
test("Request log is placeholder (BUG vs spec)", not has_placeholder,
     "Panel 5 is placeholder — spec requires functional request log")

# Bug: Metrics uses served_name (vllm_qwen27b_vl) instead of friendly model name
data, status = api_get("/api/metrics?window=24h")
if data.get("models"):
    model_names = list(data["models"].keys())
    has_served_name = any("vllm_" in n or "_" in n for n in model_names)
    test("Model names use served_name not friendly name", has_served_name,
         f"Dashboard shows '{model_names[0]}' instead of 'Qwen3.6-27B VL'")

# ─── Test 4: Performance Monitoring Coverage ──────────────────────
print("\n" + "="*60)
print("TEST 4: Performance Monitoring Coverage")
print("="*60)

# 4.1 SSELineBuffer coverage
sse_buffer = (IFF_DIR / "inferfabric/proxy/sse_buffer.py").read_text()
test("SSELineBuffer: feed() method exists", "def feed" in sse_buffer)
test("SSELineBuffer: flush() method exists", "def flush" in sse_buffer)
test("SSELineBuffer: usage extraction", "prompt_tokens" in sse_buffer and "completion_tokens" in sse_buffer)
test("SSELineBuffer: [DONE] handling", "[DONE]" in sse_buffer)

# 4.2 MetricsAggregator coverage
agg = (IFF_DIR / "inferfabric/metrics_aggregator.py").read_text()
test("Aggregator: sliding window", "window_s" in agg)
test("Aggregator: quantile calculation", "def quantile" in agg)
test("Aggregator: model grouping", "by_model" in agg)
test("Aggregator: cost estimation", "cost_yuan" in agg and "price_config" in agg)
test("Aggregator: SQLite replay", "_replay_from_db" in agg)

# 4.3 RequestLogDB coverage
rldb = (IFF_DIR / "inferfabric/request_log_db.py").read_text()
test("RequestLogDB: WAL mode", "WAL" in rldb or "journal_mode" in rldb)
test("RequestLogDB: auto_vacuum", "auto_vacuum" in rldb)
test("RequestLogDB: buffer flush", "flush" in rldb)
test("RequestLogDB: query method", "query_request_log" in rldb)
test("RequestLogDB: cleanup/retention", "cleanup" in rldb or "retention" in rldb or "delete" in rldb.lower())

# 4.4 Prometheus metrics (vLLM)
metrics_py = (IFF_DIR / "inferfabric/proxy/metrics.py").read_text()
test("vLLM metrics: KV cache", "kv_cache" in metrics_py.lower() or "gpu_cache" in metrics_py)
test("vLLM metrics: TTFT tracking", "ttft" in metrics_py.lower())
test("vLLM metrics: EMA throughput", "EMA" in metrics_py or "ema" in metrics_py)

# 4.5 Actual DB data integrity
if DB_PATH.exists():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT count(*) FROM request_log")
    total = c.fetchone()[0]
    test(f"SQLite has data ({total} rows)", total > 0)
    
    # Check schema completeness
    c.execute("PRAGMA table_info(request_log)")
    columns = {r[1] for r in c.fetchall()}
    required = {"model", "status", "tokens_in", "tokens_out", "ttft_ms", "duration_ms", "route", "timestamp"}
    missing = required - columns
    test("request_log schema has all required columns", not missing, f"Missing: {missing}")
    
    # Check data quality
    c.execute("SELECT count(*) FROM request_log WHERE tokens_in = 0 AND tokens_out = 0 AND status = 200")
    zero_tokens_ok = c.fetchone()[0]
    test("No status=200 with zero tokens", zero_tokens_ok == 0,
         f"{zero_tokens_ok} rows with status=200 but 0 tokens")
    
    # BUG: Failed requests (429) should not pollute latency metrics
    c.execute("SELECT count(*) FROM request_log WHERE status = 429 AND duration_ms > 0")
    fail_with_duration = c.fetchone()[0]
    test("No 429 with duration_ms (data quality)", fail_with_duration == 0,
         f"{fail_with_duration} failed requests with duration_ms polluting latency stats")
    
    # Check streaming usage extraction — tokens_out should be non-zero for most requests
    c.execute("SELECT count(*) FROM request_log WHERE tokens_out > 0 AND status = 200")
    with_tokens = c.fetchone()[0]
    c.execute("SELECT count(*) FROM request_log WHERE status = 200")
    total_ok = c.fetchone()[0]
    if total_ok > 0:
        pct = with_tokens / total_ok * 100
        test(f"Streaming usage extraction coverage", pct > 80,
             f"Only {pct:.1f}% of OK requests have tokens_out > 0")
    
    conn.close()
else:
    skip("SQLite DB exists", "DB not found")

# ─── Test 5: Rate Limiter (v4.6.3 observe mode) ──────────────────
print("\n" + "="*60)
print("TEST 5: Rate Limiter v4.6.3 Observe Mode")
print("="*60)

ratelimit = (IFF_DIR / "inferfabric/ratelimit.py").read_text()
test("DualGateLimiter exists", "class DualGateLimiter" in ratelimit)
test("Observe mode implemented", "observe" in ratelimit)
test("Reject mode implemented", "reject" in ratelimit)
test("RPM gate skip when rpm=0", "rpm=0" in ratelimit or "rpm == 0" in ratelimit or "server_rpm" in ratelimit)

# Check that observe mode does not return 429
# Look for the acquire logic
observe_no_reject = "observe" in ratelimit and "不拒绝" in ratelimit
test("Observe mode: non-blocking RPM gate", observe_no_reject)

# Check iff.yaml config
config_path = Path.home() / ".inferfabric" / "iff.yaml"
if config_path.exists():
    config = config_path.read_text()
    test("iff.yaml has rate_limit config", "rate_limit" in config)
    test("iff.yaml observe mode", "mode: observe" in config)
    test("iff.yaml server_rpm=0 (unlimited)", "server_rpm: 0" in config)

# ─── Test 6: Dashboard Bug Analysis ───────────────────────────────
print("\n" + "="*60)
print("TEST 6: Dashboard Bug Analysis")
print("="*60)

# Bug 1: Empty window missing fields
data, _ = api_get("/api/metrics?window=1h")
if data.get("total") == 0:
    test("BUG: Empty window returns truncated schema", 
         "total_requests" not in data,
         "Should return full schema with total_requests=0, success=0, etc.")

# Bug 2: Failed requests included in latency
if DB_PATH.exists():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    # Check aggregator includes 429 in duration
    c.execute("SELECT count(*) FROM request_log WHERE status >= 400 AND duration_ms > 0")
    fail_dur = c.fetchone()[0]
    test("BUG: Failed requests counted in latency", fail_dur > 0,
         f"{fail_dur} failed requests with duration_ms counted in p50/p95/p99")
    
    # Actual impact: p95 with vs without 429
    c.execute("SELECT duration_ms FROM request_log WHERE status = 200 ORDER BY duration_ms")
    all_ok = [r[0] for r in c.fetchall()]
    c.execute("SELECT duration_ms FROM request_log ORDER BY duration_ms")
    all_req = [r[0] for r in c.fetchall()]
    
    def p95(data):
        if not data: return 0
        idx = int(0.95 * (len(data) - 1))
        return sorted(data)[idx]
    
    p95_ok = p95(all_ok)
    p95_all = p95(all_req)
    print(f"  📊 Duration p95 (status=200 only): {p95_ok:.0f}ms")
    print(f"  📊 Duration p95 (all including 429): {p95_all:.0f}ms")
    print(f"  📊 Inflation: {((p95_all/p95_ok)-1)*100:.1f}%" if p95_ok > 0 else "  📊 N/A")
    
    conn.close()

# Bug 3: Request log panel not implemented
test("BUG: Request log panel is placeholder", has_placeholder)

# ─── Summary ──────────────────────────────────────────────────────
print("\n" + "="*60)
print("SUMMARY")
print("="*60)
print(f"  ✅ Pass: {results['pass']}")
print(f"  ❌ Fail: {results['fail']}")
print(f"  ⏭️  Skip: {results['skip']}")

if results["issues"]:
    print("\n🔴 Issues Found:")
    for i, issue in enumerate(results["issues"], 1):
        print(f"  {i}. {issue}")

# Save results
report = {
    "timestamp": datetime.now().isoformat(),
    "version": "4.6.3",
    "pass": results["pass"],
    "fail": results["fail"],
    "skip": results["skip"],
    "issues": results["issues"],
}
report_path = IFF_DIR / "bench_results" / f"v463-test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
print(f"\n📄 Report saved to {report_path}")
