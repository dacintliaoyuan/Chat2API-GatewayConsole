# 云端 onboarding（仅受信任的同事）

云端不对外直接开放，也不提供桌面快捷方式。这份文档写给**读取后逐步教学的 agent**：
每一步都有可执行的命令、成功标志和失败时的停止条件。不要跳步，也不要把凭据写进
日志、提交或回复。

## 0. 前置条件（维护者交给对方）

- 对方有自己的机器，并已安装 PowerShell 7 与 `ssh`。
- 维护者提供 SSH 配置（私网优先）：`~/.ssh/aliyun-um-ecs.conf`，主机别名
  `um-share-ecs`。需要公网回退时再提供 `~/.ssh/aliyun-um-ecs-public.conf`。
- 对方能访问 Tailscale 私网；公网回退只作为私网失败时的备选。
- 对方拿到本项目仓库（含 `cloud/Open-CloudConsole.bat`）。
- 维护者在云端控制台为对方新建**长期 API Key**，并把 API 地址与 Key 交给对方。
  控制令牌（`/etc/um-share/admin-control-token`）只留在云机，不交给对方。

## 1. 验证 SSH 配置

```powershell
ssh -F "$HOME\.ssh\aliyun-um-ecs.conf" -o BatchMode=yes um-share-ecs true
```

成功标志：退出码 0，没有密码提示。失败时停止，让维护者补 SSH 配置或 Tailscale
访问；不要改用密码或把私钥复制到别处。

## 2. 验证 Tailscale 私网

```powershell
tailscale status
```

成功标志：输出里能看到云机节点且状态为 online。失败时停止并让维护者确认
Tailscale 共享；不要直接切公网。

## 3. 打开云端控制台

在仓库根运行：

```powershell
cloud\Open-CloudConsole.bat
```

它会：

1. 建立到 `127.0.0.1:8002` 的 SSH loopback 隧道（私网优先，公网回退）；
2. 等待 `http://127.0.0.1:8002/health` 返回 200；
3. 打开 `http://localhost:8002/` 控制台页面。

成功标志：浏览器打开云端控制台，`/health` 为 200。失败时按提示检查隧道状态；
`cloud\Open-CloudConsole.bat -Check` 只做检查，`-Stop` 停止自建隧道。

## 4. 在控制台里给对方 API 访问

1. 打开「Key」页，新建长期 Key（名称、并发上限）。
2. 复制「API 地址」和刚创建的 Key；Key 明文只显示一次。
3. 把 API 地址与 Key 交给实际使用者。不要把控制令牌、SSH 私钥或云机登录信息
   发给使用者。

成功标志：对方用 `Authorization: Bearer <Key>` 请求 `http://<API 地址>/v1/models`
返回 200。

## 5. 验证分享入口

在能访问 API 地址的机器上：

```bash
curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $SHARE_KEY" \
  https://<公网域名>/v1/models
```

成功标志：`200`。`401/403` 表示 Key 不对或已撤销；`404` 表示公网入口没有反代
该路径。不要用控制令牌代替分享 Key。

## 6. 停止与回滚

```powershell
cloud\Open-CloudConsole.bat -Stop
```

- 只停止本机自建的 SSH 隧道，不停止云端服务。
- 云端回滚用 `um-cloud-deploy/rollback.sh`（维护者执行）；Python 回滚单元是
  `um-share-console-python.service`（8096）。
- 回滚后重新打开控制台仍用 `cloud\Open-CloudConsole.bat`。

## 7. 边界

- 云端入口默认端口是 8002；Python 回滚单元监听 8096；旧 18096 只作显式覆盖。
- 公网只反代分享白名单到 8003，不反代 8002/8096/8097。
- 云端不提供桌面快捷方式；对方机器上的快捷方式由对方自己创建。
- 凭据留在原存储：控制令牌在云机 `/etc/um-share/admin-control-token`，分享 Key
  只进受限交付文件；日志、交接与提交只记录存储类别。
