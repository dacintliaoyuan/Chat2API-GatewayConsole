#!/usr/bin/env bash
# Shared helpers for the UM cloud deployment scripts.
#
# Every path below is a DEPLOYMENT fact, not a product default: the payload is
# installed under /opt/um-share, the writable state under /var/lib, and the
# secrets/configuration under /etc.  No script writes into a release directory
# once it is the "current" target.
set -euo pipefail

EX_OK=0
EX_REFUSED=1
EX_UNCONFIRMED=2
EX_USAGE=64
EX_DATAERR=65
EX_NOINPUT=66
EX_UNAVAILABLE=67
EX_HEALTH=68
EX_ROLLBACK=69
EX_SOFTWARE=70
EX_OSERR=71
EX_OSFILE=72
EX_CANTCREAT=73
EX_IOERR=74
EX_TEMPFAIL=75
EX_NOPERM=77

UM_PREFIX="${UM_PREFIX:-/opt/um-share}"
UM_ETC="${UM_ETC:-/etc/um-share}"
UM_VAR="${UM_VAR:-/var/lib/um-share}"
UM_CONSOLE_VAR="${UM_CONSOLE_VAR:-/var/lib/um-share-console}"
UM_RUN="${UM_RUN:-/run/um-share}"
UM_LOG="${UM_LOG:-/var/log/um-share}"
UM_CONSOLE_LOG="${UM_CONSOLE_LOG:-/var/log/um-share-console}"
# The installed systemd units live here.  Tests point this at an isolated
# directory; production uses the real /etc/systemd/system.
UM_SYSTEMD_DIR="${UM_SYSTEMD_DIR:-/etc/systemd/system}"
UM_BROKER_USER="${UM_BROKER_USER:-um-share}"
UM_BROKER_GROUP="${UM_BROKER_GROUP:-um-share}"
# The broker host, the console, the 8100 child and the loopback display
# share ONE restricted system user.  Privilege separation is by cgroup, unit
# hardening and the InaccessiblePaths/ReadOnlyPaths fences, not by a second uid;
# the 0600 control/instance keys are read by this same uid.
UM_CONSOLE_USER="${UM_CONSOLE_USER:-$UM_BROKER_USER}"
UM_CONSOLE_GROUP="${UM_CONSOLE_GROUP:-$UM_BROKER_GROUP}"
UM_VENV="${UM_VENV:-$UM_PREFIX/venv}"
UM_RELEASES="${UM_RELEASES:-$UM_PREFIX/releases}"
UM_CURRENT="${UM_CURRENT:-$UM_PREFIX/current}"
UM_PW_BROWSERS="${UM_PW_BROWSERS:-$UM_PREFIX/pw-browsers}"
UM_DEPLOY_LOCK="${UM_DEPLOY_LOCK:-$UM_RUN/deploy.lock}"
UM_BROKER_SOCKET="${UM_BROKER_SOCKET:-$UM_RUN/broker.sock}"
UM_BROKER_KEY="${UM_BROKER_KEY:-$UM_ETC/broker-control.key}"
UM_INSTANCE_KEY="${UM_INSTANCE_KEY:-$UM_ETC/instance.key}"
UM_ATTRIBUTION_KEY="${UM_ATTRIBUTION_KEY:-$UM_ETC/share-attribution.key}"
UM_DRAIN_BUDGET_S="${UM_DRAIN_BUDGET_S:-900}"
UM_BROKER_DRAIN_MAX_S="${UM_BROKER_DRAIN_MAX_S:-90}"
UM_HEALTH_TIMEOUT_S="${UM_HEALTH_TIMEOUT_S:-60}"
# Expand-contract (2026-11-14): the cloud console defaults to 8096.  The
# deployed console.env is authoritative (an existing deployment may still carry
# the legacy 18096 explicitly for one release cycle), so the ops scripts never
# hardcode a port that could disagree with the running service.
CONSOLE_PORT_DEFAULT=8096

log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

die() {
  local message="$1"
  local code="${2:-$EX_SOFTWARE}"
  log "ERROR: $message"
  exit "$code"
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "must run as root" "$EX_NOPERM"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || \
    die "required command not found: $1" "$EX_OSERR"
}

require_ubuntu_2604() {
  [ -r /etc/os-release ] || die "/etc/os-release missing" "$EX_OSERR"
  # Read the two fields in a subshell: sourcing os-release in this shell would
  # overwrite the caller's VERSION (the release identifier) with the Ubuntu
  # VERSION string, and install/update then compare the manifest against it.
  local os_id os_version_id
  os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
  os_version_id="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
  [ "$os_id" = "ubuntu" ] || \
    die "target OS is not Ubuntu (ID=${os_id:-unknown})" "$EX_OSERR"
  [ "$os_version_id" = "26.04" ] || \
    die "target OS must be Ubuntu 26.04 (VERSION_ID=${os_version_id:-unknown})" \
        "$EX_OSERR"
}

python_bin() {
  if [ -x "$UM_VENV/bin/python" ]; then
    printf '%s\n' "$UM_VENV/bin/python"
    return 0
  fi
  if command -v python3.14 >/dev/null 2>&1; then
    command -v python3.14
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  return 1
}

ensure_dir() {
  local path="$1" mode="$2" owner="$3" group="$4"
  install -d -m "$mode" -o "$owner" -g "$group" "$path"
}

ensure_user() {
  local user="$1" group="$2" home="$3"
  if ! getent group "$group" >/dev/null 2>&1; then
    groupadd --system "$group"
  fi
  if ! id -u "$user" >/dev/null 2>&1; then
    useradd --system --gid "$group" --home-dir "$home" \
      --shell /usr/sbin/nologin --no-create-home "$user"
  fi
}

sha256_file() {
  sha256sum "$1" | awk '{print $1}'
}

atomic_symlink() {
  local target="$1" link="$2"
  local tmp="${link}.tmp.$$"
  ln -sfn "$target" "$tmp"
  mv -Tf "$tmp" "$link"
}

# $UM_PREFIX/bin is the operator/systemd entry directory for the CURRENT
# release (install.sh points it at the installed release's bin).  update.sh and
# rollback.sh switch "current", so they must move this symlink in the same step:
# the broker unit's ExecStartPost/ExecStop and operator commands use this path,
# and a stale target would keep running the previous release's brokerctl (e.g. its
# old drain-budget default instead of the new bounded one).
repoint_bin() {
  local target="$1"
  if [ -L "$UM_PREFIX/bin" ]; then
    atomic_symlink "$target/um-cloud-deploy/bin" "$UM_PREFIX/bin"
  elif [ ! -e "$UM_PREFIX/bin" ]; then
    atomic_symlink "$target/um-cloud-deploy/bin" "$UM_PREFIX/bin"
  else
    die "$UM_PREFIX/bin exists and is not a symlink" "$EX_CANTCREAT"
  fi
}

verify_release_manifest() {
  local root="$1" manifest="$2"
  local py
  py="$(python_bin)" || die "python interpreter not found" "$EX_OSERR"
  "$py" - "$root" "$manifest" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
manifest = Path(sys.argv[2]).resolve()
try:
    data = json.loads(manifest.read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    print(f"manifest unreadable: {exc}", file=sys.stderr)
    raise SystemExit(65)

def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

bad = []
for key in ("files", "deployment_files"):
    for entry in data.get(key) or []:
        rel = entry.get("path")
        want = entry.get("sha256")
        if not rel or not want:
            bad.append(f"{key}: malformed entry {entry!r}")
            continue
        target = root / rel
        if not target.is_file():
            bad.append(f"{key}: missing {rel}")
            continue
        got = digest(target)
        if got != want:
            bad.append(f"{key}: sha256 mismatch {rel}")
if bad:
    for line in bad:
        print(line, file=sys.stderr)
    raise SystemExit(65)
print(f"manifest verified: {manifest}")
PY
}

count_established() {
  local port="$1"
  local py
  py="$(python_bin)" || return 1
  "$py" - "$port" <<'PY'
import sys
from pathlib import Path

port = int(sys.argv[1])
hex_port = f"{port:04X}"
count = 0
for name in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = Path(name).read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        continue
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "01":  # 01 = ESTABLISHED
            continue
        local = fields[1]
        if ":" not in local:
            continue
        if local.rsplit(":", 1)[1].upper() == hex_port:
            count += 1
print(count)
PY
}

wait_port_open() {
  local host="$1" port="$2" timeout_s="${3:-60}"
  local deadline=$(( $(date +%s) + timeout_s ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      exec 3>&-
      exec 3<&-
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_port_closed() {
  local host="$1" port="$2" timeout_s="${3:-60}"
  local deadline=$(( $(date +%s) + timeout_s ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      return 0
    fi
    exec 3>&-
    exec 3<&-
    sleep 1
  done
  return 1
}

wait_established_zero() {
  local port="$1" budget_s="${2:-810}" interval_s="${3:-5}"
  local deadline=$(( $(date +%s) + budget_s ))
  local count
  while :; do
    count="$(count_established "$port" 2>/dev/null || printf 'unknown')"
    if [ "$count" = "0" ]; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "drain timeout: port $port still has ${count} established connection(s)"
      return 1
    fi
    log "drain: port $port established=${count}; waiting"
    sleep "$interval_s"
  done
}

wait_established_zero() {
  local port="$1" budget_s="${2:-810}" interval_s="${3:-5}"
  local deadline=$(( $(date +%s) + budget_s ))
  local count
  while :; do
    count="$(count_established "$port" 2>/dev/null || printf 'unknown')"
    if [ "$count" = "0" ]; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      log "drain timeout: port $port still has ${count} established connection(s)"
      return 1
    fi
    log "drain: port $port established=${count}; waiting"
    sleep "$interval_s"
  done
}

console_port() {
  local file="${UM_ETC:-/etc/um-share}/console.env"
  local value="" found=0
  if [ -r "$file" ]; then
    value="$(sed -n 's/^[[:space:]]*USAGESRV_PORT[[:space:]]*=[[:space:]]*//p' "$file" \
      | tail -n 1 | tr -d '\r')"
    if [ -n "$value" ] \
        || grep -Eq '^[[:space:]]*USAGESRV_PORT[[:space:]]*=' "$file"; then
      found=1
    fi
  fi
  if [ "$found" -eq 0 ]; then
    printf '%s\n' "$CONSOLE_PORT_DEFAULT"
    return 0
  fi
  case "$value" in
    ''|*[!0-9]*)
      die "$file: USAGESRV_PORT must be 1..65535 (actual=$value)" "$EX_DATAERR"
      ;;
  esac
  if [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
    die "$file: USAGESRV_PORT must be 1..65535 (actual=$value)" "$EX_DATAERR"
  fi
  printf '%s\n' "$value"
}

health_gate() {
  local console_code gateway_code port
  port="$(console_port)" || return 1
  console_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://127.0.0.1:$port/" 2>/dev/null || true)"
  case "$console_code" in
    2??|3??|4??) ;;
    *)
      log "console HTTP probe failed: $console_code (port $port)"
      return 1
      ;;
  esac
  gateway_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
    http://127.0.0.1:8100/v1/models 2>/dev/null || true)"
  case "$gateway_code" in
    200|401|403) ;;
    *)
      log "gateway HTTP probe failed: $gateway_code"
      return 1
      ;;
  esac
  return 0
}

_brokerctl_run() {
  local cli="$1"
  shift
  local py
  py="$(python_bin)" || die "python interpreter not found" "$EX_OSERR"
  if [ "$(id -u)" -eq 0 ] && [ "${UM_BROKER_USER:-}" != "root" ]; then
    require_cmd runuser
    runuser -u "$UM_BROKER_USER" -- "$py" "$cli" \
      --socket "$UM_BROKER_SOCKET" --key-file "$UM_BROKER_KEY" "$@"
  else
    "$py" "$cli" \
      --socket "$UM_BROKER_SOCKET" --key-file "$UM_BROKER_KEY" "$@"
  fi
}

# The operator/systemd entry: install.sh (and a version switch) points it at
# the CURRENT release's bin.
brokerctl() {
  _brokerctl_run "$UM_PREFIX/bin/um-share-brokerctl.py" "$@"
}

# An explicit release CLI.  update.sh/rollback.sh use this for the drain that
# runs BEFORE a switch: the old current release's parser may predate a new
# drain option, so the caller names the implementation it verified.
brokerctl_release() {
  local release="$1"
  shift
  _brokerctl_run "$release/um-cloud-deploy/bin/um-share-brokerctl.py" "$@"
}

# The r16 CLI reserves its default 90-second broker drain deadline even when the
# total budget is smaller, and rejects any budget <= 90.  update.sh/rollback.sh may
# have to run that older CLI (the live current release), so name the reservation
# explicitly for every accepted budget: min(90, budget - 1).  The new CLI accepts
# the same value and still refuses an explicit reservation >= the budget.  A
# 1-second budget leaves no positive reservation; the old CLI cannot run it, so the
# scripts refuse that value with a clear message instead of its exit 64.
broker_drain_max_for_budget() {
  local budget="$1"
  case "$budget" in
    ''|*[!0-9]*) die "drain budget must be an integer" "$EX_USAGE" ;;
  esac
  [ "$budget" -ge 2 ] || \
    die "--drain-budget must be at least 2 seconds: the broker drain reservation must be smaller than the total budget" \
      "$EX_USAGE"
  if [ "$budget" -gt 90 ]; then
    printf '90\n'
  else
    printf '%s\n' "$((budget - 1))"
  fi
}

ensure_env_line() {
  local file="$1" key="$2" value="$3"
  [ -f "$file" ] || die "environment file missing: $file" "$EX_NOINPUT"
  [ -r "$file" ] && [ -w "$file" ] || \
    die "environment file is not readable and writable: $file" "$EX_NOINPUT"
  if grep -q "^${key}=." "$file" 2>/dev/null; then
    return 0
  fi
  if grep -q "^${key}=" "$file" 2>/dev/null; then
    die "$file has an empty ${key}= line; refusing to invent a share-key path" \
      "$EX_DATAERR"
  fi
  printf '%s=%s\n' "$key" "$value" >> "$file"
}

share_secret_configured_path() {
  local file="$UM_ETC/console.env" value=""
  if [ -f "$file" ]; then
    [ -r "$file" ] || \
      die "console environment file unreadable: $file" "$EX_NOINPUT"
    value="$(sed -n 's/^CHAT2API_SHARE_SECRET_KEY_FILE=//p' "$file" \
      | tail -n 1)"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
  fi
  printf '%s\n' "$value"
}

# The console's SQLite store path.  deployment_paths() uses the same default
# (<console_var>/share-store.db) when console.env does not name one; the install
# key guard must inspect the store the console will actually open.
share_store_configured_path() {
  local file="$UM_ETC/console.env" value=""
  if [ -f "$file" ]; then
    value="$(sed -n 's/^SHARE_STORE_FILE=//p' "$file" | tail -n 1)"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
  fi
  if [ -z "$value" ]; then
    value="$UM_CONSOLE_VAR/share-store.db"
  fi
  printf '%s\n' "$value"
}

# Return 0 when the existing store has at least one AES-GCM row, 1 when it has
# none (or no store yet), and 2 when the store cannot be inspected.  A missing
# table is an empty/new store, not a corrupt one; any other SQLite error is
# fail-closed.
share_store_has_encrypted_rows() {
  local store py
  store="$(share_store_configured_path)"
  [ -e "$store" ] || return 1
  py="$(python_bin)" || die "python interpreter not found" "$EX_OSERR"
  "$py" - "$store" <<'PY'
import sqlite3
import sys
from urllib.parse import quote

try:
    conn = sqlite3.connect(
        "file:" + quote(sys.argv[1], safe="/") + "?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM keys WHERE secret_blob IS NOT NULL"
            " AND secret_algo = 'aesgcm-file-key-v1' LIMIT 1").fetchone()
    finally:
        conn.close()
except sqlite3.OperationalError as exc:
    if "no such table" in str(exc).lower():
        raise SystemExit(1)
    print(f"share store is unreadable: {exc}", file=sys.stderr)
    raise SystemExit(2)
except sqlite3.Error as exc:
    print(f"share store is unreadable: {exc}", file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(0 if row else 1)
PY
}

# A new key may only be created when no existing row depends on the old one.
# The store is the only evidence that a key is already in use; an unreadable
# store is a refusal, never permission to overwrite the deployment's encryption.
refuse_new_share_key_for_existing_rows() {
  local rc=0
  share_store_has_encrypted_rows || rc=$?
  if [ "$rc" -eq 2 ]; then
    die "cannot inspect the existing share store; refusing to create a new share key" \
      "$EX_DATAERR"
  fi
  if [ "$rc" -eq 0 ]; then
    die "the share store already has AES-GCM rows but the configured share secret key is missing; refusing to create a different key (restore the existing key or use explicit recovery)" \
      "$EX_NOINPUT"
  fi
}

# Create one new 32-byte share-key file at $1 (0600 console uid/gid).  The value
# is never printed.
create_share_secret_file() {
  local target="$1"
  refuse_new_share_key_for_existing_rows
  install -d -m 0750 -o "$UM_CONSOLE_USER" -g "$UM_CONSOLE_GROUP" \
    "$(dirname "$target")"
  local tmp="${target}.tmp.$$"
  ( umask 077; python3.14 -c \
      'import secrets; print(secrets.token_hex(32))' > "$tmp" )
  chmod 0600 "$tmp"
  chown "$UM_CONSOLE_USER:$UM_CONSOLE_GROUP" "$tmp"
  mv "$tmp" "$target"
  log "created $target (0600 $UM_CONSOLE_USER:$UM_CONSOLE_GROUP)"
}

# The POSIX AES-GCM share-key file is persistent deployment state, not release
# material.  The running console uses the console.env path when that line is set;
# with no line its actual default is <current>/data/share-secret.key
# (share_secret_protection.default_key_file).  An update must never create a
# second key at a new path: that would silently make existing AES-GCM rows
# undecryptable.  This keeps the effective key/path, migrates the
# release-relative default to the persistent console state only when the target is
# absent, and refuses to replace a missing configured key.
#
# The first install passes "install": a fresh deployment has no effective key yet,
# so a missing configured path may be created there.  An existing
# release-relative key is still migrated instead of being replaced.  update.sh and
# rollback.sh keep the strict default; explicit recovery passes "recover" because
# the sealed backup carries the key it must restore.
ensure_share_secret_wiring() {
  local mode="${1:-strict}"
  local configured target legacy
  configured="$(share_secret_configured_path)"
  target="$UM_CONSOLE_VAR/share-secret.key"
  legacy="${CURRENT_TARGET:-}/data/share-secret.key"
  if [ -n "$configured" ]; then
    if [ -e "$configured" ]; then
      log "keeping configured share secret key: $configured"
      return 0
    fi
    if [ "$mode" = "recover" ]; then
      # The sealed backup restores this path.  Do not migrate or invent a key:
      # a live key may be missing precisely because recovery must recreate it.
      log "explicit recovery: the sealed backup restores $configured"
      return 0
    fi
    if [ -n "${CURRENT_TARGET:-}" ] && [ -e "$legacy" ]; then
      # The console's effective key was the release-relative default.  Copy it to
      # the configured path instead of inventing a different key.
      install -d -m 0750 -o "$UM_CONSOLE_USER" -g "$UM_CONSOLE_GROUP" \
        "$(dirname "$configured")"
      install -m 0600 -o "$UM_CONSOLE_USER" -g "$UM_CONSOLE_GROUP" \
        "$legacy" "$configured"
      log "migrated the effective release-relative share secret key to $configured"
      return 0
    fi
    if [ "$mode" = "install" ]; then
      create_share_secret_file "$configured"
      return 0
    fi
    die "configured share secret key is missing: $configured; refusing to create a different key" \
      "$EX_NOINPUT"
  fi
  if [ "$mode" = "recover" ]; then
    # No configured path: name the persistent state path so the restore writes
    # there instead of the release-relative product default.
    ensure_env_line "$UM_ETC/console.env" CHAT2API_SHARE_SECRET_KEY_FILE \
      "$target"
    log "explicit recovery: the sealed backup restores $target"
    return 0
  fi
  if [ -n "${CURRENT_TARGET:-}" ] && [ -e "$legacy" ]; then
    if [ -e "$target" ] && ! cmp -s "$legacy" "$target"; then
      # Both files are valid material.  The running console used the
      # release-relative default, so wire that effective path instead of
      # overwriting either key.
      log "keeping the effective release-relative share secret key: $legacy"
      ensure_env_line "$UM_ETC/console.env" CHAT2API_SHARE_SECRET_KEY_FILE \
        "$legacy"
      return 0
    fi
    if [ ! -e "$target" ]; then
      install -d -m 0750 -o "$UM_CONSOLE_USER" -g "$UM_CONSOLE_GROUP" \
        "$UM_CONSOLE_VAR"
      install -m 0600 -o "$UM_CONSOLE_USER" -g "$UM_CONSOLE_GROUP" \
        "$legacy" "$target"
      log "migrated the effective release-relative share secret key to $target"
    else
      log "keeping existing $target"
    fi
  elif [ -e "$target" ]; then
    log "keeping existing $target"
  else
    create_share_secret_file "$target"
  fi
  ensure_env_line "$UM_ETC/console.env" CHAT2API_SHARE_SECRET_KEY_FILE \
    "$target"
}

unit_active() {
  systemctl is-active --quiet "$1"
}

# Install the release-owned unit files into $UM_SYSTEMD_DIR.  install.sh and every
# version switch must use the SAME source release as the release "current" points
# at; otherwise an old unit keeps naming the removed budget arguments and pulls
# the broker back up through Wants=.  Callers install the units after moving the
# links and before `systemctl daemon-reload`.  Return 1 on the first failure so
# the caller can compensate instead of running a mixed release.
install_units() {
  local target="$1"
  shift
  local units=("$@")
  if [ "${#units[@]}" -eq 0 ]; then
    units=(um-share-broker.service um-share-console.service
           um-share-display.service)
  fi
  local unit source
  for unit in "${units[@]}"; do
    source="$target/um-cloud-deploy/systemd/$unit"
    if [ ! -f "$source" ]; then
      log "ERROR: systemd unit missing from release: $source"
      return 1
    fi
    if ! install -m 0644 -o root -g root "$source" "$UM_SYSTEMD_DIR/$unit"; then
      log "ERROR: cannot install systemd unit: $unit"
      return 1
    fi
  done
  # A stale tmpfiles rule from an older deployment named the retired
  # ``umshare`` account, so ``systemd-tmpfiles --create`` re-owned
  # /run/um-share to a user that runs no service and the display unit lost
  # write access to its own runtime directory (Xvfb then died with
  # "(EE) Failed to open authorization file").  Keep the rule pointed at the
  # CURRENT service account on every install/switch.
  local tmpfiles=/etc/tmpfiles.d/um-share-prep.conf
  local wanted="d $UM_RUN 0750 $UM_BROKER_USER $UM_BROKER_GROUP -"
  if [ "$(cat "$tmpfiles" 2>/dev/null)" != "$wanted" ]; then
    printf '%s\n' "$wanted" > "$tmpfiles" || \
      log "WARNING: cannot write $tmpfiles"
    systemd-tmpfiles --create "$tmpfiles" 2>/dev/null || \
      log "WARNING: cannot apply $tmpfiles"
  fi
  return 0
}

stop_unit_if_active() {
  local unit="$1"
  if unit_active "$unit"; then
    systemctl stop "$unit"
  fi
}

start_unit_if_inactive() {
  local unit="$1"
  if ! unit_active "$unit"; then
    systemctl start "$unit"
  fi
}
