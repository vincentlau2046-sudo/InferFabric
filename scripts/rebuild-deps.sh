#!/usr/bin/env bash
# scripts/rebuild-deps.sh — 重建 _deps/ 的 vendored wheel 以匹配当前 Python（PR-19）
#
# 背景：_deps/ 是 gitignored 的 vendored 运行时依赖（无 pip install）。
# 若 Python 大版本升级（如 3.12 → 3.13），cp312 编译的 C 扩展 .so 会静默
# 失效，各包回退纯 Python 实现（功能正常但性能下降）。本脚本下载与当前
# 解释器匹配的 wheel，提取 C .so 覆盖 _deps/，恢复 C 加速。
#
# 回滚：删除新 .so 即自动回退纯 Python（各包已验证 fallback）。
set -euo pipefail

# 版本基线（与 _deps/*.dist-info 保持一致；升级时同步修改）
PKGS=(
  "aiohttp==3.14.3"
  "multidict==6.8.0"
  "yarl==1.24.5"
  "attrs==26.1.0"
  "aiosignal==1.4.0"
  "aiohappyeyeballs==2.7.1"
  "propcache==0.5.2"
  "idna==3.19"
  "cachetools==7.1.8"
  "frozenlist==1.8.0"
)

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPS="$REPO_ROOT/_deps"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

echo "→ 目标解释器: $(python3 --version)"
echo "→ 下载 wheel: ${PKGS[*]}"
python3 -m pip wheel --no-deps --only-binary=:all: -q -w "$SCRATCH/wheels" "${PKGS[@]}"

# 仅替换 C 扩展 .so；纯 Python 包（attrs/idna/cachetools/typing_extensions…）不动
EXTRACT="$SCRATCH/extract"
mkdir -p "$EXTRACT"
(cd "$EXTRACT" && for w in "$SCRATCH"/wheels/*cp*.whl; do unzip -q -o "$w"; done)

FOUND=0
for so in $(cd "$EXTRACT" && find . -name "*.so" | sort); do
  if [ -f "$DEPS/$so" ]; then rm -v "$DEPS/$so"; fi
  cp -v "$EXTRACT/$so" "$DEPS/$so"
  FOUND=$((FOUND + 1))
done
if [ "$FOUND" -eq 0 ]; then
  echo "⚠️ 未找到 cp 匹配 wheel（解释器过新/无对应发布？）—— 跳过，继续纯 Python 模式"
  exit 1
fi

echo "→ 验证 C 扩展加载"
python3 - "$DEPS" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
mods = [
    "aiohttp._http_parser", "aiohttp._http_writer",
    "aiohttp._websocket.mask", "aiohttp._websocket.reader_c",
    "multidict._multidict", "yarl._quoting_c",
    "propcache._helpers_c", "frozenlist._frozenlist",
]
failed = []
for m in mods:
    try:
        __import__(m)
        print(f"  ✓ {m}")
    except ImportError as e:
        failed.append(m)
        print(f"  ✗ {m}: {e}（该模块回退纯 Python，删对应 .so 不影响运行）")
if failed:
    sys.exit(1)
print("全部 C 扩展加载成功")
EOF
echo "✅ _deps 已更新为当前解释器的 C 扩展"
