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

# A fresh worktree starts without web/node_modules; the gates below need the
# locked frontend dependencies. Install them once so a missing install fails here
# with this message instead of MODULE_NOT_FOUND in every web gate.
if [ ! -d web/node_modules ]; then
  echo "== web: npm ci (node_modules missing)"
  if ! (cd web && npm ci); then
    echo "web dependencies could not be installed; run 'cd web && npm ci' and retry" >&2
    exit 1
  fi
fi

run python -m compileall -q . -x '(^|[/\\])(node_modules|web/node_modules|\.git|\.worktrees)([/\\]|$)'
run python -m ruff check .
run python scripts/check_share_names.py
run python scripts/check_retired_features.py
run python scripts/check_line_endings.py
run python scripts/check_docs_references.py
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
      tests.test_check_gate \
      tests.test_rust_contract_fixture \
      tests.test_cloud_deploy_assembly \
      tests.test_cloud_rollback_release \
      tests.test_cloud_deploy_rust_release \
      tests.test_manifest_stability \n      tests.test_manifest_rust_closure
    web_run npm run check:cloud-console
    ;;
  --full)
    run python -m unittest discover -s tests -p "test_*.py"
    ;;
esac

exit "$FAIL"
