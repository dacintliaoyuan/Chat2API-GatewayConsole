# um-desktop

本机控制台的独立 Slint crate（二进制 `um-desktop`），不在 `rust/` workspace 内，也不由 8000/8001 启动器拉起。

## 开发与验证

```bash
cargo test --manifest-path desktop/Cargo.toml
```

`desktop/tests/layout.rs` 和 `desktop/tests/usage_ui.rs` 用 `i-slint-backend-testing` 的 headless 后端检查布局、键盘和图表交互。

## UI 迭代

- 优先用 `output/impeccable-ui-review` 的预览 crate；把 `desktop/ui/*.slint` 同步到它的 `ui/` 后构建，渲染截图。
- 要构建真实 App 时用独立 target，避免锁住运行中的 `desktop/target/debug/um-desktop.exe`：

  ```bash
  CARGO_TARGET_DIR=desktop/target/preview cargo build --manifest-path desktop/Cargo.toml
  desktop/target/preview/debug/um-desktop.exe
  ```

- 需要停 App 时只停本轮记录的 App PID，以及它的子网关；不要按进程名停 `um-gateway-candidate`。详见 [docs/agents/desktop-preview.md](../docs/agents/desktop-preview.md)。

## 图标

图标来自第三方 Lucide v0.468.0（ISC），SVG 在 `ui/icons/`，许可证在 `ui/icons/LICENSE`。`ui/icons.slint` 只做 kind 到 SVG 的映射和着色。
