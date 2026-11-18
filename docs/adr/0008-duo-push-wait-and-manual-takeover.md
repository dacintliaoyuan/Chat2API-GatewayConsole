# Duo 推送等待与人工接管

自动 headless 重登录在 UMPASS 之后遇到 Duo 推送时，当前只把 `manual_reason=duo_prompt_waiting` 留在 `_auth_login`，不写 `_auth_error`；`Proxy.call` 的 `refresh_before_retry` 在 30 秒认证等待预算耗尽后硬编码 `ReauthenticationThrottled("auth_wait_timeout")`，业务请求得到 503；控制台把它显示成“自动重登录失败”，而不是“等待本人确认 Duo 推送”。17:46–17:48 的日志里，共享轮次仍在 `logging_in`，请求侧 503；17:49 的强制可见轮次 11 秒成功。我们决定：网关 `Proxy.call`/`_wait_recovery` 把 `duo_prompt_waiting` 作为 503 `reauthentication_required` 的 reason 返回，并带 `Retry-After: 30`；自动轮次检测到 Duo 后自动打开可见登录窗口；控制台 adapter 透传 `login.phase/origin/manual_reason`；控制台 action 遇到 `duo_prompt_waiting` 立即返回 `login_manual_takeover_required`，消息说明“等待本人确认 Duo 推送”，UI 提供「打开可见窗口」；本地和云端同一套；回归测试保留网关 seam，并加控制台 adapter/UI 测试。

**Status**: accepted

本机 Windows 8000/8001 的 Duo 等待提示与可见窗口规则见 [ADR 0011](0011-local-um-reauthentication-lifecycle.md)。

## Considered Options

- **保持 `auth_wait_timeout` 只改 UI 文案**：不解决自动轮次没有可见窗口的问题；放弃。
- **自动轮次直接失败，要求人重新点登录**：用户体验差，且共享轮次还在跑；放弃。
- **网关 reason + 自动可见窗口 + 控制台 manual takeover**：采用。

## Consequences

- 503 的 reason 从 `auth_wait_timeout` 变成 `duo_prompt_waiting`，并带 `Retry-After: 30`。
- 自动轮次在 Duo 等待时自动打开可见窗口，不再只 headless。
- 控制台 status 透传 `login.manual_reason`，显示“等待本人确认 Duo 推送”；action 返回 `login_manual_takeover_required`，UI 有「打开可见窗口」。
- 本地和云端共用同一套行为。
