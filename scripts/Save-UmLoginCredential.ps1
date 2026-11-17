[CmdletBinding()]
param([switch]$Replace, [switch]$ValidateOnly)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw '本工具仅支持 Windows。' }
if (-not $ValidateOnly -and $PSVersionTable.PSEdition -ne 'Desktop') {
    throw '请使用 powershell.exe -NoProfile -STA -File scripts\Save-UmLoginCredential.ps1，以显示本地密码输入窗口。'
}

Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class UmLocalCredentialSetup {
    const string Target = "Chat2API-GatewayConsole/UM/local";
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct Credential {
        public uint Flags, Type;
        public string TargetName, Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist, AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias, UserName;
    }
    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool Read(string target, uint type, uint flags, out IntPtr record);
    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool Write(ref Credential record, uint flags);
    [DllImport("advapi32.dll")]
    static extern void CredFree(IntPtr record);

    public static bool Exists() {
        IntPtr pointer;
        if (!Read(Target, 1, 0, out pointer)) {
            int code = Marshal.GetLastWin32Error();
            if (code == 1168) return false;
            throw new InvalidOperationException("检查专用凭据失败，Win32=" + code);
        }
        try {
            var value = (Credential)Marshal.PtrToStructure(pointer, typeof(Credential));
            for (int i = 0; i < value.CredentialBlobSize; i++) Marshal.WriteByte(value.CredentialBlob, i, 0);
        } finally { CredFree(pointer); }
        return true;
    }

    public static void Save(string account, IntPtr password, int characters) {
        if (String.IsNullOrWhiteSpace(account) || account.IndexOf('\0') >= 0 || characters <= 0 || characters > 1280)
            throw new InvalidOperationException("账号或密码长度无效，未保存。");
        var record = new Credential {
            Type = 1, TargetName = Target, UserName = account,
            Comment = "本机 UM 网关自动登录；密码由本人在本地输入",
            CredentialBlob = password, CredentialBlobSize = checked((uint)characters * 2), Persist = 2
        };
        if (!Write(ref record, 0)) throw new InvalidOperationException("保存专用凭据失败，Win32=" + Marshal.GetLastWin32Error());
    }
}
'@

if ($ValidateOnly) { Write-Output '本地凭据工具编译通过；未读取或保存凭据。'; return }
if ([UmLocalCredentialSetup]::Exists() -and -not $Replace) {
    throw '已有本机 UM 专用凭据；需要更新时显式添加 -Replace。本次未覆盖。'
}
$credential = Get-Credential -Message '为本机 UM 网关保存自动登录凭据（仅此 Windows 用户）。请填写 UMPASS 账号和密码。'
if ($null -eq $credential) { Write-Output '已取消；未保存凭据。'; exit 2 }
$secretPointer = [IntPtr]::Zero
try {
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($credential.Password)
    [UmLocalCredentialSetup]::Save($credential.UserName, $secretPointer, $credential.Password.Length)
    Write-Output '已保存本机 UM 专用凭据。下一轮后台登录会读取；未修改 Edge 已保存密码。'
} finally {
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeGlobalAllocUnicode($secretPointer) }
    $credential.Password.Dispose()
    Remove-Variable credential -ErrorAction SilentlyContinue
}
