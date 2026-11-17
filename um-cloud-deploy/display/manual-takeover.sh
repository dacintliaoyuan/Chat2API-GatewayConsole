#!/usr/bin/env bash
# Start or stop the visible login display on demand (ticket #14).
#
# install.sh installs um-share-display.service but never enables it.  The normal
# login path is headless and does not need this unit.  When the automatic flow
# returns manual_takeover_required:<reason>, the operator explicitly runs
# `manual-takeover.sh start`: it starts the unit for the CURRENT boot only
# (systemctl start, never enable) and turns the console's /display/ surface on.
# `stop` reverses both without disabling the unit; the next takeover can start it
# again.  Nothing here runs automatically, and boot does not start the display.
#
# Exit codes: 0 success, 64 usage, 69 start/stop not confirmed, 74 I/O,
# 77 permission/root-only auth, 78 missing input (auth file or console.env).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$SCRIPT_DIR/../lib/common.sh"

UNIT="${UM_DISPLAY_UNIT:-um-share-display.service}"
if [ -n "${UM_CONSOLE_UNIT:-}" ]; then
  CONSOLE_UNIT="$UM_CONSOLE_UNIT"
else
  CONSOLE_UNIT="$(release_console_unit \
    "$(release_runtime "${UM_CURRENT:-/opt/um-share/current}")")"
fi
VNC_PASSWD="${UM_VNC_PASSWD_FILE:-/etc/um-share/vnc.passwd}"
CONSOLE_ENV="${UM_CONSOLE_ENV_FILE:-/etc/um-share/console.env}"
DISPLAY_STATE="${UM_LOGIN_DISPLAY_FILE:-/run/um-share/login-display.env}"

fail() {
  echo "$1" >&2
  exit "$2"
}

usage() {
  echo "Usage: $0 start|stop|status" >&2
  exit 64
}

require_root() {
  [ "$(id -u)" -eq 0 ] || \
    fail "manual takeover must run as root" 77
}

# Set CONSOLE_DISPLAY_ENABLED in console.env.  Returns 0 when the file
# changed (the console must be restarted to read it), 1 when it already matches.
set_console_display() {
  local value="$1"
  [ -f "$CONSOLE_ENV" ] || \
    fail "console environment file is missing: $CONSOLE_ENV" 78
  if grep -qx "CONSOLE_DISPLAY_ENABLED=$value" "$CONSOLE_ENV"; then
    return 1
  fi
  local tmp="${CONSOLE_ENV}.manual-takeover.$$"
  cp -p "$CONSOLE_ENV" "$tmp" || \
    fail "cannot copy console environment file: $CONSOLE_ENV" 74
  awk -v value="$value" '
    BEGIN { done = 0 }
    /^CONSOLE_DISPLAY_ENABLED=/ {
      if (!done) {
        print "CONSOLE_DISPLAY_ENABLED=" value
        done = 1
      }
      next
    }
    { print }
    END {
      if (!done) {
        print "CONSOLE_DISPLAY_ENABLED=" value
      }
    }
  ' "$CONSOLE_ENV" > "$tmp" || {
    rm -f "$tmp"
    fail "cannot update console environment file: $CONSOLE_ENV" 74
  }
  mv -f "$tmp" "$CONSOLE_ENV" || {
    rm -f "$tmp"
    fail "cannot replace console environment file: $CONSOLE_ENV" 74
  }
  return 0
}

# The display script publishes DISPLAY/XAUTHORITY only after Xvfb and the VNC
# auth helper are ready.  Wait for it before telling the operator to open a new
# login round: without this state the round would stay headless.
wait_for_display_state() {
  local _
  for _ in $(seq 1 100); do
    [ -f "$DISPLAY_STATE" ] && return 0
    sleep 0.1
  done
  return 1
}

start_display() {
  require_root
  [ -f "$VNC_PASSWD" ] || \
    fail "root-only vnc auth file is missing: $VNC_PASSWD (create it with x11vnc -storepasswd)" 78
  local owner mode
  owner="$(stat -c '%u' "$VNC_PASSWD")" || \
    fail "cannot stat vnc auth file: $VNC_PASSWD" 74
  mode="$(stat -c '%a' "$VNC_PASSWD")" || \
    fail "cannot stat vnc auth file: $VNC_PASSWD" 74
  [ "$owner" = "0" ] || \
    fail "vnc auth file must be owned by root: $VNC_PASSWD" 77
  case "$mode" in
    400|600|0400|0600) ;;
    *) fail "vnc auth file must be root-only (0400/0600): $VNC_PASSWD" 77 ;;
  esac
  # An inactive unit cannot own a live display state; drop a stale file so the
  # wait below observes THIS start rather than a previous run.
  if ! systemctl is-active --quiet "$UNIT"; then
    rm -f "$DISPLAY_STATE"
  fi
  systemctl start "$UNIT" || \
    fail "cannot start $UNIT" 69
  systemctl is-active --quiet "$UNIT" || \
    fail "$UNIT did not become active" 69
  if ! wait_for_display_state; then
    # A display left active by an older release has no runtime state.  Restart
    # it once so the new script publishes the state before the operator opens
    # a login round.
    systemctl restart "$UNIT" || \
      fail "cannot restart $UNIT to publish the login display state" 69
    wait_for_display_state || \
      fail "$UNIT did not publish the login display state" 69
  fi
  if set_console_display 1; then
    systemctl restart "$CONSOLE_UNIT" || \
      fail "display started but $CONSOLE_UNIT restart failed" 69
    systemctl is-active --quiet "$CONSOLE_UNIT" || \
      fail "display started but $CONSOLE_UNIT is not active" 69
  fi
  echo "display is active for this boot: $UNIT"
  echo "open the console /display/ page and start a new login round;"
  echo "that round opens its visible browser on this display."
  echo "run '$0 stop' when the manual step is done."
}

stop_display() {
  require_root
  systemctl stop "$UNIT" || \
    fail "cannot stop $UNIT" 69
  # The display cleanup trap removes this too; the explicit removal covers an
  # old display process that did not publish the new runtime state.
  rm -f "$DISPLAY_STATE"
  if set_console_display 0; then
    systemctl restart "$CONSOLE_UNIT" || \
      fail "display stopped but $CONSOLE_UNIT restart failed" 69
  fi
  echo "display is stopped; new login rounds are headless again."
  echo "the unit is still installed and NOT disabled."
}

status_display() {
  local active
  active="$(systemctl is-active "$UNIT" 2>/dev/null || true)"
  echo "$UNIT: ${active:-unknown}"
  if [ -f "$CONSOLE_ENV" ] && \
      grep -qx 'CONSOLE_DISPLAY_ENABLED=1' "$CONSOLE_ENV"; then
    echo "console display surface: enabled"
  else
    echo "console display surface: disabled"
  fi
  if [ -f "$DISPLAY_STATE" ]; then
    echo "login display state: published (new rounds open visibly)"
  else
    echo "login display state: absent (new rounds stay headless)"
  fi
}

case "${1:-}" in
  start) start_display ;;
  stop) stop_display ;;
  status) status_display ;;
  *) usage ;;
esac
