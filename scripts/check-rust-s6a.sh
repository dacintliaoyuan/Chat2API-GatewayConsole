#!/usr/bin/env bash
# S6a parity gates for the candidate 8002 operations and share Key write face
# (ticket #53).
#
# Runs the Rust workspace tests, the console write-face tests, the ticket's Python
# parity gate and the clippy/fmt gates. The candidate never binds 8002 or any other
# governed production port.
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
run python -m unittest tests.test_console_operations tests.test_console_share_facade \
  tests.test_console_share_actions tests.test_console_share_secret_redaction \
  tests.test_console_share_production tests.test_share_management \
  tests.test_share_limits_contract
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
