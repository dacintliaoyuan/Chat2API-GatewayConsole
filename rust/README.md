# Rust workspace（S0 骨架）

本目录是 Rust 重写的 workspace 根。S0 建立工程骨架、HTTP 测试 harness 与
临时数据目录；S1a 增加候选网关 crate（8003 只读面、分享 Key 门与请求拒绝），
仍不切换生产端口。

## 结构

```text
rust/
├── Cargo.toml                 workspace 根
└── crates/
    ├── contract/              只读契约夹具的类型与加载器
    ├── gateway/               S1a 候选网关：只读面、Key 门、请求拒绝
    └── test-harness/          临时 loopback HTTP server、临时数据目录
```

`tests/fixtures/rust_contract_rev1.json` 由
`tests/generate_rust_contract_fixture.py` 从 `docs/rust-contract.md` 与
当前 Python 常量生成。Rust 测试只反序列化该夹具，不手写端点、错误码、
SSE 事件名或超时预算。

后续切片（S1–S8）在 `crates/` 下增加实现 crate，并复用本目录的 harness。
验收记录见 [docs/rust-s0-acceptance.md](../docs/rust-s0-acceptance.md) 与
[docs/rust-s1a-acceptance.md](../docs/rust-s1a-acceptance.md)。

## 命令

```bash
python tests/generate_rust_contract_fixture.py
python tests/generate_rust_gateway_fixture.py
cargo test --manifest-path rust/Cargo.toml
bash scripts/check-rust-s0.sh
```

## 边界

- `TestServer` 默认绑定 `127.0.0.1:0`，由内核分配临时端口；只有显式传入
  非 loopback host 或治理段生产端口时才会失败，且失败发生在 bind 之前。
- `TempDataDir` 只在 OS 临时目录下创建自己的目录，拒绝绝对路径和 `..`
  穿越，`close()`/drop 时删除；不会读写 `data/`、`profile/`、`tmp/`、
  `output/`、`/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share`。
- 本目录不监听 `AGENTS.md` 端口表里的生产端口，也不启动 `chat2api.py`、
  broker 或 display relay。
