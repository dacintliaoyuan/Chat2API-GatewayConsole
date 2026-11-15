# ADR 0002：UM 重新认证采用 headless-first 与按需 display

- 状态：Accepted
- 日期：2026-11-14

## 背景

云端 UM 重新认证会打开可见浏览器窗口，并可能等待最长 30 分钟。窗口在超时、取消或失败后没有可靠关闭，重复触发又会打开新窗口；多个 Chromium 进程把约 1.7 GB 的云机内存打爆，只能重启服务。云端正常路径并不需要有人看着窗口：只有 MFA 验证码、自动填充缺失或页面无法识别时才真正需要人工。

登录交互本身依赖真实浏览器：Open WebUI 登录页的「使用 UMPASS 继续」入口，随后是 UMPASS（Keycloak websso）表单；账号与密码来自实例自己的 profile 自动填充，会话 token 存在 Open WebUI 的 localStorage，Duo 推送由手机批准。这些步骤不是本仓库能自行构造的 HTTP 请求。

## 决策

1. **headless-first**：一次登录交互默认用 headless Chromium 打开同一登录页；正常路径不创建可见窗口，不要求 `DISPLAY` 或 `WAYLAND_DISPLAY`。可见窗口只在人工接管时按需出现。
2. **复用同一 profile**：headless 与可见模式共用实例现有 profile，不新建专用 profile；保留 cookie、自动填充和登录态。
3. **按需 display**：真正需要人工的步骤立即结束当前轮、关闭并释放浏览器，返回 `manual_takeover_required:<reason>`；操作员显式选择时才 `systemctl start` 云端 display unit，再开新轮。display unit 安装但默认不 enable，默认路径不依赖它。
4. **同一 profile 单实例**：同一 profile 目录在同一时刻只允许一个登录浏览器，用 profile 目录内的 OS 级文件锁保证；打开前只清理同 profile 的旧 Chromium。

## 备选

- **纯 HTTP 登录**：否决。仓库里没有经过验证的 UM SSO HTTP 契约；登录要经过 Open WebUI、UMPASS/Keycloak 和 Duo，MFA 与推送批准必须由本人完成；自己重放账号密码还会触发已知的 UMPASS 账号锁定风险。纯 HTTP 还要在每次登录页改版后重新逆向，并长期保存或重放账号密码。token 注入只作为人工兜底，不作为正常登录路径。
- **保持默认可见浏览器，只补超时关闭**：否决。正常路径仍然要求 display 和可见窗口，旧窗口的生命周期问题只是被缩短，而不是被消除。
- **默认常驻 display**：否决。display 与 Xvfb/x11vnc 在不需要人工时也占内存；按需启动才能把常驻成本降下来。
- **每次登录新建专用 profile**：否决。会丢掉 cookie、自动填充和登录态，把 headless 登录退化成每次人工登录。

## 后果

- 云端正常路径不再需要可见窗口或 display；内存不再随重复触发开窗增长。
- 真正需要人工时默认失败并给出 `manual_takeover_required:<reason>`；操作员要显式拉起 display 再开新轮。
- 登录仍依赖真实浏览器与现有 profile；MFA 和 Duo 仍由本人完成，自动化不猜验证码或密码错误。
- 同一 profile 的并发登录由单实例锁约束；第二个触发加入当前轮，而不是开第二个浏览器。
- 端口治理段归属由独立端口轨道 #40 处理：云端控制台默认 8096、display x11vnc 默认 8097；旧 18096/5901 只在一个发布周期内作为显式配置值保留。

## 相关实现

- `chat2api/chat2api.py`：`browser_login`、重新认证轮次与 profile 启动
- `um-cloud-deploy/systemd/um-share-display.service`：按需 display unit，并与 8100 子进程共享私有 `/tmp`
- `um-cloud-deploy/display/start-loopback-display.sh`：display 就绪后发布 `login-display.env`；`browser_login` 据此在新轮次打开可见浏览器
- `um-cloud-deploy/install.sh`：display unit 安装但不默认 enable
- `um-cloud-deploy/display/README.md`：私有 display 入口与人工接管说明
- `console/display_proxy.py`：控制台 display 读面
