# Windows 本机 UM 自动登录凭据

Edge 在本机 headless 模式中可能始终不填入已保存密码。网关在既有 20 秒表单稳定预算后，读取专用的 Windows 通用凭据，继续通过真实 HTTPS UMPASS 表单登录；不提取 Edge 密码。

## 一次性设置

本人确认保存后，在仓库根目录运行：

```powershell
powershell.exe -NoProfile -STA -File scripts\Save-UmLoginCredential.ps1
```

在本地 Windows 密码窗口填写 UMPASS 账号及密码。取消不保存；已有记录时拒绝覆盖，需要本人显式使用 `-Replace`。不要将密码发到聊天、命令行参数、日志或用户环境变量。

记录类型为 `CRED_TYPE_GENERIC`，目标名为 `Chat2API-GatewayConsole/UM/local`，持久化为 `CRED_PERSIST_LOCAL_MACHINE`。它归属于当前 Windows 用户，保存在 Windows 凭据管理器中；同用户运行的应用有能力读取，不是对其他同用户进程隔离的保险箱。可在 Windows 凭据管理器中删除该记录。

## 读取与失败边界

- 仅 Windows 的 `local` 角色自动装配该来源，云端和 Linux 不读取本机记录。
- 完整的 `CHAT2API_UM_ACCOUNT` / `CHAT2API_UM_PASSWORD` 环境配置优先；只提供一项时明确报配置不完整，不混合两个来源。密码不裁剪空格。
- 每次浏览器登录会话在需要填表时读取一次；保存或修改记录后，下次登录立即生效。已有 SSO 或浏览器自动填充成功时不读取。
- 只向 `https://websso.um.edu.mo`、`https://login.um.edu.mo` 的表单填入。不存在记录沿用人工输入路径；访问失败和损坏记录明确报独立错误，不作为“缺少密码”处理，不自动反复提交。
- 内存中读取的临时凭据会清零；`LoginConfig` 的 Debug 输出不包含账号和密码。设置工具不输出账号或密码。
- 正常登录仍复用原 profile。Duo、验证码、密码过期或账号被拒需要本人处理；本方案不能绕过 MFA。

## 无副作用检查

```powershell
powershell.exe -NoProfile -File scripts\Save-UmLoginCredential.ps1 -ValidateOnly
```

仅编译本地 WinCred 调用，不读取或写入任何凭据。

原生读取与释放契约依据 Microsoft 的 [CredReadW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw)，保存依据 [CredWriteW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)。
