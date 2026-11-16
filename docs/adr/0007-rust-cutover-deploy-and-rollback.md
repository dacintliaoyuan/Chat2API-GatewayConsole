# Rust 切流、部署脚本与回滚边界

Rust 8003/8002 的生产切换需要一套与 Python 不同的部署脚本，但 `um-cloud-deploy` 的 install/update/rollback、unit、env、健康门、ingress checker、端口审计和文档仍全部指向 Python 8100/8096/18096；当前 release `s2-20261113b` 的 unit 模板缺 `/var/lib/um-share-console` 的 `ReadWritePaths`，`update.sh`/`rollback.sh` 健康门硬编码 8100，上一 release `s2-20261110f` 的 broker unit 期望 `share_gateway.py`，不能作为本次 Rust 切换的直接回滚目标。我们决定：更新 `um-cloud-deploy` 支持 Rust 二进制打包、安装、unit、env、健康门和端口审计；切流前先做一个带 `ReadWritePaths` 修复的 Python release，并让 update/rollback 的健康门参数化到目标端口，回滚目标是这个新 Python release；8003 先切，8002 后切；Cloudflare ingress 从 8100 改到 8003 由维护者通过 dashboard/API 完成，agent 只做切换后的公网验证；agent 执行代码、构建、安装、服务切换、回滚演练和验证，真实 UM 登录/MFA 由维护者完成。

**Status**: accepted

## Considered Options

- **单独写一套 Rust deploy 脚本，不改 Python 脚本**：两套脚本并存，回滚路径分叉；放弃。
- **先手工部署，脚本后续补**：切流无法重复验证；放弃。
- **更新 `um-cloud-deploy` 支持 Rust + 参数化健康门 + 带 ReadWritePaths 的 Python 回滚 release**：采用。

## Consequences

- `um-cloud-deploy` 的 install/update/rollback、unit、env、健康门、ingress checker、端口审计和文档都要同时支持 8003/8002 与回滚 Python release。
- 回滚 release 必须带 `/var/lib/um-share-console` 的 `ReadWritePaths`；`update.sh`/`rollback.sh` 的健康门必须参数化到目标端口。
- Cloudflare ingress 的 8003 变更是 human-only；agent 只验证公网 `/v1/models`、`/health`、未知路径。
- 切流顺序：8003 先，观察至少一次真实请求、一条账本行、一次 SSE、一次 Key 撤销，再切 8002。
