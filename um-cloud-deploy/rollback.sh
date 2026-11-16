#!/usr/bin/env bash
# Switch to a previously installed release with the same 15-minute gate.
# This never deletes a release, a data directory or a secret.  The default target
# is the "previous" symlink written by update.sh; an explicit --to VERSION is
# allowed only for an already-installed release.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The release that owns THIS script.  Checkpoint and recovery code must come
# from this release, not from the old "current" release: the old CLI may not
# know r17's --share-secret-owner/--share-secret-group, and its
# deployment_restore did not require the share-secret key.
RELEASE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

TARGET_VERSION=""
DRAIN_BUDGET="$UM_DRAIN_BUDGET_S"
NO_EDGE=0
# The rollback target is normally the Python release at 8100; the CURRENT release
# may be Rust at 8003.  A failed rollback restores the current release, so the two
# ports are separate.  Neither is hardcoded in the probes below.
HEALTH_PORT="${TARGET_PORT:-$GATEWAY_PORT_DEFAULT}"
CURRENT_HEALTH_PORT="${CURRENT_TARGET_PORT:-$GATEWAY_PORT_DEFAULT}"
RECOVER_BACKUP=""
RECOVER_SECURITY_STATE=""

usage() {
  cat <<'EOF'
Usage: rollback.sh [--to VERSION] [options]

Options:
  --to VERSION         installed release version (default: previous symlink)
  --drain-budget SEC   total drain budget, 2..900 (default 900)
  --health-port PORT   gateway port of the TARGET release (default:
                       $TARGET_PORT or 8100; the Python rollback release)
  --current-health-port PORT
                       gateway port of the CURRENT release, used when a failed
                       rollback restores it (default: $CURRENT_TARGET_PORT or
                       8100; pass 8003 when the current release is Rust)
  --no-edge            do not stop/start the managed cloudflared unit
  --recover-backup PATH explicit recovery only: restore this sealed backup;
                        the release version is NOT switched
  --recover-security-state PATH
                        independent latest security state (required with
                        --recover-backup)
  -h, --help            show this help

Exit codes: 0 success; 64 usage; 66 no target; 68 health check failed and the
current release was restored; 69 rollback/compensation failed (manual intervention
required; the public entry may still be open); 70 software; 71 command missing;
72 file error; 73 missing release; 74 I/O; 75 drain timed out; 77 permission.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --to) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; TARGET_VERSION="$2"; shift 2 ;;
    --drain-budget) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; DRAIN_BUDGET="$2"; shift 2 ;;
    --health-port) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; HEALTH_PORT="$2"; shift 2 ;;
    --current-health-port) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; CURRENT_HEALTH_PORT="$2"; shift 2 ;;
    --no-edge) NO_EDGE=1; shift ;;
    --recover-backup) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RECOVER_BACKUP="$2"; shift 2 ;;
    --recover-security-state) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RECOVER_SECURITY_STATE="$2"; shift 2 ;;
    -h|--help) usage; exit "$EX_OK" ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit "$EX_USAGE" ;;
  esac
done

case "$DRAIN_BUDGET" in
  ''|*[!0-9]*) echo "--drain-budget must be an integer" >&2; exit "$EX_USAGE" ;;
esac
[ "$DRAIN_BUDGET" -ge 2 ] && [ "$DRAIN_BUDGET" -le 900 ] || {
  echo "--drain-budget must be within 2..900 seconds" >&2
  exit "$EX_USAGE"
}
validate_port "--health-port" "$HEALTH_PORT"
validate_port "--current-health-port" "$CURRENT_HEALTH_PORT"
# The live current release may be an r16 CLI: name its broker drain deadline
# explicitly so every accepted budget is also valid there.  A 1-second budget
# cannot reserve a positive deadline and is refused above.
DRAIN_MAX="$(broker_drain_max_for_budget "$DRAIN_BUDGET")"
if [ -n "$RECOVER_BACKUP" ] || [ -n "$RECOVER_SECURITY_STATE" ]; then
  [ -n "$RECOVER_BACKUP" ] && [ -n "$RECOVER_SECURITY_STATE" ] || \
    die "--recover-backup and --recover-security-state must be given together" "$EX_USAGE"
  [ -z "$TARGET_VERSION" ] || \
    die "explicit recovery does not switch releases; drop --to" "$EX_USAGE"
  [ -f "$RECOVER_BACKUP" ] || \
    die "recovery backup not found: $RECOVER_BACKUP" "$EX_NOINPUT"
  [ -f "$RECOVER_SECURITY_STATE" ] || \
    die "recovery security state not found: $RECOVER_SECURITY_STATE" "$EX_NOINPUT"
fi

require_root
require_ubuntu_2604
require_cmd systemctl
require_cmd flock
require_cmd curl
require_cmd install
require_cmd python3.14

deploy_python() {
  if [ -x "$UM_VENV/bin/python" ]; then
    printf '%s\n' "$UM_VENV/bin/python"
    return 0
  fi
  python_bin
}

run_as_share() {
  if [ "$(id -u)" -eq 0 ] && [ "${UM_BROKER_USER:-}" != "root" ]; then
    require_cmd runuser
    runuser -u "$UM_BROKER_USER" -- "$@"
  else
    "$@"
  fi
}

run_recovery() {
  local release="$1"
  local py
  py="$(deploy_python)" || die "python interpreter not found" "$EX_OSERR"
  "$py" "$release/um-cloud-deploy/backup_restore.py" \
    --deployment-restore \
    --restore-backup "$RECOVER_BACKUP" \
    --restore-security-state "$RECOVER_SECURITY_STATE" \
    --deployment-current "$UM_PREFIX/current" \
    --deployment-etc "$UM_ETC" \
    --deployment-var "$UM_VAR" \
    --deployment-console-var "$UM_CONSOLE_VAR" \
    --share-secret-owner "$(id -u "$UM_CONSOLE_USER")" \
    --share-secret-group "$(id -g "$UM_CONSOLE_GROUP")" || return 1
  chown -R "$UM_BROKER_USER:$UM_BROKER_GROUP" "$UM_VAR" "$UM_CONSOLE_VAR"
  if [ -f "$UM_ETC/share-management.json" ]; then
    chown root:"$UM_BROKER_GROUP" "$UM_ETC/share-management.json"
    chmod 0640 "$UM_ETC/share-management.json"
  fi
}

exec 9>"$UM_DEPLOY_LOCK"
flock -n 9 || die "another deploy operation holds $UM_DEPLOY_LOCK" "$EX_TEMPFAIL"

CURRENT_LINK="$UM_PREFIX/current"
PREVIOUS_LINK="$UM_PREFIX/previous"
[ -L "$CURRENT_LINK" ] || die "no current release" "$EX_NOINPUT"
CURRENT_TARGET="$(readlink -f "$CURRENT_LINK")"
CURRENT_VERSION="$(basename "$CURRENT_TARGET")"
# Repair the persistent share-key wiring before the checkpoint and the switch.
# Explicit recovery is the exception: the sealed backup carries the share-key
# file, and a configured live path may be missing precisely because recovery
# must restore it.
if [ -z "$RECOVER_BACKUP" ]; then
  ensure_share_secret_wiring
else
  log "explicit recovery: the sealed backup carries the share-key file; skipping live wiring"
fi

if [ -z "$RECOVER_BACKUP" ]; then
  if [ -z "$TARGET_VERSION" ]; then
    [ -L "$PREVIOUS_LINK" ] || die "no previous release; pass --to VERSION" "$EX_NOINPUT"
    TARGET_VERSION="$(basename "$(readlink -f "$PREVIOUS_LINK")")"
  fi
  case "$TARGET_VERSION" in
    *[!A-Za-z0-9._-]*) die "unsafe version: $TARGET_VERSION" "$EX_USAGE" ;;
  esac
  TARGET="$UM_RELEASES/$TARGET_VERSION"
  [ -d "$TARGET" ] || die "release not installed: $TARGET" "$EX_NOINPUT"
  [ -f "$TARGET/RELEASE-MANIFEST.json" ] || \
    die "release manifest missing: $TARGET" "$EX_NOINPUT"
  verify_release_manifest "$TARGET" "$TARGET/RELEASE-MANIFEST.json" || \
    die "target release manifest verification failed" "$EX_DATAERR"
  # A rollback release must be able to start the merged Python gateway: its
  # broker unit has to allow writes to /var/lib/um-share-console, or the share
  # gateway child dies with "Read-only file system" and the rollback is not a
  # rollback.
  require_share_backend_write_path \
    "$TARGET/um-cloud-deploy/systemd/um-share-broker.service"

  if [ "$TARGET_VERSION" = "$CURRENT_VERSION" ]; then
    log "release $TARGET_VERSION is already current"
    exit "$EX_OK"
  fi
fi

CLOUDFLARED_MANAGED=0
CLOUDFLARED_WAS_ACTIVE=0
CONSOLE_WAS_ACTIVE=0
BROKER_WAS_ACTIVE=0
if [ -f "$UM_SYSTEMD_DIR/cloudflared.service" ]; then
  CLOUDFLARED_MANAGED=1
  if unit_active cloudflared.service; then CLOUDFLARED_WAS_ACTIVE=1; fi
fi
if unit_active um-share-console.service; then CONSOLE_WAS_ACTIVE=1; fi
if unit_active um-share-broker.service; then BROKER_WAS_ACTIVE=1; fi

# Start the cloudflared edge only when this deployment manages it AND it was
# active before the switch.  An unmanaged or inactive edge is never started: the
# script must not claim it stopped or restored an entry it never managed.  A failed
# start does not prove the unit is down (a readiness timeout can leave the process
# running), so stop it again and confirm.  Return 0 when the edge serves again (or
# was never managed/started), 1 when the start failed and the stop is confirmed,
# 2 when the stop is not confirmed and the unit may still serve public traffic.
restore_edge() {
  if [ "$NO_EDGE" -eq 1 ]; then
    return 0
  fi
  if [ "$CLOUDFLARED_MANAGED" -eq 0 ]; then
    log "the public edge is not managed by this deployment; nothing to restore"
    return 0
  fi
  if [ "$CLOUDFLARED_WAS_ACTIVE" -eq 1 ]; then
    if ! systemctl start cloudflared.service; then
      if ! systemctl stop cloudflared.service 2>/dev/null; then
        return 2
      fi
      if unit_active cloudflared.service; then
        return 2
      fi
      return 1
    fi
  fi
  return 0
}

# Restore exactly the units that were active before the switch and wait for their
# listeners.  A unit that was already inactive is neither started nor waited for;
# health_gate probes the console and the CURRENT release's gateway port, so it runs
# only when both units were active.
# Return 0 only when every originally-active object is serving again.
restore_units() {
  local failed=0 console_listen_port
  if [ "$BROKER_WAS_ACTIVE" -eq 1 ]; then
    systemctl start um-share-broker.service || failed=1
    brokerctl start-explicit || failed=1
  fi
  if [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
    systemctl start um-share-console.service || failed=1
  fi
  [ "$failed" -eq 0 ] || return 1
  if [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
    console_listen_port="$(console_port)" || return 1
    wait_port_open 127.0.0.1 "$console_listen_port" "$UM_HEALTH_TIMEOUT_S" || return 1
  fi
  if [ "$BROKER_WAS_ACTIVE" -eq 1 ]; then
    wait_port_open 127.0.0.1 "$CURRENT_HEALTH_PORT" "$UM_HEALTH_TIMEOUT_S" || return 1
  fi
  if [ "$BROKER_WAS_ACTIVE" -eq 1 ] && [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
    health_gate "$CURRENT_HEALTH_PORT" || return 1
  fi
  return 0
}

# Record the links and the installed unit facts as they actually are after an
# incomplete restore.  The operator needs this residue, not a "restored" claim that
# did not happen.  The unit state also shows whether the installed unit still
# carries the removed Wants= or budget arguments.
report_switch_residue() {
  local current bin unit state wants exec_start budget
  current="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
  bin="$(readlink -f "$UM_PREFIX/bin" 2>/dev/null || true)"
  log "ERROR: actual residue: $CURRENT_LINK -> ${current:-unreadable}; $UM_PREFIX/bin -> ${bin:-unreadable}"
  for unit in um-share-broker.service um-share-console.service; do
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    wants="$(systemctl show -p Wants --value "$unit" 2>/dev/null || true)"
    exec_start="$(systemctl show -p ExecStart --value "$unit" 2>/dev/null || true)"
    case "$exec_start" in
      *--budget-db*|*--budget-config*) budget="budget args present" ;;
      *) budget="no budget args" ;;
    esac
    log "ERROR: actual residue: $unit active=${state:-unknown} Wants=${wants:-unknown} ExecStart=${budget}"
  done
}

# A failed restore must not assert the public edge is stopped when this run never
# managed it: --no-edge or an absent unit leaves the real public entry untouched,
# so the operator has to verify it instead of trusting the script.
edge_stopped_clause() {
  if [ "$NO_EDGE" -eq 1 ]; then
    printf '%s' "cloudflared is not managed by this run; verify the public entry (it may still be serving)"
  elif [ "$CLOUDFLARED_MANAGED" -eq 0 ]; then
    printf '%s' "the public edge is not managed by this deployment; verify the public entry (it may still be serving)"
  elif [ "$CLOUDFLARED_WAS_ACTIVE" -eq 0 ]; then
    printf '%s' "cloudflared was inactive before the switch and stays stopped"
  else
    printf '%s' "cloudflared stays stopped"
  fi
}

# Log the truth about a failed restore_edge: 1 means the stop is confirmed (the
# public entry stays closed), 2 means it is not (the entry may still be open).
report_edge_failure() {
  local rc="$1"
  if [ "$rc" -eq 2 ]; then
    log "ERROR: cloudflared did not start and its stop could not be confirmed; the public entry may still be open; manual intervention required"
  else
    log "ERROR: cloudflared did not start; its stop is confirmed, so the public entry stays stopped; manual intervention required"
  fi
}

# Exit after a failed restore_edge with the matching truth.
die_edge_failure() {
  local code="$1" context="$2" rc="${3:-1}"
  if [ "$rc" -eq 2 ] || [ "$rc" -eq 4 ]; then
    die "$context; cloudflared did not start and its stop could not be confirmed; the public entry may still be open; manual intervention required" "$code"
  fi
  die "$context; cloudflared did not start; its stop is confirmed, so the public entry stays stopped; manual intervention required" "$code"
}

# Exit after a failed compensate_switch.  Status 4 means the links and units are
# back but cloudflared's stop is unconfirmed (the public entry may still be open);
# 3 means cloudflared did not start and its stop is confirmed.
die_compensation_failed() {
  local rc="$1"
  if [ "$rc" -eq 4 ]; then
    die "the previous state was not fully restored; cloudflared did not start and its stop could not be confirmed; the public entry may still be open; manual intervention required" "$EX_ROLLBACK"
  fi
  if [ "$rc" -eq 3 ]; then
    die "the previous state was not fully restored; cloudflared did not start; its stop is confirmed, so the public entry stays stopped; manual intervention required" "$EX_ROLLBACK"
  fi
  die "the previous state was not fully restored; $(edge_stopped_clause); manual intervention required" "$EX_ROLLBACK"
}

# Restore both links and verify them.  Best effort on each link, deliberately not
# repoint_bin() first: that helper die()s on a non-symlink path, which would
# abort the compensation with a different code and skip the unit/edge restore.
# Return 0 only when both links resolve to the previous release.
restore_switch_links() {
  local failed=0
  if ! atomic_symlink "$CURRENT_TARGET" "$CURRENT_LINK"; then
    log "ERROR: could not restore $CURRENT_LINK -> $CURRENT_TARGET"
    failed=1
  fi
  if [ -L "$UM_PREFIX/bin" ] || [ ! -e "$UM_PREFIX/bin" ]; then
    if ! repoint_bin "$CURRENT_TARGET"; then
      log "ERROR: could not restore $UM_PREFIX/bin -> $CURRENT_TARGET"
      failed=1
    fi
  else
    log "ERROR: $UM_PREFIX/bin is not a symlink; not restoring it"
    failed=1
  fi
  if [ "$failed" -eq 0 ]; then
    if [ "$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)" != \
         "$CURRENT_TARGET" ]; then
      log "ERROR: $CURRENT_LINK does not resolve to $CURRENT_TARGET"
      failed=1
    fi
    if [ "$(readlink -f "$UM_PREFIX/bin" 2>/dev/null || true)" != \
         "$CURRENT_TARGET/um-cloud-deploy/bin" ]; then
      log "ERROR: $UM_PREFIX/bin does not resolve to $CURRENT_TARGET/um-cloud-deploy/bin"
      failed=1
    fi
  fi
  return "$failed"
}

# Undo a half-applied switch: links first, then the units that were active
# before the switch, then cloudflared.  The edge is opened last and only when the links
# and units are back; an incomplete restore must not reopen a managed public entry
# and the caller must report the residue instead of "restored".  Return 1 for a
# link failure, 2 for a unit failure, 3 for a cloudflared start failure whose stop is
# confirmed, 4 for a cloudflared failure whose stop is unconfirmed (the public entry may
# still be open), 0 only when everything is serving again.
compensate_switch() {
  local rc=0
  if ! restore_switch_links; then
    report_switch_residue
    log "ERROR: the release/operator-bin links were not restored; $(edge_stopped_clause); manual intervention required"
    return 1
  fi
  if ! restore_units; then
    report_switch_residue
    log "ERROR: the links are restored but a unit that was active before the switch did not come back; $(edge_stopped_clause); manual intervention required"
    return 2
  fi
  restore_edge || rc=$?
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  report_switch_residue
  report_edge_failure "$rc"
  if [ "$rc" -eq 2 ]; then
    return 4
  fi
  return 3
}

# Start every service the target release needs and confirm it.  This is the
# forward switch, not a restore: it starts both units regardless of their prior
# state because the target release owns both.
start_switch() {
  local console_listen_port
  systemctl start um-share-broker.service || return 1
  brokerctl start-explicit || return 1
  systemctl start um-share-console.service || return 1
  console_listen_port="$(console_port)" || return 1
  wait_port_open 127.0.0.1 "$console_listen_port" "$UM_HEALTH_TIMEOUT_S" || return 1
  wait_port_open 127.0.0.1 "$HEALTH_PORT" "$UM_HEALTH_TIMEOUT_S" || return 1
  health_gate "$HEALTH_PORT" || return 1
  return 0
}

# The rollback also replaces the broker host process: the unit's ExecStart was
# loaded at process start, so starting the unit again after the link move is not
# enough while the old process stays alive.  Stop the unit before the target link
# moves and confirm it is inactive; the broker CLI drain above only stopped the
# share gateway child.  A failed stop is a hard precondition, never a version switch.
stop_broker_before_switch() {
  if ! systemctl stop um-share-broker.service; then
    return 1
  fi
  if unit_active um-share-broker.service; then
    return 1
  fi
  return 0
}

# The switch moves two links: $CURRENT_LINK (the release) and $UM_PREFIX/bin
# (the operator/systemd entry).  repoint_bin() rejects a real file/directory at
# $UM_PREFIX/bin with die(); that is a static precondition, so refuse it while
# cloudflared, the broker and the console are still running instead of after the drain.
# An ln/mv failure can only surface during the switch and is compensated below.
preflight_switch_links() {
  if [ -e "$UM_PREFIX/bin" ] && [ ! -L "$UM_PREFIX/bin" ]; then
    die "$UM_PREFIX/bin exists and is not a symlink; refusing to switch" \
      "$EX_CANTCREAT"
  fi
}

preflight_switch_links

if [ -n "$RECOVER_BACKUP" ]; then
  log "explicit recovery mode: no release switch; data is restored after the drain"
fi

if [ "$NO_EDGE" -eq 0 ] && [ "$CLOUDFLARED_MANAGED" -eq 1 ] && \
   [ "$CLOUDFLARED_WAS_ACTIVE" -eq 1 ]; then
  log "stopping cloudflared: no new public requests during rollback drain"
  systemctl stop cloudflared.service || die "cannot stop cloudflared" "$EX_OSERR"
elif [ "$NO_EDGE" -eq 1 ]; then
  log "cloudflared is not managed by this run; the public edge may still serve during the drain"
elif [ "$CLOUDFLARED_MANAGED" -eq 0 ]; then
  log "the public edge is not managed by this deployment; the public edge may still serve during the drain"
else
  log "cloudflared.service is inactive; not stopping it"
fi

log "draining the share gateway (budget ${DRAIN_BUDGET}s)"
# Use the live current release's CLI: it is installed under /opt/um-share and
# readable by the broker user, while this script owns the top-level option order.
# Name the broker drain deadline explicitly: the r16 CLI reserves its default 90
# seconds even for a smaller budget, and the new CLI accepts the same value.
if brokerctl_release "$CURRENT_TARGET" \
    --broker-drain-max "$DRAIN_MAX" \
    --drain-budget "$DRAIN_BUDGET" --poll-interval 5 stop; then
  :
else
  code=$?
  log "drain/stop failed with exit $code; restoring the units and the public edge"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    exit "$code"
  fi
  die_compensation_failed "$rc"
fi

if [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
  if ! systemctl stop um-share-console.service; then
    log "console stop failed; restoring the units and the public edge"
    rc=0
    compensate_switch || rc=$?
    if [ "$rc" -eq 0 ]; then
      die "cannot stop the console unit" "$EX_OSERR"
    fi
    die_compensation_failed "$rc"
  fi
fi

if [ -n "$RECOVER_BACKUP" ]; then
  log "explicit recovery: restoring $RECOVER_BACKUP into the live paths"
  # Use the recovery implementation shipped with THIS script's release.  The old
  # current release's CLI rejects --share-secret-owner/--share-secret-group and
  # its deployment_restore does not require the share-secret key (R6).
  if ! run_recovery "$RELEASE_ROOT"; then
    log "explicit recovery failed; restarting the current release without data changes"
    rc=0
    compensate_switch || rc=$?
    if [ "$rc" -eq 0 ]; then
      exit "$EX_SOFTWARE"
    fi
    die_compensation_failed "$rc"
  fi
  # Recovery does not switch releases, but the operator entry must still match
  # the live current release.  A failure here would otherwise exit under set -e
  # with every service stopped and the edge closed.
  if repoint_bin "$CURRENT_TARGET"; then
    :
  else
    log "operator bin restore failed; restarting $CURRENT_TARGET without it"
    rc=0
    compensate_switch || rc=$?
    if [ "$rc" -eq 0 ]; then
      die "operator bin restore failed after the data restore" "$EX_IOERR"
    fi
    die_compensation_failed "$rc"
  fi
  if ! start_switch; then
    die "recovery restored data but the current release failed its health gate; $(edge_stopped_clause); manual intervention required" "$EX_HEALTH"
  fi
  rc=0
  restore_edge || rc=$?
  if [ "$rc" -ne 0 ]; then
    die_edge_failure "$EX_OSERR" "recovery restored the data but cloudflared did not start" "$rc"
  fi
  log "recovery complete: data restored from $RECOVER_BACKUP; release unchanged"
  exit "$EX_OK"
fi

# The broker unit must stop before the target links move: start_switch below
# starts it again so it loads the target release's ExecStart.  A failure here
# aborts before the switch and restores the units and the edge.
log "stopping the broker unit: the target release must own a fresh broker host"
if ! stop_broker_before_switch; then
  log "broker unit stop failed or is still active; no version switch"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    die "cannot stop the broker unit" "$EX_OSERR"
  fi
  die_compensation_failed "$rc"
fi

if ! atomic_symlink "$TARGET" "$CURRENT_LINK"; then
  log "cannot switch current symlink; restoring the units and the public edge"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    die "current symlink switch failed" "$EX_OSFILE"
  fi
  die_compensation_failed "$rc"
fi
# Move the operator/systemd entry with "current"; otherwise the broker unit's
# ExecStartPost/ExecStop keeps running the previous release's brokerctl.  A failure
# here leaves the two links on different releases with every service stopped, so undo
# the version link and bring the previous release's units and edge back.
if repoint_bin "$TARGET"; then
  :
else
  code=$?
  log "operator bin switch failed with exit $code; restoring $CURRENT_TARGET"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    die "operator bin switch failed; previous release restored" "$code"
  fi
  die_compensation_failed "$rc"
fi
# The installed units must come from the release "current" now points at: an old
# unit would still name the removed budget arguments or pull the broker up through
# Wants=.  Install after the link move and before daemon-reload; a failure restores
# the current release's units and links.
if ! install_units "$TARGET"; then
  log "systemd unit installation failed; restoring $CURRENT_TARGET"
  install_units "$CURRENT_TARGET" || \
    log "ERROR: cannot reinstall the current release's units"
  systemctl daemon-reload || \
    log "ERROR: cannot reload systemd after the failed unit installation"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    die "systemd unit installation failed; current release restored" "$EX_OSFILE"
  fi
  die_compensation_failed "$rc"
fi
if ! systemctl daemon-reload; then
  log "systemd daemon-reload failed; restoring $CURRENT_TARGET units and links"
  install_units "$CURRENT_TARGET" || \
    log "ERROR: cannot reinstall the current release's units"
  systemctl daemon-reload || \
    log "ERROR: cannot reload systemd after restoring the current units"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    die "cannot reload systemd after installing the units; current release restored" "$EX_OSERR"
  fi
  die_compensation_failed "$rc"
fi
# Stop what the forward switch started before restoring the current release.
# start_switch starts both units regardless of their prior state, so both are
# stopped here.  A stop is accepted only when confirmed: the console stop must
# the broker unit must be stopped and inactive afterwards.
# The broker CLI stop only stops the share gateway host child, not the unit
# itself, so the unit stop always runs whatever the CLI returned.  Return 0 only
# when both stops are confirmed.
stop_switch_units() {
  local failed=0
  if ! systemctl stop um-share-console.service; then
    log "ERROR: the console unit did not stop"
    failed=1
  fi
  if ! brokerctl --broker-drain-max 30 --drain-budget 60 --poll-interval 1 stop 2>/dev/null; then
    log "WARNING: the broker CLI stop did not confirm; stopping the broker unit directly"
  fi
  if ! systemctl stop um-share-broker.service 2>/dev/null; then
    log "ERROR: the broker unit did not stop"
    failed=1
  elif unit_active um-share-broker.service; then
    log "ERROR: the broker unit is still active after its stop"
    failed=1
  fi
  return "$failed"
}

if ! start_switch; then
  log "target release failed its health gate; restoring $CURRENT_TARGET"
  if ! stop_switch_units; then
    report_switch_residue
    die "a unit started for the target release could not be stopped; the release/operator-bin links were not restored; $(edge_stopped_clause); manual intervention required" "$EX_ROLLBACK"
  fi
  if compensate_switch; then
    die "target release failed; previous release $CURRENT_VERSION restored" "$EX_HEALTH"
  else
    rc=$?
    if [ "$rc" -eq 3 ] || [ "$rc" -eq 4 ]; then
      die_edge_failure "$EX_OSERR" "target release failed and the previous release is back, but cloudflared did not start" "$rc"
    fi
    die "target release failed and rollback also failed; $(edge_stopped_clause); manual intervention required" "$EX_ROLLBACK"
  fi
fi

rc=0
restore_edge || rc=$?
if [ "$rc" -ne 0 ]; then
  die_edge_failure "$EX_OSERR" "rollback switched to $TARGET_VERSION" "$rc"
fi
log "rollback complete: $CURRENT_VERSION -> $TARGET_VERSION"
exit "$EX_OK"
