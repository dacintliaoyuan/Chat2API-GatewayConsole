#!/usr/bin/env bash
# Build a self-contained UM cloud release archive.
#
# The release closure is FROZEN from the current source tree:
#   * static superset: every product Python module reachable from the declared
#     entry points (including Windows-only lazy branches);
#   * cloud runtime: the narrower modules the cloud role actually imports;
#   * deployment files: this directory (scripts, units, Caddyfile, lock file);
#   * the built console tree: NOT built here.  The caller passes the
#     production cloud build (web/dist-cloud, built with
#     `CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud npm run build`) or
#     explicitly --allow-missing-web.  A candidate tree (release/candidate/cloud)
#     is a read-only 8099 preview and is refused: usagesrv.py answers 500 for
#     any build whose line is not production.  The whole tree is carried, never
#     an allowlist of a few files, and --web-role refuses a tree built for
#     another console role.
#
# This script never writes a production dist and never touches a deployed release.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_ROOT="$SCRIPT_DIR"
# shellcheck source=lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"

VERSION=""
OUTPUT_DIR=""
WEB_DIST=""
WEB_ROLE="cloud"
ALLOW_MISSING_WEB=0
WHEELHOUSE=""
PYTHON="${PYTHON:-python3}"

usage() {
  cat <<'EOF'
Usage: package-release.sh --version VERSION [options]

Options:
  --version VERSION      release identifier (required; [A-Za-z0-9._-])
  --source PATH          source root (default: ai-gateway)
  --output PATH          output directory (default: <source>/output/um-share-release)
  --web-dist PATH        production cloud console tree to carry (web/dist-cloud)
  --web-role ROLE        console role the carried tree must be built as
                         (default: cloud; local|cloud)
  --allow-missing-web    package without a console tree (console root will answer 500)
  --wheelhouse PATH      optional offline wheelhouse copied into the payload
  -h, --help              show this help

Exit codes: 0 success; 64 usage; 65 manifest/data; 66 missing input;
70 software; 71 command missing; 72 file; 73 cannot create; 74 I/O.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; VERSION="$2"; shift 2 ;;
    --source) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; SOURCE="$(cd "$2" && pwd)"; shift 2 ;;
    --output) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; OUTPUT_DIR="$2"; shift 2 ;;
    --web-dist) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; WEB_DIST="$(cd "$2" && pwd)"; shift 2 ;;
    --web-role) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; WEB_ROLE="$2"; shift 2 ;;
    --allow-missing-web) ALLOW_MISSING_WEB=1; shift ;;
    --wheelhouse) [ "$#" -ge 2 ] || { usage >&2; exit "$EX_USAGE"; }; WHEELHOUSE="$(cd "$2" && pwd)"; shift 2 ;;
    -h|--help) usage; exit "$EX_OK" ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit "$EX_USAGE" ;;
  esac
done

[ -n "$VERSION" ] || { usage >&2; exit "$EX_USAGE"; }
case "$VERSION" in
  *[!A-Za-z0-9._-]*) echo "unsafe version: $VERSION" >&2; exit "$EX_USAGE" ;;
esac
case "$WEB_ROLE" in
  local|cloud) ;;
  *) echo "unsafe web role: $WEB_ROLE (local|cloud)" >&2; exit "$EX_USAGE" ;;
esac
[ -f "$SOURCE/usagesrv.py" ] || die "source root lacks usagesrv.py: $SOURCE" "$EX_NOINPUT"
[ -f "$SOURCE/chat2api/share_gateway.py" ] || \
  die "source root lacks chat2api/share_gateway.py" "$EX_NOINPUT"
[ -f "$SOURCE/chat2api/chat2api.py" ] || \
  die "source root lacks chat2api/chat2api.py" "$EX_NOINPUT"
[ -f "$DEPLOY_ROOT/make-manifest.py" ] || \
  die "make-manifest.py missing: $DEPLOY_ROOT" "$EX_NOINPUT"
[ -f "$DEPLOY_ROOT/dependencies.lock.json" ] || \
  die "dependencies.lock.json missing: $DEPLOY_ROOT" "$EX_NOINPUT"
command -v "$PYTHON" >/dev/null 2>&1 || \
  die "python interpreter not found: $PYTHON" "$EX_OSERR"
command -v tar >/dev/null 2>&1 || die "tar not found" "$EX_OSERR"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum not found" "$EX_OSERR"

if [ -z "$WEB_DIST" ] && [ "$ALLOW_MISSING_WEB" -eq 0 ]; then
  die "console tree is not built: pass --web-dist or --allow-missing-web" "$EX_NOINPUT"
fi
if [ -n "$WEB_DIST" ]; then
  [ -f "$WEB_DIST/index.html" ] || \
    die "console tree is incomplete: $WEB_DIST/index.html missing" "$EX_NOINPUT"
  [ -f "$WEB_DIST/candidate-build.json" ] || \
    die "console tree has no build identity (candidate-build.json): $WEB_DIST" "$EX_NOINPUT"
fi

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
STAGE="$(mktemp -d "$OUTPUT_DIR/.um-share-stage.XXXXXX")"
PAYLOAD="$STAGE/um-share-$VERSION"
cleanup() {
  if [ -n "${STAGE:-}" ] && [ -d "$STAGE" ]; then
    rm -rf -- "$STAGE"
  fi
}
trap cleanup EXIT
mkdir -p "$PAYLOAD"

# The manifest is generated from the SOURCE first, so its file list is the
# closure authority.  It is regenerated inside the payload afterwards.
MANIFEST_SOURCE="$STAGE/manifest-source.json"
WEB_ARGS=()
if [ -n "$WEB_DIST" ]; then
  WEB_ARGS=(--web-dist "$WEB_DIST" --require-web-role "$WEB_ROLE" \
    --require-web-line production)
else
  WEB_ARGS=(--allow-missing-web)
fi
"$PYTHON" "$DEPLOY_ROOT/make-manifest.py" \
  --root "$SOURCE" \
  --deploy-root "$DEPLOY_ROOT" \
  --version "$VERSION" \
  --dependencies "$DEPLOY_ROOT/dependencies.lock.json" \
  --output "$MANIFEST_SOURCE" \
  "${WEB_ARGS[@]}" || die "manifest generation failed" "$EX_DATAERR"

"$PYTHON" - "$MANIFEST_SOURCE" "$SOURCE" "$PAYLOAD" "${WEB_DIST:--}" <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
source = Path(sys.argv[2]).resolve()
payload = Path(sys.argv[3]).resolve()
web_arg = sys.argv[4]
web_dist = None if web_arg == "-" else Path(web_arg)
data = json.loads(manifest_path.read_text(encoding="utf-8"))
source = source.resolve()
payload = payload.resolve()

def copy_entry(entry):
    rel = entry["path"]
    src = (source / rel).resolve()
    try:
        src.relative_to(source)
    except ValueError:
        raise SystemExit(f"path escapes the source root: {rel}")
    if not src.is_file():
        raise SystemExit(f"closure file missing: {rel}")
    dst = payload / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

for entry in data.get("files") or []:
    copy_entry(entry)
for entry in data.get("deployment_files") or []:
    copy_entry(entry)

if web_dist is not None:
    dst = payload / "web" / "dist"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(web_dist, dst)
    if not (dst / "index.html").is_file():
        raise SystemExit("web/dist/index.html missing after copy")
PY

if [ -n "$WHEELHOUSE" ]; then
  [ -d "$WHEELHOUSE" ] || die "wheelhouse not found: $WHEELHOUSE" "$EX_NOINPUT"
  cp -a "$WHEELHOUSE" "$PAYLOAD/um-cloud-deploy/wheelhouse"
fi

# A Windows checkout checks out shell scripts with CRLF, and the packaged bytes
# then carry a trailing \r on every line.  On the cloud host that breaks the
# shebang (`env: 'bash\r': No such file or directory`) and, worse, `sed`/`grep`
# patterns that end in `$`.  Normalize the PAYLOAD to LF BEFORE the manifest is
# generated so the recorded hashes describe exactly the bytes the cloud extracts.
# Only text shells are rewritten: every other file keeps its bytes untouched.
"$PYTHON" - "$PAYLOAD" <<'PY' || die "line-ending normalization failed" "$EX_IOERR"
import sys
from pathlib import Path

payload = Path(sys.argv[1])
rewritten = []
for path in sorted(payload.rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    if path.suffix not in (".sh", ".bash") and path.name not in (
            "configure-tailscale-serve.sh", "fluxbox-init"):
        continue
    data = path.read_bytes()
    if b"\r\n" not in data:
        continue
    path.write_bytes(data.replace(b"\r\n", b"\n"))
    rewritten.append(str(path.relative_to(payload)))
print("normalized %d shell file(s) to LF" % len(rewritten))
PY

# Final manifest: hashes the PAYLOAD, not the source.  The deployment files
# are already copied, so their hashes are the packaged bytes.
"$PYTHON" "$PAYLOAD/um-cloud-deploy/make-manifest.py" \
  --root "$PAYLOAD" \
  --deploy-root "$PAYLOAD/um-cloud-deploy" \
  --version "$VERSION" \
  --dependencies "$PAYLOAD/um-cloud-deploy/dependencies.lock.json" \
  --output "$PAYLOAD/RELEASE-MANIFEST.json" \
  "${WEB_ARGS[@]}" || die "payload manifest generation failed" "$EX_DATAERR"

verify_release_manifest "$PAYLOAD" "$PAYLOAD/RELEASE-MANIFEST.json" || \
  die "payload manifest verification failed" "$EX_DATAERR"

# The archive is built on a Windows checkout too, where executable mode bits are
# not meaningful.  Mark the shell entries and the bin helper executable so the
# Linux extraction keeps systemd ExecStart/ExecStop working.
find "$PAYLOAD/um-cloud-deploy" -type f \
  \( -name '*.sh' -o -path '*/bin/*.py' \) -exec chmod 0755 {} +
# The operator runs `console_magpie_prices.py --sync` directly (README); keep the
# root-level synchronizer executable in the extracted release.
chmod 0755 "$PAYLOAD/console_magpie_prices.py"

if command -v pwsh >/dev/null 2>&1 && [ -f "$PAYLOAD/um-cloud-deploy/verify-release.ps1" ]; then
  pwsh -NoProfile -NonInteractive -File \
    "$PAYLOAD/um-cloud-deploy/verify-release.ps1" -PackageRoot "$PAYLOAD" || \
    die "pwsh release verification failed" "$EX_DATAERR"
fi

ARCHIVE="$OUTPUT_DIR/um-share-$VERSION.tar.gz"
tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 \
  --numeric-owner -C "$STAGE" -czf "$ARCHIVE" "um-share-$VERSION" || \
  die "tar failed" "$EX_IOERR"
# The sidecar must stay machine-independent: install.sh reads the first field,
# while an operator on another host runs `sha256sum -c` from the directory that
# holds both the archive and this file.
(cd "$OUTPUT_DIR" && sha256sum "$(basename "$ARCHIVE")") > "$ARCHIVE.sha256"
log "release archive: $ARCHIVE"
log "release sha256: $(awk '{print $1}' "$ARCHIVE.sha256")"
log "web.dist.included=$(python3 - "$PAYLOAD/RELEASE-MANIFEST.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["web_dist"]["included"])
PY
)"
exit "$EX_OK"
