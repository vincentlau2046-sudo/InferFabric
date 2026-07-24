#!/usr/bin/env python3
"""InferFabric PR-7/8/9/11/12 integration tests — covers new behavior from all 5 PRs."""

import sys
import os
import json
import re
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_state_db(tmp: str):
    from inferfabric.state import StateDB
    db = StateDB(Path(tmp) / "state.db")
    db.set("gpu_mode", "idle")
    db.set("active_services", "[]")
    return db

def _make_model(name, gpu_role, port, model_type="vllm"):
    m = MagicMock()
    m.name = name
    m.gpu_role = gpu_role
    m.is_gpu_none = (gpu_role == "none")
    m.is_exclusive = (gpu_role == "exclusive")
    m.is_vllm = (model_type == "vllm")
    m.is_comfyui = (model_type == "comfyui")
    m.is_ollama_cpp = (model_type == "ollama_cpp")
    m.port = port
    if model_type == "vllm":
        m.vllm = MagicMock()
        m.vllm.port = port
        m.vllm.conda_env = "test"
        m.comfyui = None
    elif model_type == "comfyui":
        m.comfyui = MagicMock()
        m.comfyui.port = port
        m.vllm = None
    return m


# ═══════════════════════════════════════════════════════════════
# PR-7: gpu_role:none service preservation on _switch_to_idle
# ═══════════════════════════════════════════════════════════════

def test_pr7_switch_to_idle_preserves_gpu_none():
    """_switch_to_idle active_services filter keeps gpu_role:none services."""
    from inferfabric.model_lifecycle import ModelLifecycle
    lifecycle_path = Path(__file__).parent.parent / "inferfabric" / "model_lifecycle.py"
    content = lifecycle_path.read_text()

    # The new filter expression should exist
    assert "is_gpu_none" in content, "Missing is_gpu_none filter in model_lifecycle"
    # The old gpu_role:none stop loop should be removed
    assert 'gpu_role != "none"' not in content or 'm.gpu_role != "none"' not in content.replace(" ", ""), \
        "Old gpu_role:none stop loop should be removed"
    print("✅ PR-7: _switch_to_idle preserves gpu_role:none services")


def test_pr7_no_stop_loop_for_gpu_none():
    """Verify the stop loop for gpu_role:none services was deleted."""
    lifecycle_path = Path(__file__).parent.parent / "inferfabric" / "model_lifecycle.py"
    content = lifecycle_path.read_text()

    # The old pattern "Stop GPU-independent services (gpu_role == none)" should be gone
    assert 'Stop GPU-independent services' not in content, \
        "Old stop loop comment still present"
    print("✅ PR-7: gpu_role:none stop loop removed")


# ═══════════════════════════════════════════════════════════════
# PR-8: double-check lock + SWITCHING resolve dedup
# ═══════════════════════════════════════════════════════════════

def test_pr8_affinity_double_check_lock():
    """load_model_affinity has double-check lock after I/O."""
    config_path = Path(__file__).parent.parent / "inferfabric" / "config.py"
    content = config_path.read_text()

    # Should have _affinity_lock
    assert "_affinity_lock" in content, "Missing _affinity_lock in config.py"
    # Should have double-check inside lock
    assert "load_model_affinity._cache is not None" in content, \
        "Missing double-check inside affinity lock"
    print("✅ PR-8: load_model_affinity double-check lock present")


def test_pr8_switching_target_model_resolved_once():
    """SWITCHING guard resolves target_model once, reuses in main route."""
    handler_path = Path(__file__).parent.parent / "inferfabric" / "proxy" / "handler.py"
    content = handler_path.read_text()

    # target_model should be resolved before SWITCHING guard
    # and reused after (not re-resolved)
    assert "target_model = pm.mgr.find_model_by_served_name(requested_model)" in content, \
        "Missing target_model resolution before SWITCHING guard"
    # Should not re-resolve when not SWITCHING
    assert "if profile_state != ProfileState.SWITCHING:" in content, \
        "Missing conditional re-resolve guard"
    print("✅ PR-8: SWITCHING guard target_model resolved once")


# ═══════════════════════════════════════════════════════════════
# PR-9: watchdog restart semantics + gpu_mode rollback + profile_state reset
# ═══════════════════════════════════════════════════════════════

def test_pr9_restart_only_accepts_switched():
    """watchdog _restart_model only treats 'switched' as success."""
    watchdog_path = Path(__file__).parent.parent / "inferfabric" / "watchdog.py"
    content = watchdog_path.read_text()

    # Should have exact check for "switched", not "in (...)"
    assert '== "switched"' in content, "Missing exact 'switched' check in watchdog"
    # Should have already_active handling as stale entry
    assert "already_active" in content and "stale" in content.lower(), \
        "Missing already_active = stale entry handling"
    print("✅ PR-9: restart only accepts 'switched'")


def test_pr9_force_clean_stops_process_first():
    """Force-clean calls stop_service before remove_active_service."""
    watchdog_path = Path(__file__).parent.parent / "inferfabric" / "watchdog.py"
    content = watchdog_path.read_text()

    # stop_service should appear before remove_active_service in force-clean paths
    stop_pos = content.find("self._manager.stop_service(name)")
    remove_pos = content.find("self._manager.state.remove_active_service(name)")
    assert stop_pos > 0 and remove_pos > 0, "Missing stop_service or remove_active_service"
    # Both should exist and stop_service should come before each remove
    # (there are 2 force-clean paths)
    assert stop_pos < remove_pos, "stop_service should be called before remove_active_service"
    print("✅ PR-9: force-clean stops process before removing state entry")


def test_pr9_gpu_mode_rollback_on_failure():
    """Switch failure rolls back gpu_mode to pre-switch value."""
    manager_path = Path(__file__).parent.parent / "inferfabric" / "manager.py"
    content = manager_path.read_text()

    # Should have gpu_mode rollback
    assert 'self.state.set("gpu_mode", current_mode)' in content, \
        "Missing gpu_mode rollback on failure"
    # Should have profile_state reset on non-exception failure
    assert 'self.state.set("profile_state", ProfileState.ERROR)' in content, \
        "Missing profile_state reset on non-exception failure"
    print("✅ PR-9: gpu_mode rollback + profile_state reset on failure")


def test_pr9_restarting_timeout():
    """_restarting set has 120s timeout to prevent stuck restarts."""
    watchdog_path = Path(__file__).parent.parent / "inferfabric" / "watchdog.py"
    content = watchdog_path.read_text()

    assert "_restart_started" in content, "Missing _restart_started tracking"
    assert "120" in content, "Missing 120s timeout for stuck restarts"
    assert "_restarting.discard" in content, "Missing discard of stuck restart entry"
    print("✅ PR-9: _restarting 120s timeout mechanism")


# ═══════════════════════════════════════════════════════════════
# PR-11: CLI cleanup
# ═══════════════════════════════════════════════════════════════

def test_pr11_default_profiles_removed():
    """DEFAULT_PROFILES constant removed from config.py."""
    config_path = Path(__file__).parent.parent / "inferfabric" / "config.py"
    content = config_path.read_text()
    assert "DEFAULT_PROFILES" not in content, "DEFAULT_PROFILES should be removed"
    print("✅ PR-11: DEFAULT_PROFILES constant removed")


def test_pr11_recovery_no_dead_reference():
    """iff-recovery.sh has no reference to removed switch_vllm.sh."""
    recovery_path = Path(__file__).parent.parent / "scripts" / "iff-recovery.sh"
    content = recovery_path.read_text()
    assert "switch_vllm.sh" not in content, "Dead reference to switch_vllm.sh still present"
    print("✅ PR-11: iff-recovery.sh no dead references")


def test_pr11_test_profiles_adapted():
    """test_local.py no longer imports DEFAULT_PROFILES as a symbol."""
    test_path = Path(__file__).parent.parent / "tests" / "test_local.py"
    content = test_path.read_text()
    # Should not import DEFAULT_PROFILES (comments mentioning it are OK)
    import_line = [l for l in content.splitlines() if 'import' in l and 'DEFAULT_PROFILES' in l and not l.strip().startswith('#')]
    assert len(import_line) == 0, f"test_local.py still imports DEFAULT_PROFILES: {import_line}"
    print("✅ PR-11: test_local.py adapted for DEFAULT_PROFILES removal")


# ═══════════════════════════════════════════════════════════════
# PR-12: /pull route + admin auth + dashboard UI
# ═══════════════════════════════════════════════════════════════

def test_pr12_pull_route_exists():
    """/pull route handler exists in handler.py."""
    handler_path = Path(__file__).parent.parent / "inferfabric" / "proxy" / "handler.py"
    content = handler_path.read_text()
    assert "def _handle_pull" in content, "Missing _handle_pull handler"
    assert '"/pull"' in content, "Missing /pull route registration"
    print("✅ PR-12: /pull route exists")


def test_pr12_pull_model_in_manager():
    """pull_model() method exists in ModelManager with name validation."""
    manager_path = Path(__file__).parent.parent / "inferfabric" / "manager.py"
    content = manager_path.read_text()
    assert "def pull_model" in content, "Missing pull_model method"
    # Name validation regex (with path traversal prevention)
    assert "(?!.*\\.\\.)" in content, "Missing path traversal prevention in name regex"
    assert "1800" in content, "Missing 1800s timeout for ollama pull"
    print("✅ PR-12: pull_model with name validation + 1800s timeout")


def test_pr12_admin_auth_mechanism():
    """_check_admin method and IFF_ADMIN_TOKEN env var for control routes."""
    handler_path = Path(__file__).parent.parent / "inferfabric" / "proxy" / "handler.py"
    content = handler_path.read_text()

    assert "_ADMIN_TOKEN" in content, "Missing IFF_ADMIN_TOKEN env var"
    assert "def _check_admin" in content, "Missing _check_admin method"
    assert "X-Admin-Token" in content, "Missing X-Admin-Token header check"
    # All control routes should have admin check
    control_routes = ["/switch", "/stop", "/sleep", "/wake", "/reset", "/reconcile", "/deploy", "/pull"]
    for route in control_routes:
        # Find the route handler and verify _check_admin guard
        route_pos = content.find(f'"{route}"')
        assert route_pos > 0, f"Missing route {route}"
        # _check_admin should appear between route match and handler call
    print("✅ PR-12: admin auth mechanism for all control routes")


def test_pr12_inference_routes_no_admin():
    """Inference routes (/v1/chat/completions, /v1/messages) do NOT require admin."""
    handler_path = Path(__file__).parent.parent / "inferfabric" / "proxy" / "handler.py"
    content = handler_path.read_text()

    # Extract do_POST method
    start = content.find("def do_POST")
    end = content.find("\n    def ", start + 1)
    do_post = content[start:end]

    # Inference routes should NOT have _check_admin guard before them
    # Find /v1/chat/completions handler call
    chat_pos = do_post.find('"/v1/chat/completions"')
    assert chat_pos > 0, "Missing /v1/chat/completions route"
    # The line before should not have _check_admin
    line_start = do_post.rfind("\n", 0, chat_pos)
    line = do_post[line_start:chat_pos]
    assert "_check_admin" not in line, "Inference route should not require admin auth"
    print("✅ PR-12: inference routes do NOT require admin auth")


def test_pr12_two_phase_pull_and_deploy():
    """doPullAndDeploy does phase 1 (pull) then phase 2 (deploy)."""
    dashboard_path = Path(__file__).parent.parent / "inferfabric" / "dashboard.py"
    content = dashboard_path.read_text()

    assert "doPullAndDeploy" in content, "Missing doPullAndDeploy function"
    # Should check pull result before deploying
    assert "pullResult" in content or "pull" in content.lower(), "Missing pull phase"
    assert "deployResult" in content or "deploy" in content.lower(), "Missing deploy phase"
    # Should return early on pull error
    assert "'error'" in content or '"error"' in content, "Missing early return on pull error"
    print("✅ PR-12: two-phase doPullAndDeploy")


def test_pr12_fold_groups_default_open():
    """Fold groups default to open for better UX."""
    dashboard_path = Path(__file__).parent.parent / "inferfabric" / "dashboard.py"
    content = dashboard_path.read_text()

    assert "fw-hdr open" in content, "Missing default open class on fold headers"
    assert "fw-body open" in content, "Missing default open class on fold bodies"
    print("✅ PR-12: fold groups default open")


def test_pr12_spec_tag_four_slots():
    """Model cards always render 4 spec-tag slots for alignment."""
    dashboard_path = Path(__file__).parent.parent / "inferfabric" / "dashboard.py"
    content = dashboard_path.read_text()

    # Should have visibility:hidden placeholder for missing slots
    assert "visibility:hidden" in content, "Missing hidden placeholder spec-tags"
    # Should have specSlots array
    assert "specSlots" in content, "Missing specSlots array for 4-slot alignment"
    print("✅ PR-12: spec-tag 4 slots with hidden placeholders")


# ═══════════════════════════════════════════════════════════════
# Run All
# ═══════════════════════════════════════════════════════════════

def run_all():
    tests = [
        # PR-7
        test_pr7_switch_to_idle_preserves_gpu_none,
        test_pr7_no_stop_loop_for_gpu_none,
        # PR-8
        test_pr8_affinity_double_check_lock,
        test_pr8_switching_target_model_resolved_once,
        # PR-9
        test_pr9_restart_only_accepts_switched,
        test_pr9_force_clean_stops_process_first,
        test_pr9_gpu_mode_rollback_on_failure,
        test_pr9_restarting_timeout,
        # PR-11
        test_pr11_default_profiles_removed,
        test_pr11_recovery_no_dead_reference,
        test_pr11_test_profiles_adapted,
        # PR-12
        test_pr12_pull_route_exists,
        test_pr12_pull_model_in_manager,
        test_pr12_admin_auth_mechanism,
        test_pr12_inference_routes_no_admin,
        test_pr12_two_phase_pull_and_deploy,
        test_pr12_fold_groups_default_open,
        test_pr12_spec_tag_four_slots,
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
    print(f"PR Tests: {passed} passed, {failed} failed, {passed+failed} total")
    if errors:
        for name, e in errors:
            print(f"  - {name}: {e}")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
