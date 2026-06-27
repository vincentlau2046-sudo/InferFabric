#!/usr/bin/env bash
# ============================================================
# switch_vllm.sh — Thin wrapper around edge-llm
#
# NOTE: This script is DEPRECATED for interactive use.
#   Prefer: edge-llm switch <profile> | edge-llm status | edge-llm reset
#
# This wrapper exists for backward compatibility with scripts
# and tools that call switch_vllm.sh directly.
#
# Usage: switch_vllm.sh <qw36|qw35|gemma> [context_len]
#   switch_vllm.sh stop
#   switch_vllm.sh status
# ============================================================

set -euo pipefail

# Map short names to profile names
declare -A PROFILE_MAP=(
    [qw36]="qw36_full"
    [qw35]="qw35_comfyui"
    [gemma]="gemma_full"
)

case "${1:-}" in

    stop)
        exec edge-llm reset idle
        ;;

    status)
        exec edge-llm status
        ;;

    qw36|qw35|gemma)
        PROFILE="${PROFILE_MAP[$1]}"
        shift || true

        # Optional: context_len override (ignored by edge-llm, but logged)
        if [ -n "${1:-}" ]; then
            echo "[switch_vllm.sh] Note: context_len override ($1) ignored — use profiles.yaml"
        fi

        exec edge-llm switch "$PROFILE"
        ;;

    *)
        echo "Usage: $0 <qw36|qw35|gemma|stop|status> [context_len]"
        echo ""
        echo "  Preferred: edge-llm switch <profile>"
        echo "  Profiles:  qw36_full  qw35_comfyui  gemma_full  comfyui_only  idle"
        exit 1
        ;;
esac
