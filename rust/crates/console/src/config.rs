//! Candidate console (8002) configuration.
//!
//! The Rust console only assembles the cloud role (S5a). Role, reported port,
//! gateway facts and pid-file paths come from the deployment environment; the
//! candidate process itself binds only an ephemeral loopback port and never
//! switches 8002. Invalid values fail closed before the bind.

use std::net::IpAddr;
use std::path::PathBuf;

pub const CLOUD_DEFAULT_PORT: u16 = 8002;
pub const DEFAULT_GATEWAY_PORT: u16 = 8003;
pub const DEFAULT_GATEWAY_PID_FILE: &str = "/run/um-share/share-gateway.pid";
pub const DEFAULT_GATEWAY_STATE_DIR: &str = "/run/um-share";
pub const DEFAULT_GATEWAY_ENTRY: &str = "chat2api.py";
pub const DEFAULT_PROC_ROOT: &str = "/proc";

pub const ROLE_ENV: &str = "UM_SHARE_ROLE";
pub const DEPLOYMENT_ENV: &str = "UM_SHARE_DEPLOYMENT";
pub const PORT_ENV: &str = "USAGESRV_PORT";
pub const GATEWAY_PORT_ENV: &str = "UM_SHARE_GATEWAY_PORT";
pub const GATEWAY_PID_FILE_ENV: &str = "UM_SHARE_GATEWAY_PID_FILE";
pub const GATEWAY_STATE_DIR_ENV: &str = "UM_SHARE_HOST_STATE_DIR";
pub const GATEWAY_ENTRY_ENV: &str = "UM_SHARE_GATEWAY_ENTRY";
pub const GATEWAY_ENTRY_KIND_ENV: &str = "UM_SHARE_GATEWAY_ENTRY_KIND";
pub const PROC_ROOT_ENV: &str = "UM_SHARE_PROC_ROOT";
pub const EXPECTED_UID_ENV: &str = "UM_SHARE_EXPECTED_UID";
pub const CONSOLE_STATE_FILE_ENV: &str = "CONSOLE_STATE_FILE";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    Local,
    Cloud,
}

impl Role {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "local" => Some(Self::Local),
            "cloud" => Some(Self::Cloud),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Local => "local",
            Self::Cloud => "cloud",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryKind {
    Python,
    Native,
}

impl EntryKind {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "python" => Some(Self::Python),
            "native" => Some(Self::Native),
            _ => None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct ConsoleConfig {
    pub bind_host: String,
    pub bind_port: u16,
    /// The port this console reports as its deployment port. It is never bound
    /// by the candidate.
    pub port: u16,
    pub role: Role,
    pub candidate: bool,
    pub console_state_file: Option<PathBuf>,
    pub gateway_pid_file: PathBuf,
    pub gateway_state_dir: PathBuf,
    pub gateway_port: u16,
    pub gateway_entry: String,
    pub gateway_entry_kind: EntryKind,
    pub proc_root: PathBuf,
    pub expected_uid: Option<u32>,
}

impl Default for ConsoleConfig {
    fn default() -> Self {
        Self {
            bind_host: "127.0.0.1".to_string(),
            bind_port: 0,
            port: CLOUD_DEFAULT_PORT,
            role: Role::Cloud,
            candidate: true,
            console_state_file: None,
            gateway_pid_file: PathBuf::from(DEFAULT_GATEWAY_PID_FILE),
            gateway_state_dir: PathBuf::from(DEFAULT_GATEWAY_STATE_DIR),
            gateway_port: DEFAULT_GATEWAY_PORT,
            gateway_entry: DEFAULT_GATEWAY_ENTRY.to_string(),
            gateway_entry_kind: EntryKind::Python,
            proc_root: PathBuf::from(DEFAULT_PROC_ROOT),
            expected_uid: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigError(pub String);

impl std::fmt::Display for ConfigError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for ConfigError {}

pub fn is_loopback_host(host: &str) -> bool {
    let host = host.to_ascii_lowercase();
    if host == "localhost" {
        return true;
    }
    host.parse::<IpAddr>()
        .map(|address| address.is_loopback())
        .unwrap_or(false)
}

/// Ports the candidate must never bind while it is a candidate.
pub fn governed_ports() -> [u16; 7] {
    [5173, 8000, 8001, 8002, 8003, 8097, 8099]
}

pub fn strict_port(name: &str, value: &str) -> Result<u16, ConfigError> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(ConfigError(format!(
            "{name} must be a decimal integer in 1..65535"
        )));
    }
    let port = value
        .parse::<u16>()
        .map_err(|_| ConfigError(format!("{name} must be a decimal integer in 1..65535")))?;
    if port == 0 {
        return Err(ConfigError(format!(
            "{name} must be a decimal integer in 1..65535"
        )));
    }
    Ok(port)
}

pub fn parse_expected_uid(value: &str) -> Result<u32, ConfigError> {
    let text = value.trim();
    if text.is_empty() || !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(ConfigError(
            "UM_SHARE_EXPECTED_UID must be a decimal integer".to_string(),
        ));
    }
    text.parse::<u32>()
        .map_err(|_| ConfigError("UM_SHARE_EXPECTED_UID must be a decimal integer".to_string()))
}

/// Resolve the explicit role. Unset -> cloud for this 8002 candidate; unknown
/// values and conflicting role variables fail closed.
pub fn resolve_role(role: Option<&str>, deployment: Option<&str>) -> Result<Role, ConfigError> {
    let mut seen = Vec::new();
    for (name, raw) in [(ROLE_ENV, role), (DEPLOYMENT_ENV, deployment)] {
        let Some(raw) = raw else { continue };
        let text = raw.to_ascii_lowercase();
        let parsed = Role::parse(&text)
            .ok_or_else(|| ConfigError(format!("{name} accepts only local/cloud")))?;
        seen.push(parsed);
    }
    if seen.is_empty() {
        return Ok(Role::Cloud);
    }
    if seen.windows(2).any(|pair| pair[0] != pair[1]) {
        return Err(ConfigError(
            "UM_SHARE_ROLE and UM_SHARE_DEPLOYMENT conflict".to_string(),
        ));
    }
    Ok(seen[0])
}

impl ConsoleConfig {
    pub fn from_env() -> Result<Self, ConfigError> {
        let mut config = Self::default();
        if let Ok(value) = std::env::var(ROLE_ENV) {
            config.role =
                resolve_role(Some(&value), std::env::var(DEPLOYMENT_ENV).ok().as_deref())?;
        } else if let Ok(value) = std::env::var(DEPLOYMENT_ENV) {
            config.role = resolve_role(None, Some(&value))?;
        }
        if let Ok(value) = std::env::var(PORT_ENV) {
            config.port = strict_port(PORT_ENV, &value)?;
        }
        if let Ok(value) = std::env::var(GATEWAY_PORT_ENV) {
            config.gateway_port = strict_port(GATEWAY_PORT_ENV, &value)?;
        }
        if let Ok(value) = std::env::var(GATEWAY_PID_FILE_ENV) {
            if !value.trim().is_empty() {
                config.gateway_pid_file = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(GATEWAY_STATE_DIR_ENV) {
            if !value.trim().is_empty() {
                config.gateway_state_dir = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(GATEWAY_ENTRY_ENV) {
            if !value.trim().is_empty() {
                config.gateway_entry = value;
            }
        }
        if let Ok(value) = std::env::var(GATEWAY_ENTRY_KIND_ENV) {
            config.gateway_entry_kind = EntryKind::parse(&value).ok_or_else(|| {
                ConfigError(format!(
                    "{GATEWAY_ENTRY_KIND_ENV} accepts only python/native"
                ))
            })?;
        }
        if let Ok(value) = std::env::var(PROC_ROOT_ENV) {
            if !value.trim().is_empty() {
                config.proc_root = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(EXPECTED_UID_ENV) {
            config.expected_uid = Some(parse_expected_uid(&value)?);
        }
        if let Ok(value) = std::env::var(CONSOLE_STATE_FILE_ENV) {
            if !value.trim().is_empty() {
                config.console_state_file = Some(PathBuf::from(value));
            }
        }
        Ok(config)
    }

    /// Fail closed before binding a socket.
    pub fn validate_candidate(&self) -> Result<(), ConfigError> {
        if self.role != Role::Cloud {
            return Err(ConfigError(
                "the S5a console candidate assembles only the cloud role".to_string(),
            ));
        }
        if !is_loopback_host(&self.bind_host) {
            return Err(ConfigError(format!(
                "candidate bind host is not loopback: {}",
                self.bind_host
            )));
        }
        if self.candidate && self.bind_port != 0 {
            return Err(ConfigError(format!(
                "candidate must bind an ephemeral loopback port, got {}",
                self.bind_port
            )));
        }
        if governed_ports().contains(&self.bind_port) {
            return Err(ConfigError(format!(
                "refusing to bind governed production port {}",
                self.bind_port
            )));
        }
        if self.port == 0 {
            return Err(ConfigError(
                "the reported console port must be 1..65535".to_string(),
            ));
        }
        Ok(())
    }
}
