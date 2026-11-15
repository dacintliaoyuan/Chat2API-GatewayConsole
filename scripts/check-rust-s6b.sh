#!/usr/bin/env bash
# S6b parity gates for the candidate 8002 session/display/legacy write face
# (ticket #54).
#
# Runs the Rust workspace tests, the ticket's Python parity gate, the cloud
# console frontend gate and the fmt/clippy/diff gates. The candidate binds only
# an ephemeral loopback port and never switches 8002.
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
run python -m unittest tests.test_console_display_route \
  tests.test_console_share_facade tests.test_console_share_actions \
  tests.test_console_share_secret_redaction tests.test_console_share_production
run bash -c 'cd web && npm run check:cloud-console'
run git -c core.whitespace=cr-at-eol diff --check

exit "$FAIL"
