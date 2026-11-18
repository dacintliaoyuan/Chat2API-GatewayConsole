# 本地控制台桌面版采用 Windows 托盘和现有前端

本机功能完善后，首版桌面应用优先支持 Windows，在用户登录 Windows 后运行，可选择随登录启动。关闭主窗口后应用留在系统托盘，托盘入口恢复已有窗口。主窗口沿用并调整本地控制台现有的 TypeScript、Vite 和 CSS 页面，以复用本机管理流程和现有设计规范。

## 应用与服务的边界

8000 负责网关和 UM 认证恢复，8001 提供本地控制台与管理接口。桌面应用提供窗口、托盘和通知，沿用现有服务启动与监督机制。关闭窗口保持 8000 和 8001 的服务状态。建议将「退出桌面应用」和「停止本机服务」作为独立操作，分别触发应用退出和显式服务停止。首版驻留范围为已登录的 Windows 用户会话。人工接管继续按 ADR 0011 执行，由本人通过控制台打开可见 Edge。 [本机控制台启动入口](../../scripts/Open-GatewayConsole.ps1) · [ADR 0011](0011-local-um-reauthentication-lifecycle.md)

## 技术与界面方向

桌面壳优先用 Rust 和 Tauri 2 验证。首个原型让 WebView2 加载 8001 的回环地址，验证现有同源页面、会话和管理操作。页面继续通过 8001 的管理接口操作服务，托盘和通知由桌面壳处理。框架选型以原型结果为准。 [前端传输](../../web/src/console/backend/transport.ts) · [Tauri 托盘](https://v2.tauri.app/learn/system-tray/) · [WebView2](https://v2.tauri.app/reference/webview-versions/)

主窗口沿用现有主题、字体和语义化样式变量，窗口与托盘交互遵循 Windows 习惯。首版界面使用简体中文。首页分别显示服务运行、UM 认证和上游可用性，说明下一步可执行的操作。状态用文字和图标表达，详细错误码以及请求、账本和日志放在诊断页面。 [前端样式变量](../../web/src/shell/tokens.css) · [Windows 无障碍指南](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessibility-overview)

## 原型验证

- 在 Windows 登录后启动桌面应用，关闭窗口后用托盘恢复。重复打开入口时复用原窗口。在窗口关闭期间向 8000 发起请求，确认服务可继续使用。
- 验证 WebView2 内能正常打开 8001 的同源页面、读取状态以及执行现有管理动作。验证需要人工接管时打开可见 Edge，同一轮核验通过后同步更新窗口和托盘状态。
- 验证 Windows 浅色和深色主题、键盘焦点及缩放下的主要操作。记录窗口打开和托盘驻留时的空闲资源占用，作为选型实测依据。
