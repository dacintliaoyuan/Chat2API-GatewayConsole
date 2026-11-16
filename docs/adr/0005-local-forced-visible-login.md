# 本地控制台强制可见重新登录

本地控制台的 `account.login` 先核验凭据，valid 时返回 `login_skipped`，UI 显示「认证有效，无需重新登录」，所以不会打开登录窗口；本地网关的登录默认 headless，Windows 上只有 POSIX 的 `/run/um-share/login-display.env` 能触发可见窗口。结果是维护者无法从本地控制台显式强制一次可见重新登录。我们决定：`account.login` 增加 `force` 与 `visible` 两个可选参数；`force=true` 跳过按需核验直接触发登录轮次，`visible=true` 只对这次显式强制登录生效，自动重登录仍 headless；Windows 本地直接显示 Edge，Linux 有 `DISPLAY` 时显示，没有则明确返回 `login_unavailable`；控制台保留「检查并按需登录」，新增次级「强制重新登录（可见窗口）」；强制/可见只在本地控制台注册，云端不暴露；用户关闭可见窗口返回 `login_cancelled`，profile 被占用返回 `login_busy`，浏览器启动失败返回 `login_unavailable`，服务保持运行。

**Status**: accepted

## Considered Options

- **直接把现有按钮改成强制**：失去按需登录，且每次点击都跳过核验；放弃。
- **网关全局 `CHAT2API_LOGIN_VISIBLE=1`**：自动重登录也会弹窗，打断桌面；放弃。
- **只加 `force` 不加 `visible`**：控制台仍看不到窗口；放弃。
- **只加 `visible` 不加 `force`**：valid 时仍跳过登录；放弃。
- **`force` + `visible` 作为 `account.login` 的加性参数**：采用。

## Consequences

- 控制台多一个「强制重新登录（可见窗口）」入口；按需登录不变。
- 自动重登录仍 headless；14:19/14:43 的自动恢复行为不变。
- 强制/可见只在本地控制台；云端仍走分享专用后端的登录动作 + 手动 display。
- 用户关闭可见窗口 → `login_cancelled`，释放 profile 锁，服务保持运行；profile 被占用 → `login_busy`；无显示 → `login_unavailable`。
- 实现后需重启本地 8000/8001 让新参数生效。
