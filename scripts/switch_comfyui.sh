#!/usr/bin/env bash
# ============================================================
# switch_comfyui.sh — Thin wrapper around iff (v4.0)
#
# Usage: switch_comfyui.sh <start|stop|status>
# ============================================================

set -euo pipefail

case "${1:-}" in

    start)
        # If a shared vLLM is running, just add ComfyUI
        exec iff switch comfyui
        ;;

    stop)
        exec iff stop comfyui
        ;;

    status)
        exec iff status
        ;;

    *)
        echo "Usage: $0 <start|stop|status>"
        echo ""
        echo "  Preferred: iff switch comfyui"
        echo "             iff stop comfyui"
        exit 1
        ;;
esac
