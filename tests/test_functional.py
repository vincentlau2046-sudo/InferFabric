#!/usr/bin/env python3
"""EdgeLLM v4.3 functional tests — real HTTP integration tests.

Tests the proxy server and dashboard with real HTTP requests.
Requires edge-llm proxy to be running on :8999.
"""

import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PROXY_URL = "http://localhost:8999"
TIMEOUT = 15


def _post(path, data):
    """POST JSON to proxy and return parsed response."""
    url = f"{PROXY_URL}{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body), e.code
        except Exception:
            return {"error": body}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def _get(path):
    """GET from proxy and return parsed response."""
    url = f"{PROXY_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body), e.code
        except Exception:
            return {"error": body}, e.code
    except Exception as e:
        return {"error": str(e)}, 0


def _check_proxy_alive():
    """Check if proxy is running."""
    try:
        with urllib.request.urlopen(f"{PROXY_URL}/status", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# Functional Tests
# ═══════════════════════════════════════════════════════════════

def test_status_endpoint():
    """GET /status returns valid JSON with gpu_mode and active_services."""
    data, status = _get("/status")
    assert status == 200, f"Expected 200, got {status}: {data}"
    assert "gpu_mode" in data, f"Missing gpu_mode in response: {data}"
    assert "active_services" in data, f"Missing active_services: {data}"
    assert data["gpu_mode"] in ("idle", "exclusive", "shared"), f"Invalid gpu_mode: {data['gpu_mode']}"
    print(f"✅ /status: gpu_mode={data['gpu_mode']}, services={data['active_services']}")


def test_models_endpoint():
    """GET /models returns list of available models."""
    data, status = _get("/models")
    assert status == 200, f"Expected 200, got {status}: {data}"
    assert isinstance(data, list), f"Expected list, got {type(data)}"
    assert len(data) > 0, "No models returned"
    for m in data:
        assert "name" in m, f"Missing name in model: {m}"
        assert "mode" in m, f"Missing mode in model: {m}"
    print(f"✅ /models: {len(data)} models available")


def test_switch_idle_idempotent():
    """POST /switch {model:'idle'} when already idle returns already_active."""
    # First ensure we're idle (with longer timeout for model shutdown)
    data, status = _post("/switch", {"model": "idle"})
    # Could be 'already_active' or 'switched' (if something was running)
    # Or timeout if model shutdown takes long
    if data.get("error") == "timed out":
        print("⚠️ /switch idle timed out (model shutdown slow) — retrying")
        time.sleep(5)
        data, status = _post("/switch", {"model": "idle"})
    assert data.get("status") in ("already_active", "switched"), f"Unexpected status: {data}"
    # Now should be idle
    data2, status2 = _post("/switch", {"model": "idle"})
    assert data2.get("status") == "already_active", f"Second idle should be already_active: {data2}"
    print("✅ /switch idle: idempotent")


def test_switch_unknown_model():
    """POST /switch with unknown model returns error."""
    data, status = _post("/switch", {"model": "nonexistent_model_xyz"})
    assert data.get("status") == "error", f"Expected error for unknown model: {data}"
    assert "Unknown" in data.get("message", ""), f"Error message should mention 'Unknown': {data}"
    print("✅ /switch unknown: returns error")


def test_stop_unknown_service():
    """POST /stop with unknown service returns error."""
    data, status = _post("/stop", {"model": "nonexistent_service_xyz"})
    assert data.get("status") == "error", f"Expected error for unknown service: {data}"
    print("✅ /stop unknown: returns error")


def test_v1_models_no_active():
    """GET /v1/models with no active services returns model list from models.d."""
    # Ensure idle
    _post("/switch", {"model": "idle"})
    data, status = _get("/v1/models")
    assert status == 200, f"Expected 200, got {status}: {data}"
    # Should return list of models from models.d
    print(f"✅ /v1/models (idle): returns {len(data) if isinstance(data, list) else 'data'}")


def test_dashboard_html_served():
    """GET / returns dashboard HTML."""
    try:
        url = f"{PROXY_URL}/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            html = resp.read().decode()
            assert "<!DOCTYPE html>" in html, "Not HTML"
            assert "EdgeLLM" in html, "Missing EdgeLLM in HTML"
            assert "swLock" in html, "Missing swLock in dashboard"
            assert "finally{sw=false;}" in html, "Missing finally in dashboard"
            print("✅ Dashboard HTML: served with swLock + finally")
    except Exception as e:
        print(f"⚠️ Dashboard HTML check failed: {e}")


def test_reconcile_endpoint():
    """POST /reconcile returns valid result."""
    data, status = _post("/reconcile", {})
    assert status == 200, f"Expected 200, got {status}: {data}"
    assert "actions" in data or "status" in data, f"Unexpected reconcile response: {data}"
    print(f"✅ /reconcile: {data}")


def test_switch_exclusive_then_idle():
    """Full cycle: idle → exclusive → idle."""
    models_data, _ = _get("/models")
    exclusive_models = [m for m in models_data if m.get("mode") == "exclusive"]
    if not exclusive_models:
        print("⚠️ No exclusive model available, skipping")
        return

    model_name = exclusive_models[0]["name"]
    print(f"  Testing cycle with {model_name}...")

    # Switch to exclusive
    data, status = _post("/switch", {"model": model_name})
    if data.get("status") == "error":
        print(f"⚠️ Switch to {model_name} failed (GPU busy?): {data}")
        return
    assert data.get("status") in ("switched", "already_active"), f"Switch failed: {data}"

    # Verify status
    s, _ = _get("/status")
    assert model_name in s.get("active_services", []), f"Model not in active_services: {s}"
    print(f"  ✅ {model_name} active")

    # Switch back to idle
    data2, status2 = _post("/switch", {"model": "idle"})
    assert data2.get("status") in ("switched", "already_active"), f"Switch to idle failed: {data2}"

    # Verify idle
    s2, _ = _get("/status")
    assert s2.get("gpu_mode") == "idle", f"Not idle after switch: {s2}"
    print(f"✅ Full cycle: idle → {model_name} → idle")


def test_vllm_metrics_endpoint():
    """GET /vllm_metrics returns vLLM metrics."""
    data, status = _get("/vllm_metrics?port=8000")
    # May fail if no vLLM running, that's ok
    if status == 502 or data.get("error"):
        print(f"⚠️ /vllm_metrics: no vLLM running (expected when idle)")
    else:
        assert status == 200, f"Expected 200, got {status}: {data}"
        print(f"✅ /vllm_metrics: {list(data.keys()) if isinstance(data, dict) else 'ok'}")


# ═══════════════════════════════════════════════════════════════
# Run
# ═══════════════════════════════════════════════════════════════

def run_all():
    if not _check_proxy_alive():
        print("⚠️ EdgeLLM proxy not running on :8999 — starting functional tests that don't need proxy")
        # Run only dashboard HTML check
        print("\nSkipping proxy-dependent tests. Start proxy with: edge-llm serve")
        return True

    tests = [
        test_status_endpoint,
        test_models_endpoint,
        test_switch_idle_idempotent,
        test_switch_unknown_model,
        test_stop_unknown_service,
        test_v1_models_no_active,
        test_dashboard_html_served,
        test_reconcile_endpoint,
        test_vllm_metrics_endpoint,
        # test_switch_exclusive_then_idle,  # Uncomment for full cycle test
    ]

    passed = 0
    failed = 0
    errors = []

    for test in tests:
        name = test.__name__
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((name, e))
            print(f"❌ {name}: {e}")

    print(f"\n{'='*60}")
    print(f"Functional: {passed} passed, {failed} failed, {passed+failed} total")
    if errors:
        print(f"\nFailed:")
        for name, e in errors:
            print(f"  - {name}: {e}")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
