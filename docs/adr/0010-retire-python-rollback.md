# 退役 Python 回滚实现

ADR 0009 把 Rust 迁移限定在常驻服务，并保留 Python 回滚实现：云端保留一个 release 窗口，本机把 `StartPath` 改回 `start_chat2api.bat` / `start_usage_service.bat`。现在 8000/8001/8002/8003 的生产进程都已由 Rust 承担，但 Python 仍留在回滚实现、Rust 本机动作的一次性 sidecar、broker、云端部署脚本与测试里。我们决定：在满足前置条件后退役“Python 回滚实现”，只保留 Rust 生产实现；broker、云端部署脚本与测试不在本次退役范围，各自另开 spec。删除顺序是：先部署并观察新 Rust 8003，再把本机 12 个 sidecar 动作与 `model_sync` 迁到 Rust，最后打包 Python 回滚并删除回滚闭包；恢复路径是云端旧 release、`tmp/python-rollback-<date>.tar.gz` 与 git 历史。

**Status**: accepted

## Considered Options

- **立即删除全部 Python（含 broker、部署脚本、测试）**：broker 仍是 8003 的唯一拉起者，云端部署脚本在 unit 的安全路径上，测试是当前唯一回归网；放弃。
- **永久保留 Python 回滚实现**：双实现维护成本不会下降，回滚窗口没有终点；放弃。
- **只删回滚实现，保留 broker、部署脚本与测试**：采用。
- **先删回滚、再迁 sidecar**：会让本机 UM 登录卡和模型页立刻回退为“后端未提供”；放弃。

## Preconditions

1. 新 Rust 8003 已部署到云端，并在一个发布窗口 / 7 天真实流量下观察；期间完成一次“新 Rust → 旧 release → 再切回”的回滚演练。
2. 本机 8001 的 12 个动作（`service.*`、`account.*`、`model.context_*`、`model.catalog`）与 `model_sync` 已迁到 Rust，不再调用 Python sidecar。
3. 云端旧 release（至少 `s2-20261117b`）仍可回滚；旧 release 自带 Python，不依赖仓库里的 Python 文件。
4. 本地回滚包已生成：`tmp/python-rollback-<date>.tar.gz`（gitignored），包含 `usagesrv.py`、`chat2api/`、`console/`、`shared/`、`model_sync/`、`start_chat2api.bat`、`start_usage_service.bat` 与 `um-share-console-python.service`。
5. 本 ADR 已合入，相关 ticket 已开。

## Consequences

- 删除后，本机不再有 Python 回滚点；恢复只能靠回滚包、云端旧 release 或 git 历史。
- `share/` 保留给 broker；`um-cloud-deploy/` 保留给部署与回滚；`tests/` 保留给仍存在的 Python 模块，随实现迁移逐步换成 Rust 测试。
- `console/`、`shared/`、`model_sync/` 在 sidecar 迁移完成后随回滚闭包一起删除；`login_contract.py` 随最后一个 Python 登录调用方一起删除。
- GitHub 语言比例不作为验收项；验收看“生产端口跑什么、哪些 Python 还有调用方”。
- ADR 0009 的“本机回滚点是不删除的 Python 实现”条款被本 ADR 取代，仅对上述回滚闭包生效；broker 与部署脚本的回滚不受影响。
