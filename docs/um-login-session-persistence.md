# UM 首次登录后的保存与复用

本机继续沿用旧 Python 的方式：本人首次在原 Edge profile 中完成 UM 登录；网关保存已核验的 token，浏览器保留 Cookie、SSO 会话及其自行管理的自动填充。程序不创建额外的账号密码库，也不读取或解密 Edge 保存的密码。

## 保存边界

- 本机使用 `chat2api/token.json` 与 `chat2api/.edge-profile`；Python 与 Rust 的默认路径和 token JSON 格式一致。服务重启先加载已保存 token，无需为正常重启重新输入账号密码。
- token 经上游核验有效、浏览器正常退出后，才原子写入 token 文件；取消和失败保留旧文件，不发布未经核验的 token。
- 登录浏览器先发送 CDP `Browser.close`，等待它退出，让 Chromium 正常保存 profile。只有正常关闭失败或超过 5 秒时才强制清理，并把此次关闭报告为失败，不能声称登录状态已经可靠保存。
- 首次可见登录时，是否记住账号密码由本人选择 Edge 原有功能。网关复用同一 profile，不另外复制密码。
- 网络超时、5xx 或连接中断不等于认证过期。保存的 token 也可能在声明到期前被上游撤销；只有明确的认证拒绝才触发重新认证。
- 会话被撤销、SSO 失效、密码过期、验证码或 Duo 要求仍可能需要本人操作。保存成功不能保证上游永久接受会话。

## 回归方法

真实 Edge 回归使用独立临时 profile、loopback HTTP 夹具和虚构持久 Cookie，不访问 UM、不使用真实登录数据：

```powershell
$env:CARGO_PROFILE_TEST_DEBUG='0'
cargo test --manifest-path rust/Cargo.toml -p um-gateway --test browser_persistence -- --ignored --nocapture
```

它经过生产 `SystemBrowserFactory` 的启动、关闭和同 profile 重开，断言重开后请求仍带持久 Cookie。该用例默认忽略，需本机安装 Edge 后显式运行。

本机已实际观察到：旧的强制结束实现使此回归失败；正常关闭实现使同一回归通过。这个结果证明浏览器适配器的 Cookie 复用边界，不替代真实账号的上游核验和重启验收。
