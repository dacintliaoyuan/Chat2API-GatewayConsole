#!/usr/bin/env bash
# S0 parity gates for the Rust contract fixture (ticket #43).
#
# Runs the same commands as the ticket's parity gate plus the Rust workspace
# tests. No production port is bound and no production data is read.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FAIL=0
run() {
  echo "== $*"
  "$@" || FAIL=1
}

run node web/scripts/backend-contract/check-generated.mjs
run python -m unittest \
  tests.test_rust_contract_fixture \
  tests.test_retired_features \
  tests.test_check_gate
run cargo test --manifest-path rust/Cargo.toml
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
