# Rust 8003 生产入口与 Rust 登录执行器

Rust 8003 目前只能以 candidate 模式绑定临时 loopback 端口，`--port 8003`、`--port 8100`、`--bind-port 8002` 都被拒绝；同时 Rust 网关没有生产登录，`/admin/auth/*` 是 501，默认 `reauth_unavailable`，停掉 Python 8100 后没人负责真实 UM 登录。我们决定：Rust 二进制增加 production 模式，允许在 systemd 下绑定 8003/8002 loopback；candidate 模式保留给 parity 测试；UM 登录由 Rust 8003 自己实现，包括 headless/headful 浏览器、profile 锁、token 存储、`/admin/auth/status|verify|login|cancel`、登录窗口关闭/取消和 `BrowserDisabled`；Python 8100 在 8003 切换后完全退役，不再保留登录辅助进程。Rust 8003 读写与 Python 相同的 `/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share`，不迁移数据；切流前做只读 parity 和一次真实账本追加测试。切流门禁是 candidate parity + 真实云端 smoke + 回滚演练；agent 执行代码、构建、安装、服务切换、回滚演练和验证，Cloudflare ingress 与真实 UM 登录/MFA 由维护者完成。先切 8003，观察至少一次真实请求、一条账本行、一次 SSE、一次 Key 撤销，再切 8002。

**Status**: accepted

## Considered Options

- **Python 8100 保留为登录辅助，Rust 8003 委托登录**：双进程、双端口，回滚路径更复杂；放弃。
- **Rust 8003 自己实现登录，Python 8100 完全退役**：采用。
- **暂不切 8003，继续 Python**：切流无限期阻塞；放弃。

## Consequences

- Rust 二进制需要 production/candidate 两种绑定模式；candidate 仍只允许临时 loopback 端口。
- Rust 需要完整移植登录：ADFS/Duo、headless-first、headful 可见窗口、profile 单实例锁、token 存储、登录窗口关闭/取消、`BrowserDisabled`。
- Python 8100 在切换后不再运行；回滚 release 仍保留 Python 代码。
- 8003 先切，8002 后切；8003 观察期至少一次真实请求 + 账本行 + SSE + Key 撤销。
