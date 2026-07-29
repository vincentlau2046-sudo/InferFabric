#!/bin/bash
# Run benchmark for each model sequentially, collecting results
set -e
cd ~/inferfabric

MODELS=(
    "bge-m3"
    "qwen3-embedding-0.6b"
    "qwen25-omni-3b"
    "ovis-ocr2"
    "qwen35-9b"
    "qwen36-27b-vl"
    "qwen36-35b"
    "gemma-4-31B-it-NVFP4"
)

for model in "${MODELS[@]}"; do
    echo ""
    echo "=========================================="
    echo "📊 Benchmarking: $model"
    echo "=========================================="
    python3 benchmarks/bench_baseline.py --models "$model" 2>&1 || {
        echo "⚠️  $model benchmark failed, continuing..."
    }
done

echo ""
echo "✅ All benchmarks complete"
ls -la benchmarks/baseline-*.json
