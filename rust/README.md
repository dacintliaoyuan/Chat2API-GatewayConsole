# Rust workspace（S0 骨架 + S1a/S1b 候选网关 + S3a broker 控制通道）

本目录是 Rust 重写的 workspace 根。S0 建立工程骨架、HTTP 测试 harness 与
临时数据目录；S1a 增加候选网关 crate（8003 只读面、分享 Key 门与请求拒绝），
S1b 增加 Chat 非流式/SSE 转发、提交门、超时重试、断开取消与分享账本；
S3a 增加 broker 控制通道的协议内核与 POSIX AF_UNIX 适配器。本目录不切换
任何生产端口。

## 结构

```text
rust/
├── Cargo.toml                 workspace 根
└── crates/
    ├── broker/                S3a broker 控制通道（帧、HMAC、replay、action、AF_UNIX 策略）
    ├── contract/              只读契约夹具的类型与加载器
    ├── gateway/               S1a/S1b 候选网关：只读面、Key 门、Chat 转发、SSE、账本
    └── test-harness/          临时 loopback HTTP server、临时数据目录
```


`tests/fixtures/rust_contract_rev1.json` 由
`tests/generate_rust_contract_fixture.py` 从 `docs/rust-contract.md` 与
当前 Python 常量生成。Rust 测试只反序列化该夹具，不手写端点、错误码、
SSE 事件名或超时预算。

`tests/fixtures/rust_broker_wire_rev1.json` 由
`tests/generate_rust_broker_fixture.py` 从 `share/broker_protocol.py` 生成；
Rust broker 测试只反序列化该夹具里的 canonical JSON、帧与 HMAC 向量，不用自己的
序列化器生成期望值。

后续切片（S1–S8）在 `crates/` 下增加实现 crate，并复用本目录的 harness。
验收记录见 [docs/rust-s0-acceptance.md](../docs/rust-s0-acceptance.md)、
[docs/rust-s1a-acceptance.md](../docs/rust-s1a-acceptance.md)、
[docs/rust-s1b-acceptance.md](../docs/rust-s1b-acceptance.md) 与
[docs/rust-s3a-acceptance.md](../docs/rust-s3a-acceptance.md)。

## 命令

```bash
python tests/generate_rust_contract_fixture.py
python tests/generate_rust_gateway_fixture.py
python tests/generate_rust_broker_fixture.py
cargo test --manifest-path rust/Cargo.toml
bash scripts/check-rust-s0.sh
bash scripts/check-rust-broker.sh
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
- `um-gateway` 的 Chat 转发只在临时 loopback 上游上测试；账本路径必须由
  `CHAT2API_USAGE_LOG` 或测试配置显式给出，未配置时不写任何账本。SSE 生产者
  在取消后由自己的 `finally` 关闭上游并释放 reader 槽；客户端断开不触发第二次
  状态行或把部分完成写成成功。
