#!/usr/bin/env bash
# S3b parity gates for the Rust share store, Key lifecycle, projection
# publishing and the broker start identity (ticket #49).
#
# Runs the Rust workspace tests, the fixture freshness checks and the
# clippy/fmt gates. AF_UNIX/SO_PEERCRED execution tests are Linux-only and
# are skipped on Windows by their `#![cfg(unix)]` guard.
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
run python -m unittest tests.test_rust_store_fixture tests.test_rust_broker_fixture \
  tests.test_rust_contract_fixture tests.test_rust_gateway_fixture
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
