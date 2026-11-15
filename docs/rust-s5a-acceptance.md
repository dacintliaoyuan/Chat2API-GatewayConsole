# Rust S5a 验收记录：8002 只读核心（#51）

- 状态：实现完成；候选进程只监听临时 loopback 端口，未切换生产 8002。
- 分支：`rust/51`；基线：`integration/rust-rewrite` @ `260f443`。
- 实现：`rust/crates/console`（crate `um-console`，二进制 `um-console-candidate`）。
- 契约来源：[docs/rust-contract.md](rust-contract.md) §6、[docs/rust-plan.md](rust-plan.md) S5。

## 1. 交付范围

| 验收条目 | 实现 | 定向测试 |
|---|---|---|
| AC1 六个只读端点形状与 Python 一致；缺失字段保持 `null`/省略 | `server.rs`、`summary.rs`、`actions.rs`、`operations.rs`、`runtime.rs` | `console_readonly.rs`、`runtime_facts.rs` |
| AC2 `/api/summary` 云端角色为 `cloud`，不读私人账本 | `summary.rs::build_cloud_summary` 只构造固定 payload，无账本路径/读取器 | `summary_is_cloud_role_and_does_not_read_a_private_ledger` |
| AC3 非 UM 读面 503 `capability_not_assembled`，不探测也不读取 | `server.rs` 的 `CLOUD_CAPABILITY_PREFIXES` 在路由与 body 读取之前返回 | `non_um_read_faces_are_capability_not_assembled_without_probing`、`capability_gate_precedes_the_request_body` |
| AC4 请求体/敏感头/未知字段/Content-Length 门与 Python 一致 | `body.rs`、`server.rs` | `request_boundary.rs` |
| AC5 读面不写 SQLite、账本、日志 | `operations.rs` 只读打开；无锁目录/账本/日志写入 | `readonly_side_effects.rs` |
| AC6 候选 8002 只监听临时 loopback 端口，不切生产 | `config.rs::validate_candidate`、`main.rs` | `candidate_process.rs` |

## 2. 实现要点

- 六个只读端点：
  - `GET /health` -> `{status, service, port}`；`port` 来自 `USAGESRV_PORT`（默认 8002）。
  - `GET /api/summary` -> Python `_build_cloud_summary` 的字段
    `generatedAt`、`role`、`assembly`、`services`、`dsh`、`dshMergedIntoTotal`，
    再加 `_generated_epoch`、`_cached_age_seconds`；无 `sources` 字段，不读私人账本。
  - `GET /api/console/services` -> `ok`、`services[]`、`meta`；第一次 `meta.cached=false`，
    2 秒内第二次 `meta.cached=true` 且带 `cache_age_seconds`。
  - `GET /api/console/health` -> `checked_at`、`cached`、`services[]`（含
    `usagesrv` 自身）。
  - `GET /api/console/actions` -> 云端 registry 的六个动作；`service.*`、
    `share.diagnostics`、`share.gateway_restart`、`share.status`。
  - `GET /api/console/operations` 与 `/operations/{id}` -> 只读
    `console-state.sqlite3`；库不存在时返回空列表，不创建库。
- `runtime.rs` 只读 pid 文件、`/proc/<pid>/cmdline` 与 `/proc/<pid>` uid；
  状态映射 `running`/`stopped`/`unknown`/`conflict` 与 Python 一致，
  不发送信号、不写 pid/状态/投影。
- `body.rs` 保留 Python 的 400/413 码：重复敏感头、非法/重复
  Content-Length、Transfer/Content-Encoding、Content-Type、非法 JSON、
  非对象、`null` 视作空对象、未知字段排序列表。
- `um_gateway::http::read_request_with_body_cap` 在读取请求体之前用
  `Content-Length` 上限拒绝，避免声明超限但未发送 body 时等待；8003 继续用
  1 GiB 的既有上限。
- `config.rs` 严格解析角色与端口；候选只装配 cloud 角色，拒绝 local、
  非 loopback host、8002/8003/其他治理段端口，绑定端口必须为 0。
- `operations.rs` 用 `SQLITE_OPEN_READ_ONLY` 打开调用方给定的那一个
  `console-state.sqlite3`；schema 版本/外来表不可读时 503，绝不创建或迁移。

## 3. 门禁结果（2026-11-15）

已运行：

```bash
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets --offline
cargo test --manifest-path rust/Cargo.toml -p um-console --offline
cargo test --manifest-path rust/Cargo.toml --offline
python -m unittest tests.test_rust_console_fixture
python -m unittest tests.test_console_api tests.test_console_stats \
  tests.test_console_log_reader tests.test_console_public_probe \
  tests.test_gateway_connection_info tests.test_um_stats_read_surface
cd web && npm run check:cloud-console
bash scripts/check.sh --full
git -c core.whitespace=cr-at-eol diff --check
```

实际结果：

- `cargo fmt --check`：退出码 0。
- `cargo clippy --all-targets --offline`：退出码 0，无 warning。
- `cargo test -p um-console --offline`：
  - `console_readonly`：18 passed；
  - `readonly_side_effects`：4 passed；
  - `request_boundary`：5 passed（含“声明超限在读取前拒绝”）；
  - `runtime_facts`：8 passed；
  - doc-tests 0。
- `cargo test --offline`（整个 Rust workspace）：所有 `test result: ok`，无 FAILED。
- `python -m unittest tests.test_rust_console_fixture`：Ran 2 tests，OK；夹具由
  `tests/generate_rust_console_fixture.py` 从真实 Python 云端 `usagesrv.Handler`
  生成，freshness 检查通过。
- `python -m unittest tests.test_console_api tests.test_console_stats
  tests.test_console_log_reader tests.test_console_public_probe
  tests.test_gateway_connection_info tests.test_um_stats_read_surface`：
  Ran 266 tests，OK。
- `cd web && npm run check:cloud-console`：71 pass，0 fail。
- `bash scripts/check.sh --full`：退出码 0；尾部 `local/tests` 为
  `Ran 54 tests ... OK`。
- `git -c core.whitespace=cr-at-eol diff --check`：退出码 0。

## 4. 夹具

`tests/fixtures/rust_console_rev1.json` 记录：

- `/health` 的精确 body 与 `Cache-Control: no-store`；
- `/api/summary` 的 shape、`role=cloud`、`assembly`、`services={}`、
  `dsh`/`dshMergedIntoTotal` 的 `null` 字段与 cache 字段类型；
- `/api/console/services` 的 shape、稳定服务行、`meta.source` 与两次请求的
  `cached` 变化；
- `/api/console/health` 的 shape、稳定服务行与 `usagesrv` 自身行；
- `/api/console/actions` 的完整云端动作目录；
- `/api/console/operations` 的空列表、预置行、limit 边界与未知 id；
- 9 个云端 `capability_not_assembled` 前缀；
- 重复敏感头、Content-Length、编码、Content-Type、非法 JSON、
  非对象、`null`、未知字段、空 POST、伪造 Host/Origin 与
  `/gw/usagesrv` 别名。

Rust 测试只读该夹具，不手写 Python 期望值。

## 5. 边界与未验证范围

- S5a 只装配云端角色；`UM_SHARE_ROLE=local` 在 bind 前被拒绝。
  Python 本地 8001 角色不在本切片范围。
- `/api/console/services` 与 `/api/console/health` 不装配 Python 的
  `console.OperationManager`（因此不创建 `console-state.sqlite3` 或锁目录）；
  在库缺失/损坏时 Python 会 503，而 Rust 仍返回 pid 文件事实。S5a 的
  “读面不写 SQLite” 优先于该装配差异。
- `/api/console/operations` 的提交（POST）留给 S6；S5a 在请求体/未知字段
  门之后返回 `501 not_implemented`，不冒充 Python 的写入结果。
- Tailscale Serve 身份边界未在 S5a 装配；候选只实现 loopback Host/Origin 门。
  远程控制台访问仍由 Python 8002 承担。
- 未在真实 Linux `/proc`、真实 pid 文件或真实 8002 进程上验证；pid-file
  测试使用临时 `/proc` 树与注入的 liveness 探针。
- 未在云端切换 8002、未重启 broker、未执行真实部署或 UM 登录。
- `/api/console/services` 的 `display_name` 沿用 Python 当前冻结文本
  `UM 分享入口 8100`；`port` 来自 `UM_SHARE_GATEWAY_PORT`（夹具中为 8003）。
