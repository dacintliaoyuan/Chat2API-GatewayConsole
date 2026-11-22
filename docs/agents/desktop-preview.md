# 桌面 App 构建、预览与安全重启

`desktop/` 是独立的 Slint crate（`um-desktop`），它不随 `rust/` workspace 构建，也不由 8000/8001 启动器拉起。本文只写本机 UI 迭代和重启的安全边界。

## 迭代顺序

1. **只改 UI 时用 preview**：`output/impeccable-ui-review` 是同一份 UI 的预览 crate（主工作区保留，删 worktree 前先确认它不在里面）。把 `desktop/ui/*.slint` 同步到它的 `ui/`，构建它自己的 target，渲染截图；不要停真实 App，也不要动 8000/8001。夹具的合成数据必须按产品实际格式生成（千分位/亿、本页口径），否则评审会把夹具文案当成产品缺陷。
2. **要真实 App 截图时用 smoke**：`python output/desktop-sse-smoke.py` 用隔离的 `LOCALAPPDATA` 和随机端口启动 App，结束时只停本轮启动的 App 和它的子网关。
3. **要重建 `desktop/target/debug` 时用独立 target**：

   ```bash
   CARGO_TARGET_DIR=desktop/target/preview cargo build --manifest-path desktop/Cargo.toml
   desktop/target/preview/debug/um-desktop.exe
   ```

   运行中的 `desktop/target/debug/um-desktop.exe` 会被 Windows 锁住；独立 target 不碰它，因此不需要停 App。

4. **桌面测试用 `scripts/check-desktop.sh`**：`CARGO_INCREMENTAL=0 cargo test -j 2 --manifest-path desktop/Cargo.toml`。默认并行度会在这台机器上触发 `link.exe` 1102 与 rustc ICE；`scripts/check.sh --desktop` 和 `--full` 都走这个脚本。

## 安全停止

App 的 7999 网关是 App 的子进程；生产 8000 入口是另一个进程，只是共用 `um-gateway-candidate.exe` 这个名字。停止时必须同时匹配：

- 父 PID 是本轮 App 的 PID；
- 可执行文件路径是本轮启动的 `um-gateway-candidate.exe`；
- 进程创建时间不早于本轮 App 的启动时间。

`output/desktop-sse-smoke.py` 的 `stop_round_processes` 是模板：它先记录 App PID 和启动时间，再按上述三项筛选子网关，最后只终止这些进程。不要写 `Get-Process um-gateway-candidate | Stop-Process`、`Stop-Process -Name um-gateway-candidate` 或 `taskkill /IM um-gateway-candidate.exe`：这些会误伤 8000。

## 动效与视觉核对

- 静态截图：`output/capture-screen.ps1`。
- 动效：`output/record-screen.ps1 -Seconds 4` 录 4 秒，再切页/分段/侧栏；Read 工具可直接看视频。
- Magpie 参考界面在本机 App 里；不要把它的截图提交到仓库（含账号与项目数据）。需要对比时在本机看，或用 `output/record-screen.ps1` 重新录。
- 设计评审用 `impeccable` 的 `critique`；`impeccable detect` 只覆盖 web，Slint 界面以截图和 `desktop/tests/layout.rs` 为准。
