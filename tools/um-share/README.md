# um-share：旧 Windows 分享链的只读核对与验收工具

本目录服务**旧 Windows 本机分享链**（控制台宿主 8096、分享入口 8100、专用后端 8110），是动作门 ①→②→③→④→⑤ 的只读工具化。现行云端分享链见 [um-cloud-deploy](../../um-cloud-deploy/README.md)；本目录工具不启动、不重启、不生成密钥、不发模型请求。

## 目录结构

| 子目录 | 内容 |
|---|---|
| `readonly/` | 只读核对 / 快照 / 验收：`share_chain_snapshot.py`、`watchdog_forensics.py`、`window_preflight.py`、`authoritative_readings.ps1`、`post_restart_acceptance.py`、`request_budget_estimate.py` |
| `actions/` | 会写盘或执行动作：`generate_attribution_key.py`、`test_key_ops.py`、`ledger_row_recheck.py`、`metering_acceptance.py`、`metering_request_probe.py` |
| `fixtures/` | 夹具 / 测试辅助的预留落点；当前夹具数据仍在 `tests/fixtures/` |
| `lib/` | 共享路径 helper（`lib/paths.py`）：由脚本位置推出 `tools/um-share` 与仓库根，并引导 `sys.path` |

每个脚本仍可直接按路径运行：

```bash
python tools/um-share/readonly/share_chain_snapshot.py --help
python tools/um-share/actions/test_key_ops.py --help
```

## 工具

| 工具 | 用途 | 退出码 |
|---|---|---|
| `readonly/share_chain_snapshot.py` | 现场只读快照（`--out`）与 8096 重启前后逐项比对（`--compare`）；`--authoritative` 并入提升身份读数 | 0=写出/全 PASS，2=失败/任一 FAIL 或不可判定 |
| `readonly/authoritative_readings.ps1` | 提升身份只读探针，供 `--authoritative` 合并；默认只打印，`-Out` 才写文件 | 0=成功 |
| `actions/metering_acceptance.py` | 三事实验收只读打印 | 0=三件事实成立且私人账本未变，2=任一不成立或不可判定 |
| `readonly/post_restart_acceptance.py` | 8096 重启后八层验收 | 0=八层全 PASS，2=任一层 FAIL/UNDECIDABLE |
| `readonly/watchdog_forensics.py` | 看门狗机制取证（纯函数 + 夹具） | `--print-rules` 0=已打印；`--from-fixture` 0=不动作，2=不可判定，3=停止，4=重启 |
| `readonly/request_budget_estimate.py` | ④ 的授权声明与费用上限预估 | 0=已算出，2=模型不在价目表或请求数非正 |
| `actions/ledger_row_recheck.py`、`actions/metering_request_probe.py`、`actions/test_key_ops.py`、`readonly/window_preflight.py`、`actions/generate_attribution_key.py` | 账本行复核、请求探针、Key 操作、窗口预检与归因密钥生成 | 见各脚本 `--help` |

## 只读边界

- 默认只读：`readonly/share_chain_snapshot.py`（无 `--out`）、`readonly/watchdog_forensics.py`、`readonly/window_preflight.py`（无 `--out`）、`actions/metering_acceptance.py`、`readonly/post_restart_acceptance.py` 不发写请求。
- 显式写开关：`readonly/share_chain_snapshot.py --out`、`readonly/authoritative_readings.ps1 -Out`、`actions/ledger_row_recheck.py --authorized --out`、`actions/generate_attribution_key.py --apply`、`actions/test_key_ops.py`、`readonly/window_preflight.py --out`。它们只写自己声明的目标，不重启服务、不发模型请求。
- `actions/metering_acceptance.py` 与 `readonly/post_restart_acceptance.py` 的 8110 观察走 `forget_stale=False`，不删任何文件。
- `readonly/share_chain_snapshot.py --out` 的 `collect_snapshot` 仍用默认 `forget_stale=True`：拍快照时那份陈旧 `backend.pid` 仍可能被 `os.remove`。这是该工具自己的委托副作用，未消掉。
- 工具自身只发不带凭据的 `GET http://127.0.0.1:<旧链端口>`；委托的 8110 观察带 `X-Admin-Control` 只读 `GET /admin/auth/status`。
- 进程查询只跑 `netstat -ano -p TCP` 与 `Get-CimInstance Win32_Process`；账本只读打开。
- 绝不打印密钥内容或分享 Key 明文：账本里只有 `key_id`，读面只给路径、原因码与 `strip()` 后的长度。
- 本目录的工具不执行 ③重启、④真实请求、⑤回退；②的密钥生成只由 `actions/generate_attribution_key.py --apply` 执行，且只在允许根下写文件。授权与停止点见 [SHARING.md](../../SHARING.md)。

## 回归

```bash
python -m unittest tests.test_um_share_tools tests.test_um_share_metering_ops tests.test_um_share_keygen tests.test_authoritative_readings tests.test_post_restart_acceptance tests.test_share_chain_window tests.test_watchdog_forensics tests.test_window_preflight tests.test_request_budget_estimate tests.test_tool_write_surface -q
```

旧链端口与步骤以 [SHARING.md](../../SHARING.md) 和 [um-share-deploy/README.md](../../um-share-deploy/README.md) 为准。
