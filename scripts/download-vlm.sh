#!/bin/bash
# Download Huihui-Qwen3.6-27B-abliterated-NVFP4-MTP (VLM + MTP)
# Source: sakamakismile/Huihui-Qwen3.6-27B-abliterated-NVFP4-MTP
# Size: ~21 GB (vision encoder included)
# Requires: hf-cli (huggingface-cli)

set -euo pipefail

TARGET_DIR="$HOME/models/Huihui-Qwen3.6-27B-abliterated-NVFP4-MTP"
HF_REPO="sakamakismile/Huihui-Qwen3.6-27B-abliterated-NVFP4-MTP"

echo "=== VLM Model Download ==="
echo "Target: $TARGET_DIR"
echo "Source: $HF_REPO"
echo ""

if [ -d "$TARGET_DIR" ] && [ -f "$TARGET_DIR/model.safetensors" ]; then
    SIZE=$(du -sh "$TARGET_DIR" | cut -f1)
    echo "Model already exists ($SIZE). Skipping download."
    exit 0
fi

mkdir -p "$TARGET_DIR"

echo "Downloading... (this may take 10-30 minutes depending on bandwidth)"
huggingface-cli download "$HF_REPO" --local-dir "$TARGET_DIR" --local-dir-use-symlinks false

echo ""
echo "Verifying..."
if [ -f "$TARGET_DIR/model.safetensors" ] && [ -f "$TARGET_DIR/config.json" ]; then
    SIZE=$(du -sh "$TARGET_DIR" | cut -f1)
    echo "✅ Download complete: $SIZE"
    echo ""
    echo "Next steps:"
    echo "  1. Verify: diff <(ls $HOME/models/Qwen3.6-27B-Text-NVFP4-MTP/) <(ls $TARGET_DIR/)"
    echo "  2. Config: ~/inferfabric/models.d/qwen36-27b-vl.yaml (ready)"
    echo "  3. Start: iff switch qwen36-27b-vl"
else
    echo "❌ Download verification failed"
    exit 1
fi
