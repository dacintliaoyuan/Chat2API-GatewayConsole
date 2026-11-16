#!/usr/bin/env bash
# Cutover acceptance gate (#72).
#
# Thin wrapper: the Python tool owns the three gates so it can emit the JSON
# evidence document #73 consumes.  `cloud-smoke` only prints commands and
# expectations; `rollback-drill` only plans unless --execute is explicit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

PYTHON="$(python_bin || printf 'python3')"
exec "$PYTHON" "$SCRIPT_DIR/cutover_gate.py" "$@"
