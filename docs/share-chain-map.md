# 分享链命名与边界

本文件只做一件事：把分享链里容易混淆的名字钉到身份上。生产代码与契约以本表为准。

| 名字 | 身份 | 生产是否使用 |
|---|---|---|
| `chat2api/chat2api.py` | 合并后的 8100 分享进程；本机 8000 也跑同一文件 | 是 |
| `chat2api/share_entry.py` | 旧两进程分享服务，测试专用 | 否 |
| `share_gateway` | 分享读面组件键（`share.status` / `share.diagnostics`） | 是，API 契约 |
| `tests/linux_share_chain.py` | 单进程 Linux 链检查（28100 + 28999） | 是 |
| `tests/linux_share_chain_legacy.py` | 旧两进程 Linux 链检查（28100 + 28110） | 否，legacy |
| `um-cloud-deploy/` | 云端发布/运维材料 | 是 |

## 边界规则

- 生产 8100 只运行 `chat2api/chat2api.py`；`chat2api/share_entry.py` 不再被 broker 或 systemd 拉起。
- `share_gateway` 是读面键，不是模块名；模块名是 `share_entry`。
- `chat2api.py` 只在 `--share-config` 分支内延迟 import `share_entry`，顶层不 import。
- 旧 Windows 本机分享链的部署材料已从仓库删除，只保留在 Git 历史。
- 旧链测试只允许出现在 `tests/linux_share_chain_legacy.py`、`tests/r19/cloud_fixture.py` 与 `tests/linux_resource_envelope.py`。
- 改分享链名字时先跑 `python scripts/check_share_names.py`；它失败就说明旧链/新链混了。
