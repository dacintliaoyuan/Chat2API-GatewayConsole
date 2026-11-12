# 分享链边界

分享 Key 的权威存储在 SQLite（`share-store.db`）；broker 把运行投影写到 `/var/lib/um-share/run/share-projection.json`。8100 合并进程在启动时读取投影，并在每次请求时重新校验 Key；账本直接使用鉴权后的 `key_id` 写 `attribution=verified`。

## 单进程边界

- 云端只有 `um-share-broker.service` 拉起 `chat2api/chat2api.py`（8100）；`um-share-console.service` 只跑 18096 控制台，不再拉 8110。
- 8100 进程内完成 API Key 鉴权、每 Key 并发准入、模型目录、chat/messages/responses 翻译、UM 会话与账本。
- 8100 只监听 loopback；Cloudflare Tunnel 只反代分享白名单到 8100。
- `.edge-profile` 与 `token.json` 位于 `/var/lib/um-share-console/share-backend`，由 `um-share` 拥有；合并进程与 18096 控制台以同一 `um-share` 身份读写，不再使用 `dacint`。
- 撤销 Key：控制台更新投影并把 `terminate_keys` 发给 broker；8100 关闭该 Key 的在途请求，新请求立即被拒。
- 归因不再经过跨进程 HMAC 标记：鉴权成功后直接用 `key_id` 记账。
- 旧 Windows 本机分享链（`um-share-deploy/`）已退役；`chat2api/share_gateway.py` 不再存在。
