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

web_run() {
  echo "== web: $*"
  (cd web && "$@") || FAIL=1
}

run python -m compileall -q chat2api . -x '(^|/)(node_modules|web/node_modules|\.git)/'
run python -m ruff check .
run python scripts/check_share_names.py
run python scripts/check_retired_features.py
run node web/scripts/backend-contract/check-generated.mjs
run git -c core.whitespace=cr-at-eol diff --cached --check
run git -c core.whitespace=cr-at-eol diff --check
web_run npx tsc --noEmit
web_run npm run check:contracts
web_run npm run check:logs
web_run npm run check:um

case "${1:-}" in
  --quick)
    run python -m unittest \
      tests.test_share_gate_merged \
      tests.test_share_gate_citations \
      tests.test_check_gate \
      tests.test_r19_um_imports \
      tests.test_console_share_production \
      tests.test_cloud_deploy_assembly \
      tests.test_manifest_stability
    web_run npm run check:cloud-console
    ;;
  --full)
    run python -m unittest discover -s tests -p "test_*.py"
    ;;
esac

exit "$FAIL"
