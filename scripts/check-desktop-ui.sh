#!/usr/bin/env bash
# Desktop UI gate: headless Slint tests plus the source-level design rules.
# The preview screenshots are local evidence, not a CI golden.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FAIL=0
run() {
  echo "== $*"
  "$@" || FAIL=1
}

run python scripts/check_desktop_ui.py
run cargo test --manifest-path desktop/Cargo.toml

if [ -d output/impeccable-ui-review ]; then
  echo "== preview screenshots (local evidence)"
  echo "同步 desktop/ui/*.slint 到 output/impeccable-ui-review/ui/ 后构建并渲染；"
  echo "本脚本不自动覆盖预览副本，截图路径见 docs/agents/desktop-preview.md。"
fi

exit "$FAIL"
