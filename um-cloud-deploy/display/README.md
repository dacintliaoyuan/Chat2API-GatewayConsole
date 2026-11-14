# 私有 Tailscale 入口与云端登录窗口

本目录只描述云端私有入口，不改变现有分享 API 的 Cloudflare Tunnel 入口。控制台只监听 `127.0.0.1:18096`；Tailscale Serve 只把 tailnet 内的 `https://<node>.<tailnet>.ts.net/` 反代到它。

## Tailscale Serve

```bash
sudo tailscale funnel status --json   # 有 Funnel 时脚本拒绝继续
sudo bash ./configure-tailscale-serve.sh          # 只读检查
sudo bash ./configure-tailscale-serve.sh --apply  # 写入 / → http://127.0.0.1:18096
```

脚本只应用 `--https=443` 的 root handler，并在应用后再次解析 `tailscale serve status --json`，要求：

- 只有一个 `Web` handler，路径为 `/`；
- 代理目标精确为 `http://127.0.0.1:18096`；
- `AllowFunnel` 为空；
- `tailscaled` 的 80/443 监听只在 tailnet 地址上，不在 `0.0.0.0`/`::`；
- 18096、8100、5901 监听仍在 loopback。

不使用 Funnel，不新增 80/443/8100/5901 公网监听。

## Tailnet 策略

`/etc/um-share/console-tailnet-access.json` 只读、仅 root 可读，不提交 Git：

```json
{
  "schema_version": 1,
  "enabled": true,
  "origin": "https://<node>.<tailnet>.ts.net",
  "allowed_login": "owner@example.com",
  "allowed_peer_ips": ["100.x.y.z", "100.a.b.c"]
}
```

`allowed_peer_ips` 必须是 PC/手机的具体 tailnet 地址，不能写网段、`0.0.0.0` 或整个 tailnet。`origin` 必须与浏览器实际打开的 HTTPS 源完全一致。策略缺失或损坏时远程入口关闭，本机 `127.0.0.1:18096` 不受影响。

## 登录窗口

默认路径是 headless 登录：`browser_login` 打开 headless Chromium，不创建可见窗口，也不要求 `DISPLAY`/`WAYLAND_DISPLAY`，因此默认路径不依赖 display。`um-share-display.service` 只作为人工接管的按需出口；`install.sh` 安装该 unit，但不 enable、不开机自启。

`um-share-display.service` 启动 `Xvfb :99 -nolisten tcp` 和 `x11vnc -localhost -rfbport 5901`。浏览器通过控制台同源的 `/display/` 页面和 `/display/websockify` WebSocket 访问它；手机打开 `https://<node>.<tailnet>.ts.net/`，页面使用 `location.host`，不连接手机自己的 `127.0.0.1`。

VNC 认证由云端完成：控制台代理与 x11vnc 完成 RFB 3.8/3.7/3.3 的 VncAuth 握手，浏览器不接收 VNC 密码、不弹密码框。密码只持久化在 root-only 文件；不写入命令行、URL、日志、`console.env` 或浏览器存储。

`display` unit 通过 `LoadCredential` 持有只读密码副本，并在 `/run/um-share/vnc-auth.sock` 启动 `vnc-auth-helper.py`；控制台代理通过 `CONSOLE_DISPLAY_VNC_AUTH_SOCKET` 连接它。`x11vnc` 继续 `-localhost -rfbauth`。

## 按需启动（人工接管）

自动流程返回 `manual_takeover_required:<reason>` 后，操作员显式执行下面的脚本；默认安装不会 enable display，也不会在开机时启动它：

```bash
# 仅首次：创建 root-only VNC 认证文件（install.sh 不会创建密码）。
sudo x11vnc -storepasswd /etc/um-share/vnc.passwd
sudo chown root:root /etc/um-share/vnc.passwd
sudo chmod 0600 /etc/um-share/vnc.passwd

# 人工接管：只 systemctl start，不 enable；同时打开控制台 /display/ 面并重启控制台。
sudo /opt/um-share/current/um-cloud-deploy/display/manual-takeover.sh start

# 人工步骤完成后释放内存：停止 display，并关闭 /display/ 面；unit 仍保持安装且未 enable。
sudo /opt/um-share/current/um-cloud-deploy/display/manual-takeover.sh stop

# 只读确认：unit 是否只在本次人工接管中运行。
systemctl is-enabled um-share-display.service   # 必须是 disabled
systemctl is-active um-share-display.service    # 只在接管期间为 active
```

脚本要求 root，检查 `/etc/um-share/vnc.passwd` 为 root-only（0400/0600），只调用 `systemctl start`/`systemctl stop`，从不调用 `systemctl enable`/`systemctl disable`；`start` 会把 `console.env` 的 `CONSOLE_DISPLAY_ENABLED=1` 写入并重启控制台，`stop` 写回 0；默认安装不 enable display。真实 systemd 启动、`LoadCredential` 与真实 noVNC/VNC 握手仍须在目标机 follow-up 验证。

## 认证与来源

- 显示面的来源校验由控制台承担：远程请求必须通过 `console/tailnet.py` 的 Serve 来源/owner/具体设备校验；本地请求必须来自 loopback Host/Origin 和同一端口。
- `/display/websockify` 只接受 Tailscale Serve 的 owner/具体设备加控制台会话、管理控制令牌、控制台 `usage_session`，或由 `POST /display/grant` 换来的 HttpOnly 显示会话 Cookie；仅 loopback 而没有上述凭据的请求一律 403。
- `POST /api/console/share-management/display-grant` 只在管理身份成立时发放一次性、短时、绑定来源的 grant；`POST /display/grant` 用 grant 换取 `um_display_session` Cookie（`HttpOnly; SameSite=Strict; Path=/display/`，HTTPS 时加 `Secure`）。grant 只能成功兑换一次。
- 默认最多 2 个并发显示连接，空闲 900 秒、单会话最长 4 小时；均在 `console.env` 可调。

## 未验证

真实 Ubuntu 26.04 上的 `systemd-analyze verify`、`LoadCredential` 启动、真实 x11vnc VncAuth 握手、真实 SSH 隧道 grant 兑换、真实 Tailscale Serve 与手机访问都未在本机验证；脚本只提供可审阅的部署检查。
