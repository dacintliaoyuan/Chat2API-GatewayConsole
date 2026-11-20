# Windows 桌面原型对比：#109 Slint 与 Tauri 2

> 测量日期：2026-11-20。范围：状态总览 + 实时日志。两个原型都只读现有 8000/8001，不登录、不写账本、不改服务。
>
> 工作目录：`.worktrees/109-desktop-prototypes`；分支 `prototype/109-desktop`；原型在 `prototype/109-slint` 与 `prototype/109-tauri`。

## 环境

- Windows 10.0.26300 x64，32 个逻辑处理器
- Rust 1.100.0；Node 26.13.1
- Slint 1.19.0；Tauri 2.12.7；WebView2（Tauri 运行时）
- 8000 `/health` 200；8001 `/health` 200；测量时 8000 认证有效

## 构建

```bash
cd prototype/109-slint && cargo build --release
cd prototype/109-tauri && npm run build
cd prototype/109-tauri && npm run tauri build -- --no-bundle
```

结果：

- Slint：`target/release/proto109-slint.exe`，11.43s
- Tauri：`src-tauri/target/release/proto109-tauri.exe`，1m08s

两个原型都能启动并显示状态卡和日志列表；关闭窗口后隐藏到托盘，托盘恢复后重新显示。

## 冷启动与托盘恢复

用 `PROTO109_AUTO_HIDE_MS` / `PROTO109_AUTO_RESTORE_MS` 自动触发托盘隐藏与恢复，指标写入 `PROTO109_METRICS`。

| 指标 | Slint | Tauri 2 |
|---|---:|---:|
| `process_start` → `ui_ready` / `ui_interactive` | **151 ms** | **1,230 ms** |
| `process_start` → `window_shown` / `webview_ready` | 87 ms | 1,105 ms |
| 托盘隐藏 | 899 ms | 2,058 ms |
| 托盘点击 → 窗口可见 | **51 ms** | **68 ms** |
| 托盘点击 → 恢复事件 | 20 ms | 1 ms |

Slint 的冷启动明显更快；托盘恢复两者都在 100 ms 内，Slint 略快。

## 空闲资源

关闭到托盘后，`measure-idle.ps1` 采样 10 秒，计入子进程。

| 指标 | Slint | Tauri 2 |
|---|---:|---:|
| 进程数 | 1 | 6（含 6 个 `msedgewebview2`） |
| 空闲 CPU（单核百分比） | **0.312%** | **6.404%** |
| 空闲 CPU（全部核心百分比） | 0.01% | 0.20% |
| Working Set | **33.16 MB** | **369.88 MB** |
| Private | **11.33 MB** | **206.50 MB** |
| WebView2 Working Set | 0 | **341.01 MB** |
| WebView2 Private | 0 | 199.73 MB |

Tauri 的空闲内存主要来自 WebView2：6 个 `msedgewebview2` 进程合计 341 MB。Slint 单进程 33 MB。

## 实时日志延迟

**未完成。** 需要把原型指向隔离的候选 8001，并用 `inject-log.mjs` 写入带 `id=` / `t=` 的标记行，再用 `summarize.mjs` 配对 `log_visible_frame` 或 `log_visible`。

本次只验证了：
- 两个原型都能读取 8001 的日志接口并渲染；
- 关闭到托盘后轮询暂停，恢复后继续；
- 没有对生产 `chat2api/chat2api.log` 注入测试行。

未测原因：后台 agent 在写报告前失败；主线程补了构建和空闲/托盘测量，但没有启动隔离候选 8001 做日志注入。这个缺口不改变资源结论，但日志延迟的 p95 仍未知。

## 结论

对「托盘驻留的本机控制台」这个目标，**Slint 更合适**：

- 冷启动 151 ms vs 1.23 s；
- 托盘恢复 51 ms vs 68 ms；
- 空闲 Working Set 33 MB vs 370 MB；
- 空闲 CPU 0.31% vs 6.4% 单核；
- 单进程，不依赖 WebView2。

Tauri 2 的优势是复用现有 TypeScript 页面和 Web 技术栈；如果后续把桌面版定位为「复用现有前端」，Tauri 仍值得保留。当前 #109 的目标是状态总览 + 实时日志，Slint 的资源与启动优势更符合托盘常驻。

## 选型决定（2026-11-20）

采用 **Slint**。Tauri 2 原型保留在 `prototype/109-tauri` 作为对照，不进入生产；不把 WebView2 作为桌面版运行时依赖。实施规格另开票，复用 `prototype/109-slint` 的状态卡、托盘和日志列表，但把原型轮询改成正式的本机接口读取、断线补读、单实例和安装包。

## 未验证

- 实时日志 p50/p95 延迟未测。
- 只做了一次 10 秒空闲采样，不是长时间驻留统计。
- 未测睡眠唤醒、主题切换、DPI 缩放、多显示器。
- 未接真实 8000/8001 故障、断线补读和登录事件；原型只读。
- 未打包安装器（Tauri `bundle.active=false`，Slint 未做安装包）。
