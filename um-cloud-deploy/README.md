# UM 分享云端部署包

本目录是 UM 分享上云（S2）的离线发布与运维材料：manifest、依赖锁定、systemd 模板、显示入口、显式备份恢复工具与安装/更新/回滚/回滚演练脚本。它不修改产品 Python 源码、tests、web 或构建守卫，也不执行真实部署。

公网入口是 Cloudflare Tunnel（`https://dacintapi.de5.net/v1` → `127.0.0.1:8003` Rust 网关）；Caddy 只保留历史回退样例。Cloudflare ingress 的 8100 → 8003 由维护者在 dashboard/API 完成，agent 只校验配置和公网结果，不改 tunnel。

## 发布闭包

`make-manifest.py` 用 AST 从当前字节冻结闭包，不导入产品模块：

- **静态超集**从 `usagesrv.py`、`chat2api/chat2api.py`、`chat2api/chat2api.py`、`share/broker_ops.py` 出发，跟随所有函数体内的 `import`，并补上 4 个 `importlib.util.spec_from_file_location` 目标。
- **云端动态装配**只列云端角色实际导入的产品模块；manifest 校验 `cloud_runtime ⊆ static_superset`，并列出 `static_only` / `cloud_only`。
- **部署材料**在 `deployment_files` 单独列出，不与产品闭包混为一谈；`RELEASE-MANIFEST.json`、`SHA256SUMS`、解释器缓存与本地工具状态按规则排除。

文件数量与哈希以 `RELEASE-MANIFEST.json` 为准，不在本文件抄写。当前 manifest 的 `web_dist.root` 是 `web/dist-cloud`。

重新生成（仓库根）：

```bash
python um-cloud-deploy/make-manifest.py \
  --root . \
  --deploy-root um-cloud-deploy \
  --version <version> \
  --dependencies um-cloud-deploy/dependencies.lock.json \
  --output um-cloud-deploy/RELEASE-MANIFEST.json \
  --web-dist web/dist-cloud \
  --require-web-role cloud \
  --require-web-line production
```

`--check <manifest>` 只比较当前字节与已生成 manifest，不写文件；不一致返回 65。只有明确记录“没有前端”时才用 `--allow-missing-web` 显式打包 API-only 版本。

## 前端产物

云端发布包必须携带云端角色的生产构建：

```bash
cd web
CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud npm run build
# 产物：dist-cloud/
cd ..
```

`--require-web-role cloud` 读取该树的 `candidate-build.json`：角色不是 `cloud`、或 `index.html` 加载的入口脚本不在携带文件里时直接失败。`--require-web-line production` 同时核对 `build.line`，候选树不能打进部署包。`web/dist` 是本机生产页（8001 托管），不能作为云端 `web_dist`。

候选 `release/candidate/cloud` 只用于 8099 只读预览；8096 根路径按产品既有行为返回 500，不伪造页面。

## 依赖锁定

目标固定为 Ubuntu 26.04 LTS（resolute）amd64 + CPython 3.14；`bootstrap-wsl.sh` 只用于 WSL 隔离验证，不是云机安装器。Python 直接依赖、传递依赖、Playwright/Chromium 与 apt 包版本见 `dependencies.lock.json` 与 `RELEASE-MANIFEST.json`。

离线安装把 wheelhouse 放到 `/opt/um-share/wheelhouse`，脚本用 `--no-index --find-links`，不访问网络。

## systemd 单权威

```text
Python 回滚 release                      Rust 目标 release
├── um-share-broker.service               ├── um-share-gateway.service
│   → broker_ops.py → chat2api.py :8100     │   → broker_ops.py → um-gateway :8003
├── um-share-console.service :8096          ├── um-share-console.service :8002
└── um-share-display.service :8097（可选）  └── um-share-display.service :8097（可选）
```

Rust release 的 `um-share-gateway.service` 仍由 Python broker 持有唯一 Popen：`ExecStart` 是 `share/broker_ops.py`，`--host-entry-kind native` 让 broker 以 `--mode production --port 8003` 启动 `/opt/um-share/current/bin/um-gateway`；`ExecStartPost` 调用 `um-share-brokerctl.py start`。Rust release 的 `um-share-console.service` 指向 8002，display 仍为 8097 并加入 `um-share-gateway.service` 的 namespace。

1. **网关只由 broker 拉起**：Python release 的 `um-share-broker.service` 和 Rust release 的 `um-share-gateway.service` 的 `ExecStartPost` 调用 `um-share-brokerctl.py start`，这是唯一允许的网关 Popen 路径。
2. **控制台不直接执行网关入口**：`NoExecPaths` 阻止直接执行 `chat2api.py` 和 `bin/um-gateway`，`ReadOnlyPaths` 阻止写停止意图；旧 Windows 本机分享动作已退役、不再注册。
3. **云端不安装第二套 watchdog**：网关进程由 broker 唯一拉起；`share.gateway_restart` 走 broker restart。
4. **systemd 是最终 cgroup 围栏**：两个 unit 都是 `KillMode=control-group`；`systemctl stop` 不会留下孤儿进程。
5. **停止意图持久化**：`LinuxHostOps.stop_host()` 在发信号前写 `/run/um-share/share-host-desired.json`；只有 `um-share-brokerctl.py start-explicit` 能清停止意图。
6. **broker 管理通道**是 `/run/um-share/broker.sock`（目录 0700、socket 0600，HMAC 密钥 `/etc/um-share/broker-control.key`）；网关入口由 `SO_PEERCRED` 与 `/proc/<pid>/cmdline` 验证。
7. **8002 是控制台唯一监听**（`127.0.0.1`）；分享 API 与 UM 会话都在 8003 进程内。目标契约不再保留 8100/8096/18096/5901 兼容别名；Python 回滚 release 仍使用 8100/8096。

**内存边界**：云机只有约 1.7 GB 内存，正常运行时已占用 Xvfb + x11vnc + 8003 的 Chromium + 8002。不要在云机上再起第二个浏览器做页面验证；必须在云机验时，先 `um-share-brokerctl.py stop` 排空停链再起。

## 排障读面（只读）

云机上按这个顺序读：

| 读什么 | 命令 | 判据 |
| --- | --- | --- |
| 控制台状态 | `curl -s http://127.0.0.1:8002/api/console/share-management/status` | `gateway_running` 缺失 = 未确认（不是 `false`）；`share_url_source=configured` 才说明公网地址已配 |
| broker 状态 | `sudo -u um-share /opt/um-share/bin/um-share-brokerctl.py status` | 必须以 `um-share` 跑；root 读 0600 的 `broker-control.key` 会报 `control_key_unusable` |
| 8003 进程 | `cat /run/um-share/share-gateway.pid` 与 `journalctl -u um-share-gateway --since "-30 min"` | pid 文件与 broker 日志是 8003 的唯一实例证据 |
| 停止意图 | `cat /run/um-share/share-host-desired.json` | `state=stopped, reason=manual` 时普通 start 被拒；只有 `start-explicit` 能清意图 |
| 端口 | `ss -ltn \| grep -E ':(8097\|8003\|8002)'` | 8097 是 VNC，不是 HTTP；Python 回滚 release 为 8100/8096 |

## 安装、更新、回滚

所有脚本以 root 运行；`--release` 是 `package-release.sh` 产出的 tar.gz，`--sha256` 或相邻 `.sha256` 文件用于校验归档，manifest 内的每个文件再逐项校验。

```bash
# 构建机：生成 Python 回滚发布包（不写生产 dist）
bash um-cloud-deploy/package-release.sh \
  --source . \
  --version <python-version> \
  --output /tmp/um-share-release \
  --web-dist web/dist-cloud \
  --web-role cloud

# 构建机：生成 Rust 8003/8002 发布包。先 cargo build --release，
# 再把 target/release/um-gateway-candidate 作为 --rust-bin 传入。
bash um-cloud-deploy/package-release.sh \
  --source . \
  --version <rust-version> \
  --output /tmp/um-share-release \
  --web-dist web/dist-cloud \
  --web-role cloud \
  --runtime rust \
  --rust-bin rust/target/release/um-gateway-candidate

# 云机：安装（root；先解开 tar 再运行其中的 install.sh）
/root/um-share-<version>/um-cloud-deploy/install.sh \
  --release-dir /root/um-share-<version> \
  --version <version> \
  --wheelhouse /root/wheelhouse
```

Rust release 的 payload 含 `bin/um-gateway`、`um-share-gateway.service`、8002 console unit 和 8097 display unit；Python release 保留 `um-share-broker.service` 与 8096 console unit 作为回滚目标。`install.sh` 从 manifest 的 `runtime` 选择 unit 和健康门端口，不再把 8100 写死。

安装只写入 `um-share-display.service`，默认不 enable、不随开机启动；正常登录路径是 headless，不依赖 display。需要人工接管时，操作员显式执行：

```bash
sudo /opt/um-share/current/um-cloud-deploy/display/manual-takeover.sh start
sudo /opt/um-share/current/um-cloud-deploy/display/manual-takeover.sh stop
```

`start` 只对本次启动 `systemctl start`，从不 `enable`，等 display 发布运行时状态后打开控制台 `/display/` 面；此后开新登录轮，8100 子进程会在该 display 上打开可见浏览器。`stop` 停止 display、清除运行时状态并关闭该面，新轮次回到 headless；unit 仍保持安装且未 enable。认证文件、Tailnet 策略与未验证范围见 [display/README.md](display/README.md)。

安装脚本幂等：已存在的 release 若 manifest 相同则复用；已存在的配置、密钥与数据不覆盖；venv 与浏览器缓存已存在则复用；unit 已存在则重装模板并 `daemon-reload`。

更新必须运行新 release 自己的 `update.sh`，不能运行 `/opt/um-share/current/um-cloud-deploy/update.sh`：

```bash
install -d -m 0755 /opt/um-share/.incoming
tar -xzf /root/um-share-<version>.tar.gz -C /opt/um-share/.incoming
/opt/um-share/.incoming/um-share-<version>/um-cloud-deploy/update.sh \
  --release /root/um-share-<version>.tar.gz \
  --sha256 <archive-sha256> \
  --drain-budget 900 \
  --health-port <target-gateway-port>
rm -rf /opt/um-share/.incoming/um-share-<version>
```

更新顺序：停 cloudflared → `um-share-brokerctl.py --drain-budget 900 --poll-interval 5 stop`（未排空返回 75，不切换版本）→ `host-stop` → 停控制台 unit → 原子切换 `current` 符号链接。健康门失败自动切回 `previous`；回滚到原版本成功返回 68，回滚/补偿未完成返回 69。锁是 `/run/um-share/deploy.lock` 的非阻塞 `flock`，另一个部署操作返回 75。

健康门按目标 release 的网关端口探测，不再硬编码 8100：`--health-port` 指定目标端口（默认按目标 manifest 的 `runtime` 取 8003 或 8100），`--current-health-port` 指定失败补偿时恢复的当前 release 端口（默认按当前 manifest 的 `runtime` 取 8003 或 8100）。切 Rust 8003 时 update 会停 `um-share-broker.service`、装 `um-share-gateway.service` 并启动 8003；回滚 Python 时反向切回。控制台端口仍从 `console.env` 的 `USAGESRV_PORT` 读取。

回滚运行当前 release 自己的 `rollback.sh`；目标版本必须已安装且 manifest 校验通过。回滚不删除 release、数据、密钥或备份，只切符号链接并重启 unit：

```bash
/opt/um-share/releases/<rust-version>/um-cloud-deploy/rollback.sh \
  --to <python-version> \
  --drain-budget 900 \
  --health-port 8100 \
  --current-health-port 8003
```

回滚目标是带 `/var/lib/um-share-console` `ReadWritePaths` 的 Python release。`rollback.sh` 会停 Rust `um-share-gateway.service`、切 `current`/`bin`、装 Python `um-share-broker.service`/`um-share-console.service` 并验证 8100/8096；数据目录不迁移、不删除。

回滚 release 的 broker unit 模板必须包含 `/var/lib/um-share-console` 的 `ReadWritePaths`：合并的 8100 子进程在该目录写 token、浏览器 profile 和服务日志，缺少时以 `Read-only file system` 退出，回滚就无法启动 Python。`install.sh`、`package-release.sh` 和 `rollback.sh` 都会在安装/打包/切换前校验模板，缺少该路径时失败关闭。

显式数据恢复同样运行当前 release 自己的脚本：

```bash
/opt/um-share/releases/<current-version>/um-cloud-deploy/rollback.sh   --recover-backup /var/lib/um-share/backup-remote/um-share-<id>.umbak   --recover-security-state /var/lib/um-share/security-state.json   --drain-budget 900
```

### Rust → Python 回滚演练

`rollback-drill.sh` 在维护窗口内演练“停 Rust、启 Python 回滚 release、验证 `/ready`”。它默认只打印计划；加 `--execute` 才停/启 unit：

```bash
# 只打印计划（不需要 root）
/opt/um-share/releases/<python-version>/um-cloud-deploy/rollback-drill.sh \
  --to <python-version> \
  --health-port 8100 \
  --current-health-port 8003

# 实际演练（root；先确认公网入口已停）
/opt/um-share/releases/<python-version>/um-cloud-deploy/rollback-drill.sh \
  --to <python-version> \
  --health-port 8100 \
  --current-health-port 8003 \
  --execute
```

演练先用 `rollback.sh` 停当前 Rust release、切换 `current`/`bin`、安装并启动 Python unit；随后停掉仍以其他 unit 名存活的 Rust 进程，等待 8100 端口，要求 `/ready` 返回 200（503 表示进程在但 UM 会话或上游未就绪，演练失败），再探测 `/v1/models`，并在前后都要求 `/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share` 仍存在。脚本不删除、不重写这些目录；`--data-dir` 可替换默认目录列表，`--rust-unit` 可重复指定要停的 Rust unit。

运行数据在 `/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share`，不在 release 内，不删除、不覆盖。预算保护与自动备份已从默认装配移除；`backup_restore.py` 只保留显式备份/恢复入口，由运维单独授权执行。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 明确拒绝（broker 返回 refused） |
| 2 | 未确认（broker 未给出 started/stopped 证据） |
| 64 | 用法/参数错误 |
| 65 | manifest/数据校验失败 |
| 66 | 输入缺失（release、版本、控制台树） |
| 67 | 目标不可用 |
| 68 | 健康门失败但已恢复原版本 |
| 69 | 回滚/补偿失败需人工；入口停止未确认，可能仍开放 |
| 70 | 软件/装配失败 |
| 71 | 命令/OS 缺失 |
| 72 | 文件/OS 错误 |
| 73 | 无法创建（operator/bin 路径不是符号链接） |
| 74 | I/O 错误 |
| 75 | 排空超时/锁忙/临时失败（不切换） |
| 77 | 权限错误 |

`um-share-brokerctl.py`：0 成功；1 拒绝；2 未确认；64 用法；69 broker 不可达；75 排空超时；77 控制密钥/权限。

## 私有运维边界

| 端口 | 绑定 | 公网 | 访问方式 |
| --- | --- | --- | --- |
| 443/80 | Cloudflare Tunnel / Caddy 回退 | 是（仅 443 业务；80 仅 ACME） | 只反代分享白名单到 `127.0.0.1:8003` |
| 8003 | 127.0.0.1 | 否 | Rust 网关；只由本机 Cloudflare Tunnel/Caddy 反代；安全组不开放 |
| 8002 | 127.0.0.1 | 否 | Rust release 控制台；只经 Tailscale Serve 私网入口；SSH 本地转发仍可用 |
| 8097 | 127.0.0.1 | 否 | 只经同源 noVNC `/display/`；x11vnc `-localhost` |
| 22 | 安全组限定的管理源 | 仅 SSH | 不向全网开放 |

Cloudflare 只公开分享白名单：`GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/messages`、`POST /v1/responses`；其余路径 404，不反代 8002、8097 或 Cloudflare/Caddy admin。SSE 用 `flush_interval -1`。Python 回滚 release 的 8100/8096 只作为回滚目标，不作为公网入口。

管理面只走 Tailscale Serve 或 SSH 转发，不开公网、不加 Caddy 路由、不使用 Funnel。显示与 VNC 的认证和来源边界见 [display/README.md](display/README.md)。

## 未验证

真实 Ubuntu 26.04、Python 3.14 云机、真实 systemd、真实 Caddy、真实 Chromium/Playwright、真实 UM 登录/MFA、真实 R2/OSS、云计费、公网 DNS/TLS、Rust 8003/8002 的 systemd 安装、真实 Cloudflare ingress 切换与生产回滚都未在目标机复验。本目录的静态校验与本地隔离检查不能代替真实部署验收。
