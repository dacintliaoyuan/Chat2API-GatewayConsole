# 私有 Tailscale 入口与云端登录窗口

本目录只描述云端私有入口，不改变现有分享 API 的 Caddy 443 入口。
控制台仍只监听 `127.0.0.1:18096`；Tailscale Serve 只把 tailnet 内的
`https://<node>.<tailnet>.ts.net/` 反代到 `http://127.0.0.1:18096`。

## 1. Tailscale Serve

```bash
# 先确认没有 Funnel；脚本在 Funnel 开启时拒绝继续
sudo tailscale funnel status --json

# 只读检查：打印当前状态和将执行的命令
sudo bash ./configure-tailscale-serve.sh

# 写入精确 handler：/ -> http://127.0.0.1:18096
sudo bash ./configure-tailscale-serve.sh --apply

# 若节点已有其他 Serve handler，需显式同意清空后重建
sudo bash ./configure-tailscale-serve.sh --apply --reset-other-serve
```

脚本只应用 `--https=443` 的 root handler，并在应用后再次解析
`tailscale serve status --json`，要求：

- 只有一个 `Web` handler，路径为 `/`；
- 代理目标精确为 `http://127.0.0.1:18096`；
- `AllowFunnel` 为空；
- `tailscaled` 的 80/443 监听只能在 tailnet 地址上，不能在 `0.0.0.0`/`::`；
- `18096`、`8100`、`5901` 监听必须仍在 loopback。

不使用 Funnel，不新增 80/443/8100/5901 公网监听。现有 Caddy 443
分享入口是另一条业务链，本目录不修改它，也不把它当作控制台入口。

## 2. Tailnet 策略文件

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

`allowed_peer_ips` 必须是 PC/手机的具体 tailnet 地址，不能写
`100.64.0.0/10`、网段、`0.0.0.0` 或整个 tailnet。`origin` 必须与
浏览器实际打开的 HTTPS 源完全一致（非默认端口要写端口）。`allowed_login`
是 Tailscale 身份，不是 UM 账号。策略缺失/损坏时远程入口关闭，本机
`127.0.0.1:18096` 不受影响。

## 3. 登录窗口

`um-share-display.service` 启动 `Xvfb :99 -nolisten tcp -noreset` 和
`x11vnc -localhost -noipv6 -rfbport 5901`。浏览器通过控制台同源的
`/display/` 页面和 `/display/websockify` WebSocket 访问它；手机打开的是
`https://<node>.<tailnet>.ts.net/`，页面使用 `location.host`，不会连接手机
自己的 `127.0.0.1`。

`/display/` 返回 launcher 页面 `novnc-launcher.html`（`CONSOLE_DISPLAY_LAUNCHER_FILE`）。
该页 CSP 是 `script-src 'self'`，内联脚本会被浏览器拦截，因此连接逻辑放在同源的
`novnc-launcher.js`：`console_display_proxy.py` 把 `/display/launcher.js` 解析为
launcher 文件同目录下的 `novnc-launcher.js`，以 `text/javascript` 返回，不会到
`/usr/share/novnc` 里查找同名文件。脚本 `import './core/rfb.js'`（即
`/display/core/rfb.js`），连接 `location.host` 的 `/display/websockify`。

VNC 认证由云端完成，浏览器不接收 VNC 密码、不弹出密码框：控制台代理与
x11vnc 完成 RFB 3.8/3.7/3.3 的 VncAuth 握手，浏览器只看到无认证的
RFB 3.8 和上游 ServerInit。密码文件仍只持久化在 root-only 文件；密码不要
出现在命令行、URL、日志、`console.env` 或浏览器存储。

display service 已经通过 `LoadCredential` 持有只读密码副本。它在
`/run/um-share/vnc-auth.sock` 上启动 `vnc-auth-helper.py`：该 helper 只接收
16 字节 challenge、返回 16 字节 response，从不输出密码。控制台代理通过
`CONSOLE_DISPLAY_VNC_AUTH_SOCKET` 连接这个 helper；控制台本身不读取密码，
也不需要新增 `LoadCredential`。`x11vnc` 继续 `-localhost -rfbauth`。

VNC 认证文件只持久化在 root-only 文件。密码不要出现在命令行、URL、日志或
`console.env`；`x11vnc -storepasswd` 不带参数时交互式读取密码并写入
`~/.vnc/passwd`，随后立即安装为 root-only 并删除临时文件：

```bash
sudo x11vnc -storepasswd
sudo install -m 0600 -o root -g root ~/.vnc/passwd /etc/um-share/vnc.passwd
sudo rm -f ~/.vnc/passwd
```

已有旧文件（例如 `0600 um-share:um-share`）先迁移为 root-only：

```bash
sudo chown root:root /etc/um-share/vnc.passwd
sudo chmod 0600 /etc/um-share/vnc.passwd
```

systemd `LoadCredential=vnc.passwd:/etc/um-share/vnc.passwd` 把只读副本放到
`$CREDENTIALS_DIRECTORY/vnc.passwd`，服务用户不直接读源文件。密码只由
x11vnc 在 RFB 会话内校验；控制台代理通过 display service 的 helper socket
完成 challenge-response，浏览器不接收密码、不提示输入。密码不写入 URL、查询串、
服务端日志、`console.env` 或 Git。`/display/` 拒绝任何查询串。

控制台环境（`/etc/um-share/console.env`）：

```ini
CONSOLE_DISPLAY_ENABLED=1
CONSOLE_DISPLAY_VNC_HOST=127.0.0.1
CONSOLE_DISPLAY_VNC_PORT=5901
CONSOLE_DISPLAY_VNC_AUTH_SOCKET=/run/um-share/vnc-auth.sock
CONSOLE_DISPLAY_NOVNC_ROOT=/usr/share/novnc
CONSOLE_DISPLAY_LAUNCHER_FILE=/opt/um-share/current/um-cloud-deploy/display/novnc-launcher.html
CONSOLE_DISPLAY_MAX_CONNECTIONS=2
CONSOLE_DISPLAY_IDLE_TIMEOUT=900
CONSOLE_DISPLAY_MAX_SESSION=14400
```

`console.env` 还会由 `install.sh` 追加 `CHAT2API_SHARE_SECRET_KEY_FILE`（AES-GCM
分享 Key 密钥文件；只在缺失时创建 `/var/lib/um-share-console/share-secret.key`）和
`CHAT2API_SHARE_REVOKE_STATE_FILE`（`/var/lib/um-share/share-revoke.state`，
8100 通过 `broker.env` 读取同一登记文件）；显示配置不替代这两个持久状态键。
`um-share-console.service` 的 `ExecStartPre` 要求 `console.env` 仍有
`CHAT2API_SHARE_SECRET_KEY_FILE` 等 `CONSOLE_REQUIRED_ENV` 键，缺失时 systemd
启动失败；`CHAT2API_SHARE_REVOKE_STATE_FILE` 不在该检查内。

`/usr/share/novnc` 来自发行版 `novnc` 包；`install.sh` 的 `APT_PACKAGES`
已包含 `novnc`，启用前仍需确认目标机 `/usr/share/novnc/core/rfb.js` 存在。
页面加载 `core/rfb.js`，不加载 `vnc.html` 的 URL 参数，也不接受
`?password=`/`?path=`。

## 4. 认证、来源与生命周期

### SSH 桌面入口的浏览器授权

`Open-CloudConsole.ps1` 在确认专用 SSH 配置、隧道归属及健康后，通过同一 SSH
身份运行本目录的 `issue-display-grant.py --console-session`。helper 只在云机内
读取既有管理令牌，调用固定 `127.0.0.1:18096/api/session/reissue`，返回短期
一次性引导地址；长期令牌不传回电脑。SSH 输出仅在内存捕获，不写隧道日志。

浏览器使用 `http://localhost:18096`，隧道绑定和健康检查仍用 `127.0.0.1`。
本机工作台默认使用 `http://127.0.0.1:8001`，两入口因此不会覆盖同名 cookie；
cookie 不按端口隔离，不要把本机工作台也手动改为 `localhost`。

`/display/grant#console=<一次性码>` 的脚本先清除 fragment，再向同源
`/api/session` 兑换既有 HttpOnly、SameSite=Strict、Path=/ 的管理会话，成功后
回到总览。该会话同时供页面可用性检查和私有显示使用；失败时停在错误提示，
重新打开桌面入口可取得新码。不要把带码地址复制到日志、聊天或剪贴板。

这里复用的是 **10 分钟单次引导码、12 小时管理会话**，不是下述短期显示 grant。
后端目前只维护一个管理会话：新码替代旧的未兑换码，新兑换替代旧浏览器会话。
会话有效不等于 UM 已认证；UM 登录/MFA 仍由本人完成。

### 显示 grant 与连接生命周期

- 显示面的来源校验仍由控制台承担：远程请求必须通过
  `console_tailnet.py` 的 Serve 来源/owner/具体设备校验；本地请求必须来自
  loopback Host/Origin 和同一端口。来源校验本身不是管理员身份：SSH `-L`
  隧道到达云端时也表现为 loopback。
- `usagesrv.py` 的显示面不再使用 `session_valid=lambda _cookie: True`。
  `/display/websockify` 只接受以下管理身份之一：Tailscale Serve 的
  owner/具体设备加控制台会话、管理控制令牌、控制台 `usage_session`、或由
  `POST /display/grant` 换来的 HttpOnly 显示会话 Cookie。仅 loopback 而没有
  上述凭据的请求一律 403。
- `GET /api/console/share-management/display-session` 是只读可用性检查：
  没有管理身份时返回 `available=false` 和
  `reason=display_authorization_required`，不发放 grant。
- `POST /api/console/share-management/display-grant` 只在管理身份成立时
  发放一次性、短时、绑定来源的 grant；响应带 `Cache-Control: no-store`。
  `POST /display/grant` 用 grant 换取 `um_display_session` Cookie：
  `HttpOnly; SameSite=Strict; Path=/display/`，HTTPS 时再加 `Secure`。
  grant 只能成功兑换一次；错误来源、过期和重放都 403。grant token 只放在
  URL fragment 中（不发送到服务端、不写访问日志），launcher 在兑换前用
  `history.replaceState` 清除；不写浏览器存储。
- SSH 隧道入口：在云端以 root 运行，或用 `sudo -u um-share` 以服务用户运行
  `um-cloud-deploy/display/issue-display-grant.py`。root 可读取 owner 为 root
  或 `um-share` 的 0600 管理控制令牌；非 root 只能读取 owner 为自己的令牌。
  脚本向 loopback 控制台申请 grant，只把同源 launcher URL 打印到 stdout。PC 侧
  把该 URL 经现有 SSH `-L` 隧道交给浏览器；管理控制令牌不进入浏览器。任意
  localhost 进程没有控制令牌，不能自升为管理员。
- WebSocket 握手要求 `Origin` 精确等于当前同源源；拒绝缺失/跨源 Origin、
  重复头、非 13 版本、非 binary 子协议和未掩码客户端帧。
- 每个连接占用一个并发槽；握手失败、上游不可达、认证失败、客户端断开、
  上游断开、空闲超时、会话上限、协议错误和取消都会释放槽并关闭 5901
  上游连接。正常客户端关闭、上游 EOF、空闲/最长会话到期、取消和异常分别
  处理；健康会话不会被固定 join 超时强停。
- 默认最多 2 个并发显示连接，空闲 900 秒、单会话最长 4 小时；均在
  `console.env` 可调。TCP 连接超时只约束握手，不约束 RFB 中继的空闲读数。

## 5. 未覆盖 / 部署前检查

- 本目录未启动真实 VNC、未执行真实 UM 登录/MFA；`install.sh` 由 r14 追加了 `novnc`
  包、`CONSOLE_DISPLAY_*` 环境行、display unit 安装，并在 `console.env` 追加
  `CHAT2API_SHARE_SECRET_KEY_FILE` 与 `CHAT2API_SHARE_REVOKE_STATE_FILE` 等持久状态键。
- `vnc-auth-helper.py` 与 `issue-display-grant.py` 只在隔离测试中验证协议和参数；
  未在真实 Ubuntu 26.04 上执行 `systemd-analyze verify`、真实 `LoadCredential`
  启动、真实 x11vnc VncAuth 握手或真实 SSH 隧道 grant 兑换。DES 已知答案向量
  只证明算法实现，不等于真实云端密码已验证。
- `console_display_proxy.py` 已在 `make-manifest.py` 的 `CLOUD_RUNTIME` 中，
  `RELEASE-MANIFEST.json` 的 `closures.cloud_runtime` 会列出它。
- 未在真实 Ubuntu 26.04 上执行 `systemd-analyze verify`、`LoadCredential`
  启动或真实 18096/Tailscale Serve/noVNC WebSocket 连接；这些只有静态检查。
- 现有 Caddy 443 分享入口按 `um-cloud-deploy/README.md` §6.1 保持公开；
  本功能没有新增公网 80/443/8100/5901 监听。若要求 443 完全不公开，需要
  单独变更 Caddy/分享入口，超出本目录范围。
- 真实云机上的 `tailscale serve status`、`tailscaled` 监听地址和手机访问
  未在本机验证；脚本只提供可审阅的部署检查。
