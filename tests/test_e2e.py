#!/usr/bin/env python3
"""EdgeLLM E2E tests — real model lifecycle with GPU.

Requires: edge-llm proxy running on :8999, GPU available.
Tests: start model → verify healthy → release → verify idle → switch again.
"""

import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

PROXY = "http://localhost:8999"
T = 180  # timeout for model operations


def post(path, data, timeout=T):
    body = json.dumps(data).encode()
    req = urllib.request.Request(f"{PROXY}{path}", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code
    except Exception as e:
        return {"error": str(e)}, 0


def get(path, timeout=10):
    try:
        with urllib.request.urlopen(f"{PROXY}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode()), resp.status
    except Exception as e:
        return {"error": str(e)}, 0


def ensure_idle():
    """Force idle state before test."""
    d, _ = get("/status")
    if d.get("gpu_mode") != "idle":
        print("  → switching to idle first...")
        post("/switch", {"model": "idle"}, timeout=120)
        time.sleep(3)
    d2, _ = get("/status")
    assert d2.get("gpu_mode") == "idle", f"Failed to reach idle: {d2}"


def test_shared_lifecycle():
    """Shared model: start → verify → stop (释放) → verify idle."""
    print("\n=== Shared model lifecycle (qwen35-9b) ===")
    ensure_idle()

    # 1. Start
    print("  [1] Starting qwen35-9b...")
    d, s = post("/switch", {"model": "qwen35-9b"})
    assert d.get("status") == "switched", f"Start failed: {d}"
    print(f"      ✅ Started in {d.get('elapsed_sec')}s")

    # 2. Verify healthy
    st, _ = get("/status")
    assert "qwen35-9b" in st.get("active_services", []), f"Not in active: {st}"
    assert st["gpu_mode"] == "shared", f"Not shared: {st}"
    health = st.get("services_health", {}).get("qwen35-9b", "")
    print(f"      Health: {health}")

    # 3. Test /v1/models returns model info
    models, _ = get("/v1/models")
    if isinstance(models, dict) and "data" in models:
        model_ids = [m["id"] for m in models["data"]]
        print(f"      /v1/models: {model_ids}")
    else:
        print(f"      /v1/models: {models}")

    # 4. Release (shared → POST /stop)
    print("  [2] Releasing qwen35-9b (POST /stop)...")
    d2, s2 = post("/stop", {"model": "qwen35-9b"})
    assert d2.get("status") == "stopped", f"Stop failed: {d2}"
    print(f"      ✅ Stopped: {d2.get('message', '')}")

    # 5. Verify idle
    time.sleep(2)
    st2, _ = get("/status")
    assert st2["gpu_mode"] == "idle", f"Not idle after stop: {st2}"
    assert st2["active_services"] == [], f"Services not empty: {st2}"
    print(f"      ✅ GPU idle, VRAM: {st2.get('gpu_used_mb')} MiB")

    return True


def test_exclusive_lifecycle():
    """Exclusive model: start → verify → release (switch idle) → verify idle."""
    print("\n=== Exclusive model lifecycle (qwen36-27b) ===")
    ensure_idle()

    # 1. Start
    print("  [1] Starting qwen36-27b...")
    d, s = post("/switch", {"model": "qwen36-27b"})
    assert d.get("status") == "switched", f"Start failed: {d}"
    print(f"      ✅ Started in {d.get('elapsed_sec')}s")

    # 2. Verify healthy
    st, _ = get("/status")
    assert "qwen36-27b" in st.get("active_services", []), f"Not in active: {st}"
    assert st["gpu_mode"] == "exclusive", f"Not exclusive: {st}"
    health = st.get("services_health", {}).get("qwen36-27b", "")
    print(f"      Health: {health}")

    # 3. Release (exclusive → POST /switch idle)
    print("  [2] Releasing qwen36-27b (POST /switch idle)...")
    d2, s2 = post("/switch", {"model": "idle"}, timeout=120)
    assert d2.get("status") in ("switched", "already_active"), f"Release failed: {d2}"
    print(f"      ✅ Released in {d2.get('elapsed_sec', '?')}s")

    # 4. Verify idle
    time.sleep(3)
    st2, _ = get("/status")
    assert st2["gpu_mode"] == "idle", f"Not idle: {st2}"
    print(f"      ✅ GPU idle, VRAM: {st2.get('gpu_used_mb')} MiB")

    return True


def test_release_button_idempotent():
    """Double release should not break state."""
    print("\n=== Double release safety ===")
    ensure_idle()

    # Start shared model
    print("  [1] Starting qwen35-9b...")
    d, _ = post("/switch", {"model": "qwen35-9b"})
    assert d.get("status") == "switched", f"Start failed: {d}"

    # Release once
    print("  [2] First release...")
    d1, _ = post("/stop", {"model": "qwen35-9b"})
    assert d1.get("status") == "stopped", f"First stop failed: {d1}"

    # Release again (should error gracefully, not crash)
    print("  [3] Second release (should be graceful error)...")
    d2, _ = post("/stop", {"model": "qwen35-9b"})
    assert d2.get("status") == "error", f"Should be error: {d2}"
    print(f"      ✅ Graceful error: {d2.get('message', '')}")

    # Verify state still clean
    st, _ = get("/status")
    assert st["gpu_mode"] == "idle", f"State corrupted: {st}"
    print(f"      ✅ State clean after double release")

    return True


def test_switch_while_running():
    """Switch from one exclusive to another (via idle)."""
    print("\n=== Switch exclusive → different exclusive ===")
    ensure_idle()

    # Start qwen36-27b
    print("  [1] Starting qwen36-27b...")
    d1, _ = post("/switch", {"model": "qwen36-27b"})
    assert d1.get("status") == "switched", f"Start failed: {d1}"

    # Try to switch to qwen35-9b directly (should fail — exclusive→shared not allowed)
    print("  [2] Direct switch to qwen35-9b (should fail)...")
    d2, _ = post("/switch", {"model": "qwen35-9b"})
    assert d2.get("status") == "error", f"Should reject: {d2}"
    print(f"      ✅ Correctly rejected: {d2.get('message', '')[:60]}")

    # Proper: idle first, then switch
    print("  [3] Proper: idle → qwen35-9b...")
    d3, _ = post("/switch", {"model": "idle"}, timeout=120)
    time.sleep(2)
    d4, _ = post("/switch", {"model": "qwen35-9b"})
    assert d4.get("status") == "switched", f"Switch failed: {d4}"
    print(f"      ✅ Switched to qwen35-9b in {d4.get('elapsed_sec')}s")

    # Cleanup
    post("/stop", {"model": "qwen35-9b"})
    time.sleep(2)
    print("      ✅ Cleaned up")

    return True


def test_dashboard_release_button_html():
    """Verify dashboard HTML contains the fix."""
    print("\n=== Dashboard HTML verification ===")
    try:
        with urllib.request.urlopen(f"{PROXY}/", timeout=5) as resp:
            html = resp.read().decode()
            checks = {
                "swLock()": "swLock" in html,
                "finally{sw=false;}": "finally{sw=false;}" in html,
                "no bare sw=true in actions": html.count("sw=true") == 1,  # only in swLock
                "30s timeout": "30000" in html,
            }
            for name, ok in checks.items():
                print(f"      {'✅' if ok else '❌'} {name}")
            return all(checks.values())
    except Exception as e:
        print(f"      ❌ Failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════

def run_all():
    # Check proxy
    try:
        with urllib.request.urlopen(f"{PROXY}/status", timeout=3) as resp:
            pass
    except Exception:
        print("❌ Proxy not running on :8999. Start with: edge-llm serve")
        return False

    tests = [
        ("Shared lifecycle", test_shared_lifecycle),
        ("Exclusive lifecycle", test_exclusive_lifecycle),
        ("Double release safety", test_release_button_idempotent),
        ("Switch exclusive→exclusive", test_switch_while_running),
        ("Dashboard HTML", test_dashboard_release_button_html),
    ]

    results = {}
    for name, test in tests:
        try:
            results[name] = test()
        except Exception as e:
            results[name] = False
            print(f"  ❌ FAILED: {e}")

    print(f"\n{'='*60}")
    for name, ok in results.items():
        print(f"  {'✅' if ok else '❌'} {name}")
    passed = sum(1 for v in results.values() if v)
    print(f"  {passed}/{len(results)} passed")
    print(f"{'='*60}")
    return all(results.values())


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
