#!/usr/bin/env bash
# S5a parity gates for the candidate 8002 console read-only core (ticket #51).
#
# Runs the Rust workspace tests, the console fixture freshness check and the
# clippy/fmt gates. The candidate never binds 8002 or any other governed
# production port.
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
run python -m unittest tests.test_rust_console_fixture tests.test_rust_gateway_fixture \
  tests.test_rust_store_fixture tests.test_rust_broker_fixture tests.test_rust_contract_fixture
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
