# cloud 组合根（E19-1-11）

本目录是云端角色的组合根：`cloud/run_cloud.py` 入口，`cloud/backend/` 装配
KeyApi（分享 Key 管理）与专用 8110 后端，broker 由 systemd（生产）或本根
（fixture）持有。它**不**读取本机客户端资料，也**不**装配 Qoder / CLIProxy /
model_sync / ZCode 读面。

## 读面

| 方法 | 路径 | 鉴权 | 返回 |
|---|---|---|---|
| GET | `/health` | 无 | `{"status":"ok","role":"cloud","pid":…}` |
| GET | `/api/summary` | `X-Admin-Control` | 云端角色的价目版本事实；未配置 `price_table_file` 时 `503 summary_unavailable` |
| GET | `/api/share/status` | `X-Admin-Control` | 分享链同步状态 |
| GET/POST | `/api/share/keys` | `X-Admin-Control` | Key 列表 / 创建 |
| GET | `/api/share/keys/<id>` | `X-Admin-Control` | 单个 Key |
| POST | `/api/share/keys/<id>/revoke-and-terminate` | `X-Admin-Control` | 撤销并终止 |
| POST | `/api/share/keys/<id>/rotate` | `X-Admin-Control` | 轮换 |
| POST | `/api/share/drain` | `X-Admin-Control` | 排空；未确认排空返回 `503 drain_not_confirmed` |
| GET | `/api/share/backend` | `X-Admin-Control` | 8110 后端状态 |
| POST | `/api/share/backend/start\|stop` | `X-Admin-Control` | 启停 8110 |
| POST | `/admin/shutdown` | `X-Admin-Control` | 停止管理面 |

除 `/health` 外全部要求 `X-Admin-Control`；非 loopback `Host`、代理头、非
loopback `Origin` 一律 `403 local-only service`。

## F14：前端 basePath 的二选一结论

前端 `web/src/console/backend/transport.ts:14` 的默认 `basePath` 是
`/gw/usagesrv/api/console`，`web/src/local/um-data-controller.ts` 的默认是
`/gw/usagesrv/api`。云端宿主只提供 `/api/share/...`，**不**提供
`/gw/usagesrv/api/...` 前缀，因此二选一的结论是：

- **cloud 端**：由调用方（前端 cloud 入口）把 `basePath` 显式设为
  `/api` 或 `/api/console` 对应的实际路径，**不**在云端宿主增加前缀别名。
  cloud 前端只使用分享读面，与 local 的 `/gw/usagesrv/api/console` 前缀
  无关；这里不复制第二套路由。
- **local 端**：由 `local/backend/app.py` 的 `_local_segments` 提供
  `/gw/usagesrv/api` 与 `/gw/usagesrv/api/console` 前缀别名（见
  `local/README.md`）。

受影响读面清单：cloud 只受影响 `GET /api/summary`、`GET /api/share/status`、
`GET|POST /api/share/keys`、`GET /api/share/keys/<id>`、`POST
/api/share/keys/<id>/revoke-and-terminate`、`POST /api/share/keys/<id>/rotate`、
`POST /api/share/drain`、`GET /api/share/backend`、`POST
/api/share/backend/start|stop`；这些读面都不在 `/gw/usagesrv/api` 前缀下。

## 价格表身份

`/api/summary` 从 `CloudConfig.price_table_file` 读取价目表，并用
`shared.pricing_rules.normalize_price_table` 归一化，与本地端读同一份文档得到
同一套纯规则结果。`price_table_file` 是可选的：未配置时该读面如实返回
`503 summary_unavailable`（不伪造价格），已配置时必须通过真实资料守卫。
