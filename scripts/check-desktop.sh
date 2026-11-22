#!/usr/bin/env bash
# Desktop crate tests.
#
# `desktop/` is a standalone Slint crate outside the `rust/` workspace, so
# `scripts/check.sh` cannot reach it through `cargo test` there. Two jobs plus no
# incremental keeps parallel rustc/link invocations inside this machine's limits;
# the default job count has produced `link.exe` 1102 and rustc ICEs.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
# Only the lib and test targets: a running app locks `um-desktop.exe`, and the
# tests link the lib, not the binary.
CARGO_INCREMENTAL=0 cargo test -j 2 --manifest-path desktop/Cargo.toml --lib --tests "$@"
