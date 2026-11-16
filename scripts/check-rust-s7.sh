#!/usr/bin/env bash
# S7 parity gates for the candidate display relay (ticket #56).
#
# Runs the Rust workspace tests, the ticket's Python display parity gates, the
# noVNC launcher-state node test, the cloud console frontend gate and the
# fmt/clippy/diff gates. The candidate binds only an ephemeral loopback port and
# never switches 8002 or connects to the real display on 8097.
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
run python -m unittest tests.test_console_display_proxy tests.test_console_display_route
run node --test tests/test_console_display_launcher_state.mjs
run bash -c 'cd web && npm run check:cloud-console'
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
