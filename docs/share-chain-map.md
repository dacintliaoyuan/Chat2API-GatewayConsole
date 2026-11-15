# 分享链命名与边界

本文件只做一件事：把分享链里容易混淆的名字钉到身份上。生产代码与契约以本表为准。

| 名字 | 身份 | 生产是否使用 |
|---|---|---|
| `chat2api/chat2api.py` | 合并后的 8100 分享进程；本机 8000 也跑同一文件 | 是 |
| `chat2api/share_auth.py` | 纯配置/鉴权 helper（投影解析、请求校验、凭据解析、`ShareGate`、撤销状态） | 是 |
| `share_gateway` | 分享读面组件键（`share.status` / `share.diagnostics`） | 是，API 契约 |
| `tests/linux_share_chain.py` | 单进程 Linux 链检查（28100 + 28999） | 是 |
| `um-cloud-deploy/` | 云端发布/运维材料 | 是 |

## 边界规则

- 生产 8100 只运行 `chat2api/chat2api.py`；不再有第二套分享 server。
- `share_gateway` 是读面键，不是模块名；helper 模块名是 `share_auth`。
- `chat2api.py` 只在 `--share-config` 分支内延迟 import `share_auth`，顶层不 import。
- 旧 Windows 本机分享链的部署材料与 `Recover-ShareControl.*` 恢复入口已从仓库删除，只保留在 Git 历史。
- 旧链测试已随 `ShareServer`/HMAC 归因/revoke notice 退役删除；broker/instance-pipe 用例只使用 `tests/_share_instance_double.py` 的最小实例端点替身。
- 改分享链名字时先跑 `python scripts/check_share_names.py`；它失败就说明旧链/新链混了。
