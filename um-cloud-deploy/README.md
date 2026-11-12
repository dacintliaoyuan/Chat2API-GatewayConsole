# UM 分享云端部署包（um-cloud-deploy）

本目录是 UM 分享上云（S2）的**离线发布与运维材料**。它包含部署入口、manifest、
依赖锁定、systemd 模板、显示入口、显式备份恢复工具和安装/更新/回滚脚本；**不修改
产品 Python 源码、tests、web 或 `verify.ps1`**，也不在本任务中执行真实部署。公网入口
是 Cloudflare Tunnel（`https://dacintapi.de5.net/v1` → `127.0.0.1:8100`），Caddy 只保留
历史回退样例，不是必需组件。

当前状态（2026-11-04，S2）：

- 发布闭包、manifest、依赖锁定、systemd 单权威方案、安装/更新/回滚脚本、
  私有运维说明与显示入口已产出并做本地语法/清单校验。本目录当前 manifest 为
  `s2-20261110d`：47 个产品 Python 文件、27 个云端运行模块、36 个部署文件，
  `web_dist` 为 787 个文件的云端生产树；数量与哈希一律以 `RELEASE-MANIFEST.json` 为准。
- **本目录的材料不等于云端已部署版本**：最近一次运行记录（2026-11-04）显示云端
  `current` 为 `s2-20261110d`。真实部署、
  启停与切换仍须按运行验收记录判断，不能由本目录的清单通过代替。
- 云端控制台前端来自**云端角色的生产构建**：候选预览用
  `cd web && npm run build -- --mode candidate-cloud` → `web/release/candidate/cloud/`
  （只读，写操作提示去生产页）；部署可写控制台用生产云端构建
  `CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud npm run build` → `web/dist-cloud/`。
  `um-cloud-deploy/RELEASE-MANIFEST.json` 的 `web_dist` 记录该树的 `root`、`entry`
  （index.html 实际加载的模块脚本）和 `build.role`/`build.line`（`cloud`/`production`）。
  `web/dist` 是本地角色的生产页（8001 托管），**不能**作为云端 `web_dist`；
  `--require-web-role cloud` 会拒绝把本地角色树打进云端发布包，
  `--require-web-line production` 会拒绝候选树（`usagesrv.py` 只托管 `line=production`）。
  没有前端时只能用 `--allow-missing-web` 显式打包 API-only 版本，manifest 记
  `web_dist.included=false` 与原因；18096 根路径按产品既有行为返回 500，不伪造页面。
- **预算保护与自动备份已从默认装配移除**（D18-11）：不再安装
  `um-share-backup.timer`、不写 `UM_CLOUD_BUDGET_DB` 等预算读面变量，`install.sh` 也不再
  要求 `--budget-config`，`update.sh`/`rollback.sh` 不再执行切换前备份检查点。
  `backup_restore.py` 仅保留**显式**备份/恢复入口（默认显式备份参数、
  `--restore-backup`、`--deployment-restore`），由运维单独授权执行；现有备份文件与
  `backup.key` **不删除**。真实云机上的 `um-share-backup.timer` 是否已停用属于
  W12 的逐对象授权动作，本目录不代为实现。软件版本切换的补偿/回退保留。
- `bootstrap-wsl.sh` 是 Windows/WSL 引导，**不是云机安装器**；云端只用本目录的
  `install.sh`，目标为 Ubuntu 26.04 + Python 3.14，不假设 WSL 的 24.04/3.12。

## 0. 归属边界

- 本目录是**现行云端**发布/运维材料；旧 Windows 本机分享链在 `../um-share-deploy/`，
  仍有恢复说明和测试消费者，不随本目录清理。
- `../output/` 与 `../tmp/` 是本机构建/诊断产物，不入库；当前只有
  `output/um-share-release-r17/um-share-s2-20261105a.tar.gz` 及其包内 `web/dist`
  被发布/交接引用，源 manifest 的 `web_dist.root` 指向 `web/dist-cloud`。
- 完整入口、候选目录与历史材料清单见 [docs/gateway-entrypoints.md](../docs/gateway-entrypoints.md)。

## 1. 目录与角色

| 路径 | 作用 |
| --- | --- |
| `README.md` | 本说明（发布、systemd、私有运维、退出码） |
| `DEPENDENCIES.md` | 依赖锁定的人类可读说明 |
| `dependencies.lock.json` | 机器可读锁定：Ubuntu 26.04 包版本、Python 包、Playwright/Chromium |
| `requirements.lock` | Python 直接依赖的精确版本（wheelhouse 的输入） |
| `make-manifest.py` | 从当前字节冻结静态超集/云端动态闭包并生成 manifest |
| `RELEASE-MANIFEST.json` | 当前代码生成的 manifest（路径 + SHA-256 + 版本 + Python + 依赖） |
| `package-release.sh` | 生成 `um-share-<version>.tar.gz`、manifest 和 SHA256SUMS |
| `install.sh` | 首次安装（幂等） |
| `update.sh` | 版本切换（15 分钟排空、健康门、失败回滚） |
| `rollback.sh` | 切回已安装版本（同样的排空与健康门） |
| `lib/common.sh` | 路径、锁、退出码、manifest 校验、drain 计数 |
| `bin/um-share-brokerctl.py` | 唯一受限 broker 控制入口（AF_UNIX、HMAC、drain/start/stop） |
| `systemd/um-share-broker.service` | systemd 监督 broker 宿主；broker 管 8100 子进程 |
| `systemd/um-share-console.service` | systemd 监督 18096 控制台；控制台管 8110 子进程 |
| `systemd/um-share-display.service` | 仅 loopback 的 Xvfb + x11vnc（私有运维） |
| `display/README.md` | 云端私有入口与登录窗口说明（Tailscale Serve、noVNC、VNC 认证） |
| `display/configure-tailscale-serve.sh` | 只把 `/` 反代到 `127.0.0.1:18096`，拒绝 Funnel |
| `display/start-loopback-display.sh` | 启动 `:99` 与 `127.0.0.1:5901`，不监听公网 |
| `display/novnc-launcher.html/.js` | 同源 `/display/` 的 noVNC 启动页 |
| `display/vnc-auth-helper.py`、`display/issue-display-grant.py` | VNC 认证辅助与一次性显示 grant |
| `display/tailscale_serve_policy.py` | Serve handler 策略校验 |
| `backup_restore.py` | 显式加密备份/恢复 CLI（默认显式备份参数、`--restore-backup`、`--deployment-restore`），不在默认装配中 |
| `Caddyfile.example` | 历史回退样例：只把分享白名单反代到 `127.0.0.1:8100`；正式公网入口是 Cloudflare Tunnel |
| `config/*.example` | broker/console 环境、管理传输、初始 projection 模板 |
| `bootstrap-wsl.sh` | 仅 Windows/WSL 引导，不是云机安装器 |
| `run-linux-checks.sh` | Linux-only 隔离检查（core/full/resource） |
| `verify-release.ps1` | Windows 侧 QA：按 manifest 重新哈希并检查必需文件 |

发布包的顶层目录是 `um-share-<version>/`，内容与 `/opt/um-share/current/` 相同；
`um-cloud-deploy/` 随包携带。运行时不读取源码仓库或 `web/node_modules`。

## 2. 发布闭包（静态超集 vs 云端动态装配）

`make-manifest.py` 用 AST 从当前字节重新冻结闭包，不导入产品模块：

- **静态超集**：从 `usagesrv.py`、`chat2api/share_gateway.py`、`chat2api/chat2api.py`、
  `share_broker_ops.py`、`share_backend_supervision.py` 出发，跟随**所有**函数体内的
  `import`/`from`（包括 Windows-only 的惰性分支），并补上 4 个
  `importlib.util.spec_from_file_location` 目标：
  `chat2api/nonstream_aggregate.py`、`chat2api/share_dispatch.py`、
  `chat2api/share_gateway.py`、`share_secret_protection.py`。
  当前为 **47 个产品 Python 文件**（含 `shared/` 纯规则包与运维 CLI `share_admin.py`）；数量随代码变化，以 manifest 的
  `closures.static_superset.count` 为准。
- **云端动态装配**：显式列出云端角色实际导入的 **27 个产品模块**（`closures.cloud_runtime`）：
  只含 UM 分享栈、18096 控制台（含私有显示代理与 Key 测试探针）、B1/B5 读面和 8100/8110 入口；不含
  Qoder / CLIProxy / model_sync / zcode / Windows 探针。
  静态超集与云端动态装配是包含关系，manifest 会校验
  `cloud_runtime ⊆ static_superset`，并列出 `static_only` / `cloud_only`。
- 部署材料（本目录的脚本、unit、配置样例、brokerctl、README）在 manifest 的
  `deployment_files` 中单独列出，不与产品闭包混为一谈。
- `deployment_files` 只收真实发布材料，按规则排除本地工具状态，避免 manifest
  在没有产品字节变化时漂移：
  - `RELEASE-MANIFEST.json`、`SHA256SUMS`：由本脚本和 `package-release.sh`
    重新生成，不是输入；
  - 任意层级的 `__pycache__/`、`.mimosa/`、`.pytest_cache/`、`.mypy_cache/`、
    `.ruff_cache/`、`.zcode/`：解释器缓存与本机 hook/agent 状态。`.mimosa/`
    下的 hook 记录每次运行都会新增文件（已在 `.gitignore` 中），若被哈希，manifest
    会在没有任何产品改动时变旧、`--check` 返回 65；
  - `*.pyc` / `*.pyo`：字节码缓存。

  新增其他本地工具状态目录时同步更新 `make-manifest.py` 的 `LOCAL_STATE_DIRS`，
  并补 `tests/test_manifest_stability.py` 的用例。

重新生成（在 `ai-gateway` 根目录）：

```bash
python um-cloud-deploy/make-manifest.py \
  --root . \
  --deploy-root um-cloud-deploy \
  --version s2-20261110d \
  --dependencies um-cloud-deploy/dependencies.lock.json \
  --output um-cloud-deploy/RELEASE-MANIFEST.json \
  --web-dist web/dist-cloud \
  --require-web-role cloud
```

当前源 manifest 的 `web_dist.root` 是 `web/dist-cloud`（打包时复制为包内 `web/dist`）；
它带 `line=production`、`role=cloud` 和 `sourceHash`，重算源码哈希即可判断是否同源。

只有明确要记录“没有前端”时才用 `--allow-missing-web`。

带已构建前端时（生产云端构建）：

```bash
cd web
CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud npm run build
# 产物：dist-cloud/（index.html、assets/、fonts/、icons/、licenses/、
# manifest.webmanifest、sw.js、favicon.ico 和 candidate-build.json 等整棵树）
cd ..

python um-cloud-deploy/make-manifest.py \
  --root . --deploy-root um-cloud-deploy \
  --version s2-20261110d \
  --dependencies um-cloud-deploy/dependencies.lock.json \
  --web-dist web/dist-cloud \
  --require-web-role cloud \
  --require-web-line production \
  --output um-cloud-deploy/RELEASE-MANIFEST.json
```

`--require-web-role cloud` 读取该树的 `candidate-build.json`：角色不是 `cloud`、
或 `index.html` 加载的入口脚本不在携带文件里时直接失败，不会把本地角色页写进
云端 manifest。`--require-web-line production` 同时核对 `build.line`：候选树
（`candidate-cloud`）只用于 8099 预览，不能打进部署包；`usagesrv.py` 对
`line != production` 的树返回 500，因此发布包必须携带生产云端构建。

`--check <manifest>` 只比较当前字节与已生成 manifest，不写文件；不一致返回 65。
manifest 顶层包含 `release_version`、`python.version/target`、`dependencies`（完整锁定）、
`dependencies_lock_sha256`、`files[]`（`path`/`sha256`/`size`/`closures`）、
`deployment_files[]` 和 `web_dist`。

### 前端必须单独构建，云端包携带记录角色的生产构建

`web/release/candidate/cloud/` 是候选预览构建产物，不属于 Python 闭包，也不由本目录构建；
它只用于 8099 只读预览。云端发布包必须携带生产云端角色：

```bash
cd web
npm ci
CONSOLE_PRODUCTION_BUILD=1 CONSOLE_ROLE=cloud npm run build
# 产物：dist-cloud/（index.html、assets/、fonts/、icons/、licenses/、
# manifest.webmanifest、sw.js、favicon.ico 和 candidate-build.json 等整棵树）
```

`package-release.sh --web-dist web/dist-cloud --web-role cloud` 会把**整棵树**
复制到 payload 的 `web/dist/` 并逐文件写入 manifest；`--web-role`（默认 `cloud`）
核对 `candidate-build.json` 的角色，本地角色树会被拒绝；`--require-web-line production`
核对 `build.line`，候选树会被拒绝。manifest 的 `web_dist`
同时记录 `root`、`entry` 与 `build`（角色、line、sourceHash、gitHead、entryAssets）。

候选页在云端是只读的（写操作被前端拒绝并提示去生产页），要部署可写控制台必须携带生产
云端构建。本地生产 `web/dist`（8001 托管）不会被发布包携带，但它也写
`candidate-build.json`，供 `usagesrv.py` 拒绝托管错误角色或候选行的树。
没有前端时只能用 `--allow-missing-web` 显式打包 API-only 版本，manifest 记
`web_dist.included=false` 与原因；18096 根路径按产品既有行为返回 500，不伪造页面。

## 3. 依赖锁定

目标固定为 **Ubuntu 26.04 LTS（resolute）amd64 + CPython 3.14**；**不使用**
`bootstrap-wsl.sh` 的 Ubuntu 24.04 / Python 3.12 假设。

### 3.1 Python 包（直接依赖）

| 包 | 版本 |
| --- | --- |
| requests | 2.34.2 |
| playwright | 1.64.1 |
| PyYAML | 6.0.3 |
| cryptography | 50.0.2 |

传递依赖（wheelhouse 必须包含）：`certifi 2026.10.19`、`charset-normalizer 3.5.2`、
`idna 3.20`、`urllib3 2.8.0`、`greenlet 3.5.6`、`pyee 14.0.0`、
`typing-extensions 4.16.0`、`cffi 2.1.1`、`pycparser 3.12`。

`requirements.lock` 只放直接依赖；离线安装：

```bash
python3.14 -m pip download --only-binary=:all: \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.14 \
  --abi cp314 -r um-cloud-deploy/requirements.lock -d wheelhouse
```

安装时把 wheelhouse 放到 `/opt/um-share/wheelhouse`（或 `install.sh --wheelhouse`），
脚本用 `--no-index --find-links`，不访问网络。

### 3.2 Playwright / Chromium

| 项 | 值 |
| --- | --- |
| Playwright | 1.64.1 |
| Chromium revision | 1248 |
| Chrome for Testing | 156.0.8078.4 |
| 浏览器目录 | `/opt/um-share/pw-browsers`（不在 release 目录） |
| 安装命令 | `PLAYWRIGHT_BROWSERS_PATH=/opt/um-share/pw-browsers /opt/um-share/venv/bin/playwright install chromium` |

`chromium-1248` 与 `156.0.8078.4` 取自 Playwright 1.64.1 的 `browsers.json`；
Linux 侧必须用同一 Playwright 版本重新 `playwright install chromium`，不能把 Windows
缓存目录复制过去。8110 的 `--profile` 是持久 profile，位于
`/var/lib/um-share-console/share-backend/edge-profile`，不随版本删除。

### 3.3 Ubuntu 26.04 系统包

`dependencies.lock.json` 的 `apt.packages` 逐项列出 **Ubuntu 26.04（resolute）**
main/universe 的精确版本（2026-11-01 解析）。安装脚本按包名安装，不强制版本；
需要严格复现时用 `apt-get install pkg=version` 或预先制作 apt snapshot。关键包：

- Python：`python3.14 3.14.4-1ubuntu0.2`、`python3.14-venv 3.14.4-1ubuntu0.2`、
  `python3-pip 25.1.1+dfsg-1ubuntu2`。
- 边缘/显示：`caddy 2.6.2-14`、`xvfb 2:21.1.22-1ubuntu1.3`、`x11vnc 0.9.17-2`、
  `novnc 1:1.6.0-2`、`xauth`、`fluxbox`、`dbus-x11`、`x11-utils`、字体
  `fonts-liberation`、`fonts-noto-cjk`、`fonts-noto-color-emoji`。
- Chromium 运行库：`libnss3`、`libnspr4`、`libatk1.0-0t64`、`libatk-bridge2.0-0t64`、
  `libcups2t64`、`libdrm2`、`libxkbcommon0`、`libxcomposite1`、`libxdamage1`、
  `libxfixes3`、`libxrandr2`、`libgbm1`、`libpango-1.0-0`、`libcairo2`、
  `libasound2t64`、`libatspi2.0-0t64`、`libx11-6`、`libxcb1`、`libxext6`、
  `libxrender1`、`libxi6`、`libxtst6`、`libglib2.0-0t64`、`libdbus-1-3`、
  `libexpat1`、`libfontconfig1`、`libfreetype6`、`libuuid1` 及 `libxss1` 等。
  完整列表与版本见 `dependencies.lock.json`。

**未验证**：本机不是 Ubuntu 26.04，不能证明 `apt-get install` 在目标机上的包名/依赖
解析、Caddy 2.6.2 配置兼容或 Chromium 沙箱；这些必须在 S4 真实云机验证。

## 4. systemd 单权威

### 4.1 进程树

```
systemd
├── um-share-broker.service          (User=um-share)
│   └── share_broker_ops.py           broker host，唯一 8100 启动者
│       └── share_gateway.py :8100      broker 的 LinuxHostOps 子进程
├── um-share-console.service          (User=um-share)
│   └── usagesrv.py :18096            控制台；唯一 8110 启动者
│       └── chat2api.py :8110           _LinuxBackendOps 子进程
└── um-share-display.service          (User=um-share, 可选)
    └── Xvfb :99 + x11vnc 127.0.0.1:5901
```

**内存边界（2C2G）**：这台实例只有 1.7 GB 内存。正常运行时已占用 Xvfb + x11vnc +
8110 的 Chromium（约 8 个进程）+ 8100 + 18096。**不要在云机上再起第二个浏览器**
（headless Chromium / Playwright / Edge）做页面验证：实测第二个 Chromium 会让整机失去响应
（Tailscale ping 超时、Cloudflare 530、22/18096 TCP 能建连但无应用层应答、SSH 被关）。
页面验证在本机做；必须在云机验时，先 `um-share-brokerctl.py stop` 排空停链再起。

**单权威规则**：

1. **8100 只由 broker 拉起**。`um-share-broker.service` 的 `ExecStartPost` 调用
   `um-share-brokerctl.py start`，由 broker 的 `start` 动词执行 `LinuxHostOps.start_host()`；
   这是唯一允许的 8100 Popen 路径。`share_gateway.py` 由 broker 的
   `--host-command` 给出，带 `--config`、`--upstream-port 8110`、
   `--auth-mode projection`、`--instance-key-file`、`--registration-file`。
2. **禁止第二条 Popen/watchdog**。`um-share-console.service` 用
   `NoExecPaths=/opt/um-share/current/chat2api/share_gateway.py` 阻止控制台直接执行
   8100 入口；文件仍可读，因为 8110 子进程要导入它读取持久撤销状态。控制台的
   `service.start` 即使被点击，也在 `Popen` 阶段失败，不会产生第二个 8100。
   `/run/um-share` 对控制台是只读（`ReadOnlyPaths`），控制台的 `service.stop`
   在写停止意图前失败，不能绕过 broker 停止。`share.start/stop/estop/retry_stop`
   在云端注册表是 `enabled=False`，提交前即拒绝。
3. **不运行第二套 watchdog**。云端不安装/不启用 `share_backend_supervision.py`、
   `share_public_supervision.py`、`watchdog-umshare.ps1` 或任何 WSL watchdog；
   `bootstrap-wsl.sh` 不用于云机。8110 只由控制台的 `share.backend_restart` /
   `share.backend_login` 路径启停，没有第二个启动器。
4. **systemd 是最终 cgroup 围栏**。两个 unit 都是 `KillMode=control-group`；
   8100/8110 子进程即使 `start_new_session=True` 仍留在各自 unit 的 cgroup，
   `systemctl stop` 的最终清理不会留下孤儿进程。
5. **停止意图持久化**。`LinuxHostOps.stop_host()` 在发信号前写
   `/run/um-share/share-host-desired.json`（`schema_version=1, state=stopped`）；
   `start_host()` 默认拒绝复活，只有显式操作者 start 才清意图。`um-share-brokerctl.py
   start-explicit` 是唯一显式启动入口：先写 `state=running`，再发 broker `start`。
6. **Unix socket / peer**。broker 管理通道是 `/run/um-share/broker.sock`，父目录 `0700`、
   socket `0600`，HMAC 控制密钥 `/etc/um-share/broker-control.key`；实例通道是
   `/run/um-share/instance-<id>`，broker 用 `SO_PEERCRED` 的 pid/uid/gid 和
   `/proc/<pid>/cmdline` 验证 8100 入口（`share_gateway.py` + `--port 8100`）。
   broker 的 `--expected-uid` 取 `UM_SHARE_UID`（install.sh 写入），不是猜测。
7. **18096 是控制台唯一监听**。`usagesrv.py` 的 `HOST="127.0.0.1"`，`UM_SHARE_ROLE=cloud`
   默认端口 18096；`usagesrv.py` 不提供公网绑定参数。8110 也只由 8100 在 loopback 访问。

### 4.2 systemd 与 broker 的职责边界

| 角色 | systemd | broker | 控制台 |
| --- | --- | --- | --- |
| broker 宿主 | 监督 `um-share-broker.service`，`ExecStartPost` 发 `start`，`ExecStop` 发 `--drain-budget ... stop` | 唯一管理 8100 子进程；唯一 `start/drain/host-stop/gateway-restart` 执行者 | 只读状态 |
| 8100 分享入口 | 在 broker 的 cgroup 内，由 broker 拉起 | 唯一 Popen 者；`LinuxHostOps` 校验 pid + `/proc` cmdline + 端口 | 只读 `share.status` / `share.diagnostics` |
| 18096 控制台 | 监督 `um-share-console.service` | 只通过 AF_UNIX 提供 `ShareManager` / 管理动作 | 唯一 18096 监听者 |
| 8110 专用后端 | 在 console 的 cgroup 内，由 console 拉起 | 不直接管理 8110 | 唯一 `_LinuxBackendOps` Popen 者；`share.backend_restart` / `share.backend_login` |
| 显示/VNC | 可选 `um-share-display.service` | 不参与 | 8110 的浏览器用 `DISPLAY=:99` |

### 4.3 停止意图与 15 分钟排空

`update.sh` / `rollback.sh` / `um-share-brokerctl.py stop` 的顺序：

1. 停 cloudflared（`--no-edge` 时由运维显式承担）：公网不再有新请求。
2. `um-share-brokerctl.py --drain-budget 900 drain`：轮询 `/proc/net/tcp{,6}`
   的 8100 ESTABLISHED 连接数，最多 900 秒；到 0 后发 broker `drain` 动词
   （broker 自己的有界 in-flight 等待 + 最终停止 + 关闭验证，wire 预算 ≤90 秒）。
   总预算 ≤900 秒；超时返回 75，**不切换版本**。
3. `host-stop`：broker 写 `share-host-desired.json` 停止意图，停 8100 并验证关闭。
4. 停控制台 unit：`KillMode=control-group` 停 8110，等待 8110 端口关闭。
5. 只有以上都成功才切换 `current` 符号链接。

`share.chain_stop` / `share.chain_estop` 是云端整链停止入口（普通停止有界排空，强制停止
立即取消在途）；`share.stop` / `share.estop` 仍可用于单层关闭。
`um-share-brokerctl.py stop` 是唯一推荐的 8100 停止入口；`stop --force` 只用于运维
明确批准的必要中断，不是普通更新。

## 5. 安装、更新、回滚

所有脚本以 root 运行；`--release` 是 `package-release.sh` 产出的 tar.gz。
`--sha256` 或相邻 `.sha256` 文件用于校验归档；manifest 内的每个文件再逐项校验。

### 5.1 首次安装

```bash
# 1) 在构建机生成发布包（不写生产 dist）
bash ai-gateway/um-cloud-deploy/package-release.sh \
  --source ai-gateway \
  --version <version> \
  --output /tmp/um-share-release \
  --web-dist ai-gateway/web/dist-cloud \
  --web-role cloud            # 本地角色树会被 --require-web-role 拒绝

# 2) 复制到云机后安装（root；先解开 tar 再运行其中的 install.sh）
install -m 0755 um-share-<version>.tar.gz /root/
tar -xzf /root/um-share-<version>.tar.gz -C /root/
/root/um-share-<version>/um-cloud-deploy/install.sh \
  --release-dir /root/um-share-<version> \
  --version <version> \
  --wheelhouse /root/wheelhouse \
  --price-table /root/prices.json \
  --with-display
# 也可直接 --release /root/um-share-<version>.tar.gz（脚本会再解一次）
```

安装脚本幂等：已存在的 release 若 manifest 相同则复用；已存在的配置/密钥/数据
不覆盖；venv 和浏览器缓存已存在则复用；unit 已存在则重装模板并 `daemon-reload`。
首次安装会创建 `um-share` 系统用户（无登录、无 sudo）、`/opt/um-share`、
`/etc/um-share`、`/var/lib/um-share`、`/var/lib/um-share-console`、`/run/um-share`、
`/var/log/um-share`、`/var/log/um-share-console`，并生成随机
`broker-control.key` / `instance.key` / `share-attribution.key` /
`admin-control-token`（仅当不存在时）。

`/var/lib/um-share/run/share-projection.json` 初始为 `schema_version=2, generation=0, data=[]` 的
projection；broker 发布真实 projection 后覆盖它。`/etc/um-share/share-management.json`
是控制台的 schema 2 `unix_socket` 传输配置。`console.env` 的
`CONSOLE_SHARE_ATTRIBUTION_KEY_FILE` 与 8100 的 `--attribution-key-file` 必须指向
**同一个** `/etc/um-share/share-attribution.key`。

**预算配置不再是安装输入。** D18-11 起 `install.sh` 不再接受 `--budget-config`，
也不安装 `/etc/um-share/budget.json` 或 `um-share-backup.timer`；云端只保留安全读面。
已存在的预算库与备份文件不删除，真实云机上的 `um-share-backup.timer` 由 W12 逐对象
授权后停用，本目录不代为执行。

**价目表同样是安装输入。** `--price-table` 安装为 `/etc/um-share/prices.json`
（0640 root:um-share，已存在则保留），8100 用 `CHAT2API_PRICE_TABLE_FILE`、控制台用
`USAGE_PRICE_FILE` 读取同一文件；缺失时 `install.sh` 失败，避免账本无版本。
文件内容沿用产品 `data/prices.json` 的 `version`/`versions`/`models`/`providers` 形状。

安装**不再**创建 `/etc/um-share/backup.key`、`backup.env` 或
`um-share-backup.service`/`.timer`；显式恢复仍可使用已有备份文件，由运维单独授权。
`console.env` 会补上（已有文件不覆盖，仅追加缺失键）：

```ini
UM_CLOUD_SECURITY_STATE=/var/lib/um-share/security-state.json
USAGE_PRICE_FILE=/etc/um-share/prices.json
CHAT2API_PRICE_TABLE_FILE=/etc/um-share/prices.json
MAGPIE_PRICE_SNAPSHOT=/var/lib/um-share/magpie-prices.snapshot.json
MAGPIE_PRICE_STATUS=/var/lib/um-share/magpie-prices.status.json
CHAT2API_SHARE_SECRET_KEY_FILE=/var/lib/um-share-console/share-secret.key
CHAT2API_SHARE_REVOKE_STATE_FILE=/var/lib/um-share/share-revoke.state
```

`CHAT2API_SHARE_SECRET_KEY_FILE` 是 POSIX 分享 Key 明文的 AES-256-GCM 密钥文件
（`share_secret_protection.py` 的 `KEY_FILE_ENV`）：文件必须持有 32 字节原始密钥或
64 个十六进制字符，权限 0600、属主为当前 uid；缺失、损坏或权限过宽一律 fail-closed，
不会退化成明文或 hash-only 行。`install.sh` 会先复用当前生效的密钥（包括旧部署
`<current>/data/share-secret.key` 的 release 相对密钥），只在没有任何既有密钥时创建
`/var/lib/um-share-console/share-secret.key`（0600 `um-share`，不覆盖、不打印），并把
该行追加到 `console.env`；控制台（18096）读取它。删除或替换该文件会让既有
`aesgcm-file-key-v1` 行无法解密（读取如实报 undecryptable），而行的 SHA-256 摘要仍能
校验密钥本身。密钥属于持久状态，不放 release 目录。

`CHAT2API_SHARE_REVOKE_STATE_FILE` 由 8100 读取（`chat2api/share_gateway.py` 的
`REVOKE_STATE_ENV`）：撤销重放登记与每 Key 已应用 generation 在 ACK 前写入该文件，
文件只含 nonce、key_id、generation 与应用时间，不含密钥或明文 Key。产品默认在 POSIX
部署为 `/var/lib/um-share/share-revoke.state`（非 POSIX 开发检出仍用
`<release>/data/share-revoke.state`）。`install.sh` 会把
`CHAT2API_SHARE_REVOKE_STATE_FILE=/var/lib/um-share/share-revoke.state` 追加到
`broker.env` 和 `console.env`（已有值不覆盖）；8100 由 broker 拉起并加载 `broker.env`，
控制台加载 `console.env`，切换 `current` 后登记状态仍在持久目录。升级时 8100 只在部署
默认路径首次初始化时读取一次旧版 `<release>/data/share-revoke.state` 作为迁移来源，不修改、
不删除旧文件，并写 `.legacy-adopted` 标记；显式覆盖的路径不会读取旧文件。同目录的
`share-revoke.state.initialized` 标记用于区分“从未初始化”和“状态文件被删/损坏”：标记
存在而状态文件缺失时读取 fail-closed，不会当成空状态；需要恢复备份或按运维流程显式重新
初始化（同时删除状态文件与标记）。

broker unit 的 `ExecStartPre` 会检查已安装 unit 仍显式包含
`--budget-db/--budget-config` 且 `broker.env` 有 `UM_SHARE_UID`；console unit 的
`ExecStartPre` 会检查 `console.env` 仍有上述云运维/价格/Magpie 读面键**以及**
`CHAT2API_SHARE_SECRET_KEY_FILE`（`CONSOLE_REQUIRED_ENV` 包含分享 Key 密钥文件；
`CHAT2API_SHARE_REVOKE_STATE_FILE` 不在该检查内，只由 8100 读取，`install.sh` 仍把它
追加到 `console.env` 以保持两个 env 指向同一文件）。缺失时 **systemd 启动失败**，不会
静默退化成“无预算/读面 configured=false”。

安装、更新和回滚都**不再执行**自动加密检查点；`update.sh` 与 `rollback.sh`
只做版本切换、unit 安装/重装和补偿，不因备份失败阻止或放行切换。需要恢复点时，
由运维显式运行 `backup_restore.py` 并单独核对结果；Magpie 快照缺失时显式备份
仍 fail-closed，先运行 `console_magpie_prices.py --sync` 生成快照/status。

### 5.2 更新

```bash
# 1) 解包到 current 之外的暂存目录；不要运行 current 里的旧 update.sh
install -d -m 0755 /opt/um-share/.incoming
tar -xzf /root/um-share-<version>.tar.gz -C /opt/um-share/.incoming
chown -R root:root /opt/um-share/.incoming/um-share-<version>
chmod -R go-w /opt/um-share/.incoming/um-share-<version>

# 2) 用新 release 自己的 update.sh：先安装并校验新 release，再排空、切换
/opt/um-share/.incoming/um-share-<version>/um-cloud-deploy/update.sh \
  --release /root/um-share-<version>.tar.gz \
  --sha256 <archive-sha256> \
  --drain-budget 900

# 3) 成功后删除暂存目录
rm -rf /opt/um-share/.incoming/um-share-<version>
```

**入口归属**：更新必须运行新 release 自己的 `update.sh`，不能运行
`/opt/um-share/current/um-cloud-deploy/update.sh`。首次切换到新版本前，
`current` 仍是旧 release；旧脚本的 drain 参数顺序、`bin` 重指和恢复实现都可能早于
本次修复。新脚本先安装并校验新 release，普通排空使用刚安装的 `TARGET`，显式恢复使用
脚本自己的 release。

- 锁：`/run/um-share/deploy.lock` 非阻塞 `flock`，另一个部署操作返回 75。
- **普通更新不执行自动备份**：`update.sh` 不运行切换前加密备份检查点，也不因
  备份结果阻止或放行切换；需要恢复点时由运维单独运行 `backup_restore.py`。
- 15 分钟排空：先停 Caddy，再 `um-share-brokerctl.py --drain-budget 900 --poll-interval 5 stop`；未排空返回 75，
  **不切换**、恢复 Caddy、保持当前版本。
- **普通更新不做数据恢复**：`update.sh` 只做版本切换；数据恢复必须显式传
  `--recover-backup <sealed-backup> --recover-security-state <security-state.json>`，
  该模式不切换版本，排空/停控制台后调用 `restore_for_recovery`，再重启**当前**
  版本并过健康门。恢复失败时重启当前版本，不改数据。显式恢复不要求 live
  share-key 已存在：备份里的 `share-secret.key` 会写回当前生效路径；显式恢复同样必须
  运行新 release 自己的脚本（见 §5.3）。
- 版本切换：`/opt/um-share/current` 原子指向
  `/opt/um-share/releases/<version>`；`previous` 指向切换前版本。
- 健康门：18096 与 8100 端口 + 单元 active；失败自动切回 `previous` 并重启。
  回滚到原版本成功时返回 68；回滚/补偿未完成时返回 69（EX_ROLLBACK），并要求
  人工介入。69 表示入口停止可能未确认、可能仍开放；只有脚本确认 Caddy 已停止时
  才报告入口保持停止。`--no-caddy` 时脚本不管理 Caddy，失败消息只要求核对公网
  入口，不断言已停。70 只表示软件/装配失败，不用于回滚失败。
- 数据：`/var/lib/um-share`、`/var/lib/um-share-console`、`/etc/um-share`
  不在 release 内，不删除、不覆盖；`/var/lib/um-share/run/share-projection.json` 若已存在不覆盖。
- 不执行生产 dist 写入；携带的控制台树只来自 `--web-dist` 的只读复制，并按
  `--web-role` 核对角色。

### 5.3 回滚

```bash
# 运行当前 release 自己的 rollback.sh（不要用切换前 current 的旧树）
/opt/um-share/releases/<current-version>/um-cloud-deploy/rollback.sh \
  --drain-budget 900

# 或指定已安装版本
/opt/um-share/releases/<current-version>/um-cloud-deploy/rollback.sh \
  --to <target-version> --drain-budget 900
```

如果 `current` 仍是旧版本、但需要使用新 release 的回滚/恢复修复，先按 §5.2
解包新 release，再运行
`/opt/um-share/.incoming/um-share-<version>/um-cloud-deploy/rollback.sh`。

回滚不删除任何 release、数据、密钥或备份；只切符号链接并重启 unit。
目标版本必须已安装且 manifest 校验通过；失败恢复原 current，返回 68；恢复也失败返回 69（EX_ROLLBACK），入口停止可能未确认。
回滚同样不执行自动备份；需要恢复点时由运维单独运行 `backup_restore.py`。
普通回滚**不做数据恢复**。

显式数据恢复（版本不切换，仍停在当前 release；使用携带恢复实现的新 release 脚本；
`current` 仍为旧版时改用 §5.2 解包出的 `.incoming` 树）：

```bash
/opt/um-share/releases/<current-version>/um-cloud-deploy/rollback.sh \
  --recover-backup /var/lib/um-share/backup-remote/um-share-<id>.umbak \
  --recover-security-state /var/lib/um-share/security-state.json \
  --drain-budget 900
```

恢复前不要求 live share-key 已存在；备份里的 `share-secret.key` 会写回当前生效路径
（`console.env` 指定的路径，或没有该行时的 `<current>/data/share-secret.key`），
并按控制台用户设置 0600 权限。`restore_for_recovery` 要求独立的 security state，
且包内必须有 budget、price/config、Magpie 快照/status 和 security 记录；旧包或缺失
快照会被拒绝（fail-closed），不会用零额度或旧价格静默恢复。恢复后价格快照的当前版本
必须与备份元数据（与账本快照同一时刻记录）以及 status 的 `snapshotVersion` 一致；
坏快照直接拒绝。

### 5.4 退出码约定

| 码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 明确拒绝（broker 返回 refused） |
| 2 | 未确认（broker 未给出 started/stopped 证据） |
| 64 | 用法/参数错误 |
| 65 | manifest/数据校验失败 |
| 66 | 输入缺失（release、版本、控制台树） |
| 67 | 目标不可用 |
| 68 | 健康门失败但已恢复原版本（update/rollback 使用） |
| 69 | 回滚/补偿失败需人工；入口停止未确认，可能仍开放（确认停止时才报告已停） |
| 70 | 软件/装配失败 |
| 71 | 命令/OS 缺失 |
| 72 | 文件/OS 错误 |
| 73 | 无法创建（operator/bin 路径不是符号链接） |
| 74 | I/O 错误 |
| 75 | 排空超时/锁忙/临时失败（不切换） |
| 77 | 权限错误 |

`um-share-brokerctl.py` 的退出码：0 成功；1 拒绝；2 未确认；64 用法；69 broker
不可达；75 排空超时；77 控制密钥/权限。`install.sh` / `update.sh` / `rollback.sh`
的退出码见各脚本 `--help` 与上表。

## 6. 私有运维边界

### 6.1 端口与暴露面

| 端口 | 绑定 | 公网 | 访问方式 |
| --- | --- | --- | --- |
| 443/80 | Caddy | 是（仅 443 业务；80 仅 ACME） | 只反代分享白名单到 `127.0.0.1:8100` |
| 8100 | 127.0.0.1 | 否 | 只由 Caddy 本机反代；安全组不开放 |
| 8110 | 127.0.0.1 | 否 | 只由 8100 本机访问；不反代、不开放安全组 |
| 18096 | 127.0.0.1 | 否 | 只经 Tailscale Serve 私网入口；SSH 本地转发仍可用 |
| 5901 | 127.0.0.1 | 否 | 只经同源 noVNC `/display/`；x11vnc `-localhost`；不直接暴露 |
| 22 | 安全组限定的管理源 | 仅 SSH | 不向全网开放 |

**Caddy 只公开分享白名单**（`Caddyfile.example`）：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`
- `POST /v1/responses`
- 其余路径 404；不反代 8110、18096、5901 或 Caddy admin。
- 8100 自身不在安全组开放；8110 和 18096 不反代、不开放。
- SSE 用 `flush_interval -1`；不设 Caddy 读超时，由网关自己的活动/静默/总期限决定。

### 6.2 私网入口与 SSH 转发

```bash
# 只转发管理面与 VNC 到本机 loopback（SSH 仍可用）
ssh -N \
  -L 18096:127.0.0.1:18096 \
  -L 5901:127.0.0.1:5901 \
  um-share@<cloud-host>

# 管理面
open http://127.0.0.1:18096/
# 或命令行（默认 8096，这里显式 18096；以 um-share 身份读 0600 令牌）
runuser -u um-share -- /opt/um-share/venv/bin/python \
  /opt/um-share/current/share_admin.py \
  --base-url http://127.0.0.1:18096 \
  --control-token /etc/um-share/admin-control-token status
```

手机/PC 的常规入口是 Tailscale Serve：`display/configure-tailscale-serve.sh` 只把
`https://<node>.<tailnet>.ts.net/` 反代到 `127.0.0.1:18096`，并校验来源/设备
允许名单；不开放 80/443/8100/5901 公网，不使用 Funnel。SSH 只监听云机 22；
本机普通 SSH 是否可达取决于安全组，不以临时开放 22/VNC/18096 到全网来凑验收。
管理面不开公网、不加 Caddy 路由、不使用 Funnel。

### 6.3 显示与 VNC

- 真实云机没有 WSLg；`um-share-display.service` 提供 `Xvfb :99 -nolisten tcp`
  和 `x11vnc -rfbport 5901 -localhost`，只 loopback。
- VNC 密码由 `x11vnc -storepasswd <password> /etc/um-share/vnc.passwd` 生成；
  源文件必须 `root:root 0400/0600`，不提交 Git。systemd 用
  `LoadCredential=vnc.passwd:/etc/um-share/vnc.passwd` 把只读副本交给服务，
  `InaccessiblePaths` 隐藏源文件；服务只读 `$CREDENTIALS_DIRECTORY/vnc.passwd`。
- 浏览器打开同源 `/display/`（noVNC launcher），代理再连 loopback 5901；
  VNC 密码只由 x11vnc 在 RFB 流内挑战，网页不接收、不保存、不写 URL/日志。
  手机使用云端同源地址，不硬编码手机自己的 `127.0.0.1`。
- 人工在私有登录窗口完成 UM 登录/MFA；8110 的 Playwright 使用
  `DISPLAY=:99`、`XAUTHORITY=/run/um-share/display.auth` 和持久 profile
  `/var/lib/um-share-console/share-backend/edge-profile`。
- `about:blank` 截图、headless 通过或假 UM 登录**不等于**真实登录/MFA 恢复；
  不新增密码库、后台 SSO 分支或两机认证协调。

### 6.4 管理面与 Key

- `18096` 只 loopback；管理动作仍走 Host/Origin 门与既有控制令牌。
- 云端 `share.start/stop/estop/retry_stop` 是 `enabled=False`；不要用它们做云端
  一键管理。真实可用动作：`share.status`、`share.diagnostics`、
  `share.backend_login`、`share.backend_reauthenticate`、`share.backend_restart`、
  `share.gateway_restart`、`service.start/stop/restart`（但 8100 的 service 动作被
  单权威围栏挡住，见 §4）。
- 朋友 Key 的创建/轮换/撤销走 `share_admin.py --base-url http://127.0.0.1:18096`
  的管理 API；明文只在 create/rotate 响应出现一次，不写日志/交接。
- `share_admin.py` 默认 `http://127.0.0.1:8001`（本机）；云端必须显式
  `--base-url http://127.0.0.1:18096`。

## 7. 预算、备份与 Key 恢复围栏（已从默认装配移除）

D18-11 起预算准入与自动备份不再接入默认装配，以下只保留显式恢复入口，
且**只在离线/临时目录验证过**，不能称为真实云端已闭环：

- **预算准入已移除**：broker unit 不再传 `--budget-db/--budget-config`，
  `console.env` 不再写 `UM_CLOUD_BUDGET_DB`；缺失不再导致启动失败。
  现有预算库与配置不删除。
- **自动备份已移除**：不再安装 `um-share-backup.timer`/`um-share-backup.service`，
  `install.sh`、`update.sh` 和 `rollback.sh` 都不再执行检查点；现有备份文件不删除。
- **显式恢复围栏**：`restore_for_recovery` 只在显式 `--restore-backup`/
  `--deployment-restore` 模式调用；要求独立 security state、price/config、
  Magpie 快照/status 和 security 记录，缺一拒绝。普通 update/rollback 不会自动回滚数据。
- **Magpie 版本闭包**：备份元数据记录 `magpie_current_version`、完整版本列表和
  snapshot/status SHA-256；恢复后重新校验当前版本与 status 的 `snapshotVersion` 一致。
  快照缺失或损坏时备份直接失败（fail-closed），不会生成“无参考价”的成功备份。

**仍未验证**：真实 R2/OSS 上传/下载/生命周期/跨账号权限、真实云账单停止、
真实 systemd timer 在云机上的首次/失败/补传行为、真实 Ubuntu 26.04 上的
`install.sh/update.sh/rollback.sh`、以及真实 UM 生成。`LocalObjectStore` 只是本地目录，
不能当异机存储证据。

## 8. 本地验证（2026-11-01 历史记录）

本节保留 2026-11-01 的实际执行记录；当前状态以本文件顶部的 2026-11-04 manifest 与
运行验收记录为准。当时实际执行（Windows 开发机，非 Ubuntu 26.04）：

```bash
# shell 语法
bash -n um-cloud-deploy/lib/common.sh
bash -n um-cloud-deploy/install.sh
bash -n um-cloud-deploy/update.sh
bash -n um-cloud-deploy/rollback.sh
bash -n um-cloud-deploy/package-release.sh
bash -n um-cloud-deploy/display/start-loopback-display.sh

# PowerShell 语法
pwsh -NoProfile -Command '$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile(".../verify-release.ps1",[ref]$null,[ref]$errors) > $null; if ($errors.Count) { $errors | % { $_.Message }; exit 1 }'

# Python 语法
python -m py_compile um-cloud-deploy/make-manifest.py um-cloud-deploy/bin/um-share-brokerctl.py

# 默认装配 + 备份闭包（本机临时目录，不启动服务）
python -m unittest tests.test_cloud_deploy_assembly tests.test_cloud_ops tests.test_cloud_ops_wiring
```

额外执行（只读，不启动服务）：

```bash
# Caddy 2.6.2 配置检查
./tmp/um-cloud-s2-20261101/caddy262/caddy.exe validate \
  --config ai-gateway/um-cloud-deploy/Caddyfile.example --adapter caddyfile
# -> Valid configuration

# systemd unit 语法（WSL 24.04 临时副本；不是 26.04 启动验证）
wsl.exe -d UM-Cloud-P1 -u root -- bash -c \
  "systemd-analyze verify /tmp/um-cloud-units/um-share-broker.service ..."
# -> systemd_verify_exit=0（只有 /opt/um-share/... 不存在告警）
```

当时结果写入 `tmp/um-cloud-s2-20261101/s2-package.json`；该相对路径（`ai-gateway/tmp/`）已清理，
同名记录保留在仓库根 `SelfUseAI/tmp/um-cloud-s2-20261101/`，需要时从那里或 Git 历史追溯。
**未验证**：真实 Ubuntu 26.04、Python 3.14 云机、真实 systemd、真实 Caddy、
真实 Chromium/Playwright、真实 UM、8110 登录/MFA、真实 R2/OSS、云计费、公网 DNS/TLS
与生产切换。

## 9. 发布包使用顺序（S2 离线）

1. 在 `ai-gateway` 根目录生成 manifest（`--allow-missing-web`，或带
   `--web-dist web/dist-cloud --require-web-role cloud --require-web-line production`）。
2. `package-release.sh --version <v> --output <dir>` 生成 tar.gz + `.sha256`。
3. 把 tar.gz、`.sha256`、wheelhouse 复制到云机（不在本包执行）。
4. 云机 `install.sh --release ... --sha256 ... --wheelhouse ... --price-table <prices.json>`。
5. 之后 `update.sh` / `rollback.sh` 只切已安装 release；普通更新/回滚不自动备份，数据恢复只走显式
   `--recover-backup/--recover-security-state`。
6. 真实部署、启停、公网开放与迁移仍须 S4—S6 的单独授权与验收；本包不执行这些动作。

## 10. 已知缺口

- `install.sh` 已在真实云机安装过 `s2-20261103e`／`s2-20261104g`；`update.sh` /
  `rollback.sh` 的完整 15 分钟 drain、健康门与自动回滚未按全绿执行，r16 使用受控手工
  切换。apt 包名、Caddy 2.6.2 配置、Chromium 沙箱和 systemd 属性未在本目录独立复验。
- 云端控制台页面已按 2026-11-04 运行记录经本机 SSH 隧道在 18096 打开；Tailscale Serve
  私网入口与手机访问未独立复验。发布包只携带通过 `--web-dist` 指定的生产云端角色树
  （`dist-cloud`，`build.line=production`），manifest 记录其 `root`、`entry`
  与 `build.role`；候选 `release/candidate/cloud` 只用于 8099 只读预览，不能作为发布包前端。
- 未在真实云机复验 `systemd-analyze verify` 与 `LoadCredential`；noVNC/WebSocket 已按
  2026-11-04 运行记录通过（canvas 1280×900），本目录未独立复验。
- 未接真实 R2/OSS、预算真实停止边界；systemd timer 与自动备份已从默认装配移除，
  显式备份/恢复围栏只在离线/临时目录验证，未在真实云机独立复验。
- 真实 UM 登录/MFA、持久 profile、Xvfb/x11vnc 人工操作已按 2026-11-04 运行记录由本人
  完成；本目录未独立复验。
- 真实 8100/8110 在 cloud 角色的 `service.start/stop` 围栏未独立复验（当前方案是
  `NoExecPaths`／`ReadOnlyPaths`；旧 `InaccessiblePaths` 会同时挡住 8110 的模块导入）。
- 未验证 Caddy 的 `read_timeout 0`、`flush_interval -1` 与 Caddy 2.6.2 的兼容性。
- 未在真实云机验证安全组、公网 DNS/证书或朋友 Key 端到端；SSH 转发只按
  2026-11-04 的本机隧道记录。
