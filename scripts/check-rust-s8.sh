#!/usr/bin/env bash
# S8 parity gates for the read-only ops crate (ticket #57).
#
# Runs the Rust ops tests, the Python-generated fixture freshness check and the
# clippy/fmt gates. No backup, restore, update, rollback or public probe runs.
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
run python -m unittest tests.test_rust_ops_fixture tests.test_rust_s8_boundaries \
  tests.test_rust_gateway_fixture tests.test_rust_store_fixture \
  tests.test_rust_broker_fixture tests.test_rust_contract_fixture
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
