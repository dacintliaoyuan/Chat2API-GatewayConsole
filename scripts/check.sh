#!/usr/bin/env bash
# Root guardrail for the merged 8100 share process.
#
#   bash scripts/check.sh          fast checks (compile, names, contract, whitespace)
#   bash scripts/check.sh --quick  fast checks + the share/console key tests
#   bash scripts/check.sh --full   fast checks + the whole Python suite
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FAIL=0
run() {
  echo "== $*"
  "$@" || FAIL=1
}

run python -m compileall -q chat2api . -x '(^|/)(node_modules|web/node_modules|\.git)/'
run python scripts/check_share_names.py
run node web/scripts/backend-contract/check-generated.mjs
run git -c core.whitespace=cr-at-eol diff --cached --check
run git -c core.whitespace=cr-at-eol diff --check

case "${1:-}" in
  --quick)
    run python -m unittest \
      tests.test_share_gate_merged \
      tests.test_share_gate_citations \
      tests.test_share_chain_window \
      tests.test_r19_um_imports \
      tests.test_console_share_production \
      tests.test_cloud_deploy_assembly \
      tests.test_manifest_stability
    ;;
  --full)
    run python -m unittest discover -s tests -p "test_*.py"
    ;;
esac

exit "$FAIL"
