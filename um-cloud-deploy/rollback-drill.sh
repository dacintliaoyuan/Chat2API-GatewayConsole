#!/usr/bin/env bash
# Drill: stop the Rust release, roll back to an installed Python release, and
# prove the Python gateway answers /ready on its own port.
#
# This is a DRILL, not a normal deploy.  It runs rollback.sh (which drains the
# current release, moves the release links, installs the target release's units and
# starts them), then stops any Rust unit that still survives under a different unit
# name, waits for the target gateway port, requires /ready to return 200, and
# re-checks the persistent data directories.  It never deletes or rewrites
# /var/lib/um-share, /var/lib/um-share-console or /etc/um-share.
#
# The script plans by default.  --execute is required to stop or start anything.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

TARGET_VERSION=""
# The drill rolls Rust (8003) back to Python (8100).  --health-port names the
# Python rollback target; --current-health-port names the Rust release that a failed
# rollback would restore.  Both are explicit because neither side is hardcoded.
HEALTH_PORT="${TARGET_PORT:-$GATEWAY_PORT_DEFAULT}"
CURRENT_HEALTH_PORT="${CURRENT_TARGET_PORT:-8003}"
RUST_UNITS=("um-share-gateway.service")
READY_PATH="/ready"
READY_TIMEOUT="$UM_HEALTH_TIMEOUT_S"
ROLLBACK_SCRIPT="${ROLLBACK_DRILL_SCRIPT:-$SCRIPT_DIR/rollback.sh}"
DATA_DIRS=("$UM_VAR" "$UM_CONSOLE_VAR" "$UM_ETC")
DATA_DIRS_SET=0
EXECUTE=0

usage() {
  cat <<'EOF'
Usage: rollback-drill.sh --to VERSION [options]

Options:
  --to VERSION          installed Python rollback release (required)
  --health-port PORT     Python rollback gateway port (default: $TARGET_PORT
                         or 8100)
  --current-health-port PORT
                         Rust gateway port restored by a failed rollback
                         (default: $CURRENT_TARGET_PORT or 8003)
  --rust-unit UNIT      Rust unit to stop after the rollback; repeatable
                         (default: um-share-gateway.service)
  --ready-path PATH      readiness path on the rollback gateway (default /ready)
  --ready-timeout SEC    readiness budget in seconds (default 60)
  --data-dir PATH        persistent state directory to require; repeatable
                         (default: /var/lib/um-share, /var/lib/um-share-console,
                         /etc/um-share)
  --rollback-script PATH rollback.sh from this release (default: this directory)
  --execute              actually stop/start units; without it the script only
                         prints the plan
  -h, --help              show this help

Exit codes: 0 success; 64 usage; 66 missing release/data; 68 the rollback
gateway failed /ready; 69 rollback/compensation failed (manual intervention);
70 software; 71 command missing; 72 file error; 75 lock held; 77 permission.
EOF
}

parse_args() {
while [ "$#" -gt 0 ]; do
  case "$1" in
    --to) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; TARGET_VERSION="$2"; shift 2 ;;
    --health-port) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; HEALTH_PORT="$2"; shift 2 ;;
    --current-health-port) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; CURRENT_HEALTH_PORT="$2"; shift 2 ;;
    --rust-unit)
      [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }
      if [ "${#RUST_UNITS[@]}" -eq 1 ] && [ "${RUST_UNITS[0]}" = "um-share-gateway.service" ]; then
        RUST_UNITS=()
      fi
      RUST_UNITS+=("$2")
      shift 2
      ;;
    --ready-path) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; READY_PATH="$2"; shift 2 ;;
    --ready-timeout) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; READY_TIMEOUT="$2"; shift 2 ;;
    --data-dir)
      [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }
      if [ "$DATA_DIRS_SET" -eq 0 ]; then
        DATA_DIRS=()
        DATA_DIRS_SET=1
      fi
      DATA_DIRS+=("$2")
      shift 2
      ;;
    --rollback-script) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; ROLLBACK_SCRIPT="$2"; shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    -h|--help) usage; exit "$EX_OK" ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit "$EX_USAGE" ;;
  esac
done

[ -n "$TARGET_VERSION" ] || { usage >&2; exit "$EX_USAGE"; }
case "$TARGET_VERSION" in
  *[!A-Za-z0-9._-]*) die "unsafe version: $TARGET_VERSION" "$EX_USAGE" ;;
esac
validate_port "--health-port" "$HEALTH_PORT"
validate_port "--current-health-port" "$CURRENT_HEALTH_PORT"
case "$READY_TIMEOUT" in
  ''|*[!0-9]*) die "--ready-timeout must be an integer" "$EX_USAGE" ;;
esac
[ "$READY_TIMEOUT" -ge 1 ] || \
  die "--ready-timeout must be at least 1 second" "$EX_USAGE"
case "$READY_PATH" in
  /*) ;;
  *) die "--ready-path must start with /" "$EX_USAGE" ;;
esac
[ "${#RUST_UNITS[@]}" -gt 0 ] || \
  die "at least one --rust-unit is required" "$EX_USAGE"
[ "${#DATA_DIRS[@]}" -gt 0 ] || \
  die "at least one --data-dir is required" "$EX_USAGE"
}

# The plan is also the documented drill order: rollback.sh stops the current
# release and starts the Python target, then the drill removes a Rust unit that
# survived under another name and proves /ready and the data directories.
drill_plan() {
  printf 'PLAN: release=%s gateway_port=%s current_gateway_port=%s\n' \
    "$TARGET_VERSION" "$HEALTH_PORT" "$CURRENT_HEALTH_PORT"
  printf 'PLAN: verify %s/RELEASE-MANIFEST.json\n' \
    "$UM_RELEASES/$TARGET_VERSION"
  printf 'PLAN: require broker unit ReadWritePaths /var/lib/um-share-console\n'
  printf 'PLAN: require data directories: %s\n' "${DATA_DIRS[*]}"
  printf 'PLAN: %s --to %s --health-port %s --current-health-port %s --no-edge\n' \
    "$ROLLBACK_SCRIPT" "$TARGET_VERSION" "$HEALTH_PORT" "$CURRENT_HEALTH_PORT"
  for unit in "${RUST_UNITS[@]}"; do
    if [ "$unit" = "um-share-broker.service" ] || \
       [ "$unit" = "um-share-console-python.service" ]; then
      printf 'PLAN: %s is a Python target unit; rollback.sh already replaced it\n' "$unit"
    else
      printf 'PLAN: if active, systemctl stop %s\n' "$unit"
    fi
  done
  printf 'PLAN: wait_port_open 127.0.0.1 %s\n' "$HEALTH_PORT"
  printf 'PLAN: curl http://127.0.0.1:%s%s (require 200)\n' \
    "$HEALTH_PORT" "$READY_PATH"
  printf 'PLAN: curl http://127.0.0.1:%s/v1/models (require 200/401/403)\n' \
    "$HEALTH_PORT"
  printf 'PLAN: require data directories: %s\n' "${DATA_DIRS[*]}"
}

drill_execute() {
  local target models_code unit
  target="$UM_RELEASES/$TARGET_VERSION"
  [ -d "$target" ] || \
    die "rollback release is not installed: $target" "$EX_NOINPUT"
  [ -f "$target/RELEASE-MANIFEST.json" ] || \
    die "rollback release manifest missing: $target" "$EX_NOINPUT"
  verify_release_manifest "$target" "$target/RELEASE-MANIFEST.json" || \
    die "rollback release manifest verification failed" "$EX_DATAERR"
  require_share_backend_write_path \
    "$target/um-cloud-deploy/systemd/um-share-broker.service"
  require_data_dirs "${DATA_DIRS[@]}"
  [ -x "$ROLLBACK_SCRIPT" ] || \
    die "rollback script is missing or not executable: $ROLLBACK_SCRIPT" "$EX_NOINPUT"

  log "rollback drill: switching to Python release $TARGET_VERSION (gateway $HEALTH_PORT)"
  log "this drill does not manage cloudflared; stop the public edge first if it points at the Rust port"
  "$ROLLBACK_SCRIPT" --to "$TARGET_VERSION" \
    --health-port "$HEALTH_PORT" \
    --current-health-port "$CURRENT_HEALTH_PORT" \
    --no-edge || \
    die "rollback.sh failed; the Rust release may still be current" "$EX_ROLLBACK"

  for unit in "${RUST_UNITS[@]}"; do
    if [ "$unit" = "um-share-broker.service" ] || \
       [ "$unit" = "um-share-console-python.service" ]; then
      log "$unit is a Python target unit; rollback.sh already replaced it"
      continue
    fi
    if unit_active "$unit"; then
      log "stopping the Rust unit $unit after the rollback"
      systemctl stop "$unit" || \
        die "cannot stop the Rust unit $unit" "$EX_OSERR"
      if unit_active "$unit"; then
        die "the Rust unit $unit is still active after its stop" "$EX_ROLLBACK"
      fi
    else
      log "the Rust unit $unit is already inactive"
    fi
  done

  wait_port_open 127.0.0.1 "$HEALTH_PORT" "$UM_HEALTH_TIMEOUT_S" || \
    die "Python rollback gateway $HEALTH_PORT did not open" "$EX_HEALTH"
  wait_ready_http "$HEALTH_PORT" "$READY_PATH" "$READY_TIMEOUT" || \
    die "Python rollback gateway $READY_PATH did not return 200 on $HEALTH_PORT" \
      "$EX_HEALTH"
  models_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$HEALTH_PORT/v1/models" 2>/dev/null || true)"
  case "$models_code" in
    200|401|403) ;;
    *)
      die "Python rollback gateway /v1/models probe failed: $models_code (port $HEALTH_PORT)" \
        "$EX_HEALTH"
      ;;
  esac
  require_data_dirs "${DATA_DIRS[@]}"
  printf 'drill ok: release=%s gateway_port=%s ready=%s data_dirs=%s\n' \
    "$TARGET_VERSION" "$HEALTH_PORT" "$READY_PATH" "${DATA_DIRS[*]}"
}

main() {
  parse_args "$@"
  if [ "$EXECUTE" -eq 0 ]; then
    drill_plan
    exit "$EX_OK"
  fi
  require_root
  require_ubuntu_2604
  require_cmd systemctl
  require_cmd curl
  require_cmd flock
  require_cmd install
  require_cmd python3.14
  # rollback.sh takes the deploy lock itself; holding it here too would make
  # the child's own flock fail on the inherited descriptor.
  drill_execute
  exit "$EX_OK"
}

# Sourcing this file lets a test drive drill_execute with stubbed systemctl/curl;
# executing it runs the drill.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
