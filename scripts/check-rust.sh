#!/usr/bin/env bash
# Stable Rust workspace test command.
#
# The login suite has a pre-existing race when test binaries run in parallel
# (a_background_duo_manual_prompt_waits_for_the_visible_choice_in_the_same_round);
# the real PowerShell service tests also share the machine's service lock. Running
# each test binary with one thread keeps the same assertions and removes the
# cross-test contention that makes `cargo test --workspace` intermittently red.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/rust"
cargo test --workspace -- --test-threads=1
