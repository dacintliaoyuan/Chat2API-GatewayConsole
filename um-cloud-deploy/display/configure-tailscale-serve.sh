#!/usr/bin/env bash
# Configure the tailnet-only Serve entry for the private console.
#
# The console listener remains 127.0.0.1:18096.  Serve terminates HTTPS on the
# tailnet and proxies only the root path to that loopback address.  Funnel is never
# enabled, and this script never adds a public 80/443/8100/5901 listener.
#
# Without --apply the script only reports the current state and the exact command
# it would run.  Replacing a different existing Serve configuration requires
# --reset-other-serve; that reset clears every Serve handler on this node.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3.14}"
APPLY=0
RESET_OTHER=0

usage() {
  cat <<'EOF'
usage: configure-tailscale-serve.sh [--apply] [--reset-other-serve]

  --apply               write the Serve configuration (default: dry run)
  --reset-other-serve   allow `tailscale serve reset` when the current Serve
                        configuration is not exactly / -> 127.0.0.1:18096

The only applied handler is:
  tailscale serve --bg --https=443 http://127.0.0.1:18096

Funnel must already be off (`tailscale funnel reset`); this script refuses to
continue while Funnel is enabled.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --reset-other-serve) RESET_OTHER=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

command -v tailscale >/dev/null 2>&1 || {
  echo "tailscale is not installed" >&2
  exit 69
}
command -v ss >/dev/null 2>&1 || {
  echo "ss (iproute2) is not installed" >&2
  exit 69
}
command -v "$PYTHON" >/dev/null 2>&1 || {
  echo "python interpreter not found: $PYTHON" >&2
  exit 69
}
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: Tailscale Serve writes the node configuration" >&2
  exit 77
fi

if ! funnel_json="$(tailscale funnel status --json 2>/dev/null)"; then
  echo "cannot read Tailscale Funnel status" >&2
  exit 69
fi
if ! printf '%s' "$funnel_json" | "$PYTHON" -c '
import json
import sys
sys.path.insert(0, sys.argv[1])
import tailscale_serve_policy
try:
    document = json.load(sys.stdin)
except ValueError:
    raise SystemExit(65)
raise SystemExit(1 if tailscale_serve_policy.validate_funnel_status(document) else 0)
' "$HERE"; then
  echo "Funnel is enabled; run 'tailscale funnel reset' before configuring Serve" >&2
  exit 77
fi

if ! serve_json="$(tailscale serve status --json 2>/dev/null)"; then
  echo "cannot read Tailscale Serve status" >&2
  exit 69
fi
if printf '%s' "$serve_json" | "$PYTHON" -c '
import json
import sys
sys.path.insert(0, sys.argv[1])
import tailscale_serve_policy
try:
    document = json.load(sys.stdin)
except ValueError:
    raise SystemExit(65)
errors = tailscale_serve_policy.validate_serve_status(document)
for error in errors:
    print(error, file=sys.stderr)
raise SystemExit(1 if errors else 0)
' "$HERE"; then
  current_ok=1
else
  current_ok=0
fi

echo "== private Tailscale Serve =="
echo "handler : / -> http://127.0.0.1:18096 (HTTPS 443, tailnet only)"
if [ "$current_ok" -eq 1 ]; then
  echo "status  : already exactly the private 18096 handler"
else
  echo "status  : current Serve configuration differs"
fi
echo "funnel  : off"

if [ "$APPLY" -eq 0 ]; then
  echo "dry run; add --apply to write the configuration"
  exit 0
fi

if [ "$current_ok" -eq 0 ]; then
  if [ "$RESET_OTHER" -eq 0 ]; then
    echo "existing Serve handlers differ; pass --reset-other-serve to replace them" >&2
    exit 73
  fi
  tailscale serve reset
fi

tailscale serve --bg --https=443 http://127.0.0.1:18096

if ! serve_json="$(tailscale serve status --json 2>/dev/null)"; then
  echo "cannot verify Tailscale Serve status" >&2
  exit 69
fi
if ! printf '%s' "$serve_json" | "$PYTHON" -c '
import json
import sys
sys.path.insert(0, sys.argv[1])
import tailscale_serve_policy
try:
    document = json.load(sys.stdin)
except ValueError:
    raise SystemExit(65)
errors = tailscale_serve_policy.validate_serve_status(document)
for error in errors:
    print(error, file=sys.stderr)
raise SystemExit(1 if errors else 0)
' "$HERE"; then
  echo "Serve verification failed; expected only / -> http://127.0.0.1:18096" >&2
  exit 69
fi

# Tailscale Serve must listen only on the tailnet address.  A tailscaled listener
# on 0.0.0.0/:: would be public exposure, not Serve.
if ! ss -H -ltnp | awk '$4 ~ /:(80|443)$/ && /tailscaled/ {print $4}' \
  | while read -r address; do
      case "$address" in
        100.*:*|\[fd7a:*) ;;
        *) echo "tailscaled is listening on a non-tailnet address: $address" >&2
           exit 1 ;;
      esac
    done; then
  echo "tailscaled public listener check failed" >&2
  exit 1
fi

# The private display and backend never listen off loopback.
if ! ss -H -ltn | awk '$4 ~ /:(18096|8100|5901)$/ {print $4}' \
  | while read -r address; do
      case "$address" in
        127.0.0.1:*|\[::1\]:*) ;;
        *) echo "private port is not loopback: $address" >&2
           exit 1 ;;
      esac
    done; then
  echo "private loopback check failed" >&2
  exit 1
fi
if ! ss -H -ltn | awk '$4 ~ /:18096$/ {found=1} END {exit !found}'; then
  echo "18096 is not listening on loopback" >&2
  exit 69
fi

echo "verified: no Funnel, Serve only 18096, private ports loopback-only"
echo "note: the existing Caddy 443 share ingress is separate and unchanged"
