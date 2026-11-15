#!/usr/bin/env bash
# S5b parity gates for the candidate 8002 console read faces (ticket #52).
#
# Runs the Rust workspace tests, the reads fixture freshness check and the
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
run python -m unittest tests.test_rust_console_reads_fixture
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
