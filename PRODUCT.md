# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

> 平台说明：产品的主要界面是 Windows 原生 Slint App（`desktop/`，`um-desktop`），设计语言来自浏览器控制台（`web/`）。`web` 是两者共同的设计语言基线；Slint 原生窗口不在 impeccable 的 web/iOS/Android 机械检测、`live` 与 `generate` 范围内。

## Users

主要用户：任意 UM 用户。他们在自己的 Windows 机器上运行本机 UM 网关（8000）与控制台宿主（8001），需要查看服务状态、完成 UM 登录、查用量与请求诊断，并在需要时人工接管登录。

次要用户（仓库证据）：受信任同事通过云端分享 Key（8003）调用模型 API，不直接打开控制台。

## Product Purpose

本机网关的运维控制台：把 UM 上游认证恢复（UMPASS/Duo、会话复用、自动重登）、本机服务进程监督、用量账本和请求诊断放进一个界面。

成功意味着：用户不用翻日志或手动折腾浏览器，就能看清网关、UM 登录、上游三项状态，并完成下一步动作（登录、重启、诊断、导出用量）。

桌面 App 为主界面；浏览器控制台保留同一设计语言。App 缺少的浏览器控制台功能要补齐（App-first parity），不是两套产品。

## Positioning

本机网关运维控制台。邻近产品（如 Magpie、Open WebUI）可以读接口或展示用量，但不能接管本机 UM 上游登录恢复与本机服务进程监督；这是本产品不可替代的机制。

## Operating Context

- Windows 已登录用户会话；App 随登录启动、驻留托盘，关闭主窗口收起到托盘，托盘退出才停止本实例网关。
- 本机端口当前为 8000 网关、8001 控制台宿主；云端为 8002 控制台、8003 分享网关；桌面 App 独立回环实例默认 7999、测试 7998。端口与角色契约是当前事实，不作为用户确认的长期产品约束。
- UM 登录可能走 UMPASS 表单 + Duo 确认；需要人工接管时打开可见 Edge。
- 用量账本与运行日志是日常查看对象；请求诊断用于逐条排查。
- 云端分享链供受信任同事使用；公网入口只反代分享白名单。

## Capabilities and Constraints

- 已确认功能：本机网关/控制台状态、UM 认证与按需登录、模型目录与 context probe、运行日志、端口审计、用量账本、请求诊断、云端分享 Key 管理。
- 已确认技术约束：桌面 App 是独立 Slint crate（不在 `rust/` workspace），Windows 原生窗口 + 托盘；浏览器控制台与 App 共用同一设计语言；界面使用简体中文。
- 已确认约束：桌面凭据保存在 `%LOCALAPPDATA%\Chat2API\credentials.json`，明文 0600（用户明确不要 DPAPI）。
- 明确未定：端口与角色契约不作为长期产品约束保留（2026-11-22 用户未选入）；多租户、公开发布与计费形态未定。
- 平台检测限制：机械设计检测只读 HTML/CSS，Slint（`.slint`）不适用；评审以人工/评审 subagent 为准。

## Brand Commitments

- 名称：AI 网关控制台（AIGatewayConsole）。
- 简体中文界面；Windows 习惯（托盘、关闭到托盘、随登录启动）。
- Magpie 只作为初期界面设定与打磨参考（2026-11-22 确认），不是长期依赖或品牌资产。

## Evidence on Hand

- `desktop/`：Slint 桌面 App 源码、`desktop/ui/` 界面、`desktop/tests/` headless 布局/键盘/图表测试。
- `web/`：浏览器控制台源码与前端契约门禁脚本。
- `docs/adr/`：ADR 0011、0012、0013、0014、0015 等已确认决策。
- `docs/research/desktop-prototype-109.md`：Slint 与 Tauri 2 原型测量记录。
- `output/impeccable-ui-review/`：本地 UI 预览与截图证据（gitignored，不入库）。
- 缺口：日志及时性 p95 ≤ 500ms 目标尚未在真实负载下验收；不得编造基准、用量或用户数据。

## Product Principles

1. 先看状态，再做动作：总览先显示网关运行、UM 登录、上游三项状态，再给下一步入口。
2. 本机优先：Windows 用户会话、托盘与简体中文是默认场景，不是附加主题。
3. 登录恢复是一等公民：UMPASS/Duo、会话复用和自动重登必须可见、可接管。
4. App 与浏览器控制台同一设计语言：App 为主界面，缺的功能补齐，不另起一套视觉。
5. 不编造事实：用量、日志、延迟与失败原因都来自真实账本或接口，缺失就显示缺失。

## Accessibility & Inclusion

- 简体中文界面；键盘可完成账号表单、导航、图表取值与列表操作。
- 已知 Windows 场景：150% 缩放下总览状态区、登录提示与用量图同时可见；最小 540×420 可滚动到达。
