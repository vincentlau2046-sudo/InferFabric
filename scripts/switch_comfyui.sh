#!/usr/bin/env bash
# ============================================================
# switch_comfyui.sh — Thin wrapper around edge-llm
#
# NOTE: This script is DEPRECATED for interactive use.
#   Prefer: edge-llm switch comfyui_only | edge-llm switch qw35_comfyui
#           edge-llm reset idle | edge-llm status
#
# This wrapper exists for backward compatibility.
#
# Usage: switch_comfyui.sh <start|stop|status>
# ============================================================

set -euo pipefail

case "${1:-}" in

    start)
        # Determine which ComfyUI profile to use
        # If vLLM is running on port 8002, use shared mode
        if ss -tlnp 2>/dev/null | grep -q ':8002 '; then
            exec edge-llm switch qw35_comfyui
        else
            exec edge-llm switch comfyui_only
        fi
        ;;

    stop)
        exec edge-llm reset idle
        ;;

    status)
        exec edge-llm status
        ;;

    *)
        echo "Usage: $0 <start|stop|status>"
        echo ""
        echo "  Preferred: edge-llm switch comfyui_only"
        echo "             edge-llm switch qw35_comfyui  (vLLM + ComfyUI)"
        echo "             edge-llm reset idle"
        exit 1
        ;;
esac
