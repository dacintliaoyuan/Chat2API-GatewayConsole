# 设计参考

- **Magpie 参考界面在本机 App 里**，不提交截图到仓库：截图里有账号、项目和任务数据。需要对比时在本机看，或用 `output/record-screen.ps1` 重新录。
- **静态截图**：`output/capture-screen.ps1 -Out <path>`。
- **动效**：`output/record-screen.ps1 -Seconds 4` 录 4 秒，再切页、分段和侧栏；Read 工具可直接看视频。
- **当前 UI 截图**：`output/impeccable-ui-review/*.png`（gitignored，不作为提交证据）。
- **设计评审**：用 `impeccable` 的 `critique`；`impeccable detect` 只覆盖 web，Slint 界面以截图和 `desktop/tests/layout.rs` 为准。
