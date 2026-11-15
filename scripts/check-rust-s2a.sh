#!/usr/bin/env bash
# S2a parity gates for the Rust Messages forwarding (ticket #46).
#
# Runs the Rust workspace tests, the Messages-specific suites and the fixture
# freshness check. The candidate binds only ephemeral loopback ports; no production
# port is bound and no production data is read.
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
run python -m unittest tests.test_rust_gateway_fixture tests.test_rust_contract_fixture \
  tests.test_rust_broker_fixture tests.test_rust_store_fixture
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
