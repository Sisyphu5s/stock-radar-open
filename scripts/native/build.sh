#!/usr/bin/env bash
# 编译 C 滚动窗口内核为 darwin 动态库：backend/native/librolling.dylib。
# 默认产出 arm64 + x86_64 双架构通用库；双架构编译失败时回退仅本机架构。
# 产物不入 git（backend/native/.gitignore 忽略 *.dylib）；
# 未构建时 Python 侧自动回退 numpy 实现。本脚本失败返回非零（可重试），
set -euo pipefail
NATIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../backend/native" && pwd)"
cd "$NATIVE_DIR"

ARCHS=""
case "$(uname -m)" in
  arm64 | aarch64) ARCHS="-arch arm64 -arch x86_64" ;;
  x86_64 | amd64)  ARCHS="-arch x86_64 -arch arm64" ;;
esac

# build <arch-flags>：先编译到临时文件再原子替换，失败不残留损坏产物。
build() {
  local tmp="librolling.dylib.tmp"
  rm -f "$tmp"
  # shellcheck disable=SC2086
  if clang $1 -O3 -fPIC -shared -Wall -Wextra -o "$tmp" rolling_ops.c; then
    mv -f "$tmp" librolling.dylib
    return 0
  fi
  rm -f "$tmp"
  return 1
}

if [ -n "$ARCHS" ] && build "$ARCHS"; then
  echo "[native] built universal librolling.dylib ($ARCHS)"
elif build ""; then
  echo "[native] built librolling.dylib (host arch only)"
else
  echo "[native] 编译失败：native 内核不可用，将回退 numpy（请安装 Xcode CLT 后重试）" >&2
  exit 1
fi
