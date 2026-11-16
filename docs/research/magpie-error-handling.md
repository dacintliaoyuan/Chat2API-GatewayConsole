# Magpie 错误处理调查：与 Chat2API-GatewayConsole 的逐项对照

- 日期：2026-11-15
- 调查对象：`yetone/magpie`（Go，`main`，提交 `4a4424ea842608e9bcd564bca731aefb11345dc8`，2026-11-15 18:38 +0800）
- 对照对象：本仓库 `D:/git/github/Chat2API-GatewayConsole`（提交 `01b69edf1f65fd442d2799cfe7bdb78c88ea0d6d`，2026-11-15 15:33 +0800）
- 本机运行版本：Magpie 日志显示 `magpie 0.1.4026`（`C:/Users/dacint/AppData/Local/magpie/logs/app.log:10739`），与 `main` 最新提交可能不完全一致；本报告把源码结论和本机日志观察分开标注。

## 1. 范围与方法

### 1.1 做了什么

1. 把 `yetone/magpie` 浅克隆到仓库外临时目录 `C:/Users/dacint/AppData/Local/Temp/magpie-research`，确认工作树干净，记录提交 `4a4424ea842608e9bcd564bca731aefb11345dc8`。
2. 阅读官方文档：`docs/subsystems/gateway-middleware.md`、`gateway-request-hooks.md`、`gateway-routing.md`、`gateway-sessions.md`、`provider-plugins.md`、`providers-accounts.md`，以及 `README.md`、`AGENTS.md` 中与错误、重试、超时、状态码有关的段落。
3. 阅读 Magpie 网关源码：`internal/gateway/gateway.go`、`fallback.go`、`patience.go`、`concurrency.go`、`rpm.go`、`key_limit.go`、`key_slots.go`、`auththrottle.go`、`lan.go`、`cors.go`、`readonly.go`、`usage_api.go`、`kept_error.go`、`pass_errors.go`、`request_bounds.go`、`upstream_transport.go`、`sse.go`、`chat.go`、`anthropic.go`、`responses.go`、`gemini.go`、`unknown_model.go`、`redact.go`、`internal/redact/scrub.go`、`internal/usage/ledger.go`。
4. 只读本机 Magpie 日志 `C:/Users/dacint/AppData/Local/magpie/logs/app.log` 与 `app.old.log`，只摘取状态码、错误消息、重试/回退事实；没有复制请求体、响应体、凭据或 token。
5. 阅读本程序 `chat2api/chat2api.py`、`usagesrv.py`、`chat2api/share_auth.py`、`docs/rust-contract.md`（§5.2、§5.3、§5.4、§6.3、§6.4）、`docs/rust-plan.md`，以及 `tests/test_chat2api_endpoints.py`、`tests/test_chat2api_stream.py`、`tests/test_chat2api_request_rejection.py`、`tests/test_share_gate_merged.py`、`tests/test_cloud_role_assembly.py`、`tests/test_um_stats_read_surface.py`。
6. 向本机 Magpie（`127.0.0.1:3250`）和本程序（`127.0.0.1:8000`、`127.0.0.1:8001`）发送无 Key、无真实上游的无效请求，观察状态码、错误体和响应头。没有发送任何可能产生费用的推理请求，也没有带真实 Key 访问上游。

### 1.2 没有做什么

- 没有运行 Magpie 或本程序的测试套件。
- 没有触发 Magpie 的 401、403、408、409、429、500、502、503、504，也没有触发本程序的 401、403、429、502、503、504 或真实 SSE。
- 没有做受控的并发/限流/重试实验；重试次数、退避和回退路径来自源码与文档，不是实测。
- 没有验证 Magpie 在真实付费上游上的 4xx 透传、429 `Retry-After` 或断流行为。
- 本机日志是历史运行记录，不是受控实验；统计数字只说明“曾经出现过”，不能说明触发条件或概率。

## 2. 结论摘要

1. **错误体**：Magpie 按客户端协议返回三种错误体：OpenAI 形 `{"error":{"message","type","code","param"}}`、Anthropic 形 `{"type":"error","error":{"type","message"}}`、Gemini 形 `{"error":{"code","message","status"}}`。`code`/`param` 即使为空也保留为 `null`。本程序只返回 OpenAI 形 `{"error":{"message","type","detail"}}`；`usagesrv.py` 多数返回 `{"error":"机器码"}`，两者形状不同。
2. **Content-Type 与缓存头**：Magpie 自产错误用 `application/json`，普通错误不设 `Cache-Control: no-store`；SSE 用 `text/event-stream; charset=utf-8` 和 `Cache-Control: no-cache`。本程序 `chat2api.py` 用 `application/json; charset=utf-8`，普通错误也不设 `no-store`；`usagesrv.py` 的 JSON 响应统一 `Cache-Control: no-store`。
3. **连接关闭**：Magpie 只在请求体被拒绝的路径（413、415 等）设 `Connection: close`；普通 400/404/405 不断连；SSE 设 `Connection: keep-alive`。本程序 `chat2api.py` 的所有 `_error()` 都设 `Connection: close`，SSE 也设 `Connection: close`；`usagesrv.py` 不设 `Connection` 头。本程序更保守，Magpie 更接近长连接复用。
4. **上游 4xx 透传**：Magpie 对同形上游错误保留原状态码和原错误体，只改 `error.message` 为“provider 名 + 上游原话 + 说明”；`errors=pass` 时完全原样透传。本程序 Chat 路径只对 400/422 保留状态码，401 改成 503，其余非 200 一律 502；Messages 路径原样透传；Responses 路径只保留 400/422，401 转 503，其余转 502。这是最值得对照的差异。
5. **重试与回退**：Magpie 对 401/402/403/404/408/429/5xx 默认可回退，同一候选重试 2 次（429 为 3 次），组内最多 12 轮、总预算 540 秒，且只在回复尚未发出时回退。本程序只对 408/429/500/502/503/504 重试，快速失败最多 5 次、慢失败最多 2 次、物理尝试 12 次、总预算 360 秒；401/403/404 不重试，Chat 路径把 401 变成 503、把 403/404 变成 502。
6. **SSE**：两者都坚持“提交前失败返回 HTTP 错误，提交后失败写 in-band error，不把部分完成当成功”。Magpie 自己解码并重新编码 `[DONE]`/协议事件，错误事件分别是 Chat 的 `data: {"error":...}`、Anthropic 的 `event: error`、Responses 的 `response.failed`、Gemini 的 `{"error":...}`。本程序 Chat 路径按字节透传上游 SSE，错误时写 `data: {"error":{...,"detail":...}}`，且不转发 `[DONE]`。
7. **限流与配额**：Magpie 有网关 Key 的预算/并发、provider 的 `MaxConcurrency`/`MaxRPM`、认证失败限流和 lane 队列；本地 429 会带 `Retry-After`（Key 限流还带 `X-Should-Retry: false`）。本程序分享门有每 Key 并发、全局并发、每 Key 20 次/60 秒，429 `sharing_limit_reached` 用 `reason` 区分，但不带 `Retry-After`；本程序的重试 429 也不把上游 `Retry-After` 交给客户端（只有重新登录 503 会带）。
8. **鉴权与路由**：Magpie 的 401 是“没有/无效网关 Key”，403 是“浏览器来源、只读 Key、被限制的 Key”，404 是“未知路由/未知模型”；405 只出现在 `GET /v1/responses` 等显式路由，其他方法不匹配会落到兜底 `/` 变成 404。本程序分享模式 401 `invalid_api_key`、404 `route_not_shared`、429 `sharing_limit_reached`；方法不允许时 Python `BaseHTTPRequestHandler` 返回 501 HTML，不是 405 JSON。
9. **超时**：Magpie 的请求体读取超时 60 秒返回 408；上游响应头等待 10 分钟；组内首 token 超时返回 504；组总预算 540 秒；客户端断开在 usage 里记 499。本程序首字节 120 秒、单次 socket 静默 180 秒、总预算 360 秒，超时统一 502，没有 504，也没有请求体读取超时；客户端断开不产生 HTTP 499，只在 usage 里记 `client_disconnected`。
10. **日志与脱敏**：Magpie 失败日志只写 agent/model/provider/status/耗时/错误文本（错误文本截到 300 字符），同一失败每分钟最多一行；流结束日志只写经过校验的 trace 头；归档和头日志读取时脱敏。本程序失败日志写 stage/upstream_status/exception/downstream/stream/model/耗时，不写请求体、响应体和凭据；`usagesrv.py` 只打印异常类型。

## 3. Magpie 错误处理模型

### 3.1 错误体形状、Content-Type、no-store、连接关闭

Magpie 的 `writeError` 最终调用 `writeErrorAs`，按客户端协议选择错误体：

- Chat 和 Responses：`{"error":{"message":...,"type":...,"code":...,"param":...}}`；`code` 和 `param` 没有值时仍输出 `null`（`internal/gateway/gateway.go:11145-11161`）。
- Anthropic：`{"type":"error","error":{"type":...,"message":...}}`（`internal/gateway/gateway.go:11148-11150`）。
- Gemini：`{"error":{"code":<HTTP 状态>,"message":...,"status":"INVALID_ARGUMENT|...|UNKNOWN"}}`（`internal/gateway/gateway.go:11150-11156`）。

`errorTypeFor` 把 400 映射为 `invalid_request_error`、401 为 `authentication_error`、403 为 `permission_error`、404 为 `not_found_error`、413 为 `request_too_large`、429 为 `rate_limit_error`、529 为 `overloaded_error`，其余为 `api_error`（`internal/gateway/gateway.go:11119-11141`）。

`writeJSON` 只设 `Content-Type: application/json`，没有 `Cache-Control`（`internal/gateway/gateway.go:10919-10925`）。本机无 Key 探测也确认：

- `POST /v1/chat/completions` 空体返回 400，`Content-Type: application/json`，错误体含 `"code":null` 与 `"param":null`，没有 `Cache-Control`，没有 `Connection: close`。
- `GET /no-such-route` 返回 404，`Content-Type: application/json`，没有 `Cache-Control`，没有 `Connection: close`。
- `POST` 超大 `Content-Length` 返回 413，`Content-Type: application/json`，`Connection: close`，错误体 `type` 为 `request_too_large`。
- `POST` 带 `Content-Encoding: br` 返回 415，`Content-Type: application/json`，`Connection: close`，错误体 `type` 为 `api_error`。

因此，Magpie 的普通错误响应**不保证 no-store**；只有管理 API（`internal/gateway/admin.go:48`）和共享 Library（`internal/gateway/gateway.go:1318`）等少数端点显式设 `Cache-Control: no-store`。连接关闭也不是所有错误都设：请求体拒绝路径显式 `Connection: close`（`internal/gateway/request_bounds.go:110`、`internal/gateway/request_bounds.go:216-224`），其余普通错误不设。SSE 的 `sseWriter.begin` 设 `Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`Connection: keep-alive`、`X-Accel-Buffering: no`（`internal/gateway/sse.go:80-95`）。

### 3.2 状态码映射

Magpie 的状态码有两条来源：本地拒绝和上游透传。

本地拒绝：

| 状态 | Magpie 的触发 | 源码 |
|---|---|---|
| 400 | 请求 JSON 不是对象、模型名不是非空字符串、请求体无法解析 | `internal/gateway/gateway.go:9982-9996`；本机观察 400 `invalid request: model must be a nonempty string` |
| 401 | 远端请求没有有效网关 Key；Key 被禁用、删除或拼错 | `internal/gateway/lan.go:797-833`、`internal/gateway/lan.go:947-955` |
| 403 | 浏览器来源未被允许；只读 Key 访问模型路由；被预算/模型/账户限制的 Key 读取他人配额；管理 API 权限不足 | `internal/gateway/cors.go:50-98`、`internal/gateway/readonly.go:90-117`、`internal/gateway/usage_api.go:48-57`、`internal/gateway/admin.go:43-78` |
| 404 | 未知路径；未知模型 | `internal/gateway/gateway.go:1184-1186`、`internal/gateway/gateway.go:2789-2814`、`internal/gateway/unknown_model.go:15-28` |
| 405 | 只有 `GET /v1/responses` 这类显式路由；其他方法不匹配会落到兜底 `/`，表现为 404 | `internal/gateway/gateway.go:1193-1203`；本机观察 `PUT /v1/chat/completions` 返回 404 |
| 408 | 请求体读取超时 | `internal/gateway/request_bounds.go:239-243` |
| 409 | CPU profile 已在录制、Codex reset 已被占用等本地冲突 | `internal/gateway/cpu.go:93-102`、`internal/gateway/gateway.go:1430` |
| 413 | 请求体超过 Magpie 自己的字节上限（默认 512 MB，Codex 路径最多 2 GB） | `internal/gateway/request_bounds.go:29-32`、`internal/gateway/request_bounds.go:232-265`、`internal/gateway/request_bounds.go:339-341` |
| 415 | 不支持的 `Content-Encoding` | `internal/gateway/request_bounds.go:254-257` |
| 422 | Magpie 本地一般不用 422；上游 422 会按上游状态透传 | `internal/gateway/fallback.go:2039-2080` |
| 429 | 网关 Key 预算/并发、provider lane/队列、MaxRPM、认证失败限流 | `internal/gateway/key_limit.go:42-107`、`internal/gateway/concurrency.go:423-432`、`internal/gateway/rpm.go:16-59`、`internal/gateway/auththrottle.go:21-57` |
| 500 | CPU profile 启动失败、Library 读取失败等本地内部错误 | `internal/gateway/cpu.go:102`、`internal/gateway/gateway.go:1315` |
| 502 | 连接失败、非 API 的 2xx 响应、断流、空回复 | `internal/gateway/gateway.go:9331`、`internal/gateway/fallback.go:3609-3644`、`internal/gateway/fallback.go:3225-3241`、`docs/subsystems/gateway-routing.md:264` |
| 503 | 请求体等待内存失败、masker 不可用、管理 API 未装配 | `internal/gateway/request_bounds.go:110-112`、`internal/gateway/redact.go:226-234`、`internal/gateway/admin.go:78` |
| 504 | 组内首 token 超时、组预算耗尽 | `internal/gateway/gateway.go:4561-4588` |

上游状态透传：

- 上游错误的状态码**保留**，不会统一改成 502。`writeVendorErrorFrom` 在错误体形状与客户端协议一致时，原样写出上游状态、`type`、数字 `code`、`param`、`request_id` 和未知字段，只改写 `error.message`（`internal/gateway/kept_error.go:41-101`）。
- 上游错误体形状与客户端协议不同时，用同一上游状态在客户端协议里重建错误体（`internal/gateway/kept_error.go:45` 调 `writeVendorError`，`internal/gateway/gateway.go:11079-11090`）。
- `errors=pass` 的 provider 完全原样透传上游状态、`Content-Type` 和错误体，连 `Retry-After` 与 `RateLimit-*` 也一起带出（`internal/gateway/pass_errors.go:22-59`）。
- 上游 413 或“请求太长”会被特殊处理：过长（`tooLong`）改成 400 `invalid_request_error` + `context_length_exceeded`，413 字节上限则加 `requestBytesHint` 提示（`internal/gateway/gateway.go:11101-11113`）。
- 上游 2xx 但不是 API 的答案（网页、空体、HTML、既非 JSON 也非 SSE）会变成 502（`docs/subsystems/gateway-routing.md:264`）。

上游 4xx 是否原样透传，Magpie 的答案是“同形则原样，异形则同状态重建，`errors=pass` 时完全原样”，不是“一律 502”。

### 3.3 重试与回退

Magpie 把“可重试”和“会过去”分开：

- `retryable(status, body)` 认为 401、402、403、404、408、429、5xx 可换下一个候选；400/422 只有在上游文字表示额度、未服务、形状、区域、推理回指等情况下才可重试；上下文超长（`overflowed`）不可重试（`internal/gateway/fallback.go:2039-2080`）。
- `passing(status, header, body, again)` 处理“同一个候选再试一次”：408/500/502/503/504/529/520-524 等 `busyStatus` 最多 `lastRetries = 2` 次；429 最多 `rateRetries = 3` 次，但要求 `failure == failRate` 且不是余额/额度/账户 token 墙；`Retry-After` 会覆盖默认等待；等待超过 `longestPause = 8s` 就放弃同候选重试（`internal/gateway/fallback.go:2312-2375`）。
- 默认退避是 `retryPause = 1s`，每次翻倍；429 的等待可以取 `Retry-After`；组内 `replan` 的暂停是 2s、4s、8s、16s，之后每轮最多 30s，最多 12 轮（`internal/gateway/fallback.go:2322-2329`、`internal/gateway/patience.go:19-48`、`internal/gateway/patience.go:159-180`）。
- 组总预算 `groupBudget = 540s`（`internal/gateway/fallback.go:49`）。组内首 token 超时由 `Group.FirstToken` 配置；超时后如果有下一个成员就换成员，否则 504（`internal/gateway/gateway.go:4561-4588`）。
- 回退只发生在“回复尚未发给客户端”时：`holdWriter` 在首个实质内容前保持上游响应，失败就丢弃并换下一个候选；一旦内容已发出，就不再回退，只能在流内写错误（`docs/subsystems/gateway-routing.md:250`、`internal/gateway/fallback.go:2608-2684`、`internal/gateway/fallback.go:3699-3725`）。
- 回退范围包括同一 provider 的其他账号、路由组的其他成员、provider 的 fallback 模型、以及同一模型的另一个 provider；具体由候选循环和路由组决定（`internal/gateway/fallback.go:1984-1995`、`docs/subsystems/gateway-routing.md:248-276`）。
- 重试对客户端是隐藏的：客户端只看到最终答案或最终错误；失败尝试记在 usage/trace 的 `tries`、`fallback` 和失败行里（`docs/subsystems/gateway-routing.md:253-254`）。
- `Retry-After`：上游头会随错误带给客户端（`internal/gateway/routing.go:2272-2300`）；同候选重试最多等 8 秒，超过就交给组 patience 或直接把错误给客户端；`retry_after_cap` 可把发给客户端的 `Retry-After` 截到设置上限（`internal/gateway/retry_after_cap.go:23-77`）。

### 3.4 SSE

Magpie 的 SSE 行为可以概括为：

- **首字节前失败**：上游响应被 `holdWriter` 持有，尚未向客户端提交 200；如果没有别的候选，客户端收到 HTTP 错误（通常是 502）；如果已经因 keepalive 发过 200 和 SSE 注释，则只能在流内写错误（`internal/gateway/fallback.go:2608-2684`、`internal/gateway/gateway.go:10927-10940`）。
- **提交后失败**：`streamError` 按协议写一个错误事件，不写 `[DONE]`，不把部分完成当成功。Chat 写 `data: {"error":{"message","type","code"?}}`；Anthropic 写 `event: error` + `data: {"type":"error","error":{...}}`；Responses 写 `response.failed`；Gemini 写 `data: {"error":{"code":502,"message":...,"status":"UNAVAILABLE"}}`（`internal/gateway/chat.go:1756-1767`、`internal/gateway/anthropic.go:1863-1876`、`internal/gateway/responses.go:2461-2471`、`internal/gateway/gemini.go:828-830`）。
- **in-band error**：上游自己在 200 流里发错误事件时，Magpie 把它作为失败，不让它变成成功；`carriedStatus` 会优先采用错误对象自己命名的状态（`internal/gateway/routing.go:544-567`、`docs/subsystems/gateway-routing.md:252`）。
- **客户端断开**：请求 context 取消，记 499，不再重试，也不再给客户端写错误；`canceled()` 返回 `usage.StatusCanceled`（499）和 `the agent canceled the request`（`internal/gateway/gateway.go:4553-4559`、`internal/usage/ledger.go:57-68`、`internal/gateway/side_deadline.go:71-76`）。
- **部分完成**：如果回复已经开始，状态仍是 200，但 `call.Error` 非空，不记为成功；不再重试；usage 记失败（`internal/gateway/gateway.go:5521-5528`、`docs/subsystems/gateway-routing.md:253`）。
- **`[DONE]` / 终止**：Magpie 自己编码 Chat 的 `[DONE]`（`internal/gateway/chat.go:1783`）；上游 `[DONE]` 只是被读取，不直接转发。上游缺少终止事件时，按 `errEndedShort` 处理：未提交则 502，已提交则流内错误（`internal/gateway/continuation.go:80-85`、`internal/gateway/fallback.go:3609-3644`）。
- **空回复 / 截断**：`emptyEnded`、`errEndedShort`、`didNotStream` 都是 502；如果已经发过 200，则写流内错误（`internal/gateway/fallback.go:3225-3241`、`internal/gateway/fallback.go:3609-3644`）。

### 3.5 限流与配额

- **网关 Key 预算**：`keyLimited` 在请求前检查 Key 的预算/模型/账户限制；拒绝时 429，`Retry-After` 是重置时间，`X-Should-Retry: false` 让 SDK 不要立刻重试；usage 的 `ErrType` 记为 `gateway_key_limit`；如果配置了限制但 limiter 未装配，则 503 `gateway_key_limit_off`（`internal/gateway/key_limit.go:42-107`）。错误体本身没有机器可读的 `reason` 字段。
- **provider 并发/队列**：`MaxConcurrency` 限制每个 provider 的 key/账号同时在途数；队列满或等待超过 `QueueWait` 时 429，消息说明限制和等待时间（`internal/gateway/concurrency.go:423-432`、`internal/gateway/gateway.go:4395-4422`）。
- **provider 速率**：`MaxRPM` 是每个 key/账号在任意 60 秒内发出的请求数；超过时等待，最多等 2 分钟（或 provider 的 `QueueWait`），等不到就 429 并带 `Retry-After`（`internal/gateway/rpm.go:16-59`、`internal/gateway/rpm.go:90-137`）。
- **认证失败限流**：远端错误 Key 的洪泛由一个共享令牌桶限制；桶空后返回 429，并带 `Retry-After`（`internal/gateway/auththrottle.go:21-57`、`internal/gateway/lan.go:821-827`）。
- **Magpie 没有“分享 Key 的全局并发上限”这一层**：它是本机网关，限流对象是网关 Key、provider key/账号和 provider 的 RPM，不是多租户分享链的 per-key/global 份额。429 错误体也没有机器可读的 `reason` 字段，原因体现在文本和 usage 的 `ErrType` 里。

### 3.6 鉴权与路由

- **401**：没有 Key，或 Key 不是启用的 Magpie 网关 Key；远端请求才检查，回环地址默认接受任意 token（`internal/gateway/lan.go:797-833`、`internal/gateway/lan.go:947-955`）。
- **403**：浏览器来源未列入允许列表、沙箱/本地页面没有 Key、只读 Key 访问模型路由、被预算/模型/账户限制的 Key 读取他人配额、管理 API 权限不足（`internal/gateway/cors.go:50-98`、`internal/gateway/readonly.go:90-117`、`internal/gateway/usage_api.go:48-57`、`internal/gateway/admin.go:43-78`）。
- **404**：未知路径返回 `not_found_error`；未知模型返回 `magpie knows no model "..."`，并给出最接近的模型名和已知模型 id（`internal/gateway/gateway.go:1184-1186`、`internal/gateway/gateway.go:2789-2814`、`internal/gateway/unknown_model.go:15-28`）。
- **方法不允许**：`GET /v1/responses` 显式返回 405 和 `Allow: POST`（`internal/gateway/gateway.go:1193-1203`）；其他路径的方法不匹配会落到兜底 `/`，本机观察 `PUT /v1/chat/completions` 返回 404，不是 405。
- **未知模型 vs 未知路由**：两者都是 404，但错误消息不同；未知模型会给出模型命名方式和候选 id，未知路由只列出 Magpie 服务的路径（本机观察）。

### 3.7 超时、取消与传播

- **请求体读取**：默认 60 秒；超时返回 408 `request body read timed out`，并设 `Connection: close`（`internal/gateway/request_bounds.go:29-32`、`internal/gateway/request_bounds.go:239-243`）。
- **上游响应头等待**：非绘图请求默认 10 分钟；超时是传输错误，最终 502（`internal/gateway/upstream_transport.go:47-49`、`internal/gateway/upstream_transport.go:79-88`、`internal/gateway/gateway.go:9331`）。
- **首 token**：组内 `FirstToken` 配置；超时且无内容时，若有下一个成员就换成员，否则 504 `no first token in Ns`；组总预算 `groupBudget = 540s`（`internal/gateway/gateway.go:4561-4588`、`internal/gateway/fallback.go:49`）。
- **静默/保持**：持有流最多 15 秒等首个内容，之后让出；思考最多保持 4 分钟；ChatGPT 后端缓冲最多 4 分钟；已提交的流每 3 秒起发 SSE 注释保活，每 5 秒一次（`internal/gateway/fallback.go:3058-3086`、`internal/gateway/fallback.go:3286-3302`、`internal/gateway/fallback.go:3335-3344`）。
- **取消传播**：请求 context 取消即停止上游请求、停止读、释放 slot；客户端断开不会写错误响应，只记 499（`internal/gateway/gateway.go:4553-4559`、`internal/gateway/fallback.go:3699-3725`）。
- **没有统一的“每请求总预算”**：Magpie 主要靠组预算 540 秒、上游头等待 10 分钟和客户端自己的 context；不是本程序那种 360 秒统一总预算。

### 3.8 日志与脱敏

- 失败调用写 `app.log`：`gateway: <agent>'s call to <model> on <provider> failed: <status> after <duration>: <error>`；错误文本截到 300 字符；同一失败每分钟最多一行，重复次数用 `(and N times more like it since the last line)` 计数（`internal/gateway/gateway.go:702-768`）。
- 流结束日志写 model/status/data_lines/duration/上游 request id/server/connection；trace 值只在“可声明”时原样写，否则写 `unavailable`；不写请求体、响应体或凭据（`internal/gateway/gateway.go:9591-9606`、`internal/gateway/wire_headers.go:1-16`）。
- 归档和头日志读取时脱敏：`redact.Scrub`、`redact.ScrubJSON`、`redact.ScrubHeader`；头日志只保留名字，值在读取时脱敏（`internal/gateway/archive.go:284`、`internal/gateway/archive.go:291-293`、`internal/gateway/archive.go:339-358`、`internal/redact/scrub.go:9-38`）。
- usage 行不写请求/响应体、prompt、错误文本、session 或 request id；只写时间、操作、agent、provider/account/key、模型、token、耗时、status、errType、tries、cost 等白名单字段（`docs/subsystems/gateway-middleware.md:41`）。
- 会话捕获失败不改变模型响应；文件系统失败只记日志，不带会话内容（`docs/subsystems/gateway-sessions.md:564-565`）。

### 3.9 中间件与请求 hook 的失败

这两层不是模型错误，但会影响“错误是否到达客户端”：

- JS 网关中间件 hook 有 250ms（`onRequest`/`onAccount`/`onProvider`/`onResponse`）或 50ms（`onEvent`）上限；超时、抛错、返回非法值时 **fail open**，请求或事件按原样继续，失败计数并记录最近错误，最多每分钟 60 行（`docs/subsystems/gateway-middleware.md:46-53`）。
- 异步请求 hook 的 `gatewayTimeoutMs` 默认 1000ms，范围 100..30000ms；链预算 2..10 秒，单个 hook 更长时取更长值；宿主启动最多 11 秒。失败保留最后一次被接受的请求，诊断只写固定结果码和字节数，不写私有内容（`docs/subsystems/gateway-request-hooks.md:83-97`）。
- 如果脱敏插件要求 mask 但没有 masker 服务，请求会被拒绝并返回 503，绝不把原文发给上游（`README.md:146`、`internal/gateway/redact.go:226-234`）。

## 4. 本程序错误处理模型

### 4.1 错误体与响应头

`chat2api.py` 的 `_error` 固定返回：

```json
{"error": {"message": "...", "type": "...", "detail": null}}
```

`type` 默认 `chat2api_error`，可被 `error_type` 覆盖；`extra` 可以追加 `code`、`reason`、`detail` 等字段（`chat2api/chat2api.py:6946-6957`）。`_json` 设 `Content-Type: application/json; charset=utf-8`、`Content-Length`，如果 `close_connection` 为真则设 `Connection: close`；没有 `Cache-Control`（`chat2api/chat2api.py:6929-6944`）。本机观察 400、404、413 均带 `Connection: close`。

`usagesrv.py` 的 `_send_json` 设 `Content-Type: application/json; charset=utf-8`、`Content-Length`、`Cache-Control: no-store`，不设 `Connection`（`usagesrv.py:1649-1656`）。`usagesrv.py` 的错误体多数是 `{"error":"机器码"}`，不是 `chat2api.py` 的嵌套形状（`usagesrv.py:3140-3145`、`usagesrv.py:3174-3179`、`usagesrv.py:3414-3417`）。

### 4.2 状态码与上游映射

`chat2api.py` 的 Chat 路径：

- 400：`invalid_request` / `invalid_json` / `invalid_body` / `unsupported_chat_request`；请求体缺失 `messages` 也是 400（`chat2api/chat2api.py:7115-7142`、`chat2api/chat2api.py:8405-8412`）。
- 401：`invalid_api_key`，分享门鉴权失败（`chat2api/chat2api.py:7209-7214`）；非 loopback 绑定时缺少本地 Key 是 `local_auth_error`（`chat2api/chat2api.py:7066-7083`）。
- 403：`origin_denied`，浏览器来源被拒（`chat2api/chat2api.py:7099-7103`）；本机管理端点也有 403（`chat2api/chat2api.py:7052-7064`）。
- 404：`Unknown path: ...`；分享模式非白名单路由是 `route_not_shared`（`chat2api/chat2api.py:7369-7370`、`chat2api/chat2api.py:7191-7207`）。
- 413：`payload_too_large`（`chat2api/chat2api.py:7123-7129`）。
- 429：分享门 `sharing_limit_reached`，`reason` 区分 `per_key_concurrency`、`share_concurrency`、`key_rate_limit`、`state_not_accepting`（`chat2api/chat2api.py:7217-7224`、`chat2api/share_auth.py:1305-1362`）。
- 502：上游请求失败、上游非法 JSON、流错误、请求 deadline、物理尝试预算耗尽（`chat2api/chat2api.py:8772-8776`、`chat2api/chat2api.py:8931-8978`、`chat2api/chat2api.py:8709-8720`）。
- 503：`reauthentication_required`（带 `Retry-After`，如果存在确定的下次尝试）、`too_many_concurrent_streams`（`chat2api/chat2api.py:6959-6970`、`chat2api/chat2api.py:8676-8682`）。
- 504：本程序没有本地 504 发射点；504 只作为可重试上游状态出现（`chat2api/chat2api.py:566`）。
- 501：Python `BaseHTTPRequestHandler` 对未实现的 HTTP 方法返回 HTML，不是 405；本机观察 `PUT /v1/chat/completions` 返回 501。

上游状态的处理：

- Chat 路径：401 → 503 `reauthentication_required`；400/422 → 原状态 + `upstream_request_rejected`；其他非 200 → 502 `upstream_request_failed`，`detail` 带 `HTTP <status>: <body 前 2048 字符>`（`chat2api/chat2api.py:8743-8776`）。也就是说 Chat 路径**不原样透传** 403、404、409、413、429、5xx 的状态，而是统一 502（429 在重试预算内先重试）。
- Messages 路径：非 200 状态用 `_passthrough` 原样转发上游状态、`Content-Type`、`Content-Length`、`Cache-Control`（`chat2api/chat2api.py:8362-8396`、`chat2api/chat2api.py:9692-9708`）。
- Responses 路径：401 → 503 `reauthentication_required`；400/422 → 原状态 `upstream_request_rejected`；其他 → 502（`chat2api/chat2api.py:7582-7597`）。
- 上游 200 但 body 是 `{"error":...}`：非流式 Chat 会把它当失败，返回 502 `upstream_explicit_error`，不会当成功（`chat2api/chat2api.py:8800-8821`）。
- 上游 200 流里出现 error 事件：非流式聚合返回 502 `upstream_stream_error`，流式则在已提交 200 后写 in-band error（`chat2api/chat2api.py:8931-8978`、`chat2api/chat2api.py:9639-9660`）。

`usagesrv.py`：

- 503 `capability_not_assembled`：云端角色只装配 UM 分享，非 UM 读面明确未装配；响应体是 `{"error":"capability_not_assembled","capability":...,"role":"cloud","detail":...}`（`usagesrv.py:3165-3179`）。
- 503 `console subsystem unavailable`：console 子系统不可用；响应体是 `{"error":"console subsystem unavailable"}`（`usagesrv.py:3412-3418`）。
- 502：读面/查询失败（`usagesrv.py:3297-3300`、`usagesrv.py:3422-3423`）。
- 500：分享管理内部错误（`usagesrv.py:3140-3143`）。
- 413：请求体超过 64 KiB（`usagesrv.py:1815-1816`）。
- 405：非 GET 的只读面（`usagesrv.py:2804`）。
- 401/403/409/410：管理鉴权、来源、幂等冲突、已删除 Key（`usagesrv.py:2028`、`usagesrv.py:2772`、`usagesrv.py:1026-1031`、`usagesrv.py:2313`）。

### 4.3 重试与回退

- 可重试状态：408、429、500、502、503、504（`chat2api/chat2api.py:566`）。
- 快速失败最多 5 次；耗时达到 30 秒的慢失败最多 2 次；物理尝试上限 12；请求总预算默认 360 秒（10..3600 可调）（`chat2api/chat2api.py:538-565`、`chat2api/chat2api.py:458-462`）。
- 重试延迟：429 的 `Retry-After` 最多取 10 秒；快速失败 0.5s、1s、2s 封顶；慢失败 1-3 秒随机（`chat2api/chat2api.py:720-747`）。
- 只在客户端尚未收到可见输出前重试；提交后失败不重试，写 in-band error，避免把两次完成拼在一起（`chat2api/chat2api.py:9028-9030`、`chat2api/chat2api.py:9150-9301`、`docs/rust-contract.md:171-174`）。
- 重试对客户端隐藏；usage 里用 `upstream_transient_retried`、`degenerate_repetition_retried` 等状态记录失败尝试（`chat2api/chat2api.py:9228-9232`、`chat2api/chat2api.py:9288-9292`）。
- 上游 401 不重试，直接 503 `reauthentication_required`；上游 403/404 不在可重试集合，Chat 路径直接 502（`chat2api/chat2api.py:8743-8776`）。

### 4.4 SSE

- Chat 流：200 头在首个“实质数据”事件出现后才提交；提交前失败返回 502；提交后失败写 `data: {"error":{"message","type":"upstream_stream_error","code":...,"detail":{...}}}`，不写 `[DONE]`（`chat2api/chat2api.py:9028-9043`、`chat2api/chat2api.py:9639-9690`）。
- Chat 流的 200 响应头：`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、`Connection: close`（`chat2api/chat2api.py:9410-9415`）。
- Messages 流：上游 SSE 字节透传；`message_stop` 前 EOF 不算成功；上游 `error` 事件立即终止并原样转发（`chat2api/chat2api.py:9767-9780`、`chat2api/chat2api.py:9858-9869`、`tests/test_chat2api_endpoints.py:589-611`）。
- Responses 流：`response.created`、`response.output_text.delta`、`response.function_call_arguments.delta`、`response.completed`、`response.incomplete`、`response.failed`；上游流失败写 `response.failed`（`chat2api/chat2api.py:7696-7699`、`chat2api/chat2api.py:7712-7748`）。
- 客户端断开：每个字节检查；断开后关闭上游、释放 reader slot，usage 记 `client_disconnected`，不写错误响应（`chat2api/chat2api.py:5621-5640`、`chat2api/chat2api.py:9541-9543`、`chat2api/chat2api.py:9573-9575`）。
- 部分完成：状态仍 200 但写 in-band error，usage 记失败，不记 `ok`（`chat2api/chat2api.py:9646-9667`）。
- `[DONE]`：上游 `[DONE]` 是终止条件；缺 `finish_reason` 的 `[DONE]` 是 `upstream_missing_finish_reason`；EOF 无 `[DONE]` 是 `upstream_eof_without_done`；零数据断流在提交前是 502（`chat2api/chat2api.py:9445-9449`、`chat2api/chat2api.py:9553-9555`、`chat2api/chat2api.py:1043-1078`）。

### 4.5 限流与配额

- 分享门：每 Key 并发、全局分享并发、每 Key 20 次/60 秒滑动限速，以及 `state_not_accepting`；拒绝是 429 `sharing_limit_reached`，`reason` 区分 `per_key_concurrency`、`share_concurrency`、`key_rate_limit`、`state_not_accepting`（`chat2api/share_auth.py:1305-1362`、`chat2api/chat2api.py:7217-7224`）。
- 分享 429 **不带 `Retry-After`**：`_error(429, ...)` 只带 `extra={"code","reason","detail"}`（`chat2api/chat2api.py:7217-7224`）。
- reader 槽位：全局最多 128 个并发流；满了返回 503 `too_many_concurrent_streams`（`chat2api/chat2api.py:573-577`、`chat2api/chat2api.py:8676-8682`）。
- 上游 429：在重试预算内重试；最终 Chat 路径 502，Messages 路径原样 429；`Retry-After` 只在重新登录 503 时通过 `auth_retry_after()` 带出（`chat2api/chat2api.py:6959-6970`、`chat2api/chat2api.py:5041-5064`）。

## 5. 场景 × Magpie × 本程序对照表

说明：本表把“上游 4xx 是否原样透传”拆成 Chat/Messages/Responses 三条路径，因为本程序三条路径的行为不同。Magpie 列写的是“Magpie 自产”与“上游透传”两种情况。本程序的 Messages“原样”指状态和响应体按 `_passthrough` 转发，但它只复制 `Content-Type`、`Content-Length`、`Cache-Control`，不复制上游的 `Retry-After`/`RateLimit-*`（`chat2api/chat2api.py:9692-9708`）。

| 场景 | Magpie 状态码 / 错误体 / 重试 | 本程序状态码 / 错误体 / 重试 | 差异与建议 |
|---|---|---|---|
| 请求体不是 JSON 对象 / 缺 model | 400；`invalid_request_error`；`code`/`param` 为 null；不重试 | Chat 400；`{"error":{"message":"Request body is not valid JSON","type":"invalid_json","detail":null}}`；不重试 | 形状不同；本程序的 `detail` 是自有字段，Magpie 用 `code`/`param`。如果 Rust 目标要保持 OpenAI 兼容，建议同时保留 `code`/`param` 或明确冻结 `detail` 契约 |
| 未知路由 | 404；`not_found_error`；`message` 列出 Magpie 服务的路径；不重试 | 404；`{"error":{"message":"Unknown path: ...","type":"chat2api_error","detail":null}}`；不重试 | 都是 404，但本程序 `type` 是通用 `chat2api_error`；建议 API 路径用更具体的 `not_found_error` 或 `route_not_found` |
| 方法不允许 | `GET /v1/responses` 405 + `Allow: POST`；其他方法不匹配落到兜底 `/`，本机观察 `PUT /v1/chat/completions` 为 404；不重试 | Python `BaseHTTPRequestHandler` 返回 501 HTML；本机观察 `PUT /v1/chat/completions` 为 501 | 差异明显；本程序没有 405 JSON，建议 API 路径统一 405 + `Allow`，不要 501 HTML |
| 缺少 / 无效分享 Key | 401；`authentication_error`；`no API key was sent...` 或 `the API key ending in ... is not an enabled magpie gateway key`；不重试 | 401；`invalid_api_key`，`type` 为 `share_gateway_error`，`code` 为 `invalid_api_key`；不重试 | 都是 401；本程序把机器码放在 `code`，Magpie 只在文本里；建议本程序保留 `code` 并考虑增加 `param` |
| 浏览器来源 / 只读 Key / 被限制 Key | 403；`permission_error`；CORS、只读、预算/模型/账户限制各有文本；不重试 | 403；`origin_denied` 或管理端点文本；不重试 | 都是 403；本程序分享模式没有“只读 Key”层；建议 Rust 契约保留 403 与 `permission_error` |
| 未知模型 | 404；`not_found_error`；`magpie knows no model "..."`，列出最近模型和全部模型 id；不重试 | 404；`unknown model` / `Model not found`；Chat 不重试；Messages 原样透传上游 404 | 本程序本地未知模型文案较简单；建议在 `detail` 中给出候选模型，便于客户端修正 |
| 上游 400 | 同形：原 400 + 原错误体，只改 `error.message`；异形：同 400 重建；400 可按文字重试；`errors=pass` 完全原样 | Chat 400 + `upstream_request_rejected`，`detail` 带 `upstream_status`；Messages 原样 400；Responses 400 + `upstream_request_rejected`；不重试 | 本程序 Chat/Responses 保留状态但改错误体；Magpie 尽量保留上游 body。建议 Chat/Responses 参考 `writeVendorErrorFrom`，至少保留上游 `type`/`code`/`param` |
| 上游 401 | 可重试；最终原 401 + 上游错误体；订阅登录失效时文本 `sign in again`；`errors=pass` 完全原样 | Chat 401 → 503 `reauthentication_required`（带 `Retry-After`，若有确定的下次尝试）；Messages 原样 401；Responses 401 → 503 | 本程序 Chat/Responses 把 401 改成 503，Magpie 保留 401 并先回退；建议明确“重新登录”与“上游 401”的边界，避免客户端把 503 当上游不可用 |
| 上游 403 | 可重试；最终原 403 + 上游错误体；`errors=pass` 完全原样 | Chat 403 → 502 `upstream_request_failed`；Messages 原样 403；Responses 403 → 502 | 本程序 Chat/Responses 把非瞬态 403 变成可重试 502；建议保留 403 或在 `detail` 中明确 `upstream_status=403`，避免客户端重放 |
| 上游 404 | 可重试；最终原 404 + 上游错误体 | Chat 404 → 502；Messages 原样 404；Responses 404 → 502 | 同上；本程序把 404 变成 502，客户端可能误判为可重试 |
| 上游 408 | 可重试；最终原 408 | Chat 408 → 重试；最终 502；Messages 原样 408 | 本程序 Chat 最终不保留 408；建议保留 408 或至少在 detail 中给出 |
| 上游 409 | 不在可重试列表；原 409 透传 | Chat 409 → 502；Messages 原样 409；Responses 409 → 502 | 本程序把 409 变成 502；建议保留 409，避免客户端重放冲突请求 |
| 上游 413 | 一般不重试；原 413 + 提示；本地 body 超限 413 + `request_too_large` + `Connection: close` | Chat 413 → 502；Messages 原样 413；Responses 413 → 502；本地 body 超限 413 `payload_too_large` + close | 本程序本地 413 与 Magpie 接近；上游 413 在 Chat/Responses 被改成 502，建议保留 413 |
| 上游 415 | 不在可重试列表；原 415 透传 | Chat 不识别 `Content-Encoding`，压缩体按原始字节解析 → 400 `invalid_json`；Messages 原样 415；usagesrv 400 `unsupported_body_encoding` | 本程序 Chat 缺 415 语义；建议显式 415，或像 Magpie 一样支持 identity/gzip/zstd |
| 上游 422 | 可按文字重试；最终原 422 + 上游错误体 | Chat 422 + `upstream_request_rejected`；Messages 原样 422；Responses 422 + `upstream_request_rejected` | 都是 422；本程序统一错误体，Magpie 保留上游 body；建议保留 `code`/`param` |
| 上游 429 | 可重试 3 次；尊重 `Retry-After`；最终原 429 + `Retry-After`/`RateLimit-*` | Chat 重试（最多 5 次快速/2 次慢），最终 502；Messages 原样 429；分享门本地 429 `sharing_limit_reached` 无 `Retry-After` | 本程序 Chat 最终丢失 429 和 `Retry-After`；建议 Chat 最终保留 429 或至少在 detail 中带 `Retry-After`；分享 429 建议带 `Retry-After` |
| 上游 500 | 可重试；最终原 500 + 上游错误体 | Chat 500 → 重试；最终 502；Messages 原样 500；Responses 500 → 502 | 同 403/404；本程序 Chat/Responses 把 500 变成 502；建议保留 500 或明确 detail |
| 上游 502 | 可重试；最终原 502 + 上游错误体 | Chat 502 → 重试；最终 502；Messages 原样 502；Responses 502 → 502 | 都是 502；本程序最终错误体是自有的，Magpie 尽量保留上游 body |
| 上游 503 | 可重试；最终原 503 + 上游错误体 | Chat 503 → 重试；最终 502；Messages 原样 503；Responses 503 → 502 | 本程序 Chat/Responses 把 503 改成 502；建议保留 503 或明确 detail |
| 上游 504 | 可重试；最终原 504 + 上游错误体 | Chat 504 → 重试；最终 502；Messages 原样 504；Responses 504 → 502 | 本程序没有本地 504；建议把“上游头等待超时/首字节超时”与“上游 504”分开 |
| 连接失败 / DNS / 代理 | 502；`p.Name + ": " + err.Error()`；可重试 | Chat 502 `upstream_request_failed` 或 `upstream_unavailable`；可重试 | 都是 502；本程序错误体有 `detail`，Magpie 把错误文本放在 `message`；建议保留 `upstream_status`/异常类名 |
| 首字节前失败 | 未提交 200 时返回错误状态（通常 502）；已发 keepalive 头后写 in-band error | 提交前 502 `upstream_stream_error`；已提交后 in-band error | 语义接近；本程序 Chat 用 `detail.reason`，Magpie 用 `message` + `code`；建议统一 `code` |
| 首 token 超时 | 组 `FirstToken` 超时 504；组预算 540s | 首字节预算 120s，最终 502 `upstream_stream_error` / `upstream_first_byte_timeout` | 本程序没有 504；建议区分“首字节超时”和“上游不可达”，否则客户端无法判断是否值得重试 |
| 客户端断开 | usage 记 499；不写响应；不重试 | 不产生 HTTP 499；usage 记 `client_disconnected`；不写响应；不重试 | 语义一致，机器码不同；本程序没有数值 499 状态，建议 Rust 契约明确用 `client_disconnected` 而不是伪造 499 |
| SSE 提交后失败 | 200 + in-band error；不写 `[DONE]`；不记成功 | 200 + in-band `data: {"error":...}`；不写 `[DONE]`；不记成功 | 语义一致；本程序 Chat 路径按字节透传，Magpie 会解码重编码，错误事件形状不同 |
| 上游 200 断流 | 200 + error；`errEndedShort`；未提交则 502 | 200 + in-band `upstream_read_error`；未提交则 502 | 一致；本程序 detail 有 `events`/`duration_s`/`exception`，Magpie 用错误文本 |
| 上游 200 但 `{"error":...}` | 当失败；未提交 502，已提交 in-band | Chat 非流式 502 `upstream_explicit_error`；流式 in-band | 一致 |
| 网关 Key 预算 / 并发 / RPM | 429；`Retry-After`；`X-Should-Retry: false`；文本原因 | 分享门 429 `sharing_limit_reached`；`reason`；无 `Retry-After` | 本程序机器码更明确，但缺 `Retry-After`；建议补 |
| 认证失败洪泛 | 429 + `Retry-After` | 没有同层；分享门只按 Key 鉴权 | 本程序面向分享链，不是公网网关 Key 洪泛；不需要照搬 |
| 管理 API 未装配 | 503 `admin_unavailable` | `usagesrv.py` 503 `console subsystem unavailable` / `capability_not_assembled` | 都是 503；建议 Rust 契约保留 `error` 字符串和 `capability` 字段 |
| 错误响应缓存 | 普通错误无 `no-store`；管理 API/共享 Library 有 | `chat2api.py` 无 `no-store`；`usagesrv.py` 有 | 本程序分享链错误建议加 `no-store`，尤其是 401/429/503 |
| 连接复用 | 普通 400/404/405 不关闭；请求体读取失败/413/415 关闭；SSE `keep-alive` | `chat2api.py` 所有错误关闭；SSE `close`；`usagesrv.py` 不设 `Connection` | 本程序更保守；如果 Rust 目标要长连接复用，需明确哪些错误可以关闭 |

## 6. 证据表

| 关键结论 | 来源 |
|---|---|
| Magpie 错误体按 Chat/Responses、Anthropic、Gemini 三种形状输出；Chat/Responses 保留 `code`/`param` 为 null | `internal/gateway/gateway.go:11069-11161`；本机观察 400/404 错误体 |
| Magpie 状态码到错误类型：400/401/403/404/413/429/529 | `internal/gateway/gateway.go:11119-11141` |
| Magpie 自产错误 `Content-Type: application/json`，不设 `Cache-Control` | `internal/gateway/gateway.go:10919-10925`；本机观察 400/404/405/413/415 |
| Magpie 413/415 与请求体读取失败设 `Connection: close`；普通 400/404/405 不设 | `internal/gateway/request_bounds.go:110`、`internal/gateway/request_bounds.go:216-224`；本机观察 |
| Magpie SSE 头：`text/event-stream; charset=utf-8`、`no-cache`、`keep-alive`、`X-Accel-Buffering: no` | `internal/gateway/sse.go:80-95` |
| Magpie 上游错误同形时原状态/原 body 透传，只改 `error.message` | `internal/gateway/kept_error.go:41-101` |
| Magpie `errors=pass` 完全原样透传状态、Content-Type、body、Retry-After/RateLimit | `internal/gateway/pass_errors.go:22-59` |
| Magpie 上游过长改 400 `context_length_exceeded`，413 加字节提示 | `internal/gateway/gateway.go:11101-11113` |
| Magpie 可重试状态：401/402/403/404/408/429/5xx；400/422 仅按文字 | `internal/gateway/fallback.go:2039-2080` |
| Magpie 同候选重试 2 次，429 3 次；退避 1s 翻倍，`Retry-After` 覆盖，最长 8s | `internal/gateway/fallback.go:2312-2375` |
| Magpie 组内最多 12 轮，暂停 2/4/8/16/30s；组预算 540s | `internal/gateway/patience.go:19-48`、`internal/gateway/patience.go:159-180`、`internal/gateway/fallback.go:49` |
| Magpie 只在回复尚未发出时回退；提交后只能 in-band error | `docs/subsystems/gateway-routing.md:250`、`internal/gateway/fallback.go:2608-2684` |
| Magpie 流内错误带状态提示（408/429/5xx 且文字不含可重试词时追加 `<status>: you can retry your request`） | `internal/gateway/gateway.go:11032-11037` |
| Magpie Chat/Anthropic/Responses/Gemini 的流内错误形状 | `internal/gateway/chat.go:1756-1767`、`internal/gateway/anthropic.go:1863-1876`、`internal/gateway/responses.go:2461-2471`、`internal/gateway/gemini.go:828-830` |
| Magpie Chat 流结束时写自己的 `[DONE]` | `internal/gateway/chat.go:1776-1784` |
| Magpie 上游流被截断时 `errEndedShort`；未提交 502，已提交 200 + error | `internal/gateway/continuation.go:80-85`、`internal/gateway/fallback.go:3609-3644` |
| Magpie 空回复 `emptyEnded` 是 502 | `internal/gateway/fallback.go:3225-3241`、`internal/gateway/fallback.go:3624-3627` |
| Magpie 客户端断开记 499，不重试 | `internal/gateway/gateway.go:4553-4559`、`internal/usage/ledger.go:57-68` |
| Magpie 请求体读取超时 408 | `internal/gateway/request_bounds.go:239-243` |
| Magpie 不支持 Content-Encoding 415 | `internal/gateway/request_bounds.go:254-257` |
| Magpie 请求体上限 512 MB / Codex 2 GB | `internal/gateway/request_bounds.go:29-32`、`internal/gateway/request_bounds.go:50-61` |
| Magpie 上游响应头等待 10 分钟 | `internal/gateway/upstream_transport.go:47-49`、`internal/gateway/upstream_transport.go:79-88` |
| Magpie 组内首 token 超时 504、组预算 540s | `internal/gateway/gateway.go:4561-4588`、`internal/gateway/fallback.go:49` |
| Magpie 网关 Key 限流 429、`Retry-After`、`X-Should-Retry: false` | `internal/gateway/key_limit.go:42-107` |
| Magpie lane 队列满/等太久 429、`Retry-After` | `internal/gateway/concurrency.go:423-432`、`internal/gateway/gateway.go:4395-4422` |
| Magpie MaxRPM 429、`Retry-After` | `internal/gateway/rpm.go:16-59`、`internal/gateway/rpm.go:90-137` |
| Magpie 认证失败限流 429 | `internal/gateway/auththrottle.go:21-57`、`internal/gateway/lan.go:821-827` |
| Magpie 401 无效 Key / 403 来源与权限 / 404 未知路由与模型 | `internal/gateway/lan.go:797-833`、`internal/gateway/lan.go:947-955`、`internal/gateway/cors.go:50-98`、`internal/gateway/readonly.go:90-117`、`internal/gateway/gateway.go:1184-1186`、`internal/gateway/gateway.go:2789-2814` |
| Magpie 未知模型给出候选和全部 id | `internal/gateway/unknown_model.go:15-28` |
| Magpie 405 只出现在显式路由；其他方法落到兜底 404 | `internal/gateway/gateway.go:1193-1203`；本机观察 `PUT /v1/chat/completions` 404 |
| Magpie 失败日志只写状态/耗时/错误文本，截到 300 字符，每分钟去重 | `internal/gateway/gateway.go:702-768` |
| Magpie 流结束日志只写经过校验的 trace 头 | `internal/gateway/gateway.go:9591-9606`、`internal/gateway/wire_headers.go:1-16` |
| Magpie 归档与头日志读取时脱敏 | `internal/gateway/archive.go:284`、`internal/gateway/archive.go:291-293`、`internal/gateway/archive.go:339-358`、`internal/redact/scrub.go:9-38` |
| Magpie usage 行不写 body/prompt/错误文本 | `docs/subsystems/gateway-middleware.md:41` |
| Magpie 会话捕获失败不改变响应 | `docs/subsystems/gateway-sessions.md:564-565` |
| Magpie 中间件 hook 失败 fail open，250ms/50ms 限制 | `docs/subsystems/gateway-middleware.md:46-53` |
| Magpie 异步 hook 默认 1000ms、范围 100..30000ms、链预算 2..10s | `docs/subsystems/gateway-request-hooks.md:83-97` |
| Magpie 脱敏失败返回 503 | `README.md:146`、`internal/gateway/redact.go:226-234` |
| 本程序 `_error` 形状 `{"error":{"message","type","detail"}}` | `chat2api/chat2api.py:6946-6957` |
| 本程序 `_json` 设 Content-Type、Content-Length、Connection: close，无 no-store | `chat2api/chat2api.py:6929-6944` |
| 本程序 400/404/413/501 本机观察 | 本机观察 `POST /v1/chat/completions {}` 400；`GET /no-such-route` 404；超大 Content-Length 413；`PUT /v1/chat/completions` 501 |
| 本程序 `_read_body` 400/413 分类 | `chat2api/chat2api.py:7115-7142` |
| 本程序分享门 404/401/429 分类 | `chat2api/chat2api.py:7180-7235` |
| 本程序分享限流原因 | `chat2api/share_auth.py:1305-1362` |
| 本程序 Chat 上游 401/400/422/其他映射 | `chat2api/chat2api.py:8743-8776` |
| 本程序 Chat 流错误与 in-band error | `chat2api/chat2api.py:8931-8978`、`chat2api/chat2api.py:9639-9690` |
| 本程序 Messages 原样透传上游状态 | `chat2api/chat2api.py:8362-8396`、`chat2api/chat2api.py:9692-9708` |
| 本程序 Responses 401/400/422/其他映射 | `chat2api/chat2api.py:7582-7597` |
| 本程序 Responses 流 `response.failed` | `chat2api/chat2api.py:7696-7748` |
| 本程序首字节预算 120s、socket 静默 180s、总预算 360s | `chat2api/chat2api.py:395-402`、`chat2api/chat2api.py:458-462` |
| 本程序重试 5 次快速/2 次慢/12 次物理尝试 | `chat2api/chat2api.py:538-570` |
| 本程序重试延迟与 Retry-After 最多 10s | `chat2api/chat2api.py:720-747` |
| 本程序只重试 408/429/500/502/503/504 | `chat2api/chat2api.py:566` |
| 本程序提交前 502、提交后 in-band | `chat2api/chat2api.py:9646-9660` |
| 本程序 reader 上限 128、503 `too_many_concurrent_streams` | `chat2api/chat2api.py:573-577`、`chat2api/chat2api.py:8676-8682` |
| 本程序 503 `reauthentication_required` 带 `Retry-After` | `chat2api/chat2api.py:6959-6970`、`chat2api/chat2api.py:5041-5064` |
| 本程序客户端断开不产生 HTTP 499；usage 记 `client_disconnected` | `chat2api/chat2api.py:9541-9543`、`chat2api/chat2api.py:9003`；`chat2api/chat2api.py:5621-5640` 是断开探测 |
| 本程序 `usagesrv.py` 503 `capability_not_assembled` | `usagesrv.py:3165-3179` |
| 本程序 `usagesrv.py` 503 `console subsystem unavailable` | `usagesrv.py:3412-3418` |
| 本程序 `usagesrv.py` 所有 JSON 响应 `Cache-Control: no-store` | `usagesrv.py:1649-1656` |
| 本程序 `usagesrv.py` 413 / 405 / 404 / 500 / 502 | `usagesrv.py:1815-1816`、`usagesrv.py:2804`、`usagesrv.py:3145`、`usagesrv.py:3140-3143`、`usagesrv.py:3297-3300` |
| 本程序 `usagesrv.py` 401/403/409/410 | `usagesrv.py:2028`、`usagesrv.py:2772`、`usagesrv.py:1026-1031`、`usagesrv.py:2313` |
| 本程序契约 §5.4 错误码表 | `docs/rust-contract.md:181-192` |
| 本程序契约 §6.3 云端错误码表 | `docs/rust-contract.md:275-294` |
| 本程序契约 §5.3 超时/重试 | `docs/rust-contract.md:168-179` |
| 本程序契约 §6.4 no-store | `docs/rust-contract.md:307-310` |
| 本程序 Rust 计划把 SSE 首字节/隐藏重试列为风险 | `docs/rust-plan.md:108` |
| 本程序 Rust 计划要求云端非 UM 读面 503 `capability_not_assembled` | `docs/rust-plan.md:199` |
| 本程序测试：上游 400/422 不被当成瞬态 | `tests/test_chat2api_request_rejection.py:185-201`、`tests/test_chat2api_request_rejection.py:266-272` |
| 本程序测试：零数据断流提交前 502 | `tests/test_chat2api_stream.py:1043-1078` |
| 本程序测试：reader 槽满 503 | `tests/test_chat2api_stream.py:1375-1398` |
| 本程序测试：Messages error 事件立即终止 | `tests/test_chat2api_endpoints.py:589-611` |
| 本程序测试：云端非 UM 读面 503 | `tests/test_cloud_role_assembly.py:505-515`、`tests/test_um_stats_read_surface.py:647-655` |
| 本机日志：499 / 200 / 502 / 503 / 400 / 429 / 500 / 404 / 401 计数 | 本机日志观察 `C:/Users/dacint/AppData/Local/magpie/logs/app.log`（探测前统计） |
| 本机旧日志：341 次 499、72 次 200、30 次 502、13 次 429 | 本机日志观察 `C:/Users/dacint/AppData/Local/magpie/logs/app.old.log`（全文件统计） |
| 本机日志：499 是 agent canceled；200 是流中断 | 本机日志观察 `app.log:2`、`app.log:5` |
| 本机日志：503 是 UM 需要交互登录 | 本机日志观察 `app.log:11515-11522` |
| 本机日志：429 是 Codex 用量上限 | 本机日志观察 `app.log:1262`、`app.log:6802-6804` |
| 本机日志：400/404/500 来自 UM 的错误 | 本机日志观察 `app.log:3597-3598`、`app.log:4932-4946`、`app.log:10791-10916` |
| 本机日志：401 是 Trae Global 登录过期 | 本机日志观察 `app.log:1258` |

## 7. 未验证范围

1. **Magpie 版本差异**：源码是 `main@4a4424ea`，本机日志是 `0.1.4026`；两者可能不同，本报告的源码结论不保证与正在运行的二进制逐行一致。
2. **没有真实付费请求**：没有向 Magpie 的付费上游发送任何推理请求；上游 4xx/429/5xx 的透传、重试和 `Retry-After` 都来自源码与文档，不是实测。
3. **没有触发 Magpie 的 401/403/408/409/429/500/502/503/504**：本机只观察到 400、404、405、413、415；日志中的 429/500/502/503 是历史记录，不是受控实验。
4. **没有触发本程序的 401/403/429/502/503/504 或 SSE**：本机只观察到 400、404、413、501 和 `usagesrv.py` 的 404；分享模式没有启动，真实上游没有请求。
5. **没有运行测试**：`python -m unittest` 和 Magpie 的 Go 测试都没有运行；本报告引用测试代码只说明契约存在，不说明本次通过。
6. **没有验证所有 no-store/Connection 路径**：普通 400/404/405 的本机观察支持“Magpie 不设 no-store/close”的结论，但不能排除其他端点或上游透传时带上这些头。
7. **日志计数不是概率**：`app.log` 的 397 次 499、163 次 200、108 次 502、14 次 503 等只说明历史出现过，不能说明当前版本或真实负载下的比例。
8. **“UM 指向本程序”是推断**：日志里的 provider `um`、模型 `um/DeepSeek-V4.1-Flash-1017` 和错误文案与本程序的 UM 上游语义吻合，但没有做端到端标记验证；报告中把它标为“本机日志观察/推断”。
9. **Magpie 405 的通用性**：只实测了 `PUT /v1/chat/completions` 和 `GET /v1/responses`；其他路径的方法不匹配是否都落到 404，取决于 Go `ServeMux` 与兜底 `/` 的组合，未逐一枚举。
10. **本程序 415/426 只在 `usagesrv.py` 契约文档中出现**：没有实测 `display grant` 的 415 和 WebSocket 的 426。
11. **Rust 契约文档可能滞后**：`docs/rust-contract.md` 是目标契约，不是当前实现的完整证明；行号引用以当前 Python 代码为准，文档只用于说明目标语义。

## 8. 建议

按优先级排序，前三条是行为差异，后面是契约与可观测性：

1. **保留上游 4xx 的状态语义**：本程序 Chat/Responses 目前把上游 403/404/409/413/5xx 统一成 502（Chat 还会把 429 最终变成 502）。Magpie 的做法是“同形透传、异形同状态重建、`errors=pass` 完全原样”。建议至少在 `detail` 中保留 `upstream_status`，并考虑对 403/404/409/413 保留原状态；否则客户端会把非瞬态 4xx 当可重试错误，反复重放同一请求。
2. **补 `Retry-After`**：分享门 429 `sharing_limit_reached` 没有 `Retry-After`；上游 429 最终在 Chat 路径也没有。Magpie 的 Key 限流和 lane 429 都带 `Retry-After`，且 Key 限流带 `X-Should-Retry: false`。建议分享门在能算出重置时间时带 `Retry-After`，至少让客户端知道不要立刻重试。
3. **统一 405/415/408**：本程序 API 路径的未实现方法返回 501 HTML，压缩请求体没有 415 语义，也没有请求体读取超时 408。Magpie 分别有 405（部分路由）、415（不支持的 Content-Encoding）和 408（请求体读取超时）。建议 Rust 契约明确这三类，避免把客户端错误误报成 501/400/502。
4. **错误体形状**：本程序 Chat 的 `detail` 是自有字段，`usagesrv.py` 是 `{"error": "机器码"}`。如果 Rust 目标要保持 OpenAI 兼容，建议 Chat/Responses 采用 `{"error":{"message","type","code","param"}}`，把 `route_not_shared`、`sharing_limit_reached`、`payload_too_large` 放进 `code` 而不是只放 `message`；`usagesrv.py` 保留现有 `error` 字符串以不破坏云端页面。
5. **`no-store`**：`usagesrv.py` 已统一 `Cache-Control: no-store`；`chat2api.py` 的错误响应没有。分享门的 401/429/503 涉及 Key 状态和重试时间，建议加 `Cache-Control: no-store`，避免中间缓存。
6. **SSE in-band error 的机器码**：本程序 Chat 流 in-band error 的 `type` 固定为 `upstream_stream_error`，`detail.reason` 是机器码。建议保留 `reason`，并考虑增加 `status` 或 `code` 让文本判定型客户端知道“是否值得重试”；Magpie 的 `saysPassing` 就是在做这件事。
7. **日志字段**：本程序失败日志已有 `stage`、`upstream_status`、`exception`、`downstream`、`stream`、`model`、`duration_ms`，但没有 `tries`/`physical_attempts`。建议在最终失败行加尝试次数（Magpie 的 `tries`），便于区分“第一次就失败”和“重试耗尽”。
8. **测试**：建议增加无 Key 的本地契约测试，覆盖 400/404/405/413/415 和 401/429（分享模式启动时），以及 SSE 提交前/后、客户端断开、上游 `[DONE]` 缺失；这些测试不需要真实上游或付费请求。

## 9. 结论

Magpie 的错误处理更像一个“协议感知的网关”：它按客户端协议生成错误体，尽量保留上游状态和 body，只在同形时改写 `message`，并且把重试、账号/模型回退、组 patience 都隐藏在最终响应之后。它对 4xx 的态度是“能透传就透传，不能透传就同状态重建”，而不是统一 502。

本程序更像一个“自有的 OpenAI 兼容代理”：Chat 路径把上游错误归一成 `{"error":{"message","type","detail"}}`，把 401 改成 503，把其他非 400/422 的上游 4xx 和超时统一成 502；Messages 路径保留字节透传；分享链有明确的机器码 `sharing_limit_reached`、`route_not_shared`、`payload_too_large`、`reauthentication_required`、`too_many_concurrent_streams`。这些机器码比 Magpie 的文本原因更适合前端和 Rust 契约，但缺少 Magpie 的 `Retry-After` 透传、上游 4xx 原状态保留和 405/415/408 语义。

如果 Rust 重写只允许保留一套行为，建议保留本程序现有的机器码和 SSE 提交边界，同时吸收 Magpie 的三点：同形上游错误尽量保留状态和 body、429 尽量带 `Retry-After`、API 路径不要用 501 HTML 代替 405/415。
