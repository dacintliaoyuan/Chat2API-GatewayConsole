# cloud 组合根

`cloud/` 是云端角色的组合根：`run_cloud.py` 入口，`backend/` 装配 KeyApi（分享 Key 管理）与专用 8110 后端，broker 由 systemd 或 fixture 持有。它只装配 UM 分享栈与云端读面，不装配本机客户端、Qoder/CLIProxy/model_sync/ZCode。

## 入口

```bash
python -I cloud/run_cloud.py --config <绝对路径.json>
```

配置字段与退出码见 `cloud/backend/config.py` 和 `run_cloud.py --help`。

## 读面

| 方法 | 路径 | 鉴权 | 返回 |
|---|---|---|---|
| GET | `/health` | 无 | `{"status":"ok","role":"cloud","pid":…}` |
| GET | `/api/summary` | `X-Admin-Control` | 云端价目版本事实；未配置 `price_table_file` 时 `503 summary_unavailable` |
| GET | `/api/share/status` | `X-Admin-Control` | 分享链同步状态 |
| GET/POST | `/api/share/keys` | `X-Admin-Control` | Key 列表 / 创建 |
| GET | `/api/share/keys/<id>` | `X-Admin-Control` | 单个 Key |
| POST | `/api/share/keys/<id>/revoke-and-terminate` | `X-Admin-Control` | 撤销并终止 |
| POST | `/api/share/keys/<id>/rotate` | `X-Admin-Control` | 轮换 |
| POST | `/api/share/drain` | `X-Admin-Control` | 排空；未确认排空返回 `503 drain_not_confirmed` |
| GET | `/api/share/backend` | `X-Admin-Control` | 8110 后端状态 |
| POST | `/api/share/backend/start\|stop` | `X-Admin-Control` | 启停 8110 |
| POST | `/admin/shutdown` | `X-Admin-Control` | 停止管理面 |

除 `/health` 外全部要求 `X-Admin-Control`；非 loopback `Host`、代理头、非 loopback `Origin` 一律 `403 local-only service`。

## 前端 basePath

云端宿主只提供 `/api/share/...`，不提供 `/gw/usagesrv/api/...`。云端前端显式把 `basePath` 设为实际路径；本机宿主的前缀别名只在 `local/backend/app.py` 提供，不复制第二套路由。

`/api/summary` 从 `CloudConfig.price_table_file` 读取价目表，并用 `shared.pricing_rules.normalize_price_table` 归一化；未配置时如实返回 `503 summary_unavailable`，不伪造价格。
