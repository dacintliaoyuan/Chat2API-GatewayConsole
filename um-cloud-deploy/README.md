# UM 分享云端部署包

本目录是 UM 分享上云（S2）的离线发布与运维材料：manifest、依赖锁定、systemd 模板、显示入口、显式备份恢复工具与安装/更新/回滚脚本。它不修改产品 Python 源码、tests、web 或构建守卫，也不执行真实部署。

公网入口是 Cloudflare Tunnel（`https://dacintapi.de5.net/v1` → `127.0.0.1:8100`）；Caddy 只保留历史回退样例。

## 发布闭包

`make-manifest.py` 用 AST 从当前字节冻结闭包，不导入产品模块：

- **静态超集**从 `usagesrv.py`、`chat2api/share_gateway.py`、`chat2api/chat2api.py`、`share_broker_ops.py`、`share_backend_supervision.py` 出发，跟随所有函数体内的 `import`，并补上 4 个 `importlib.util.spec_from_file_location` 目标。
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

候选 `release/candidate/cloud` 只用于 8099 只读预览；18096 根路径按产品既有行为返回 500，不伪造页面。

## 依赖锁定

目标固定为 Ubuntu 26.04 LTS（resolute）amd64 + CPython 3.14；`bootstrap-wsl.sh` 只用于 WSL 隔离验证，不是云机安装器。Python 直接依赖、传递依赖、Playwright/Chromium 与 apt 包版本见 [DEPENDENCIES.md](DEPENDENCIES.md) 与 `dependencies.lock.json`。

离线安装把 wheelhouse 放到 `/opt/um-share/wheelhouse`，脚本用 `--no-index --find-links`，不访问网络。

## systemd 单权威

```text
systemd
├── um-share-broker.service   → share_broker_ops.py → share_gateway.py :8100
├── um-share-console.service  → usagesrv.py :18096 → chat2api.py :8110
└── um-share-display.service  → Xvfb :99 + x11vnc 127.0.0.1:5901（可选）
```

1. **8100 只由 broker 拉起**：`um-share-broker.service` 的 `ExecStartPost` 调用 `um-share-brokerctl.py start`，这是唯一允许的 8100 Popen 路径。
2. **控制台不直接执行 8100 入口**：`NoExecPaths` 阻止直接执行 `share_gateway.py`，`ReadOnlyPaths` 阻止写停止意图；云端注册表里 `share.start/stop/estop/retry_stop` 是 `enabled=False`。
3. **云端不安装第二套 watchdog**：8110 只由控制台的 `share.backend_restart` / `share.backend_login` 启停。
4. **systemd 是最终 cgroup 围栏**：两个 unit 都是 `KillMode=control-group`；`systemctl stop` 不会留下孤儿进程。
5. **停止意图持久化**：`LinuxHostOps.stop_host()` 在发信号前写 `/run/um-share/share-host-desired.json`；只有 `um-share-brokerctl.py start-explicit` 能清停止意图。
6. **broker 管理通道**是 `/run/um-share/broker.sock`（目录 0700、socket 0600，HMAC 密钥 `/etc/um-share/broker-control.key`）；8100 入口由 `SO_PEERCRED` 与 `/proc/<pid>/cmdline` 验证。
7. **18096 是控制台唯一监听**（`127.0.0.1`）；8110 只由 8100 在 loopback 访问。

**内存边界**：云机只有约 1.7 GB 内存，正常运行时已占用 Xvfb + x11vnc + 8110 的 Chromium + 8100 + 18096。不要在云机上再起第二个浏览器做页面验证；必须在云机验时，先 `um-share-brokerctl.py stop` 排空停链再起。

## 安装、更新、回滚

所有脚本以 root 运行；`--release` 是 `package-release.sh` 产出的 tar.gz，`--sha256` 或相邻 `.sha256` 文件用于校验归档，manifest 内的每个文件再逐项校验。

```bash
# 构建机：生成发布包（不写生产 dist）
bash um-cloud-deploy/package-release.sh \
  --source . \
  --version <version> \
  --output /tmp/um-share-release \
  --web-dist web/dist-cloud \
  --web-role cloud

# 云机：安装（root；先解开 tar 再运行其中的 install.sh）
/root/um-share-<version>/um-cloud-deploy/install.sh \
  --release-dir /root/um-share-<version> \
  --version <version> \
  --wheelhouse /root/wheelhouse \
  --price-table /root/prices.json \
  --with-display
```

安装脚本幂等：已存在的 release 若 manifest 相同则复用；已存在的配置、密钥与数据不覆盖；venv 与浏览器缓存已存在则复用；unit 已存在则重装模板并 `daemon-reload`。`--price-table` 是安装输入，缺失时安装失败。

更新必须运行新 release 自己的 `update.sh`，不能运行 `/opt/um-share/current/um-cloud-deploy/update.sh`：

```bash
install -d -m 0755 /opt/um-share/.incoming
tar -xzf /root/um-share-<version>.tar.gz -C /opt/um-share/.incoming
/opt/um-share/.incoming/um-share-<version>/um-cloud-deploy/update.sh \
  --release /root/um-share-<version>.tar.gz \
  --sha256 <archive-sha256> \
  --drain-budget 900
rm -rf /opt/um-share/.incoming/um-share-<version>
```

更新顺序：停 cloudflared → `um-share-brokerctl.py --drain-budget 900 --poll-interval 5 stop`（未排空返回 75，不切换版本）→ `host-stop` → 停控制台 unit → 原子切换 `current` 符号链接。健康门失败自动切回 `previous`；回滚到原版本成功返回 68，回滚/补偿未完成返回 69。锁是 `/run/um-share/deploy.lock` 的非阻塞 `flock`，另一个部署操作返回 75。

回滚运行当前 release 自己的 `rollback.sh`；目标版本必须已安装且 manifest 校验通过。回滚不删除 release、数据、密钥或备份，只切符号链接并重启 unit。

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
| 443/80 | Caddy | 是（仅 443 业务；80 仅 ACME） | 只反代分享白名单到 `127.0.0.1:8100` |
| 8100 | 127.0.0.1 | 否 | 只由 Caddy 本机反代；安全组不开放 |
| 8110 | 127.0.0.1 | 否 | 只由 8100 本机访问；不反代、不开放安全组 |
| 18096 | 127.0.0.1 | 否 | 只经 Tailscale Serve 私网入口；SSH 本地转发仍可用 |
| 5901 | 127.0.0.1 | 否 | 只经同源 noVNC `/display/`；x11vnc `-localhost` |
| 22 | 安全组限定的管理源 | 仅 SSH | 不向全网开放 |

Caddy 只公开分享白名单：`GET /v1/models`、`POST /v1/chat/completions`、`POST /v1/messages`、`POST /v1/responses`；其余路径 404，不反代 8110、18096、5901 或 Caddy admin。SSE 用 `flush_interval -1`。

管理面只走 Tailscale Serve 或 SSH 转发，不开公网、不加 Caddy 路由、不使用 Funnel。显示与 VNC 的认证和来源边界见 [display/README.md](display/README.md)。

## 未验证

真实 Ubuntu 26.04、Python 3.14 云机、真实 systemd、真实 Caddy、真实 Chromium/Playwright、真实 UM 登录/MFA、真实 R2/OSS、云计费、公网 DNS/TLS 与生产切换都未在目标机复验。本目录的静态校验与本地隔离检查不能代替真实部署验收。
