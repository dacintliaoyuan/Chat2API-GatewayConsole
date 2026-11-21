#!/usr/bin/env bash
# Stable Rust workspace test command.
#
# The login suite has a pre-existing race when test binaries run in parallel
# (a_background_duo_manual_prompt_waits_for_the_visible_choice_in_the_same_round);
# the real PowerShell service tests also share the machine's service lock. Running
# each test binary with one thread keeps the same assertions and removes the
# cross-test contention that makes `cargo test --workspace` intermittently red.
#
#   bash scripts/check-rust.sh          the workspace suite
#   bash scripts/check-rust.sh --edge   the ignored real-Edge regression only
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/rust"
case "${1:-}" in
  --edge)
    # Real Edge on an isolated profile and a loopback fixture; no real UM data.
    cargo test -p um-gateway --test browser_persistence -- --ignored --nocapture --test-threads=1
    ;;
  *)
    cargo test --workspace -- --test-threads=1
    ;;
esac
