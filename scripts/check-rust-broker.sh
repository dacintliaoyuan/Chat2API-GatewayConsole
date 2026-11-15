#!/usr/bin/env bash
# S3a parity gates for the Rust broker control channel (ticket #48).
#
# Runs the Rust workspace tests, the clippy/fmt gates and the Android
# compile-check for the cfg(unix) adapter. AF_UNIX/SO_PEERCRED execution tests
# are Linux-only and are skipped on Windows by their `#![cfg(unix)]` guard.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FAIL=0
run() {
  echo "== $*"
  "$@" || FAIL=1
}

run cargo fmt --manifest-path rust/Cargo.toml -- --check
run cargo clippy --manifest-path rust/Cargo.toml --all-targets --offline
run cargo test --manifest-path rust/Cargo.toml --offline
run cargo check --manifest-path rust/Cargo.toml -p um-broker \
  --target aarch64-linux-android --tests --offline
run python -m unittest tests.test_rust_broker_fixture
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
