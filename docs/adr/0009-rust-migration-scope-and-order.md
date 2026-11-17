# Rust 迁移范围：常驻服务、顺序与回滚

8003 已是 Rust，8002 与本机 8000/8001 仍是 Python；仓库里已有 `um-console`（cloud role 的读写面）、`um-broker` 与 `um-store` 纯库，但 `um-console` 被 candidate 模式锁在临时端口，`um-broker` 没有任何生产调用方，本机没有发布机制、回滚只能改启动命令。我们决定：迁移只覆盖**常驻服务**，判据是**能删掉一整份实现**而不是语言纯度；范围是云端 8002 与本机 8000/8001，broker 与 `model_sync` 各另开 spec；顺序是 8002 → 本机 8000 → 本机 8001，每一步独立切流与回滚；8002 给 `um-console` 加对称的 `--mode candidate|production`，沿用 `um-share-console.service`（Rust 8002）与已存在的 `um-share-console-python.service`（Python 8096）作回滚；本机 8000 由 Rust gateway 承担，补通用 `/api/*` 透传且只在 local 角色启用，现有 `start_chat2api.bat`/`lib/gateway-runtime.ps1` 只改 `StartPath`；本机 8001 在 `um-console` 加 `Role::Local` 装配并托管 `web/dist`，鉴权按生产现状（console 面免凭据 + Host/Origin/代理元数据边界，`usage_session` 只用于 `/api/session`）；云端保留一个 release 的回滚窗口，本机回滚点是不删除的 Python 实现与原启动命令。

**Status**: accepted

## Considered Options

- **按语言纯度迁全部自研程序（含编排脚本、display 辅助、broker）**：编排脚本迁 Rust 后仍要 shell 包装，失去可读性与即改即生效；broker 要先收敛三份协议实现，是独立的一大块；放弃。
- **只迁云端 8002，不迁本机**：本机 8000/8001 仍两套实现并存，判据未达成；放弃。
- **另开 `um-local-console` 二进制承载本机 8001**：`Role` 枚举与 `body`/`error`/`lock`/`operations`/`display*` 已角色无关，另开二进制会复制整个 server 层；放弃。
- **本机 8001 按 `local/backend/app.py` 的候选设计迁移**：生产 8001 是 `usagesrv.py` 的 local 角色，候选组合根的 `um_session` cookie 与控制令牌未上线，按它迁会打断 `scripts/Open-GatewayConsole.bat`；放弃。
- **本次范围：8002 + 本机 8000/8001；broker 与 `model_sync` 另开 spec**：采用。

## Consequences

- 8002 的切流复用 `cutover_gate.py`，新增 console parity 步骤；`all` 在任一步失败时返回非零。
- 本机 8000 的通用 `/api/*` 透传只在 local 角色启用；云端 8003 保持 501 `local_api_surface_s1a`。
- 本机 8000 的回滚是把 `StartPath` 改回 `chat2api.py`，不需要发布目录；云端 8002 的回滚是切到 `um-share-console-python.service`（8096）。
- 本机 8001 的迁移目标是 `usagesrv.py` 的 local 角色，不是未上线的 `local/backend/app.py`；local 专属的 `summary`、`um`、`services`、`gateway-connection`、`stats`/`log-*` 与静态页要新装配，`session`/`share_read`/`share_write`/`metering`/`cloud_ops` 可复用于本机的分享管理面。
- `model_sync` 迁 Rust 前，本机 8001 的 `model.catalog` 动作按需调用 Python `model_sync`，不是常驻进程。
- `um-broker` 与 gateway 的 `instance.rs` 是重复实现；broker 票必须先决定合并到哪一份，再启用。
- 每步切流后同步 `AGENTS.md` 端口表与 `docs/rust-contract.md`；未切流的组件仍按当前运行时记录。
