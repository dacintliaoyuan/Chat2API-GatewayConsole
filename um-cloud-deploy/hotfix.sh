#!/usr/bin/env bash
# One-file hotfix for the cloud host: replace one binary or one web dist with a
# backup, then verify. It is NOT a release switch; use update.sh for that.
#
#   hotfix.sh --binary ./um-console.new --target /opt/um-share/bin/um-console \
#     --unit um-share-console.service --health-url http://127.0.0.1:8002/health
#
#   hotfix.sh --web-dist ./dist-cloud --target /opt/um-share/current/web/dist
#
# The binary path is copied beside the target and renamed into place, so a running
# executable is never opened for write (ETXTBSY). A backup is kept under
# /root/hotfix-<date>/; the script never edits config, keys or data.
set -euo pipefail

EX_OK=0
EX_USAGE=64
EX_NOINPUT=66
EX_OSERR=71
EX_HEALTH=68

usage() {
  cat <<'EOF'
Usage: hotfix.sh (--binary PATH --target PATH [--unit UNIT] [--health-url URL]
                 | --web-dist PATH --target PATH)
                 [--backup-dir DIR]

  --binary PATH       new executable to install
  --web-dist PATH     new static tree to install (replaces the target tree)
  --target PATH       installed path (binary file or web dist directory)
  --unit UNIT         systemd unit to restart after a binary install
  --health-url URL    optional URL expected to answer 200 after restart
  --backup-dir DIR    backup root (default: /root/hotfix-<UTC date>)
EOF
}

binary=""
web_dist=""
target=""
unit=""
health_url=""
backup_dir="/root/hotfix-$(date -u +%Y%m%dT%H%M%SZ)"

while [ $# -gt 0 ]; do
  case "$1" in
    --binary) binary="$2"; shift 2 ;;
    --web-dist) web_dist="$2"; shift 2 ;;
    --target) target="$2"; shift 2 ;;
    --unit) unit="$2"; shift 2 ;;
    --health-url) health_url="$2"; shift 2 ;;
    --backup-dir) backup_dir="$2"; shift 2 ;;
    -h|--help) usage; exit "$EX_OK" ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit "$EX_USAGE" ;;
  esac
done

if [ -z "$target" ]; then
  echo "--target is required" >&2
  exit "$EX_USAGE"
fi
if [ -n "$binary" ] && [ -n "$web_dist" ]; then
  echo "use either --binary or --web-dist, not both" >&2
  exit "$EX_USAGE"
fi
if [ -z "$binary" ] && [ -z "$web_dist" ]; then
  echo "one of --binary or --web-dist is required" >&2
  exit "$EX_USAGE"
fi

mkdir -p "$backup_dir"
if [ -n "$binary" ]; then
  [ -f "$binary" ] || { echo "binary not found: $binary" >&2; exit "$EX_NOINPUT"; }
  [ -e "$target" ] || { echo "target not found: $target" >&2; exit "$EX_NOINPUT"; }
  backup="$backup_dir/$(basename "$target").pre-hotfix"
  cp -a "$target" "$backup"
  staged="${target}.new.$$"
  cp "$binary" "$staged"
  chmod 0755 "$staged"
  mv -f "$staged" "$target"
  echo "installed $(sha256sum "$target" | cut -c1-16) -> $target"
  echo "backup: $backup"
  if [ -n "$unit" ]; then
    systemctl restart "$unit"
    sleep 2
    systemctl show -p NRestarts -p ActiveEnterTimestamp "$unit"
  fi
  if [ -n "$health_url" ]; then
    status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$health_url" || true)
    echo "health $health_url: $status"
    [ "$status" = "200" ] || exit "$EX_HEALTH"
  fi
else
  [ -d "$web_dist" ] || { echo "web dist not found: $web_dist" >&2; exit "$EX_NOINPUT"; }
  [ -d "$target" ] || { echo "target not found: $target" >&2; exit "$EX_NOINPUT"; }
  backup="$backup_dir/$(basename "$target").pre-hotfix"
  cp -a "$target" "$backup"
  rm -rf "$target"
  cp -a "$web_dist" "$target"
  echo "installed web dist -> $target"
  echo "backup: $backup"
fi
