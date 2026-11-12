#!/usr/bin/env bash
# Linux-only UM share chain checks. Run inside the isolated WSL distro.
#
# The fixture starts the REAL merged 8100 process on loopback test ports:
#   28100 merged share process, 28999 verification-only fake UM.  All SQLite/Key/profile/ledger paths live
#   under one private temporary directory; no production data is read or written.
#
# Modes:
#   core      (default) single-process chain: tests/linux_share_chain.py
#   full      core + long-request chain + cloud ops drill (tests.test_cloud_ops)
#   resource  2 GiB / 4 GiB resource envelope
#             (tests/linux_resource_envelope.py)
#
# Usage:
#   bash ai-gateway/um-cloud-deploy/run-linux-checks.sh [--mode MODE] [EVIDENCE_PATH]
#
# EVIDENCE_PATH overrides the evidence location.  A *.json path is treated as the
# core report file; any other path is an evidence directory.  Each scenario runs
# independently and keeps its own exit code; the script exits non-zero when any
# scenario failed.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_EVIDENCE="/mnt/d/git/github/personal_files/SelfUseAI/tmp/um-cloud-p1-20261030"

MODE="${UM_LINUX_CHECKS_MODE:-core}"
POSITIONAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -m|--mode)
      if [ "$#" -lt 2 ]; then
        echo "error: $1 requires a value" >&2
        exit 64
      fi
      MODE="$2"
      shift 2
      ;;
    --mode=*)
      MODE="${1#*=}"
      shift
      ;;
    -h|--help)
      sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --)
      shift
      POSITIONAL+=("$@")
      break
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      exit 64
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

case "$MODE" in
  core|full|resource) ;;
  *)
    echo "error: unknown mode: $MODE (expected core, full or resource)" >&2
    exit 64
    ;;
esac

EVIDENCE="${POSITIONAL[0]:-$DEFAULT_EVIDENCE}"
case "$EVIDENCE" in
  *.json)
    EVIDENCE_DIR="$(dirname "$EVIDENCE")"
    CORE_REPORT="$EVIDENCE"
    ;;
  *)
    EVIDENCE_DIR="$EVIDENCE"
    CORE_REPORT="$EVIDENCE_DIR/chain-core.json"
    ;;
esac
mkdir -p "$EVIDENCE_DIR"

# Prefer the provisioned venv; fall back to the system interpreter.
if [ -x /srv/um-share/venv/bin/python ]; then
  PYTHON=/srv/um-share/venv/bin/python
else
  PYTHON=python3
fi

cd "$ROOT"

echo "mode=$MODE"
echo "python=$PYTHON"
echo "evidence=$EVIDENCE_DIR"

SCENARIO_NAMES=()
SCENARIO_CODES=()
FINAL_EXIT=0

run_scenario() {
  local name="$1"
  shift
  local rc=0
  echo
  echo "== scenario $name"
  "$@" || rc=$?
  SCENARIO_NAMES+=("$name")
  SCENARIO_CODES+=("$rc")
  if [ "$rc" -eq 0 ]; then
    echo "== scenario $name: PASS (exit 0)"
  else
    echo "== scenario $name: FAIL (exit $rc)" >&2
    if [ "$FINAL_EXIT" -eq 0 ]; then
      FINAL_EXIT="$rc"
    fi
  fi
  return 0
}

run_core() {
  "$PYTHON" tests/linux_share_chain.py \
    --scenario core --report "$CORE_REPORT"
}

run_long() {
  "$PYTHON" tests/linux_share_chain.py \
    --scenario long --report "$EVIDENCE_DIR/chain-long.json"
}

run_ops() {
  UM_CLOUD_OPS_REPORT_DIR="$EVIDENCE_DIR" \
    "$PYTHON" -m unittest tests.test_cloud_ops -v
}

run_resource() {
  "$PYTHON" tests/linux_resource_envelope.py \
    --limit-gb 2 --limit-gb 4 \
    --report "$EVIDENCE_DIR/resource-envelope.json"
}

case "$MODE" in
  core)
    run_scenario core run_core
    ;;
  full)
    run_scenario core run_core
    run_scenario long run_long
    run_scenario ops run_ops
    ;;
  resource)
    run_scenario resource run_resource
    ;;
esac

echo
echo "== summary"
index=0
while [ "$index" -lt "${#SCENARIO_NAMES[@]}" ]; do
  echo "  ${SCENARIO_NAMES[$index]}: exit ${SCENARIO_CODES[$index]}"
  index=$((index + 1))
done
echo "  overall: exit $FINAL_EXIT"
exit "$FINAL_EXIT"
