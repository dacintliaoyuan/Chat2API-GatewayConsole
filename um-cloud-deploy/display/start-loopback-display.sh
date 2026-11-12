#!/usr/bin/env bash
# Loopback-only Xvfb + x11vnc session for the human UM login/MFA window.
#
# Xvfb never listens on TCP (-nolisten tcp); x11vnc binds 127.0.0.1 only
# (-localhost).  The browser reaches it through the console's same-origin
# /display/ WebSocket proxy; the VNC port is never opened in the cloud security
# group.  The persistent Chromium profile belongs to the 8110 child, not to this
# display process.
#
# The persistent VNC auth file is root-only.  systemd LoadCredential gives this
# service a private read-only copy at $CREDENTIALS_DIRECTORY/vnc.passwd; the source
# path is validated but never read by the service user.
set -euo pipefail

DISPLAY_NUM="${UM_DISPLAY:-:99}"
VNC_PORT="${UM_VNC_PORT:-5901}"
VNC_AUTH_SOURCE="${UM_VNC_AUTH_SOURCE:-/etc/um-share/vnc.passwd}"
VNC_AUTH_SOCKET="${UM_VNC_AUTH_SOCKET:-/run/um-share/vnc-auth.sock}"
PYTHON="${UM_DEPLOY_PYTHON:-python3.14}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
fail() {
  echo "$1" >&2
  exit "$2"
}

if [ -n "${CREDENTIALS_DIRECTORY:-}" ] \
    && [ -s "$CREDENTIALS_DIRECTORY/vnc.passwd" ]; then
  VNC_AUTH_PATH="$CREDENTIALS_DIRECTORY/vnc.passwd"
  # systemd's LoadCredential copy is root:root 0440 with an ACL for the service
  # user, not 0400/0600: only the persistent source below is required to be
  # owner-only.  0440 is still root-group-only; no other user can read it.
  credential_mode="$(stat -c '%a' "$VNC_AUTH_PATH")" || \
    fail "cannot stat vnc credential: $VNC_AUTH_PATH" 74
  case "$credential_mode" in
    400|440|0400|0440) ;;
    *) fail "vnc credential must be root-only (0400/0440)" 77 ;;
  esac
elif [ -n "${UM_VNC_AUTH_PATH:-}" ] && [ -s "$UM_VNC_AUTH_PATH" ]; then
  VNC_AUTH_PATH="$UM_VNC_AUTH_PATH"
else
  VNC_AUTH_PATH="$VNC_AUTH_SOURCE"
  # The persistent source is root-only and is normally hidden by systemd's
  # InaccessiblePaths.  Only this fallback (manual run, no credential copy)
  # may inspect it; the service namespace must never stat it.
  [ -f "$VNC_AUTH_SOURCE" ] || \
    fail "root-only vnc auth source is missing: $VNC_AUTH_SOURCE" 78
  source_owner="$(stat -c '%u' "$VNC_AUTH_SOURCE")" || \
    fail "cannot stat vnc auth source: $VNC_AUTH_SOURCE" 74
  source_mode="$(stat -c '%a' "$VNC_AUTH_SOURCE")" || \
    fail "cannot stat vnc auth source: $VNC_AUTH_SOURCE" 74
  [ "$source_owner" = "0" ] || \
    fail "vnc auth source must be owned by root: $VNC_AUTH_SOURCE" 77
  case "$source_mode" in
    600|400|0600|0400) ;;
    *) fail "vnc auth source must be root-only (0400/0600)" 77 ;;
  esac
fi
AUTH_FILE="${XAUTHORITY:-/run/um-share/display.auth}"

[ -s "$VNC_AUTH_PATH" ] || \
  fail "vnc auth file is missing or empty: $VNC_AUTH_PATH" 78
auth_mode="$(stat -c '%a' "$VNC_AUTH_PATH")" || \
  fail "cannot stat vnc auth file: $VNC_AUTH_PATH" 74
case "$auth_mode" in
  400|440|600|640|0400|0440|0600|0640) ;;
  *) fail "vnc auth file must be service-only (0400/0600/0440)" 77 ;;
esac

mkdir -p "$(dirname "$AUTH_FILE")"
rm -f "$AUTH_FILE"
umask 077
cookie="$(python3.14 -c 'import secrets; print(secrets.token_hex(16))')"
touch "$AUTH_FILE"
xauth -f "$AUTH_FILE" add "$DISPLAY_NUM" . "$cookie"
chmod 0600 "$AUTH_FILE"

Xvfb "$DISPLAY_NUM" -screen 0 1280x900x24 -nolisten tcp -noreset \
  -auth "$AUTH_FILE" &
XVFB_PID=$!
mkdir -p "$(dirname "$VNC_AUTH_SOCKET")"
"$PYTHON" "$SCRIPT_DIR/vnc-auth-helper.py" \
  "$VNC_AUTH_PATH" "$VNC_AUTH_SOCKET" &
AUTH_HELPER_PID=$!
X11VNC_PID=""
cleanup() {
  for pid in "$XVFB_PID" "$AUTH_HELPER_PID" "$X11VNC_PID"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

socket="/tmp/.X11-unix/X${DISPLAY_NUM#:}"
for _ in $(seq 1 50); do
  [ -S "$socket" ] && break
  sleep 0.1
done
[ -S "$socket" ] || fail "Xvfb did not create $socket" 70

for _ in $(seq 1 50); do
  [ -S "$VNC_AUTH_SOCKET" ] && break
  sleep 0.1
done
[ -S "$VNC_AUTH_SOCKET" ] || \
  fail "vnc auth helper did not create $VNC_AUTH_SOCKET" 70

# fbsetbg is what fluxbox runs to apply a style background.  This minimal
# image ships no wallpaper setter, so the real fbsetbg pops a CENTERED
# xmessage dialog that BLOCKS (it waits for the OK click) and covers the
# maximized login window.  No-op shims earlier in PATH make that lookup
# succeed silently, which keeps the UM login page visible.
SHIM_DIR="$(dirname "$AUTH_FILE")/bin"
mkdir -p "$SHIM_DIR"
for shim in fbsetbg xsetbg; do
  printf "%s
" "#!/bin/sh" "exit 0" > "$SHIM_DIR/$shim"
  chmod 0755 "$SHIM_DIR/$shim"
done
export PATH="$SHIM_DIR:$PATH"

if command -v fluxbox >/dev/null 2>&1; then
  # One workspace and no toolbar: the login window is a tool, not a desktop.
  #
  # -no-toolbar is REQUIRED: fluxbox 1.3.7 accepts the
  # ``session.screen0.toolbar.visible: false`` resource but still creates the
  # toolbar, so the documented command-line switch is the only reliable way to
  # drop it.
  #
  # -rc must point at a READ-ONLY copy.  fluxbox REWRITES its -rc file on every
  # start (it treats it as ``~/.fluxbox/init``), which silently replaced this
  # package's file with the stock defaults and lost the one-workspace setting.  A
  # read-only copy makes the rewrite fail and keeps the packaged resources.
  FLUXBOX_RC="$SCRIPT_DIR/fluxbox-init"
  if [ -f "$FLUXBOX_RC" ]; then
    RUNTIME_RC="$(dirname "$AUTH_FILE")/fluxbox-init"
    cp -f "$FLUXBOX_RC" "$RUNTIME_RC" 2>/dev/null && \
      chmod 0444 "$RUNTIME_RC" 2>/dev/null && \
      FLUXBOX_RC="$RUNTIME_RC"
  fi
  DISPLAY="$DISPLAY_NUM" XAUTHORITY="$AUTH_FILE" \
    fluxbox -no-toolbar -rc "$FLUXBOX_RC" >/dev/null 2>&1 &
fi

# -localhost: bind 127.0.0.1 only.  -noipv6: no IPv6 listener.  -norc: ignore
# any ~/.x11vncrc.  -nolookup: no reverse DNS.  The auth file is read by
# x11vnc, never from argv or an environment variable that would appear in the
# process table.  The narrow helper answers VNC challenges from the console; the
# console never reads the password.
x11vnc -display "$DISPLAY_NUM" -auth "$AUTH_FILE" \
  -rfbport "$VNC_PORT" -localhost -noipv6 -norc -nolookup \
  -forever -shared -repeat -noxdamage -rfbauth "$VNC_AUTH_PATH" &
X11VNC_PID=$!
wait "$X11VNC_PID"
