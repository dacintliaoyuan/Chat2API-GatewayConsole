# Rust workspace（S0 骨架 + S1a 候选网关 + S3a broker 控制通道 + S3b store/发布/启动身份）

本目录是 Rust 重写的 workspace 根。S0 建立工程骨架、HTTP 测试 harness 与
临时数据目录；S1a 增加候选网关 crate（8003 只读面、分享 Key 门与请求拒绝），
S3a 增加 broker 控制通道的协议内核与 POSIX AF_UNIX 适配器；S3b 增加
share store（SQLite schema 3、Key 生命周期、安全日志）、投影原子写、
`publish` writer→load→ACK、有界 host 生命周期与 broker 的 8003 启动身份。
本目录不切换任何生产端口，Python 仍是回滚权威。

## 结构

```text
rust/
├── Cargo.toml                 workspace 根
└── crates/
    ├── broker/                S3a 控制通道 + S3b publish/host 生命周期
    ├── contract/              只读契约夹具的类型与加载器 + canonical JSON
    ├── store/                 S3b share store、Key 生命周期与投影 writer
    ├── gateway/               S1a 候选网关：只读面、Key 门、请求拒绝
    └── test-harness/          临时 loopback HTTP server、临时数据目录
```

`tests/fixtures/rust_contract_rev1.json` 由
`tests/generate_rust_contract_fixture.py` 从 `docs/rust-contract.md` 与
当前 Python 常量生成。Rust 测试只反序列化该夹具，不手写端点、错误码、
SSE 事件名或超时预算。

`tests/fixtures/rust_broker_wire_rev1.json` 由
`tests/generate_rust_broker_fixture.py` 从 `share/broker_protocol.py` 生成；
`tests/fixtures/rust_store_rev1.json` 由
`tests/generate_rust_store_fixture.py` 从真实 Python `ShareStore`、
`BrokerHostCoordinator`、`parse_projection_payload` 与 `secret_protection` 生成。
Rust 测试只反序列化夹具，不用自己的序列化器生成期望值。

后续切片（S1–S8）在 `crates/` 下增加实现 crate，并复用本目录的 harness。
验收记录见 [docs/rust-s0-acceptance.md](../docs/rust-s0-acceptance.md)、
[docs/rust-s1a-acceptance.md](../docs/rust-s1a-acceptance.md)、
[docs/rust-s3a-acceptance.md](../docs/rust-s3a-acceptance.md) 与
[docs/rust-s3b-acceptance.md](../docs/rust-s3b-acceptance.md)。

## 命令

```bash
python tests/generate_rust_contract_fixture.py
python tests/generate_rust_gateway_fixture.py
python tests/generate_rust_broker_fixture.py
python tests/generate_rust_store_fixture.py
cargo test --manifest-path rust/Cargo.toml
bash scripts/check-rust-s0.sh
bash scripts/check-rust-broker.sh
bash scripts/check-rust-s3b.sh
```

`cargo check --manifest-path rust/Cargo.toml -p um-broker --target \
aarch64-linux-android --tests` 只做 `cfg(unix)` 代码的类型检查；AF_UNIX、
`SO_PEERCRED` 与真实权限位测试需要在 Linux/WSL 上运行。

## 边界

- `TestServer` 默认绑定 `127.0.0.1:0`，由内核分配临时端口；只有显式传入
  非 loopback host 或治理段生产端口时才会失败，且失败发生在 bind 之前。
- `TempDataDir` 只在 OS 临时目录下创建自己的目录，拒绝绝对路径和 `..`
  穿越，`close()`/drop 时删除；不会读写 `data/`、`profile/`、`tmp/`、
  `output/`、`/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share`。
- 本目录不监听 `AGENTS.md` 端口表里的生产端口，也不启动 `chat2api.py`、
  broker 或 display relay。
- `um-broker` 不 spawn 进程、不执行 shell、不接受任意路径/服务名；动作由固定
  `Action` 枚举承载，字段由固定白名单校验。
- `um-store` 只打开调用方给定的那一个 SQLite 文件（`schema.rs` 是唯一
  `Connection::open` 位置），不创建第二套库、不迁移数据；schema 3 与
  Python 逐列一致，投影 schema 2 与本地 DB marker 分开。
- 8003 启动身份由 broker 的 `--host-entry-kind` 选择：`python` 保持旧
  解释器+脚本身份与旧 unit 回滚路径，`native` 接受 Rust 网关二进制；
  两者共用同一个 broker、同一个 `LinuxHostOps` 和同一个 8003 启动者。
