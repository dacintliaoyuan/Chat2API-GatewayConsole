# 执行端任务接续能力核查（#133 B 部分）

2026-11-21，按 [#133](https://github.com/dacintliaoyuan/Chat2API-GatewayConsole/issues/133) 的 B 部分和 [ADR 0014](adr/0014-local-model-ingress-and-task-continuation.md) §执行端任务接续，核查本机实际使用的执行端能否识别网关中断与恢复等待超时、保存任务标识和工具结果，并在网关恢复后自动继续原任务。本文只记录核查结果，不包含网关实现计划；A 部分由另一路工作完成。

证据标记：

- `[实跑]`：本机实际运行，记录命令、工作目录和结果。
- `[文档/二进制]`：只读 help、官方 changelog、二进制字符串或本地数据库 schema，未运行对应路径。
- `[阻塞]`：因登录、凭据、权限或缺少可隔离环境无法执行，附错误原文。

本次没有读取用户项目内容，没有发送用户数据。黑盒探针只使用固定提示 `Reply with the single word ping.`、`Read the probe file...` 和临时文件 `probe-read.txt`（内容仅 `probe-content-v1` / `probe-content-v2` / `codex-probe-v1` / `codex-probe-v2`）。未修改任何 CLI 安装目录或用户配置；隔离配置写入 `%TEMP%\133b-audit`。除一次固定 `agy -p "/hooks"` 探针外，本次没有调用真实模型；该次不含用户数据，但无法确认是否消耗额度，按保守口径记一次真实模型回合，随后停止所有 agy 模型调用。

## 1. 本机实际执行端清单

| 执行端 | 版本 | 路径类别 | 登录/可用性 |
|---|---|---|---|
| Claude Code | 2.1.327（`claude doctor`：native，commit `f467840f2a4c`） | 用户级 native 安装，`%USERPROFILE%\.local\bin\claude.exe` | 未登录 claude.ai；`ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` 可对隔离假网关发起请求 `[实跑]` |
| Antigravity CLI（`agy`） | 1.5.11 | 用户级安装，`%LOCALAPPDATA%\agy\bin\agy.exe` | 隔离 HOME/APP_DATA 下仍报未登录；机器上另有可用登录态 `[阻塞]` |
| Codex CLI | 0.171.0（npm `@openai/codex`） | npm 全局 shim `%APPDATA%\npm\codex`，实际二进制在 `%APPDATA%\npm\node_modules\@openai\codex\...\bin\codex.exe` | 隔离 `CODEX_HOME` + 自定义 `model_provider` 可运行 `[实跑]` |

用户默认 Codex 配置把 `openai_base_url` 和 `model_providers.custom` 指向本机 magpie `127.0.0.1:3250`；本次没有使用该默认配置，所有 Codex 探针都使用临时 `CODEX_HOME` 和本地假网关，避免真实模型请求。

## 2. 503 / 错误码 / Retry-After 行为

假网关在 `127.0.0.1` 高端口返回 HTTP 503，响应体包含 `error.code = gateway_recovery_timeout` 与 `should_retry`，响应头含 `Retry-After`；Anthropic 路径 `/v1/messages` 使用 Anthropic 错误形状，Codex 路径 `/v1/responses` 使用 OpenAI 错误形状。

| 执行端 | 503 | `error.code` | `Retry-After` | `should_retry` | 重试上限/间隔 | 恢复后同进程继续 |
|---|---|---|---|---|---|---|
| Claude Code 2.1.327 | 识别 HTTP 503；结果文本含 `503 gateway_recovery_timeout` | 只把消息文本带出；`code` 单独存在时不显示（code-only 探针只得到 `fake gateway error`） | 识别为最小等待间隔：`Retry-After: 10` 时请求在 0 / 10.5 / 22.3 秒 | `x-should-retry: false` 立即停止；响应体 `should_retry: false` 单独无效 | `maxRetries: 10`；实测 8 次/90 秒，约 1.2/1.1/2.2/4.2/9.8/17.5/32.1 秒 | 3 次 503 后成功，4 次请求、同进程完成，`num_turns: 1` |
| Codex 0.171.0 | 识别 HTTP 503；`turn.failed` 文本含 `unexpected status 503 ... gateway_recovery_timeout` | 只把消息文本带出；code-only 探针只得到 `fake gateway error` | 识别：`Retry-After: 10` 时请求在 0 / 10.0 / 20.0 秒 | `x-should-retry: false` 与响应体 `should_retry: false` 都未阻止重试 | `max_retries=5`；间隔约 1 秒；5 次后 `turn.failed` | 未做 fail-then-success 探针；5 次预算内应同进程重试 |
| Antigravity CLI 1.5.11 | `[文档/二进制]` 1.5.0 changelog 写明修复 429/5xx 重试并 honoring `Retry-After`；二进制含 `Retry-After`、`retry_after`、`should_retry`、`AGY_CLI_MODEL_API_MAX_RETRIES` | 未验证；二进制含 `should_retry`，但无黑盒结果 | 未验证；文档写明 honoring，但未黑盒复现 | 未验证 | 未验证 | 未验证；`[阻塞]` 隔离探针在登录检查处失败 |

### Claude Code 实跑细节

- `[实跑]` 工作目录 `%TEMP%\133b-audit\run\claude-work`，命令为 `claude --bare --print --model claude-sonnet-4-5 --tools "" --output-format json "Reply with the single word ping."`，环境为隔离 `CLAUDE_CONFIG_DIR`、`ANTHROPIC_BASE_URL=http://127.0.0.1:<fake-port>`、`ANTHROPIC_API_KEY=<dummy>`。
- 503 恒返：90 秒内 8 次请求，退出码 124（外层 `timeout` 终止）；会话 transcript 写入 `system` 事件，字段 `subtype: api_error`、`maxRetries: 10`、`retryAttempt: 1..8`、`retryInMs: 1198/1114/2182/4151/9762/17492/32107/34925`、`source: request_retry`，错误文本保留 `503 gateway_recovery_timeout`。
- `Retry-After: 10`：25 秒内 3 次请求，时间 0 / 10.498 / 22.303 秒；`Retry-After: 1` 时约 1.2 秒起。结论：`Retry-After` 被当作最小间隔，不是固定值。
- `x-should-retry: false`：1 次请求后退出码 1，结果 `is_error: true`、`api_error_status: 503`、`api_error_type: api_error`、`result: "API Error: 503 gateway_recovery_timeout. ..."`。
- 响应体 `should_retry: false`、无 `x-should-retry` 头：20 秒内 6 次请求，说明只认头。
- 响应体 `message: "fake gateway error"`、`code: "gateway_recovery_timeout"`：结果只出现 `fake gateway error`，说明不把 `error.code` 解析成结构化字段。
- 3 次 503 后成功：4 次请求，退出码 0，`result: "pong"`、`num_turns: 1`；说明网关在重试预算内恢复时，原任务在同一进程自动继续，无需用户操作。
- `--resume <session-id>`：请求体含上一轮 `assistant` 的 `tool_use` 与 `user` 的 `tool_result`，`tool_result.content` 仍是旧值 `probe-content-v1`；`--continue` 实测复用同一 `session_id`（`c5dd9fbc-b9a5-462d-b591-d25ff84f8dc0`）。

### Codex 实跑细节

- `[实跑]` 工作目录 `%TEMP%\133b-audit\run\codex-work`，命令为 `codex exec --skip-git-repo-check --json "Reply with the single word ping."`，环境为隔离 `CODEX_HOME`、自定义 `model_providers.fake.base_url=http://127.0.0.1:<fake-port>/v1`、`FAKE_API_KEY=<dummy>`。
- 503 恒返：5 次重试，`max_retries=5`，间隔约 1 秒；5 次后 `turn.failed`，错误文本 `unexpected status 503 Service Unavailable: gateway_recovery_timeout`。`--json` 事件流同时给出 `{"type":"error","message":"Reconnecting... N/5 ..."}`。
- `Retry-After: 10`：25 秒内 3 次请求，时间 0 / 10.005 / 20.010 秒；说明 honoring `Retry-After`。
- `x-should-retry: false` 与响应体 `should_retry: false`：仍 5 次重试后失败；说明 Codex 不读这两个信号，只按 5xx 重试。
- 响应体 `message: "fake gateway error"`、`code: "gateway_recovery_timeout"`：只出现 `fake gateway error`，说明不把 `error.code` 解析成结构化字段。
- 会话 rollout 在 `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`，失败会话的 `event_msg task_complete.error.message` 保留完整 503 文本；没有逐次重试的 rollout 记录。
- `codex exec resume <thread-id>`：请求体含上一轮 `function_call` 与 `function_call_output`，`output` 仍是旧值 `codex-probe-v1`；磁盘文件已改为 `codex-probe-v2`，没有重新执行 `exec_command`。

### Antigravity CLI 阻塞证据

- `[实跑]` `agy models`：隔离 `HOME`/`USERPROFILE`/`LOCALAPPDATA`、`AGY_LLM_GATEWAY_URL=http://127.0.0.1:<fake-port>`、`GEMINI_API_KEY=<dummy>`，退出码 1，错误 `Error: Eligibility check failed: error getting token source: You are not logged into Antigravity.`。
- `[实跑]` `agy --print="Reply with the single word ping." --model fake-model`：同样隔离环境，退出码 1，错误 `error: Eligibility check failed: error getting token source: You are not logged into Antigravity.`。
- `[实跑]` `agy --settings '{"hooks":{}}'`：退出码 1，错误 `--settings: unknown setting "hooks"`；`agy --settings '{"permissions":{}}'` 被接受，但仍停在登录检查。说明 hooks 不在 `--settings` 的键里，需单独的 `hooks.json`。
- `[实跑]` 一次 `agy -p "/hooks" --output-format json` 在未指定 `AGY_LLM_GATEWAY_URL` 时返回了模型回答（`status: SUCCESS`、`num_turns: 1`、`usage` 有值，正常回合没有 `finished_reason`）。这次是固定探针，不含用户数据；无法确认是否使用真实额度，按保守口径记一次真实模型回合。随后停止所有 agy 模型调用，不再重复。
- `[文档/二进制]` `agy changelog` 1.5.0 条：修复部分模型在 429/5xx 时不重试的问题，现 honoring 服务端 `Retry-After`；1.4.11 条：`--output-format json` / `stream-json` 增加 `finished_reason`，值为 `print_timeout`、`interrupted`、`error`，正常结束时省略；`--print-timeout` 到期退出码 3 并输出 `AGY_ERROR` 与 `[agy] print timeout after ...`。
- `[文档/二进制]` `agy --help`：`--continue`、`--conversation`、`--print-timeout`（默认 0，等待回合完成）、`--output-format json|stream-json`、`--settings`、`--project`、`--remote-agent`；子命令 `acp`。`agy changelog` 1.5.0 条：`agy acp` 的 `session/resume` 可恢复会话且不重放历史。

## 3. 任务标识、进度保存、工具结果复用、自动唤醒

| 能力 | Claude Code 2.1.327 | Codex 0.171.0 | Antigravity CLI 1.5.11 |
|---|---|---|---|
| 任务标识 | `session_id`（JSON 结果、transcript、hook 输入都有）；`--session-id <uuid>` 可指定；`--resume <id|name>`、`--continue`、`--fork-session` | `thread_id`（`thread.started`、rollout `session_meta`）；`codex resume <id|--last>`、`codex exec resume <id>`、`codex fork` | `conversation_id`（JSON 结果）；`--continue`、`--conversation <id>`；`acp session/resume` |
| 进度保存 | transcript `.jsonl` 在隔离配置 `projects/<slug>/<session-id>.jsonl`；含 `assistant` `tool_use`、`user` `tool_result`、`system` 重试记录、`last-prompt`、`cost-state` `[实跑]` | rollout `.jsonl` 在 `$CODEX_HOME/sessions/...`；含 `task_started`、`response_item message`、`item_completed`、`task_complete`、`token_count`；失败时 `task_complete.error` `[实跑]` | 本地 SQLite：`conversation_summaries` 有 `step_count`、`status`、`not_fully_idle`、`killed`、`last_user_input_step_index`；`conversations/*.db` 的 `steps` 有 `step_type`、`status`、`error_details`、`permissions`、`task_details`、`step_payload` `[文档/二进制]`；本次抽查的两份 DB `steps` 行数为 0 |
| 工具结果复用 | 恢复时请求体含旧的 `tool_use` / `tool_result`，内容为旧值 `probe-content-v1`；磁盘文件已改为 `probe-content-v2`，仍未重新执行 Read `[实跑]` | 恢复时请求体含旧的 `function_call` / `function_call_output`，内容为旧值 `codex-probe-v1`；磁盘文件已改为 `codex-probe-v2`，仍未重新执行 `[实跑]` | 未验证；`steps` 表有 `step_payload` 字段，但抽查 DB 无步骤；需登录或可用企业网关凭据 `[阻塞]` |
| 自动唤醒 | 无内建网关健康感知；重试预算内同进程继续；预算耗尽或进程退出后需外部调用 `claude --resume` / `claude --bg --resume <id>`；`claude agents --json` 可列后台会话，`claude respawn` 可重启后台会话 `[实跑]` + `[文档]` | 无内建网关健康感知；重试预算内同进程继续；预算耗尽后需外部调用 `codex exec resume <thread-id>`；`codex queue --thread <id> --message <text> --resume` 可排队并尝试恢复 `[实跑]` + `[文档]` | 无内建网关健康感知；`--continue` / `--conversation` 是手动恢复；`acp session/resume` 供编辑器客户端恢复；`Stop` hook 有 `continuation_active`、`PreInvocation` 有 `terminationBehavior`，但需外部实现健康检查与唤醒 `[文档/二进制]` |

### Claude Code hook 接点

`[实跑]` 用隔离 `CLAUDE_CONFIG_DIR` 和 `--settings <file>`（未加 `--bare`，因为 `--bare` 会跳过 hooks）配置以下事件，hook 命令把 stdin JSON 追加到临时日志：

- `SessionStart`：`source: startup`；`--resume` 时 `source: resume`，并带 `seconds_since_last_response`、`context_tokens`、`prompt_cache_likely_expired`。
- `UserPromptSubmit`：带 `prompt`、`prompt_id`、`permission_mode`、`transcript_path`。
- `PreToolUse`：带 `tool_name`、`tool_input`、`tool_use_id`、`transcript_path`，可用于在执行前保存「即将执行」的工具意图。
- `PostToolUse`：带 `tool_response`（含文件内容）、`tool_use_id`、`duration_ms`，可用于保存「已完成」的工具结果。
- `Stop`：带 `last_assistant_message`、`stop_hook_active`、`transcript_path`；正常回合结束时触发。
- `SessionEnd`：带 `reason`；API 错误退出时触发，`reason` 为 `other`。
- API 错误退出时**没有** `Stop`，只有 `SessionStart`、`UserPromptSubmit`、`SessionEnd`；因此不能靠 `Stop` 记录网关错误。
- 没有「网关恢复」或「重试耗尽」专用 hook；`SessionEnd` 只能看到进程结束，需从 transcript 的 `system` 重试记录或 `result` 文本判断原因。

`[实跑]` 3 次 503 后成功的 transcript 里保留了重试 `system` 事件和最终 `assistant`；`[实跑]` 恢复请求包含旧 `tool_result`，说明 hook 之外还有 transcript 这一层可作为工具结果复用依据。

### Codex hook 接点

`[文档]` `codex features list` 显示 `hooks` 为 `stable`、`true`；用户级 `~/.codex/hooks.json` 有 `PreToolUse`（matcher `Bash`，命令 `rtk hook codex`），`~/.codex/config.toml` 有该 hook 的 trust hash；`codex exec --help` 提供 `--dangerously-bypass-hook-trust`。

`[实跑]` 在隔离 `CODEX_HOME` 写入 `SessionStart`、`PreToolUse`、`PostToolUse`、`Stop` hook 并加 `--dangerously-bypass-hook-trust` 运行 `codex exec`，只看到 bypass 警告，没有生成 hook 日志文件；因此 `codex exec` 是否加载 `hooks.json` 仍未验证，不能把 hook 当作已验收接点。

### Antigravity CLI hook 接点

`[文档/二进制]` `agy changelog` 记录 `hooks.json`、`PreToolUse`、`PostToolUse`、`Stop`、`SessionStart`、`PreInvocation`、`PostInvocation`；`Stop` hook 输入有 `continuation_active`，`PreInvocation` 有 `terminationBehavior`，hook 输入有 `hookEventName`、`cwd`、`toolCall.id`、`lastUserInput`；`PostToolUse` 失败不会把工具标成失败。`agy plugin import` 会把 Claude Code 的 `PreToolUse`、`PostToolUse`、`Stop`、`SessionStart` 转成 agy hook。

`[实跑]` `agy --settings '{"hooks":{}}'` 返回 `unknown setting "hooks"`，说明 hooks 不走 `--settings`；`[阻塞]` 隔离环境无法进入模型回合，`hooks.json` 是否实际触发未验证。

## 4. 所需执行端改动或权限

| 执行端 | 现有接点 | 仍需的改动/权限 | 阻塞证据 |
|---|---|---|---|
| Claude Code | `--resume` / `--continue` / `--session-id` / `--bg --resume`；transcript 持久化工具结果；hooks 可记录 `PreToolUse` / `PostToolUse` / `SessionEnd` | 需外部监督器：`SessionEnd` 或 `result` 失败后记录暂停原因，轮询 8000 `/health` 或 `/ready`，健康后调用 `claude --bg --resume <session-id>`；若要求自动接续，需项目侧提供「工具结果未知时查询目标系统」的工具；hooks 需设置文件、可执行命令，`--bare` 会跳过 hooks；工具执行需 `--allowedTools` / `--permission-mode` | 无内建网关健康感知；无 180 秒预算；无「未知结果先查询」内建语义；`--bg` 自动唤醒未实测 |
| Codex | `resume` / `exec resume` / `queue --resume`；rollout 持久化 `function_call_output`；`task_complete.error` 可读 | 需外部监督器读 rollout 的 `task_complete.error`，健康后调用 `codex exec resume <thread-id>`；hooks 需确认 `codex exec` 是否加载 `hooks.json`；工具执行需明确 sandbox/approval 策略 | `codex exec` 在 read-only 下 `exec_command` 被 policy 拒绝；加 `--dangerously-bypass-approvals-and-sandbox` 才能执行；hooks 未触发 |
| Antigravity CLI | `--continue` / `--conversation` / `acp session/resume`；`finished_reason`；`hooks.json` | 需可用登录或企业网关凭据；需确认 `hooks.json` 实际事件与 `Stop` 续跑行为；需外部监督器读 `finished_reason` 或 conversation DB 后调用 `--conversation <id>` | 隔离环境 `agy --print` / `agy models` 均报 `You are not logged into Antigravity`；一次固定 `/hooks` 探针使用机器登录态，未继续 |

## 5. 对 #133 验收清单中 B 部分的逐项结论

| # | 验收项 | 结论 | 依据 |
|---|---|---|---|
| B-1 | 超过 180 秒未恢复时，入口释放 HTTP 等待；实际执行端记录暂停原因、保留原任务，服务就绪后自动继续；短于网关预算的客户端超时不被误判为已验收 | **待验收** | Claude 有 10 次重试、Codex 有 5 次重试，但都不是 180 秒预算；两者都没有网关健康感知的自动唤醒；Claude `SessionEnd` / Codex `task_complete.error` 只能记录原因，需外部监督器恢复；A 部分未由本工作验证 |
| B-2 | 请求已转发但无可见输出、或已收到部分流式输出时中断；入口不重发原 HTTP 请求，执行端从最后确认进度接续 | **待验收** | Claude/Codex 恢复请求复用已记录工具结果，但未做「已转发未输出」和「部分流」的真实中断；A 部分未验证；没有真实项目 |
| B-3 | 同一个可查询的工具写操作分别验证已完成、确定未执行、结果无法确认；已完成复用结果，确定未执行才重试，无法确认只暂停受影响分支 | **阻塞** | 执行端没有内建「先查询目标系统」语义；Claude/Codex 的复用机制只用 Read/Get-Content 证明，未用写操作；Agy 隔离探针阻塞；分支级暂停需项目侧实现 |
| B-4 | 用实际执行端同时运行多个项目，验证正常项目继续、可恢复项目自动继续、结果不明项目保持可继续；记录任务标识、就绪信号、转发次数和脱敏工具结果 | **阻塞** | 未做多项目联调；A 部分隔离验收未取得；Agy 无法隔离黑盒；Claude/Codex 无内建自动唤醒，需外部监督器；没有任务标识/就绪信号/转发次数的联调记录 |
| B-5 | 回归正常请求、8001 管理诊断、UM 认证恢复和用户主动停止流程 | **待验收** | 本次 B 核查未覆盖 8001/UM/停止；属于 A/集成回归，需与 A 的隔离验收一起跑 |

## 6. 现在能否用多个真实项目联调

**不能。** 目前只有执行端能力核查和单进程假网关探针，没有 A 部分的 8000 入口隔离验收，也没有任何一个执行端的「网关恢复后自动唤醒原任务」证据。Claude Code 已实测能在重试预算内同进程继续，也能在手动 `--resume` / `--continue` 时复用旧工具结果；Codex 由 5 次重试日志推断能在预算内继续，`exec resume` 实测复用旧工具结果；但预算耗尽或进程退出后，两者都不会自己等网关恢复。Antigravity CLI 在隔离环境无法进入模型回合，503/Retry-After 行为只有文档和二进制证据。

最小解除条件：

1. A 部分交付可用的 8000 入口隔离实例，能注入「处理进程退出、180 秒内恢复、超时释放」三类故障，并给出实际转发次数。
2. 先选 Claude Code 2.1.327 作为首个执行端：保留 `session_id`，用 `PreToolUse` / `PostToolUse` 记录工具意图和结果，用 `SessionEnd` 记录 API 错误原因；网关 `/ready` 恢复后由外部监督器调用 `claude --bg --resume <session-id>`，验证同一 session ID、同一任务和旧工具结果。`--bare` 不用于需要 hooks 的联调。
3. 再选 Codex 0.171.0：从 rollout `task_complete.error` 读暂停原因，网关恢复后调用 `codex exec resume <thread-id>`；先确认 `codex exec` 是否加载 `hooks.json`，并明确 `exec_command` 的 sandbox/approval 策略。
4. Antigravity CLI 需先取得可隔离的登录或企业网关凭据，或由用户明确授权使用现有机器登录态做一次隔离故障注入；否则 B 保持待验收/阻塞，不以文档推断替代。
5. 用至少两个真实项目分别建立独立 session/task ID，记录就绪信号、每次请求的转发次数、已复用工具结果和脱敏的工具结果；再用一个可查询的写操作验证「已完成 / 确定未执行 / 无法确认」三种结果。
6. 完成 8001 管理诊断、UM 认证恢复和用户主动停止回归。

在上述条件满足前，B 部分不能标记为通过；当前结论是「待验收」，其中工具写操作、分支级暂停和多项目联调为「阻塞」。

## 7. 未验证范围

- A 部分 8000 入口的跨重启排队、取消、队列上限、显式停止和 `/ready` 行为未由本工作验证。
- 未在真实项目上运行；未做「已转发未输出」和「部分流式输出」的真实中断。
- 未用真实写操作验证工具结果复用；只用 Read / Get-Content 验证了「恢复时复用旧结果、不重新执行」。
- Claude Code 的 10 次重试最终失败文本未观察到；90 秒只观察到 8 次。`--bg` / `respawn` 的自动唤醒未实测。
- Codex 的 fail-then-success 未单独跑；hooks 在 `codex exec` 隔离环境未触发，hook 接点未验证。
- Antigravity CLI 的 503/Retry-After、`finished_reason`、工具结果复用和 `hooks.json` 实际行为未黑盒验证；仅读 changelog、help、二进制字符串和本地 SQLite schema。
- 8001 管理诊断、UM 认证恢复、用户主动停止回归未验证。
- 未验证多个项目并行时的分支级暂停和依赖隔离。
