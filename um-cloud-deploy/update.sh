#!/usr/bin/env bash
# Version switch with a 15-minute drain, health gate and automatic rollback.
#
# The ONLY lifecycle authority is:
#   * systemd supervises the broker host (um-share-broker.service);
#   * the broker host owns the 8100 child (LinuxHostOps);
#   * systemd supervises the console (um-share-console.service);
#   * the console owns the 8110 child (LinuxBackendOps).
# This script never starts 8100 or 8110 with a bare Popen.  It stops the managed
# cloudflared unit first (no new public requests), asks the broker to drain/wait/stop, and only
# switches the "current" symlink after the 15-minute gate.  Data directories are
# outside the release and are never deleted or rewritten.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The release that owns THIS script.  Recovery and checkpoint code must come
# from this release, not from the old "current" release: the old CLI may not
# know r17's --share-secret-owner/--share-secret-group, and its
# deployment_restore did not require the share-secret key.
RELEASE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

RELEASE_ARCHIVE=""
VERSION=""
SHA256=""
DRAIN_BUDGET="$UM_DRAIN_BUDGET_S"
SKIP_DRAIN=0
NO_EDGE=0
RECOVER_BACKUP=""
RECOVER_SECURITY_STATE=""

usage() {
  cat <<'EOF'
Usage: update.sh --release <um-share-VERSION.tar.gz> [options]
       update.sh --version <VERSION> [options]

Options:
  --release PATH       new release tar.gz
  --version VERSION    expected/installed version
  --sha256 HEX         expected archive SHA-256
  --drain-budget SEC   total drain budget, 2..900 (default 900)
  --skip-drain          emergency only: do not wait/drain; refuses by default
  --no-edge             do not stop/start the managed cloudflared unit
  --recover-backup PATH explicit recovery only: restore this sealed backup
  --recover-security-state PATH
                        independent latest security state (required with
                        --recover-backup)
  -h, --help             show this help

Exit codes: 0 success; 64 usage; 65 manifest; 66 missing input;
68 health check failed and the previous release was restored;
69 rollback/compensation failed (manual intervention required; the public
entry may still be open); 70 software; 71 OS/command missing; 72 file error;
73 version conflict; 74 I/O; 75 drain timed out (no version switch);
77 permission.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --release) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RELEASE_ARCHIVE="$2"; shift 2 ;;
    --version) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; VERSION="$2"; shift 2 ;;
    --sha256) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; SHA256="$2"; shift 2 ;;
    --drain-budget) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; DRAIN_BUDGET="$2"; shift 2 ;;
    --skip-drain) SKIP_DRAIN=1; shift ;;
    --no-edge) NO_EDGE=1; shift ;;
    --recover-backup) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RECOVER_BACKUP="$2"; shift 2 ;;
    --recover-security-state) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RECOVER_SECURITY_STATE="$2"; shift 2 ;;
    -h|--help) usage; exit "$EX_OK" ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit "$EX_USAGE" ;;
  esac
done

if [ -z "$RECOVER_BACKUP" ]; then
  [ -n "$RELEASE_ARCHIVE" ] || [ -n "$VERSION" ] || {
    usage >&2
    exit "$EX_USAGE"
  }
fi
case "$DRAIN_BUDGET" in
  ''|*[!0-9]*) echo "--drain-budget must be an integer" >&2; exit "$EX_USAGE" ;;
esac
[ "$DRAIN_BUDGET" -ge 2 ] && [ "$DRAIN_BUDGET" -le 900 ] || {
  echo "--drain-budget must be within 2..900 seconds" >&2
  exit "$EX_USAGE"
}
# The live current release may be an r16 CLI: name its broker drain deadline
# explicitly so every accepted budget is also valid there.  A 1-second budget
# cannot reserve a positive deadline and is refused above.
DRAIN_MAX="$(broker_drain_max_for_budget "$DRAIN_BUDGET")"
if [ "$SKIP_DRAIN" -eq 1 ]; then
  die "--skip-drain is refused: a version switch requires the 15-minute drain gate" "$EX_USAGE"
fi
if [ -n "$RECOVER_BACKUP" ] || [ -n "$RECOVER_SECURITY_STATE" ]; then
  [ -n "$RECOVER_BACKUP" ] && [ -n "$RECOVER_SECURITY_STATE" ] || \
    die "--recover-backup and --recover-security-state must be given together" "$EX_USAGE"
  [ -f "$RECOVER_BACKUP" ] || \
    die "recovery backup not found: $RECOVER_BACKUP" "$EX_NOINPUT"
  [ -f "$RECOVER_SECURITY_STATE" ] || \
    die "recovery security state not found: $RECOVER_SECURITY_STATE" "$EX_NOINPUT"
fi

require_root
require_ubuntu_2604
require_cmd install
require_cmd systemctl
require_cmd flock
require_cmd sha256sum
require_cmd tar
require_cmd curl
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
  if [ -f "$UM_ETC/prices.json" ]; then
    chown root:"$UM_BROKER_GROUP" "$UM_ETC/prices.json"
    chmod 0640 "$UM_ETC/prices.json"
  fi
}

exec 9>"$UM_DEPLOY_LOCK"
flock -n 9 || die "another deploy operation holds $UM_DEPLOY_LOCK" "$EX_TEMPFAIL"

CURRENT_LINK="$UM_PREFIX/current"
PREVIOUS_LINK="$UM_PREFIX/previous"
[ -L "$CURRENT_LINK" ] || die "no current release; run install.sh first" "$EX_NOINPUT"
CURRENT_TARGET="$(readlink -f "$CURRENT_LINK")"
CURRENT_VERSION="$(basename "$CURRENT_TARGET")"
[ -d "$CURRENT_TARGET" ] || die "current release target missing: $CURRENT_TARGET" "$EX_NOINPUT"
# Repair the persistent share-key wiring before the checkpoint and the switch:
# an update-only upgrade must not leave the console on the release-relative
# default key path.  Explicit recovery is the exception: the sealed backup
# carries the share-key file, and a configured live path may be missing precisely
# because recovery must restore it.
if [ -z "$RECOVER_BACKUP" ]; then
  ensure_share_secret_wiring
else
  log "explicit recovery: the sealed backup carries the share-key file; skipping live wiring"
fi

STAGE=""
cleanup() {
  if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
    rm -rf -- "$STAGE"
  fi
}
trap cleanup EXIT

if [ -n "$RECOVER_BACKUP" ]; then
  VERSION=""
  TARGET=""
else

if [ -z "$VERSION" ]; then
  case "$RELEASE_ARCHIVE" in
    *um-share-*.tar.gz) VERSION="$(basename "$RELEASE_ARCHIVE" .tar.gz)"; VERSION="${VERSION#um-share-}" ;;
    *) die "cannot derive version from $RELEASE_ARCHIVE; pass --version" "$EX_USAGE" ;;
  esac
fi
case "$VERSION" in
  *[!A-Za-z0-9._-]*) die "unsafe version: $VERSION" "$EX_USAGE" ;;
esac

if [ "$VERSION" = "$CURRENT_VERSION" ]; then
  log "release $VERSION is already current; no switch"
  exit "$EX_OK"
fi

[ -f "$RELEASE_ARCHIVE" ] || die "release archive not found: $RELEASE_ARCHIVE" "$EX_NOINPUT"
if [ -n "$SHA256" ]; then
  [ "$(sha256_file "$RELEASE_ARCHIVE")" = "$SHA256" ] || \
    die "release archive SHA-256 mismatch" "$EX_DATAERR"
elif [ -f "$RELEASE_ARCHIVE.sha256" ]; then
  expected="$(awk '{print $1}' "$RELEASE_ARCHIVE.sha256")"
  [ "$(sha256_file "$RELEASE_ARCHIVE")" = "$expected" ] || \
    die "release archive SHA-256 mismatch" "$EX_DATAERR"
else
  log "WARNING: release archive has no SHA-256 evidence"
fi

STAGE="$(mktemp -d "$UM_RELEASES/.update.XXXXXX")"
tar -xzf "$RELEASE_ARCHIVE" -C "$STAGE"
mapfile -t tops < <(find "$STAGE" -mindepth 1 -maxdepth 1 -type d -printf '%p\n')
[ "${#tops[@]}" -eq 1 ] || die "release archive must contain exactly one top-level directory" "$EX_DATAERR"
PAYLOAD="${tops[0]}"
[ -f "$PAYLOAD/RELEASE-MANIFEST.json" ] || \
  die "RELEASE-MANIFEST.json missing in payload" "$EX_DATAERR"
manifest_version="$(python3.14 - "$PAYLOAD/RELEASE-MANIFEST.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("release_version", ""))
PY
)"
[ "$manifest_version" = "$VERSION" ] || \
  die "manifest release_version=$manifest_version, expected $VERSION" "$EX_DATAERR"
verify_release_manifest "$PAYLOAD" "$PAYLOAD/RELEASE-MANIFEST.json" || \
  die "new release manifest verification failed" "$EX_DATAERR"

TARGET="$UM_RELEASES/$VERSION"
if [ -d "$TARGET" ]; then
  cmp -s "$TARGET/RELEASE-MANIFEST.json" "$PAYLOAD/RELEASE-MANIFEST.json" || \
    die "release $VERSION already exists with different content" "$EX_CANTCREAT"
  log "release $VERSION already installed and identical"
else
  mv "$PAYLOAD" "$TARGET"
  PAYLOAD="$TARGET"
  STAGE=""
  chown -R root:root "$TARGET"
  chmod -R go-w "$TARGET"
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
# running), so stop it again and confirm.  Return 0 when the edge serves again
# (or was never managed/started), 1 when the start failed and the stop is
# confirmed, 2 when the stop is not confirmed and the unit may still serve public
# traffic.
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
# health_gate probes both ports, so it runs only when both units were active.
# Return 0 only when every originally-active object is serving again.
restore_units() {
  local failed=0
  if [ "$BROKER_WAS_ACTIVE" -eq 1 ]; then
    systemctl start um-share-broker.service || failed=1
    brokerctl start-explicit || failed=1
  fi
  if [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
    systemctl start um-share-console.service || failed=1
  fi
  [ "$failed" -eq 0 ] || return 1
  if [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
    wait_port_open 127.0.0.1 18096 "$UM_HEALTH_TIMEOUT_S" || return 1
  fi
  if [ "$BROKER_WAS_ACTIVE" -eq 1 ]; then
    wait_port_open 127.0.0.1 8100 "$UM_HEALTH_TIMEOUT_S" || return 1
  fi
  if [ "$BROKER_WAS_ACTIVE" -eq 1 ] && [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
    health_gate || return 1
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

abort_before_switch() {
  local code="${1:-$EX_TEMPFAIL}" rc=0
  log "aborting update before the version switch; restoring the units and the public edge"
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    exit "$code"
  fi
  die_compensation_failed "$rc"
}

start_switch() {
  systemctl start um-share-broker.service || return 1
  brokerctl start-explicit || return 1
  systemctl start um-share-console.service || return 1
  wait_port_open 127.0.0.1 18096 "$UM_HEALTH_TIMEOUT_S" || return 1
  wait_port_open 127.0.0.1 8100 "$UM_HEALTH_TIMEOUT_S" || return 1
  health_gate || return 1
  return 0
}

# The forward switch replaces the broker host process: the unit's ExecStart is
# loaded at process start, so starting the unit again after the link move is not
# enough while the old process stays alive.  Stop the unit before the current link
# moves and confirm it is inactive; the broker CLI drain above only stopped the
# 8100 child.  A failed stop is a hard precondition, never a version switch.
stop_broker_before_switch() {
  if ! systemctl stop um-share-broker.service; then
    return 1
  fi
  if unit_active um-share-broker.service; then
    return 1
  fi
  return 0
}

preflight_switch_links

if [ -n "$RECOVER_BACKUP" ]; then
  log "explicit recovery mode: no version switch; data is restored after the drain"
fi

# The drain must run a CLI that understands the requested budget.  A normal
# update runs the verified target installed above, not the old "current" release
# whose parser may predate the requested options.  Explicit recovery keeps the
# live release because no version is switched.  Both trees are installed under
# /opt/um-share, so the broker user can execute them even when this script was
# started from an unpacked archive outside the deployment tree.
if [ -n "$RECOVER_BACKUP" ]; then
  DRAIN_RELEASE="$CURRENT_TARGET"
else
  DRAIN_RELEASE="$TARGET"
fi

if [ "$NO_EDGE" -eq 0 ] && [ "$CLOUDFLARED_MANAGED" -eq 1 ] && \
   [ "$CLOUDFLARED_WAS_ACTIVE" -eq 1 ]; then
  log "stopping cloudflared: no new public share requests during the drain"
  systemctl stop cloudflared.service || \
    die "cannot stop cloudflared; update aborted" "$EX_OSERR"
elif [ "$NO_EDGE" -eq 1 ]; then
  log "cloudflared is not managed by this run; the public edge may still serve during the drain"
elif [ "$CLOUDFLARED_MANAGED" -eq 0 ]; then
  log "the public edge is not managed by this deployment; the public edge may still serve during the drain"
else
  log "cloudflared.service is inactive; not stopping it"
fi

log "draining the share gateway (budget ${DRAIN_BUDGET}s)"
# One-time migration note: the drain talks to the broker that is RUNNING, and a
# broker started from a release before s2-20261110g answers every instance
# notification with instance_id=None, which the AF_UNIX seam refuses.  That reports
# notification_failed and the gate refuses the switch.  Upgrading FROM such a release
# therefore needs either a `systemctl restart um-share-broker` first (so the new
# code is loaded) or the manual switch sequence recorded in the handoff.  From
# s2-20261110g on, _drain resolves the instance itself and this does not recur.
if brokerctl_release "$DRAIN_RELEASE" \
    --broker-drain-max "$DRAIN_MAX" \
    --drain-budget "$DRAIN_BUDGET" --poll-interval 5 stop; then
  :
else
  code=$?
  log "drain/stop failed with exit $code; no version switch"
  abort_before_switch "$code"
fi

if [ "$CONSOLE_WAS_ACTIVE" -eq 1 ]; then
  log "stopping the console unit (stops its 8110 child via control-group)"
  if ! systemctl stop um-share-console.service; then
    log "console stop failed; restoring the units and the public edge"
    rc=0
    compensate_switch || rc=$?
    if [ "$rc" -eq 0 ]; then
      die "cannot stop the console unit" "$EX_OSERR"
    fi
    die_compensation_failed "$rc"
  fi
  if ! wait_port_closed 127.0.0.1 8110 120; then
    log "8110 is still listening after the console stop; no version switch"
    rc=0
    compensate_switch || rc=$?
    if [ "$rc" -eq 0 ]; then
      die "8110 is still listening after the console stop" "$EX_OSERR"
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
  if start_switch; then
    rc=0
    restore_edge || rc=$?
    if [ "$rc" -eq 0 ]; then
      log "recovery complete: data restored from $RECOVER_BACKUP; release unchanged"
      exit "$EX_OK"
    fi
    die_edge_failure "$EX_OSERR" "recovery restored the data but cloudflared did not start" "$rc"
  fi
  die "recovery restored the data but the current release failed its health gate; $(edge_stopped_clause); manual intervention required" "$EX_HEALTH"
fi

# The broker unit must stop before the release links move: start_switch below
# starts the unit again so it loads the new release's ExecStart.  A failure here
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

log "switching current: $CURRENT_VERSION -> $VERSION"
if [ -L "$CURRENT_LINK" ]; then
  atomic_symlink "$CURRENT_TARGET" "$PREVIOUS_LINK" || \
    log "WARNING: could not update previous symlink"
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
# Move the operator/systemd entry with "current": the broker unit's
# ExecStartPost/ExecStop and operator commands use $UM_PREFIX/bin, so leaving it
# on the previous release would keep running the old brokerctl.  A failure here
# leaves the two links pointing at different releases with every service stopped, so
# undo the version link and bring the previous release's units and edge back.
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
# The installed units must come from the release "current" now points at: an
# old unit would still name the removed budget arguments or pull the broker up
# through Wants=.  Install after the link move and before daemon-reload; a failure
# restores the previous release's units and links.
if ! install_units "$TARGET"; then
  log "systemd unit installation failed; restoring $CURRENT_TARGET"
  install_units "$CURRENT_TARGET" || \
    log "ERROR: cannot reinstall the previous release's units"
  systemctl daemon-reload || \
    log "ERROR: cannot reload systemd after the failed unit installation"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    die "systemd unit installation failed; previous release restored" "$EX_OSFILE"
  fi
  die_compensation_failed "$rc"
fi
if ! systemctl daemon-reload; then
  log "systemd daemon-reload failed; restoring $CURRENT_TARGET units and links"
  install_units "$CURRENT_TARGET" || \
    log "ERROR: cannot reinstall the previous release's units"
  systemctl daemon-reload || \
    log "ERROR: cannot reload systemd after restoring the previous units"
  rc=0
  compensate_switch || rc=$?
  if [ "$rc" -eq 0 ]; then
    die "cannot reload systemd after installing the units; previous release restored" "$EX_OSERR"
  fi
  die_compensation_failed "$rc"
fi

# Stop what the forward switch started before restoring the previous release.
# start_switch starts both units regardless of their prior state, so both are
# stopped here.  A stop is accepted only when confirmed: the console stop must
# also close 8110, and the broker unit must be stopped and inactive afterwards.
# The broker CLI stop only stops the 8100 host child, not the unit itself, so the
# unit stop always runs whatever the CLI returned.  Return 0 only when both stops
# are confirmed.
stop_switch_units() {
  local failed=0
  if ! systemctl stop um-share-console.service; then
    log "ERROR: the console unit did not stop; its 8110 child may still be running"
    failed=1
  elif ! wait_port_closed 127.0.0.1 8110 120; then
    log "ERROR: 8110 is still listening after the console stop"
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

rollback_switch() {
  log "health gate failed; rolling back to $CURRENT_TARGET"
  if ! stop_switch_units; then
    report_switch_residue
    log "ERROR: a unit started for the new release is still running; the release/operator-bin links were not restored; $(edge_stopped_clause); manual intervention required"
    return 1
  fi
  if ! restore_switch_links; then
    report_switch_residue
    log "cannot restore the release/operator-bin links; $(edge_stopped_clause); manual intervention required"
    return 1
  fi
  if ! restore_units; then
    report_switch_residue
    log "the links are restored but a unit that was active before the switch did not come back; $(edge_stopped_clause); manual intervention required"
    return 1
  fi
  return 0
}

if ! start_switch; then
  if rollback_switch; then
    rc=0
    restore_edge || rc=$?
    if [ "$rc" -eq 0 ]; then
      die "new release failed its health gate; previous release restored" "$EX_HEALTH"
    fi
    die_edge_failure "$EX_OSERR" "new release failed its health gate and the previous release is back, but cloudflared did not start" "$rc"
  fi
  die "new release failed and rollback also failed; $(edge_stopped_clause); manual intervention required" "$EX_ROLLBACK"
fi

rc=0
restore_edge || rc=$?
if [ "$rc" -ne 0 ]; then
  die_edge_failure "$EX_OSERR" "update switched to $VERSION but cloudflared did not start" "$rc"
fi
log "update complete: release=$VERSION (previous=$CURRENT_VERSION)"
exit "$EX_OK"
