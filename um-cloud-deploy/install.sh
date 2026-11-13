#!/usr/bin/env bash
# Idempotent first install for the UM cloud share deployment.
#
# This script installs a FROZEN release payload; it never builds product code
# and never writes into an existing release directory.  Existing configuration,
# secrets and data are reused, never overwritten.  Re-running with the same
# release version is a no-op apart from re-asserting units and directories.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

RELEASE_ARCHIVE=""
RELEASE_DIR=""
VERSION=""
SHA256=""
SKIP_APT=0
APT_LOCKED=0
NO_START=0
WITH_DISPLAY=0
WITH_CLOUDFLARED=0
CLOUDFLARED_TOKEN_FILE=""
WHEELHOUSE=""
PRICE_TABLE=""
RECOVER_BACKUP=""
RECOVER_SECURITY_STATE=""

usage() {
  cat <<'EOF'
Usage: install.sh --release <um-share-VERSION.tar.gz> [options]
       install.sh --release-dir <unpacked-payload> [options]

Options:
  --release PATH        release tar.gz produced by package-release.sh
  --release-dir PATH    already-unpacked payload (developer/testing only)
  --version VERSION     expected release version (checked against the manifest)
  --sha256 HEX          expected archive SHA-256 (recommended)
  --skip-apt            do not install Ubuntu packages
  --apt-locked          install the exact versions in dependencies.lock.json
  --no-start            install files but do not start/enable services
  --with-display        also enable the loopback Xvfb/x11vnc unit
  --with-cloudflared    also install/start the Cloudflare Tunnel edge unit
                        (default off; public edge is not managed otherwise)
  --cloudflared-token-file PATH
                        tunnel token for --with-cloudflared (required unless
                        /etc/um-share/cloudflared.env exists)
  --wheelhouse PATH     local wheelhouse for pip --no-index
  --price-table PATH    metering price table (required unless /etc/um-share/prices.json exists)
  --recover-backup PATH explicit recovery only: sealed backup to restore
  --recover-security-state PATH
                        independent latest security state (required with
                        --recover-backup)
  -h, --help            show this help

Exit codes:
  0  success
  64 usage error
  65 manifest/data error
  66 input missing
  70 software error
  71 OS/command missing
  72 OS file error
  73 cannot create/version conflict
  74 I/O error
  77 permission error
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --release) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RELEASE_ARCHIVE="$2"; shift 2 ;;
    --release-dir) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RELEASE_DIR="$2"; shift 2 ;;
    --version) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; VERSION="$2"; shift 2 ;;
    --sha256) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; SHA256="$2"; shift 2 ;;
    --wheelhouse) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; WHEELHOUSE="$2"; shift 2 ;;
    --skip-apt) SKIP_APT=1; shift ;;
    --apt-locked) APT_LOCKED=1; shift ;;
    --no-start) NO_START=1; shift ;;
    --with-display) WITH_DISPLAY=1; shift ;;
    --with-cloudflared) WITH_CLOUDFLARED=1; shift ;;
    --cloudflared-token-file) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; CLOUDFLARED_TOKEN_FILE="$2"; shift 2 ;;
    --price-table) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; PRICE_TABLE="$2"; shift 2 ;;
    --recover-backup) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RECOVER_BACKUP="$2"; shift 2 ;;
    --recover-security-state) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; RECOVER_SECURITY_STATE="$2"; shift 2 ;;
    -h|--help) usage; exit "$EX_OK" ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit "$EX_USAGE" ;;
  esac
done

[ -n "$RELEASE_ARCHIVE" ] || [ -n "$RELEASE_DIR" ] || {
  usage >&2
  exit "$EX_USAGE"
}
[ -z "$RELEASE_ARCHIVE" ] || [ -z "$RELEASE_DIR" ] || {
  echo "use either --release or --release-dir, not both" >&2
  exit "$EX_USAGE"
}

if [ -n "$CLOUDFLARED_TOKEN_FILE" ] && [ ! -f "$CLOUDFLARED_TOKEN_FILE" ]; then
  die "cloudflared token file not found: $CLOUDFLARED_TOKEN_FILE" "$EX_NOINPUT"
fi
if [ -n "$PRICE_TABLE" ] && [ ! -f "$PRICE_TABLE" ]; then
  die "price table not found: $PRICE_TABLE" "$EX_NOINPUT"
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
require_cmd sha256sum
require_cmd groupadd
require_cmd useradd
require_cmd getent

if [ -n "$RELEASE_ARCHIVE" ]; then
  [ -f "$RELEASE_ARCHIVE" ] || die "release archive not found: $RELEASE_ARCHIVE" "$EX_NOINPUT"
  if [ -n "$SHA256" ]; then
    actual="$(sha256_file "$RELEASE_ARCHIVE")"
    [ "$actual" = "$SHA256" ] || die "release archive SHA-256 mismatch" "$EX_DATAERR"
  elif [ -f "$RELEASE_ARCHIVE.sha256" ]; then
    expected="$(awk '{print $1}' "$RELEASE_ARCHIVE.sha256")"
    actual="$(sha256_file "$RELEASE_ARCHIVE")"
    [ "$actual" = "$expected" ] || die "release archive SHA-256 mismatch" "$EX_DATAERR"
  else
    log "WARNING: no --sha256 and no adjacent .sha256 file; archive integrity is not verified"
  fi
fi

if [ "$SKIP_APT" -eq 0 ]; then
  require_cmd apt-get
  log "installing Ubuntu 26.04 packages (resolute)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # The exact versions are in dependencies.lock.json.  This install keeps the
  # package names and lets apt resolve the target's current security pocket;
  # --apt-locked installs the frozen versions instead.
  APT_PACKAGES=(
    python3.14 python3.14-venv python3-pip ca-certificates curl
    xvfb x11-utils xauth x11vnc fluxbox dbus-x11 unzip xz-utils novnc
    fonts-liberation fonts-noto-cjk fonts-noto-color-emoji
    libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64
    libatspi2.0-0t64 libx11-6 libxcb1 libxext6 libxrender1 libxi6
    libxtst6 libglib2.0-0t64 libdbus-1-3 libexpat1 libfontconfig1
    libfreetype6 libuuid1 libxau6 libxdmcp6 libbsd0 libselinux1
    libpcre2-8-0 libgcrypt20 libgpg-error0 liblzma5 libzstd1
    libsystemd0 libcap2 libmount1 libblkid1 libacl1 libattr1 libc6
    zlib1g libstdc++6 gcc-14-base libgcc-s1 libxss1 libxt6t64
    libxmu6 libxpm4 libxcb-dri3-0 libxcb-render0 libxcb-shm0
    libxcb-xfixes0 libxcb-glx0 libxcb-shape0 libxcb-sync1
    libxcb-randr0 libxcb-xkb1 libxcb-present0 libxcb-dri2-0
    libxkbcommon-x11-0 libegl1 libopengl0 libgles2 libgl1 libglx0
    libglvnd0 libvulkan1 libdrm-common libwayland-client0
    libwayland-server0 libx11-xcb1 libsm6 libice6 libfontenc1
    libxfont2 libpixman-1-0 libharfbuzz0b libgraphite2-3 libthai0
    libdatrie1 libfribidi0 libpangocairo-1.0-0 libpangoft2-1.0-0
    libcairo-gobject2 libgdk-pixbuf-2.0-0 libjpeg-turbo8 libpng16-16t64
    libtiff6 libwebp7 libavahi-client3 libavahi-common3 libkrb5-3
    libk5crypto3 libcom-err2 libkeyutils1 libgssapi-krb5-2
  )
  if [ "$APT_LOCKED" -eq 1 ]; then
    command -v python3 >/dev/null 2>&1 || \
      die "python3 is required to read dependencies.lock.json" "$EX_OSERR"
    mapfile -t apt_pins < <(python3 - "$TARGET/um-cloud-deploy/dependencies.lock.json" <<'PY'
import json
import sys
lock = json.load(open(sys.argv[1], encoding="utf-8"))
for name, version in lock["apt"]["packages"].items():
    print(f"{name}={version}")
PY
)
    apt-get install -y --no-install-recommends "${apt_pins[@]}"
  else
    apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"
  fi
else
  log "skipping apt package installation (--skip-apt)"
fi

require_cmd python3.14
command -v python3.14 >/dev/null 2>&1 || \
  die "python3.14 not found: Ubuntu 26.04 python3.14 is required" "$EX_OSERR"

ensure_user "$UM_BROKER_USER" "$UM_BROKER_GROUP" "$UM_VAR"
ensure_user "$UM_CONSOLE_USER" "$UM_CONSOLE_GROUP" "$UM_CONSOLE_VAR"

ensure_dir "$UM_PREFIX" 0755 root root
ensure_dir "$UM_RELEASES" 0755 root root
ensure_dir "$UM_ETC" 0750 root "$UM_BROKER_GROUP"
ensure_dir "$UM_VAR" 0750 "$UM_BROKER_USER" "$UM_BROKER_GROUP"
ensure_dir "$UM_CONSOLE_VAR" 0750 "$UM_CONSOLE_USER" "$UM_CONSOLE_GROUP"
ensure_dir "$UM_RUN" 0700 "$UM_BROKER_USER" "$UM_BROKER_GROUP"

# A stale tmpfiles rule from an older deployment named the retired
# ``umshare`` account, so ``systemd-tmpfiles --create`` re-owned /run/um-share
# to a user that no longer runs any service.  The display unit then failed to
# create /run/um-share/display.auth ("(EE) Failed to open authorization
# file"), Xvfb exited and the whole login window never came up.  Rewrite the
# rule to the CURRENT service account and apply it now: the unit starts with
# RuntimeDirectory= empty, so this file is the only thing that owns the path.
install_run_tmpfiles() {
  local conf=/etc/tmpfiles.d/um-share-prep.conf
  local wanted="d $UM_RUN 0750 $UM_BROKER_USER $UM_BROKER_GROUP -"
  if [ -f "$conf" ] && [ "$(cat "$conf")" = "$wanted" ]; then
    return 0
  fi
  printf '%s\n' "$wanted" > "$conf"
  systemd-tmpfiles --create "$conf" || \
    log "WARNING: cannot apply $conf; /run/um-share keeps its current owner"
}
install_run_tmpfiles
ensure_dir "$UM_LOG" 0750 "$UM_BROKER_USER" "$UM_BROKER_GROUP"
ensure_dir "$UM_CONSOLE_LOG" 0750 "$UM_CONSOLE_USER" "$UM_CONSOLE_GROUP"
ensure_dir "$UM_VAR/run" 0750 "$UM_BROKER_USER" "$UM_BROKER_GROUP"
ensure_dir "$UM_VAR/broker" 0750 "$UM_BROKER_USER" "$UM_BROKER_GROUP"
ensure_dir "$UM_CONSOLE_VAR/share-backend" 0750 "$UM_CONSOLE_USER" "$UM_CONSOLE_GROUP"
ensure_dir "$UM_CONSOLE_VAR/console-locks" 0750 "$UM_CONSOLE_USER" "$UM_CONSOLE_GROUP"
ensure_dir "$UM_VAR/backup-remote" 0750 "$UM_BROKER_USER" "$UM_BROKER_GROUP"
ensure_dir "$UM_VAR/backup-spool" 0750 "$UM_BROKER_USER" "$UM_BROKER_GROUP"
ensure_dir "$UM_PW_BROWSERS" 0750 "$UM_CONSOLE_USER" "$UM_CONSOLE_GROUP"

STAGE=""
cleanup() {
  if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then
    rm -rf -- "$STAGE"
  fi
}
trap cleanup EXIT

if [ -n "$RELEASE_ARCHIVE" ]; then
  STAGE="$(mktemp -d "$UM_RELEASES/.install.XXXXXX")"
  tar -xzf "$RELEASE_ARCHIVE" -C "$STAGE"
  mapfile -t tops < <(find "$STAGE" -mindepth 1 -maxdepth 1 -type d -printf '%p\n')
  if [ "${#tops[@]}" -ne 1 ]; then
    die "release archive must contain exactly one top-level directory" "$EX_DATAERR"
  fi
  PAYLOAD="${tops[0]}"
else
  PAYLOAD="$(cd "$RELEASE_DIR" && pwd)"
fi

[ -f "$PAYLOAD/RELEASE-MANIFEST.json" ] || \
  die "RELEASE-MANIFEST.json missing in payload" "$EX_DATAERR"
[ -f "$PAYLOAD/um-cloud-deploy/dependencies.lock.json" ] || \
  die "dependencies.lock.json missing in payload" "$EX_DATAERR"

if [ -n "$VERSION" ]; then
  manifest_version="$(python3.14 - "$PAYLOAD/RELEASE-MANIFEST.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("release_version", ""))
PY
)"
  [ "$manifest_version" = "$VERSION" ] || \
    die "manifest release_version=$manifest_version, expected $VERSION" "$EX_DATAERR"
else
  VERSION="$(python3.14 - "$PAYLOAD/RELEASE-MANIFEST.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("release_version", ""))
PY
)"
fi
[ -n "$VERSION" ] || die "release version is empty" "$EX_DATAERR"
case "$VERSION" in
  *[!A-Za-z0-9._-]*) die "release version has unsafe characters: $VERSION" "$EX_DATAERR" ;;
esac

verify_release_manifest "$PAYLOAD" "$PAYLOAD/RELEASE-MANIFEST.json" || \
  die "release manifest verification failed" "$EX_DATAERR"

TARGET="$UM_RELEASES/$VERSION"
if [ -d "$TARGET" ]; then
  if [ -f "$TARGET/RELEASE-MANIFEST.json" ] && \
     cmp -s "$TARGET/RELEASE-MANIFEST.json" "$PAYLOAD/RELEASE-MANIFEST.json"; then
    log "release $VERSION already installed and identical; reusing"
  else
    die "release $VERSION already exists with different content" "$EX_CANTCREAT"
  fi
else
  if [ -n "$RELEASE_ARCHIVE" ]; then
    mv "$PAYLOAD" "$TARGET"
    PAYLOAD="$TARGET"
    STAGE="$(dirname "$TARGET")"
    STAGE=""
  elif [ "$PAYLOAD" != "$TARGET" ]; then
    cp -a "$PAYLOAD" "$TARGET"
  fi
  chown -R root:root "$TARGET"
  chmod -R go-w "$TARGET"
fi
find "$TARGET/um-cloud-deploy" -type f \
  \( -name '*.sh' -o -path '*/bin/*.py' \) -exec chmod 0755 {} +

# The running console's effective share-key path is resolved before `current`
# moves: an old deployment without the console.env line used
# <current>/data/share-secret.key.  The wiring below must migrate that key,
# never replace it with a new one.
CURRENT_TARGET=""
if [ -L "$UM_PREFIX/current" ]; then
  CURRENT_TARGET="$(readlink -f "$UM_PREFIX/current")"
fi

ln -sfn "$UM_RELEASES/$VERSION" "$UM_PREFIX/current.tmp.$$"
mv -Tf "$UM_PREFIX/current.tmp.$$" "$UM_PREFIX/current"
if [ -L "$UM_PREFIX/bin" ]; then
  atomic_symlink "$TARGET/um-cloud-deploy/bin" "$UM_PREFIX/bin"
elif [ ! -e "$UM_PREFIX/bin" ]; then
  atomic_symlink "$TARGET/um-cloud-deploy/bin" "$UM_PREFIX/bin"
else
  die "$UM_PREFIX/bin exists and is not a symlink" "$EX_CANTCREAT"
fi

if [ ! -x "$UM_VENV/bin/python" ]; then
  log "creating venv at $UM_VENV"
  python3.14 -m venv "$UM_VENV" || die "venv creation failed" "$EX_SOFTWARE"
fi
"$UM_VENV/bin/python" -m pip --version >/dev/null || \
  die "venv pip is unavailable" "$EX_SOFTWARE"

if [ -n "$WHEELHOUSE" ] || [ -d "$UM_PREFIX/wheelhouse" ] || \
   [ -d "$TARGET/um-cloud-deploy/wheelhouse" ]; then
  if [ -n "$WHEELHOUSE" ]; then
    wheelhouse="$WHEELHOUSE"
  elif [ -d "$UM_PREFIX/wheelhouse" ]; then
    wheelhouse="$UM_PREFIX/wheelhouse"
  else
    wheelhouse="$TARGET/um-cloud-deploy/wheelhouse"
  fi
  [ -d "$wheelhouse" ] || die "wheelhouse not found: $wheelhouse" "$EX_NOINPUT"
  log "installing Python dependencies from $wheelhouse"
  "$UM_VENV/bin/python" -m pip install --no-index --find-links "$wheelhouse" \
    -r "$TARGET/um-cloud-deploy/requirements.lock" || \
    die "pip install from wheelhouse failed" "$EX_SOFTWARE"
else
  log "installing Python dependencies from the network (no wheelhouse)"
  "$UM_VENV/bin/python" -m pip install \
    -r "$TARGET/um-cloud-deploy/requirements.lock" || \
    die "pip install failed" "$EX_SOFTWARE"
fi

export PLAYWRIGHT_BROWSERS_PATH="$UM_PW_BROWSERS"
mkdir -p "$UM_PW_BROWSERS"
if ! "$UM_VENV/bin/playwright" install chromium; then
  die "Playwright Chromium install failed (network or archive unavailable)" "$EX_SOFTWARE"
fi
for sandbox in "$UM_PW_BROWSERS"/chromium-*/chrome-linux64/chrome_sandbox; do
  [ -e "$sandbox" ] || continue
  chown root:root "$sandbox"
  chmod 4755 "$sandbox"
done
# Resolve the executable install.sh just installed.  The browser path is
# deployment state, not a guessed release-relative default; a missing binary is a
# visible warning, never a silent empty value.
BROWSER_PATH=""
for candidate in "$UM_PW_BROWSERS"/chromium-*/chrome-linux64/chrome \
                 "$UM_PW_BROWSERS"/chromium-*/chrome-linux/chrome; do
  [ -x "$candidate" ] || continue
  BROWSER_PATH="$candidate"
  break
done
if [ -z "$BROWSER_PATH" ]; then
  log "WARNING: Playwright Chromium executable not found under $UM_PW_BROWSERS; CHAT2API_BROWSER_PATH is not set"
fi

# Configuration and secrets are CREATED ONLY WHEN ABSENT.  An existing file is
# never overwritten: the share store, keys and projection are the only copy.
write_if_absent() {
  local target="$1" mode="$2" owner="$3" group="$4"
  shift 4
  if [ -e "$target" ]; then
    log "keeping existing $target"
    return 0
  fi
  local tmp="${target}.tmp.$$"
  umask 077
  "$@" > "$tmp"
  chmod "$mode" "$tmp"
  chown "$owner:$group" "$tmp"
  mv "$tmp" "$target"
}

random_secret() {
  python3.14 -c 'import secrets; print(secrets.token_hex(32))'
}

# Set one KEY=VALUE line in an existing env file.  Unlike ensure_env_line this
# replaces a value install.sh itself resolved (the Playwright Chromium path), so an
# upgraded release cannot keep a browser path that no longer exists.
set_env_value() {
  local file="$1" key="$2" value="$3"
  [ -f "$file" ] || die "environment file missing: $file" "$EX_NOINPUT"
  python3.14 - "$file" "$key" "$value" <<'PY'
import os
import sys
import tempfile

path, key, value = sys.argv[1:4]
with open(path, "r", encoding="utf-8") as handle:
    lines = handle.read().splitlines()
prefix = key + "="
replaced = False
out = []
for line in lines:
    if line.startswith(prefix):
        if not replaced:
            out.append(prefix + value)
            replaced = True
        continue
    out.append(line)
if not replaced:
    out.append(prefix + value)
fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                          dir=os.path.dirname(path), text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
except Exception:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}

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

check_deployment_assembly() {
  local release="$1"
  local py
  py="$(deploy_python)" || die "python interpreter not found" "$EX_OSERR"
  [ -f "$release/um-cloud-deploy/backup_restore.py" ] || \
    die "backup_restore.py missing in $release" "$EX_NOINPUT"
  run_as_share "$py" "$release/um-cloud-deploy/backup_restore.py" \
    --check-deployment-assembly broker \
    "$UM_SYSTEMD_DIR/um-share-broker.service" "$UM_ETC/broker.env" \
    >/dev/null || die "broker deployment assembly check failed" "$EX_SOFTWARE"
  run_as_share "$py" "$release/um-cloud-deploy/backup_restore.py" \
    --check-deployment-assembly console \
    "$UM_SYSTEMD_DIR/um-share-console.service" "$UM_ETC/console.env" \
    >/dev/null || die "console deployment assembly check failed" "$EX_SOFTWARE"
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
    --share-secret-group "$(id -g "$UM_CONSOLE_GROUP")" || \
    die "explicit data recovery failed" "$EX_SOFTWARE"
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

write_if_absent "$UM_BROKER_KEY" 0600 "$UM_BROKER_USER" "$UM_BROKER_GROUP" random_secret
write_if_absent "$UM_INSTANCE_KEY" 0600 "$UM_BROKER_USER" "$UM_BROKER_GROUP" random_secret
write_if_absent "$UM_ETC/admin-control-token" 0600 "$UM_BROKER_USER" "$UM_BROKER_GROUP" random_secret

# The share metering price table is deployment configuration, not release
# code.  Without it the ledger stamps no price version, so a missing table is a
# deployment refusal.
if [ ! -e "$UM_ETC/prices.json" ]; then
  [ -n "$PRICE_TABLE" ] || \
    die "price table is required: pass --price-table or pre-create $UM_ETC/prices.json" "$EX_NOINPUT"
  install -m 0640 -o root -g "$UM_BROKER_GROUP" \
    "$PRICE_TABLE" "$UM_ETC/prices.json"
else
  log "keeping existing $UM_ETC/prices.json"
fi
python3.14 - "$UM_ETC/prices.json" <<'PY'
import json
import sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError) as exc:
    raise SystemExit(f"price table is not valid JSON: {exc}")
if not isinstance(payload, dict):
    raise SystemExit("price table must be a JSON object")
PY

if [ ! -e "$UM_ETC/share-management.json" ]; then
  install -m 0640 -o root -g "$UM_BROKER_GROUP" \
    "$TARGET/um-cloud-deploy/config/share-management.json.example" \
    "$UM_ETC/share-management.json"
fi
if [ ! -e "$UM_ETC/broker.env" ]; then
  install -m 0640 -o root -g "$UM_BROKER_GROUP" \
    "$TARGET/um-cloud-deploy/config/broker.env.example" \
    "$UM_ETC/broker.env"
fi
if [ ! -e "$UM_ETC/console.env" ]; then
  install -m 0640 -o root -g "$UM_BROKER_GROUP" \
    "$TARGET/um-cloud-deploy/config/console.env.example" \
    "$UM_ETC/console.env"
fi
# Replace the placeholders with the real uid only when the file was just
# created; an existing file is left untouched.
if ! grep -q '^UM_SHARE_UID=' "$UM_ETC/broker.env" 2>/dev/null; then
  printf 'UM_SHARE_UID=%s\n' "$(id -u "$UM_BROKER_USER")" >> "$UM_ETC/broker.env"
fi
if ! grep -q '^UM_SHARE_UID=' "$UM_ETC/console.env" 2>/dev/null; then
  printf 'UM_SHARE_UID=%s\n' "$(id -u "$UM_BROKER_USER")" >> "$UM_ETC/console.env"
fi
# The POSIX share-key encryption key is persistent deployment state, not
# release material: the product default is <current>/data/share-secret.key, which
# would disappear when `current` switches.  "install" keeps an existing effective
# key (including a release-relative one from an older deployment) and only creates
# a new key when the deployment has none; the value is never printed.
# Explicit recovery is the exception: the sealed backup carries the key it must
# restore, so only the persistent path is wired and no new key is invented.
if [ -n "$RECOVER_BACKUP" ]; then
  ensure_share_secret_wiring recover
else
  ensure_share_secret_wiring install
fi
# F1's revoke journal is shared deployment state: the broker host and the
# console must read the same file under the writable state dir, not a
# release-relative default that vanishes on the next version switch.
ensure_env_line "$UM_ETC/broker.env" CHAT2API_SHARE_REVOKE_STATE_FILE \
  "$UM_VAR/share-revoke.state"
ensure_env_line "$UM_ETC/console.env" CHAT2API_SHARE_REVOKE_STATE_FILE \
  "$UM_VAR/share-revoke.state"
# Existing operator env files are never overwritten, but a missing read-face
# line is appended: an upgraded install must not silently lose the security,
# price or Magpie wiring (the console unit's ExecStartPre fails closed on any
# omission).  Budget and automatic-backup keys are deliberately absent.
ensure_env_line "$UM_ETC/console.env" UM_CLOUD_SECURITY_STATE "$UM_VAR/security-state.json"
# The merged 8100 child owns the UM login token and browser profile; keep them
# under the writable console state instead of the read-only release.
ensure_env_line "$UM_ETC/console.env" CHAT2API_TOKEN_FILE \
  "$UM_CONSOLE_VAR/share-backend/token.json"
ensure_env_line "$UM_ETC/console.env" CHAT2API_PROFILE_DIR \
  "$UM_CONSOLE_VAR/share-backend/edge-profile"
ensure_env_line "$UM_ETC/console.env" USAGE_PRICE_FILE "$UM_ETC/prices.json"
ensure_env_line "$UM_ETC/console.env" CHAT2API_PRICE_TABLE_FILE "$UM_ETC/prices.json"
ensure_env_line "$UM_ETC/console.env" MAGPIE_PRICE_SNAPSHOT "$UM_VAR/magpie-prices.snapshot.json"
ensure_env_line "$UM_ETC/console.env" MAGPIE_PRICE_STATUS "$UM_VAR/magpie-prices.status.json"
# Public API origin shown by the console and used by the public probe.  The
# domain is deployment configuration; a missing line keeps the legacy TS rule.
ensure_env_line "$UM_ETC/console.env" CONSOLE_PUBLIC_ORIGIN "https://dacintapi.de5.net"
# The cloud share does not add an extra per-key dispatch queue.
ensure_env_line "$UM_ETC/console.env" CHAT2API_SHARE_DISPATCH_MAX_WAIT_S "0"
# Private Tailscale Serve entry and same-origin noVNC launcher.  The display
# stays opt-in (0); the operator enables it after creating the root-only VNC
# credential and installing the tailnet policy.
ensure_env_line "$UM_ETC/console.env" USAGE_TAILNET_CONFIG_FILE "$UM_ETC/console-tailnet-access.json"
ensure_env_line "$UM_ETC/console.env" CONSOLE_DISPLAY_ENABLED "0"
ensure_env_line "$UM_ETC/console.env" CONSOLE_DISPLAY_VNC_HOST "127.0.0.1"
ensure_env_line "$UM_ETC/console.env" CONSOLE_DISPLAY_VNC_PORT "5901"
ensure_env_line "$UM_ETC/console.env" CONSOLE_DISPLAY_NOVNC_ROOT "/usr/share/novnc"
ensure_env_line "$UM_ETC/console.env" CONSOLE_DISPLAY_LAUNCHER_FILE "/opt/um-share/current/um-cloud-deploy/display/novnc-launcher.html"
ensure_env_line "$UM_ETC/console.env" CONSOLE_DISPLAY_VNC_AUTH_SOCKET "/run/um-share/vnc-auth.sock"
if [ -n "$BROWSER_PATH" ]; then
  set_env_value "$UM_ETC/console.env" CHAT2API_BROWSER_PATH "$BROWSER_PATH"
fi
chown root:"$UM_BROKER_GROUP" "$UM_ETC/broker.env" "$UM_ETC/console.env" 2>/dev/null || true
chmod 0640 "$UM_ETC/broker.env" "$UM_ETC/console.env"

if [ ! -e "$UM_ETC/vnc.passwd" ]; then
  log "WARNING: $UM_ETC/vnc.passwd absent; create it with x11vnc -storepasswd before enabling display"
fi

# The bootstrap projection is schema 2, generation 0, empty data: the broker
# publishes the real projection later; the share gateway must be able to start
# before the first publication (an empty projection is legal).
if [ ! -e "$UM_VAR/run/share-projection.json" ]; then
  install -m 0600 -o "$UM_BROKER_USER" -g "$UM_BROKER_GROUP" \
    "$TARGET/um-cloud-deploy/config/um-share.json.example" \
    "$UM_VAR/run/share-projection.json"
fi

ln -sfn "$TARGET/um-cloud-deploy/bin" "$UM_PREFIX/bin.tmp.$$"
mv -Tf "$UM_PREFIX/bin.tmp.$$" "$UM_PREFIX/bin"

install_units "$TARGET" um-share-broker.service um-share-console.service \
  um-share-display.service || \
  die "cannot install systemd units" "$EX_OSFILE"

if [ "$WITH_CLOUDFLARED" -eq 1 ]; then
  command -v cloudflared >/dev/null 2>&1 || \
    die "cloudflared binary not found; install it before --with-cloudflared" "$EX_OSERR"
  if [ ! -e "$UM_ETC/cloudflared.yml" ]; then
    install -m 0640 -o root -g root \
      "$TARGET/um-cloud-deploy/config/cloudflared.yml.example" \
      "$UM_ETC/cloudflared.yml"
  else
    log "keeping existing $UM_ETC/cloudflared.yml"
  fi
  if [ ! -e "$UM_ETC/cloudflared.env" ]; then
    [ -n "$CLOUDFLARED_TOKEN_FILE" ] || \
      die "cloudflared token is required: pass --cloudflared-token-file or pre-create $UM_ETC/cloudflared.env" "$EX_NOINPUT"
    token="$(tr -d '\r\n' < "$CLOUDFLARED_TOKEN_FILE")"
    [ -n "$token" ] || die "cloudflared token file is empty" "$EX_DATAERR"
    token_tmp="$UM_ETC/cloudflared.env.tmp.$$"
    ( umask 077; printf 'TUNNEL_TOKEN=%s\n' "$token" > "$token_tmp" )
    chmod 0600 "$token_tmp"
    chown root:root "$token_tmp"
    mv "$token_tmp" "$UM_ETC/cloudflared.env"
    unset token
  else
    log "keeping existing $UM_ETC/cloudflared.env"
  fi
  "$(deploy_python)" "$TARGET/um-cloud-deploy/cloudflared/check-ingress.py" \
    --config "$UM_ETC/cloudflared.yml" --expect-host dacintapi.de5.net \
    >/dev/null || die "cloudflared ingress check failed" "$EX_DATAERR"
  install -m 0644 -o root -g root \
    "$TARGET/um-cloud-deploy/systemd/cloudflared.service" \
    "$UM_SYSTEMD_DIR/cloudflared.service" || \
    die "cannot install cloudflared.service" "$EX_OSFILE"
fi
systemctl daemon-reload
systemctl enable um-share-broker.service um-share-console.service
if [ "$WITH_CLOUDFLARED" -eq 1 ]; then
  systemctl enable cloudflared.service
fi
check_deployment_assembly "$TARGET"

# Explicit recovery is a requested data operation, not a service start:
# --no-start only skips starting services, never the restore.  run_recovery dies
# with EX_SOFTWARE on failure, so a failed restore can never finish with 0.
if [ -n "$RECOVER_BACKUP" ]; then
  log "explicit recovery mode: restoring $RECOVER_BACKUP"
  run_recovery "$TARGET"
fi

if [ "$NO_START" -eq 0 ]; then
  systemctl restart um-share-broker.service
  brokerctl start-explicit || \
    die "broker start did not confirm 8100" "$EX_UNAVAILABLE"
  systemctl restart um-share-console.service
  if [ "$WITH_DISPLAY" -eq 1 ]; then
    systemctl enable --now um-share-display.service
  fi
  if [ "$WITH_CLOUDFLARED" -eq 1 ]; then
    systemctl restart cloudflared.service
  fi
  wait_port_open 127.0.0.1 18096 "$UM_HEALTH_TIMEOUT_S" || \
    die "console 18096 did not open" "$EX_UNAVAILABLE"
  wait_port_open 127.0.0.1 8100 "$UM_HEALTH_TIMEOUT_S" || \
    die "share gateway 8100 did not open (check broker start)" "$EX_UNAVAILABLE"
  wait_port_open 127.0.0.1 8110 "$UM_HEALTH_TIMEOUT_S" || \
    log "WARNING: 8110 is not listening yet; start it with share.backend_restart"
  health_gate || die "health gate failed (18096/8100 HTTP)" "$EX_UNAVAILABLE"
else
  log "services were not started (--no-start)"
fi

log "install complete: release=$VERSION prefix=$UM_PREFIX"
exit "$EX_OK"
