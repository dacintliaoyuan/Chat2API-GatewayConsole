#!/usr/bin/env bash
# #90: build the Windows offline Python rollback package from a fixed commit.
#
# The package is a *recovery* artifact, not the live implementation: it carries
# the Python 8000/8001 startup closure, the production local frontend and the
# pinned dependency list. Credentials, browser profiles, logs and runtime data stay
# in their original storage and are never copied here.
set -euo pipefail

source_dir="."
commit="HEAD"
output=""
while [ $# -gt 0 ]; do
  case "$1" in
    --source) source_dir="$2"; shift 2 ;;
    --commit) commit="$2"; shift 2 ;;
    --output) output="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

source_dir=$(cd "$source_dir" && pwd)
resolved=$(git -C "$source_dir" rev-parse --short "$commit^{commit}")
date_stamp=$(date +%Y%m%d)
output=${output:-"$source_dir/tmp/python-rollback-$date_stamp.tar.gz"}
mkdir -p "$(dirname "$output")"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The explicit Git file list: the Python startup closure plus the rollback unit
# and the pinned dependency list. The list is deliberately explicit so a later
# deletion of the closure is the same list, not a directory-wide guess.
files=(
  usagesrv.py
  login_contract.py
  check_port_bindings.py
  start_chat2api.bat
  start_usage_service.bat
  chat2api
  console
  share
  shared
  model_sync
  um-cloud-deploy/systemd/um-share-console-python.service
)
git -C "$source_dir" archive --format=tar --prefix=python-rollback/ "$commit" -- "${files[@]}" \
  | tar -x -C "$work"

# `requirements.lock` is ignored by the root `*.lock` rule, so it cannot ride the
# Git archive: copy the pinned list from the working tree and keep it beside the
# startup scripts.
mkdir -p "$work/python-rollback/um-cloud-deploy"
cp "$source_dir/um-cloud-deploy/requirements.lock" "$work/python-rollback/um-cloud-deploy/requirements.lock"

# The production local frontend is a build artifact (gitignored): copy it from
# the working tree and record that it is not from the Git commit.
if [ ! -d "$source_dir/web/dist" ]; then
  echo "web/dist is missing; run the production build first" >&2
  exit 1
fi
mkdir -p "$work/python-rollback/web"
cp -a "$source_dir/web/dist" "$work/python-rollback/web/dist"

cat > "$work/python-rollback/RECOVERY.md" <<EOF
# Python 回滚包（$date_stamp）

- 源码提交：\`$resolved\`（\`git archive\` 的显式文件清单）
- 用途：本机 8000/8001 的 Python 离线回滚闭包；不是常驻实现。
- 依赖：\`python -m pip install -r um-cloud-deploy/requirements.lock\`
  （Windows 上实际只需要 \`requests\`）。
- 启动：\`start_chat2api.bat\`（8000）与 \`start_usage_service.bat\`（8001）。
- 前端：\`web/dist\` 是生成物，来自打包机当时的本地生产构建，不来自上面的 Git 提交。
- 运行数据与凭据不在此包内：从原存储接入 \`data/\`、\`profile/\`、
  \`chat2api/token.json\` 与 \`chat2api/.edge-profile/\`。
- 回滚：把 \`lib/gateway-runtime.ps1\` 的 \`StartPath\` 改回两个 bat 并重启；
  不需要改端口，不需要重装。
- 云端回滚目标仍是上一套完整 Rust release；本包只覆盖本机 8000/8001。
EOF

tar -C "$work" -czf "$work/python-rollback.tar.gz" python-rollback
cp "$work/python-rollback.tar.gz" "$output"
echo "package $output"
echo "commit $resolved"
sha256sum "$output"
