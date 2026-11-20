# #120: write the machine-local UM credentials file with a visible password.
#
# The password is deliberately not masked: the operator asked to see it while
# typing so a typo is caught before the next native-Rust login. The file stays
# plaintext 0600 per ADR 0012; the ACL is reduced to the current user.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$localAppData = [string]$env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw 'LOCALAPPDATA is not set'
}
$directory = Join-Path $localAppData 'Chat2API'
$path = Join-Path $directory 'credentials.json'

Write-Host 'UM 账号密码会写入本机凭据文件；输入时密码会显示，方便核对。'
$account = (Read-Host 'UM 账号').Trim()
if (-not $account) {
    throw 'UM 账号不能为空'
}

$password = Read-Host 'UM 密码（输入时会显示）'
if (-not $password.Trim()) {
    throw 'UM 密码不能为空'
}

$confirm = Read-Host '再输入一次 UM 密码（输入时会显示）'
if ($password -cne $confirm) {
    throw '两次输入的密码不一致，未写入'
}

New-Item -ItemType Directory -Force -Path $directory | Out-Null
@{
    version  = 1
    account  = $account
    password = $password
} | ConvertTo-Json | Set-Content -LiteralPath $path -Encoding UTF8

# Reduce the DACL to the current user only. The runtime helper refuses to inject
# a file whose ACL still allows anyone else.
$grant = "$($env:USERNAME):(R,W)"
& icacls.exe $path /inheritance:r /grant:r $grant | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw '无法把凭据文件 ACL 限制为当前用户'
}

Write-Host "已写入 $path（仅当前用户可读写）"
Write-Host "账号：$account"
Write-Host '密码已按输入内容保存；下次启动 8000 时由启动器注入。'
