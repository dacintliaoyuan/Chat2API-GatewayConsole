//! Saved desktop settings: `%LOCALAPPDATA%\Chat2API\desktop.json`.
//!
//! The ticket only saves the configured model API port. Binding and switching the
//! listener belongs to #124; the port is still validated here so a bad value can
//! never reach that later switch.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

pub const DEFAULT_MODEL_API_PORT: u16 = 7999;
pub const MODEL_API_HOST: &str = "127.0.0.1";
pub const CONFIG_VERSION: i64 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DesktopConfig {
    pub model_api_port: u16,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadOutcome {
    /// No settings file yet. This is a legal first-open state, not an error.
    Missing,
    Loaded(DesktopConfig),
    /// The file exists but is unreadable, corrupt or invalid. The caller keeps
    /// the defaults and must show the reason instead of pretending success.
    Invalid(String),
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            model_api_port: DEFAULT_MODEL_API_PORT,
        }
    }
}

/// `%LOCALAPPDATA%\Chat2API` on Windows. The caller supplies the root so tests
/// never touch the real user profile.
pub fn config_dir(local_app_data: &Path) -> PathBuf {
    local_app_data.join("Chat2API")
}

pub fn config_path(local_app_data: &Path) -> PathBuf {
    config_dir(local_app_data).join("desktop.json")
}

/// `%LOCALAPPDATA%\Chat2API`.
pub fn app_data_root() -> Result<PathBuf, String> {
    let local = std::env::var_os("LOCALAPPDATA")
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "LOCALAPPDATA 未设置，无法定位应用数据目录".to_string())?;
    Ok(config_dir(Path::new(&local)))
}

/// `%LOCALAPPDATA%\Chat2API\desktop.json`.
pub fn default_config_path() -> Result<PathBuf, String> {
    Ok(app_data_root()?.join("desktop.json"))
}

pub fn model_api_bind(config: &DesktopConfig) -> String {
    format!("{MODEL_API_HOST}:{}", config.model_api_port)
}

pub fn validate_port(text: &str) -> Result<u16, String> {
    let trimmed = text.trim();
    let port = trimmed
        .parse::<u16>()
        .map_err(|_| "端口必须是 1-65535 的整数".to_string())?;
    if port == 0 {
        return Err("端口必须在 1-65535".to_string());
    }
    Ok(port)
}

pub fn parse_config(text: &str) -> Result<DesktopConfig, String> {
    let body: Value =
        serde_json::from_str(text).map_err(|error| format!("配置不是有效 JSON：{error}"))?;
    let port = body
        .get("model_api_port")
        .ok_or_else(|| "配置缺少 model_api_port".to_string())?
        .as_u64()
        .ok_or_else(|| "配置 model_api_port 不是整数".to_string())?;
    if port == 0 || port > u16::MAX as u64 {
        return Err("配置 model_api_port 超出 1-65535".to_string());
    }
    Ok(DesktopConfig {
        model_api_port: port as u16,
    })
}

pub fn load_from(path: &Path) -> LoadOutcome {
    match std::fs::read_to_string(path) {
        Ok(text) => match parse_config(&text) {
            Ok(config) => LoadOutcome::Loaded(config),
            Err(error) => LoadOutcome::Invalid(error),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => LoadOutcome::Missing,
        Err(error) => LoadOutcome::Invalid(format!("读取配置失败：{error}")),
    }
}

/// Writes the settings atomically and reduces the file ACL to the current user.
///
/// The temp file is hardened before the rename so the final path never exists with
/// the inherited profile ACL. A failed hardening removes the temp file and reports
/// the error instead of leaving a half-protected config behind.
pub fn save_to(path: &Path, config: &DesktopConfig) -> Result<(), String> {
    if config.model_api_port == 0 {
        return Err("端口必须在 1-65535".to_string());
    }
    let parent = path
        .parent()
        .ok_or_else(|| "配置路径没有父目录".to_string())?;
    std::fs::create_dir_all(parent).map_err(|error| format!("创建配置目录失败：{error}"))?;
    let text = serde_json::to_string_pretty(&json!({
        "version": CONFIG_VERSION,
        "model_api_port": config.model_api_port,
    }))
    .map_err(|error| format!("序列化配置失败：{error}"))?;
    let temp = parent.join(format!(".desktop.json.{}.tmp", std::process::id()));
    std::fs::write(&temp, format!("{text}\n"))
        .map_err(|error| format!("写入临时配置失败：{error}"))?;
    if let Err(error) = harden_private_file(&temp) {
        let _ = std::fs::remove_file(&temp);
        return Err(error);
    }
    if let Err(error) = std::fs::rename(&temp, path) {
        let _ = std::fs::remove_file(&temp);
        return Err(format!("替换配置文件失败：{error}"));
    }
    Ok(())
}

#[cfg(unix)]
pub(crate) fn harden_private_file(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("设置配置文件权限失败：{error}"))
}

#[cfg(windows)]
pub(crate) fn harden_private_file(path: &Path) -> Result<(), String> {
    windows_acl::harden_to_current_user(path)
}

#[cfg(windows)]
mod windows_acl {
    use std::ffi::c_void;
    use std::os::windows::ffi::OsStrExt;

    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, ERROR_SUCCESS, HANDLE};
    use windows::Win32::Security::Authorization::{
        SetNamedSecurityInfoW, SE_FILE_OBJECT,
    };
    use windows::Win32::Security::{
        AddAccessAllowedAce, GetTokenInformation, InitializeAcl, TokenUser, ACL, ACL_REVISION,
        DACL_SECURITY_INFORMATION, PSID, PROTECTED_DACL_SECURITY_INFORMATION, TOKEN_QUERY,
        TOKEN_USER,
    };
    use windows::Win32::Storage::FileSystem::FILE_ALL_ACCESS;
    use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    pub fn harden_to_current_user(path: &std::path::Path) -> Result<(), String> {
        let wide: Vec<u16> = path
            .as_os_str()
            .encode_wide()
            .chain(std::iter::once(0))
            .collect();
        unsafe {
            let mut token = HANDLE::default();
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token)
                .map_err(|error| format!("读取当前用户令牌失败：{error}"))?;

            let mut size = 0u32;
            let _ = GetTokenInformation(token, TokenUser, None, 0, &mut size);
            if size == 0 {
                let _ = CloseHandle(token);
                return Err("当前用户令牌大小不可用".to_string());
            }
            let mut token_buffer = vec![0u8; size as usize];
            let token_info = GetTokenInformation(
                token,
                TokenUser,
                Some(token_buffer.as_mut_ptr() as *mut c_void),
                size,
                &mut size,
            );
            let _ = CloseHandle(token);
            token_info.map_err(|error| format!("读取当前用户 SID 失败：{error}"))?;
            let sid: PSID = (*token_buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid;

            let mut acl_buffer = vec![0u8; 1024];
            let acl = acl_buffer.as_mut_ptr().cast::<ACL>();
            InitializeAcl(acl, acl_buffer.len() as u32, ACL_REVISION)
                .map_err(|error| format!("初始化 ACL 失败：{error}"))?;
            AddAccessAllowedAce(acl, ACL_REVISION, FILE_ALL_ACCESS.0, sid)
                .map_err(|error| format!("添加当前用户 ACE 失败：{error}"))?;

            let result = SetNamedSecurityInfoW(
                PCWSTR(wide.as_ptr()),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(acl),
                None,
            );
            if result != ERROR_SUCCESS {
                return Err(format!("设置配置文件 ACL 失败：{result:?}"));
            }
        }
        Ok(())
    }
}
