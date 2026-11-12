#!/usr/bin/env bash
# Bootstrap the task-only Ubuntu 24.04 WSL2 distro "UM-Cloud-P1".
#
# Run from Git Bash on Windows:
#   bash ai-gateway/um-cloud-deploy/bootstrap-wsl.sh            # create + provision
#   bash ai-gateway/um-cloud-deploy/bootstrap-wsl.sh --verify   # ... and run checks
#
# The script is idempotent: an existing distro, user, package set, venv and
# browser cache are reused; every download is skipped when the target file exists.
#
# Host-side limits found on 2026-10-30: WSL2 outbound TCP is dropped by the
# host Mihomo TUN (only WSL DNS answers), while Windows processes started
# through interop still have network.  The offline apt mirror and the Playwright
# archives below are therefore fetched with Windows curl.exe.
set -uo pipefail
export MSYS_NO_PATHCONV=1

DISTRO="UM-Cloud-P1"
DISTRO_TEMPLATE="Ubuntu-24.04"
LOCATION_WIN='D:\wsl\UM-Cloud-P1'
ROOTFS_URL="https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz"
ROOTFS_LOCAL='/d/wsl/um-cloud-p1-rootfs.tar.gz'

APT_ROOT=/mnt/d/wsl/um-cloud-p1-apt
WHEEL_ROOT=/mnt/d/wsl/um-cloud-p1-wheels
PW_ARCHIVE_ROOT=/mnt/d/wsl/um-cloud-p1-pw
SHARE=/srv/um-share
VENV="$SHARE/venv"
PW_BROWSERS="$SHARE/pw-browsers"

PY_PKGS="requests==2.34.2 playwright==1.64.1 pyyaml==6.0.3 cryptography==50.0.2"
APT_PKGS="python3-venv python3-pip fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
xvfb x11-utils dbus-x11 unzip xz-utils \
libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libxkbcommon0 \
libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
libatspi2.0-0t64 libx11-6 libxcb1 libxext6 libxrender1 libxi6 libxtst6 libglib2.0-0t64 libdbus-1-3 \
libexpat1 libfontconfig1 libfreetype6 libuuid1 libxau6 libxdmcp6 libbsd0 libselinux1 libpcre2-8-0 \
libgcrypt20 libgpg-error0 liblzma5 libzstd1 libsystemd0 libcap2 libmount1 libblkid1 libacl1 libattr1 \
libc6 zlib1g libstdc++6 gcc-14-base libgcc-s1"

is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

win_path() { # /mnt/d/a b -> D:\a b
  printf '%s' "$1" | sed -e 's#^/mnt/\([a-zA-Z]\)/#\1:/#' -e 's#/#\\#g'
}

to_wsl_path() { # D:\a b, D:/a b, /d/a b -> /mnt/d/a b
  case "$1" in
    /mnt/*) printf '%s' "$1" ;;
    /[a-zA-Z]/*) printf '/mnt%s' "$1" ;;
    *) printf '%s' "$1" | sed -e 's#^\([a-zA-Z]\):[\\/]#/mnt/\L\1/#' -e 's#\\#/#g' ;;
  esac
}

to_win_path() { # /d/a b -> D:\a b
  case "$1" in
    /mnt/*) win_path "$1" ;;
    /[a-zA-Z]/*) printf '%s' "$1" | sed -e 's#^/\([a-zA-Z]\)/#\1:/#' -e 's#/#\\#g' ;;
    *) printf '%s' "$1" ;;
  esac
}

# ---------------------------------------------------------------- Windows side
create_distro() {
  if wsl.exe --list --quiet 2>/dev/null | tr -d '\0' | grep -qx "$DISTRO"; then
    echo "distro $DISTRO already exists"
    return 0
  fi
  echo "creating $DISTRO from $DISTRO_TEMPLATE"
  if wsl.exe --install -d "$DISTRO_TEMPLATE" --no-launch --name "$DISTRO" --location "$LOCATION_WIN"; then
    return 0
  fi
  echo "wsl --install failed, importing a rootfs tarball instead"
  if [ ! -s "$ROOTFS_LOCAL" ]; then
    curl -L --fail --max-time 900 -o "$ROOTFS_LOCAL" "$ROOTFS_URL" || return 1
  fi
  wsl.exe --import "$DISTRO" "$LOCATION_WIN" "$(to_win_path "$ROOTFS_LOCAL")" --version 2
}

if ! is_wsl; then
  command -v wsl.exe >/dev/null 2>&1 || { echo "wsl.exe not found"; exit 1; }
  mkdir -p /d/wsl
  create_distro || { echo "distro creation failed"; exit 1; }
  case "$0" in
    /*) SELF_ABS="$0" ;;
    *) SELF_ABS="$(pwd)/$0" ;;
  esac
  SELF_WSL=$(to_wsl_path "$SELF_ABS")
  exec wsl.exe -d "$DISTRO" -u root -- bash "$SELF_WSL" --provision "$@"
fi

# ---------------------------------------------------------------- Inside WSL
[ "$(id -u)" -eq 0 ] || { echo "run the provisioning step as root"; exit 1; }

VERIFY=0
for arg in "$@"; do
  [ "$arg" = "--verify" ] && VERIFY=1
done

echo "== wsl.conf"
if ! grep -qs '^systemd=true' /etc/wsl.conf; then
  if grep -qs '^\[boot\]' /etc/wsl.conf; then
    sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
  else
    printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
  fi
fi
grep -qs '^systemd=true' /etc/wsl.conf || { echo "cannot set systemd=true"; exit 1; }

echo "== umshare user"
if ! id -u umshare >/dev/null 2>&1; then
  useradd -m -s /bin/bash umshare
fi
install -d -m 0755 /etc/sudoers.d
printf 'umshare ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/90-umshare
chmod 0440 /etc/sudoers.d/90-umshare
visudo -cf /etc/sudoers.d/90-umshare >/dev/null || { echo "invalid sudoers entry"; exit 1; }

prepare_offline_mirror() {
  command -v curl.exe >/dev/null 2>&1 || { echo "curl.exe (Windows interop) is required offline"; return 1; }
  mkdir -p "$APT_ROOT/index"
  local suites=(noble noble-updates noble-security)
  local comps=(main universe)
  local suite comp url
  for suite in "${suites[@]}"; do
    for comp in "${comps[@]}"; do
      local xz="$APT_ROOT/index/${suite}_${comp}.xz"
      local out="$APT_ROOT/dists/$suite/$comp/binary-amd64/Packages"
      mkdir -p "$(dirname "$out")"
      if [ ! -s "$xz" ]; then
        if [ "$suite" = "noble-security" ]; then
          url="http://security.ubuntu.com/ubuntu/dists/$suite/$comp/binary-amd64/Packages.xz"
        else
          url="http://archive.ubuntu.com/ubuntu/dists/$suite/$comp/binary-amd64/Packages.xz"
        fi
        echo "download $url"
        curl.exe -sS --max-time 300 -o "$(win_path "$xz")" "$url" || return 1
      fi
      if [ ! -s "$out" ]; then
        xz -dc "$xz" > "$out" || return 1
      fi
    done
    printf 'Origin: Ubuntu\nLabel: Ubuntu\nSuite: %s\nCodename: %s\nArchitectures: amd64\nComponents: main universe\nDescription: local mirror for %s\n' \
      "$suite" "$suite" "$DISTRO" > "$APT_ROOT/dists/$suite/Release"
  done

  cat > /etc/apt/sources.list.d/um-local.sources <<EOF
Types: deb
URIs: file://$APT_ROOT
Suites: noble noble-updates noble-security
Components: main universe
Trusted: yes
EOF
  local f
  for f in /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list; do
    [ -f "$f" ] && mv -f "$f" "$f.disabled"
  done
}

echo "== apt"
export DEBIAN_FRONTEND=noninteractive
if [ -f /etc/apt/sources.list.d/um-local.sources ]; then
  apt-get update -o Acquire::Check-Valid-Until=false >/dev/null 2>&1 || true
elif ! apt-get update -o Acquire::Check-Valid-Until=false >/dev/null 2>&1; then
  echo "network apt update failed, building the offline mirror"
  prepare_offline_mirror || { echo "offline mirror failed"; exit 1; }
  apt-get update -o Acquire::Check-Valid-Until=false || { echo "local apt update failed"; exit 1; }
fi
apt-get install -y --no-install-recommends $APT_PKGS || { echo "apt install failed"; exit 1; }

echo "== venv"
install -d -o umshare -g umshare "$SHARE"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV" || { echo "venv creation failed"; exit 1; }
fi
if [ -d "$WHEEL_ROOT" ] && compgen -G "$WHEEL_ROOT/*.whl" >/dev/null; then
  "$VENV/bin/pip" install --no-index --find-links "$WHEEL_ROOT" $PY_PKGS || { echo "pip install from wheels failed"; exit 1; }
else
  "$VENV/bin/pip" install $PY_PKGS || { echo "pip install failed (no wheels and no network)"; exit 1; }
fi

echo "== playwright chromium"
export PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS"
mkdir -p "$PW_BROWSERS"
if ! "$VENV/bin/playwright" install chromium >/dev/null 2>&1; then
  echo "playwright download failed, extracting pre-downloaded archives"
  mkdir -p "$PW_ARCHIVE_ROOT"
  "$VENV/bin/playwright" install --dry-run chromium 2>/dev/null |
    awk '/^  Install location:/ {loc=$3} /^  Download url:/ {print $3 "\t" loc}' |
    while IFS=$'\t' read -r url loc; do
      [ -n "$loc" ] || continue
      [ -f "$loc/INSTALLATION_COMPLETE" ] && continue
      zip="$PW_ARCHIVE_ROOT/$(basename "$url")"
      if [ ! -s "$zip" ]; then
        echo "download $url"
        curl.exe -sSL --max-time 900 -o "$(win_path "$zip")" "$url" || exit 1
      fi
      mkdir -p "$loc"
      unzip -q -o "$zip" -d "$loc" || exit 1
      touch "$loc/INSTALLATION_COMPLETE"
    done
  "$VENV/bin/playwright" install chromium >/dev/null 2>&1 || { echo "playwright install failed"; exit 1; }
fi
for sb in "$PW_BROWSERS"/chromium-*/chrome-linux64/chrome_sandbox; do
  [ -e "$sb" ] && chown root:root "$sb" && chmod 4755 "$sb"
done
chown -R umshare:umshare "$SHARE"

printf 'export PLAYWRIGHT_BROWSERS_PATH=%s\n' "$PW_BROWSERS" > /etc/profile.d/um-cloud-p1.sh
chrome_bin=$(ls -d "$PW_BROWSERS"/chromium-*/chrome-linux64/chrome 2>/dev/null | head -1)
if [ -n "$chrome_bin" ] && [ ! -e /usr/local/bin/chromium ]; then
  ln -s "$chrome_bin" /usr/local/bin/chromium
fi

echo "== done"
if [ "$VERIFY" -eq 1 ]; then
  echo "== verify: systemd"
  systemctl is-system-running
  echo "== verify: cgroup"
  stat -fc %T /sys/fs/cgroup
  systemd-run --scope -p MemoryMax=2G -- true; echo "systemd_run_exit=$?"
  echo "== verify: imports"
  sudo -u umshare env PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS" \
    "$VENV/bin/python" -c 'import requests, playwright, yaml, cryptography; print("imports ok")'
  echo "imports_exit=$?"
  echo "== verify: chromium"
  /usr/local/bin/chromium --version
  echo "== verify: umshare uid"
  id -u umshare
  echo "== verify: headed screenshot (WSLg)"
  install -d -o umshare -g umshare "$SHARE/artifacts"
  sudo -u umshare env PLAYWRIGHT_BROWSERS_PATH="$PW_BROWSERS" \
    DISPLAY="${DISPLAY:-:0}" WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" \
    XDG_RUNTIME_DIR="/run/user/$(id -u umshare)" \
    "$VENV/bin/python" - "$SHARE/artifacts/headed-about-blank.png" <<'PY'
import os
import sys

from playwright.sync_api import sync_playwright

out = sys.argv[1]
print("DISPLAY=" + os.environ.get("DISPLAY", ""))
print("WAYLAND_DISPLAY=" + os.environ.get("WAYLAND_DISPLAY", ""))
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--disable-gpu"])
    page = browser.new_page(viewport={"width": 800, "height": 600})
    page.goto("about:blank")
    page.screenshot(path=out)
    print("screenshot=" + out)
    print("browser_version=" + browser.version)
    browser.close()
PY
  echo "headed_exit=$?"
fi
