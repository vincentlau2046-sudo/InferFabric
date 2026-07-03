#!/usr/bin/env python3
"""
iff — CLI for local LLM model switching (v4.0).

Usage:
  iff status              Show GPU mode, active services, health
  iff models              List available models from models.d/
  iff switch <model>      Switch to a model (enforces tri-state rules)
  iff stop <model>        Stop a single shared service
  iff sleep <model>           Put a running vLLM model to L2 sleep
  iff wake <model>                Wake a sleeping vLLM model
  iff history             Show switch history
  iff reset               Force reset to idle
  iff reconcile           Fix DB vs actual state inconsistencies
"""

import sys
import json
import logging
import datetime
from pathlib import Path

# Bootstrap logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("inferfabric.cli")

# Add parent to path for import
sys.path.insert(0, str(Path(__file__).parent.parent))
from inferfabric.manager import ModelManager
from inferfabric.state import GPUMode, ProfileState
from inferfabric.health import gpu_used_mb


def cmd_status():
    mgr = ModelManager()
    s = mgr.status()

    gpu_mode_label = {
        GPUMode.IDLE: "⚪ idle",
        GPUMode.EXCLUSIVE: "🔒 exclusive",
        GPUMode.SHARED: "🔓 shared",
    }.get(s.get("gpu_mode", ""), s.get("gpu_mode", "?"))

    print(f"GPU Mode : {gpu_mode_label}")
    print(f"Services : {s['active_services'] or '(none)'}")

    for svc, health in s.get("services_health", {}).items():
        print(f"  {svc}: {health}")

    pid_info = []
    if s.get("vllm_pid"):
        pid_info.append(f"vLLM PID={s['vllm_pid']}")
    if s.get("comfyui_pid"):
        pid_info.append(f"ComfyUI PID={s['comfyui_pid']}")
    if pid_info:
        print(f"PIDs     : {', '.join(pid_info)}")

    print(f"GPU      : {s['gpu_used_mb']}/{s['gpu_total_mb']} MiB used")


def cmd_models(args):
    mgr = ModelManager()
    models = mgr.list_models()

    mode_filter = None
    if "--mode" in args:
        idx = args.index("--mode")
        if idx + 1 < len(args):
            mode_filter = args[idx + 1]

    if mode_filter:
        models = [m for m in models if m["mode"] == mode_filter]

    print(f"\nAvailable Models ({len(models)}):")
    print(f"{'name':<20} {'mode':<12} {'type':<10} {'description'}")
    print("-" * 70)
    for m in models:
        active = " ← active" if m["active"] else ""
        print(f"{m['name']:<20} {m['mode']:<12} {m['type']:<10} {m['description']}{active}")


def cmd_switch(args):
    if not args:
        print("Usage: iff switch <model_name|idle>")
        print("\nAvailable models:")
        mgr = ModelManager()
        for m in mgr.list_models():
            print(f"  {m['name']:<20} ({m['mode']}, {m['type']})")
        print("  idle                  (release GPU)")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()

    if target == "idle":
        # Record manual stop for all active services
        for svc in list(mgr.active_services):
            mgr.state.record_manual_stop(svc)
        print("Switching to idle...")
    else:
        model = mgr.get_model(target)
        if not model:
            print(f"❌ Unknown model: {target}")
            print("\nAvailable models:")
            for m in mgr.list_models():
                print(f"  {m['name']:<20} ({m['mode']}, {m['type']})")
            sys.exit(1)
        # Clear manual stop for target (user explicitly wants it)
        mgr.state.clear_manual_stop(target)
        print(f"Switching to '{target}' (mode={model.mode})...")

    result = mgr.switch(target)

    if result["status"] == "already_active":
        print(f"Already active: {target}")
    elif result["status"] == "switched":
        gpu_mode = result.get("gpu_mode", "")
        elapsed = result.get("elapsed_sec", 0)
        print(f"✅ Switched to '{target}' in {elapsed}s (GPU: {gpu_mode})")
        if result.get("active_services"):
            print(f"  Active services: {result['active_services']}")
        for key, res in result.get("results", {}).items():
            status = "✅" if res.get("status") in ("healthy", "started", "ok") else "❌"
            print(f"  {status} {key}: {res.get('status', '?')}")
    else:
        print(f"❌ {result['message']}")
        if result.get("results"):
            for key, res in result["results"].items():
                print(f"  {key}: {res}")
        sys.exit(1)


def cmd_stop(args):
    if not args:
        print("Usage: iff stop <model_name>")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()

    result = mgr.stop_service(target)

    if result["status"] == "stopped":
        mgr.state.record_manual_stop(target)
        gpu_mode = result.get("gpu_mode", "?")
        print(f"✅ Stopped '{target}' (GPU: {gpu_mode})")
        if result.get("remaining"):
            print(f"  Remaining: {result['remaining']}")
        if result.get("message"):
            print(f"  {result['message']}")
    else:
        print(f"❌ {result['message']}")
        sys.exit(1)


def cmd_history(args):
    mgr = ModelManager()
    history = mgr.state.get_history(limit=30)
    if not history:
        print("No switch history.")
        return
    print(f"\nSwitch History (last {len(history)} entries):")
    print(f"{'#':<4} {'from':<20} {'to':<20} {'cost':<8} {'status':<8} {'time'}")
    print("-" * 80)
    for i, h in enumerate(history):
        ts = datetime.datetime.fromisoformat(h["timestamp"])
        dur = f"{h['duration']:.1f}s" if h["duration"] else "-"
        status = h.get("status", "?")
        print(f"{i+1:<4} {h['from']:<20} {h['to']:<20} {dur:<8} {status:<8} {ts.strftime('%Y-%m-%d %H:%M:%S')}")


def cmd_reset(args):
    mgr = ModelManager()
    # Record manual stop for all active services
    for svc in list(mgr.active_services):
        mgr.state.record_manual_stop(svc)
    print("Force resetting to idle...")

    result = mgr.force_reset()

    if result["status"] == "reset":
        print(f"✅ Reset to idle (GPU: {result['gpu_mode']})")
        if not result["gpu_free"]:
            print(f"⚠️ WARNING: GPU still has {gpu_used_mb()} MB used — orphan CUDA context likely")
            print(f"   May need 'nvidia-smi --gpu-reset' or reboot")
    else:
        print(f"❌ Reset failed")
        sys.exit(1)


def cmd_reconcile(args):
    mgr = ModelManager()
    result = mgr.reconcile()

    print("State Reconciliation:")
    print(f"  DB gpu_mode   : {result['db_gpu_mode']}")
    print(f"  Actual gpu_mode: {result['actual_gpu_mode']}")
    print(f"  DB services   : {result['db_services']}")
    print(f"  Actual services: {result['actual_services']}")
    if result["actions"]:
        print("\n  Actions taken:")
        for a in result["actions"]:
            print(f"    • {a}")
    else:
        print("  ✓ State is consistent")


def cmd_sleep(args):
    if not args:
        print("Usage: iff sleep <model_name>")
        print("\nPut a running vLLM model to sleep (L2: discard weights, wake ~3-6s).")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()
    model = mgr.get_model(target)
    if not model:
        print(f"❌ Unknown model: {target}")
        sys.exit(1)

    print(f"Sleeping '{target}'...")

    result = mgr.sleep_model(target)

    if result["status"] == "ok":
        elapsed = result.get("elapsed_sec", 0)
        # Check GPU mode change
        gpu_info = ""
        if result.get("gpu_mode"):
            gpu_info = f", GPU={result['gpu_mode']}"
        print(f"✅ '{target}' sleeping ({elapsed:.1f}s)" + gpu_info)
    elif result["status"] == "already_sleeping":
        print(f"Already sleeping: {target}")
    else:
        print(f"❌ {result['message']}")
        sys.exit(1)


def cmd_wake(args):
    if not args:
        print("Usage: iff wake <model_name>")
        print("\nWake a sleeping vLLM model. Exclusive models require GPU=idle.")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()
    model = mgr.get_model(target)
    if not model:
        print(f"❌ Unknown model: {target}")
        sys.exit(1)

    print(f"Waking '{target}'...")

    result = mgr.wake_model(target)

    if result["status"] == "ok":
        elapsed = result.get("elapsed_sec", 0)
        print(f"✅ '{target}' awake ({elapsed:.1f}s)")
    elif result["status"] == "already_awake":
        print(f"Already awake: {target}")
    else:
        print(f"❌ {result['message']}")
        sys.exit(1)


def cmd_pull(args):
    """Pre-download model files: ollama pull / huggingface-cli download."""
    if not args:
        print("Usage: iff pull <model_name>")
        print("\nPre-download model files for offline switch.")
        sys.exit(1)

    target = args[0]
    mgr = ModelManager()
    model = mgr.get_model(target)
    if not model:
        print(f"❌ Unknown model: {target}")
        sys.exit(1)

    if model.is_ollama:
        import subprocess
        ref = model.ollama.model_ref
        print(f"⬇  Pulling {ref} via ollama...")
        result = subprocess.run(
            ["ollama", "pull", ref],
            capture_output=False, timeout=600
        )
        if result.returncode == 0:
            print(f"✅ Model pulled: {ref}")
        else:
            print(f"❌ Pull failed with code {result.returncode}")
            sys.exit(1)
    elif model.is_ollama_cpp:
        from pathlib import Path
        model_path = Path(model.ollama_cpp.model_path).expanduser()
        if model_path.exists():
            print(f"✅ Model already downloaded: {model_path}")
        else:
            print(f"❌ GGUF model not found: {model_path}")
            print(f"   Download from HuggingFace and place at: {model_path.parent}")
            sys.exit(1)
    elif model.is_vllm:
        model_dir = Path.home() / "models" / model.vllm.model_dir
        if model_dir.exists():
            print(f"✅ Model already downloaded: {model_dir}")
        else:
            print(f"❌ Model not found: {model_dir}")
            print(f"   Use huggingface-cli download or modelscope to download.")
            sys.exit(1)
    else:
        print(f"Pull not supported for {model.type} models")


def cmd_list_downloaded(args):
    """List pre-downloaded models on disk."""
    mgr = ModelManager()
    models = mgr.list_models()
    ollama_models = []
    local_models = []

    for m in models:
        if m["type"] == "ollama":
            import subprocess
            try:
                result = subprocess.run(
                    ["ollama", "list"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    ollama_models.extend(
                        [l.split()[0] for l in result.stdout.strip().splitlines()[1:] if l.strip()]
                    )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            break

    print(f"\nOllama models (via 'ollama list'):")
    if ollama_models:
        for m in ollama_models:
            print(f"  {m}")
    else:
        print("  (ollama not installed or no models pulled)")

    models_base = Path.home() / "models"
    if models_base.exists():
        dirs = [d.name for d in models_base.iterdir() if d.is_dir()]
        print(f"\nvLLM models (~/models/):")
        for d in sorted(dirs):
            print(f"  {d}/")


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "status":
        cmd_status()
    elif cmd == "models":
        cmd_models(rest)
    elif cmd == "switch":
        cmd_switch(rest)
    elif cmd == "stop":
        cmd_stop(rest)
    elif cmd == "sleep":
        cmd_sleep(rest)
    elif cmd == "wake":
        cmd_wake(rest)
    elif cmd == "history":
        cmd_history(rest)
    elif cmd == "reset":
        cmd_reset(rest)
    elif cmd == "reconcile":
        cmd_reconcile(rest)
    elif cmd == "pull":
        cmd_pull(rest)
    elif cmd == "list-downloaded":
        cmd_list_downloaded(rest)
    else:
        print(f"Unknown command: {cmd}")
        print("Available: status, models, switch, stop, pull, list-downloaded, sleep, wake, history, reset, reconcile")
        sys.exit(1)


if __name__ == "__main__":
    main()