//! Console (8002) configuration.
//!
//! The Rust console only assembles the cloud role (S5a). Role, reported port,
//! gateway facts and pid-file paths come from the deployment environment.
//! Candidate mode is the default and binds only an ephemeral loopback port;
//! production must be selected explicitly and binds only `127.0.0.1:8002`.
//! Invalid values fail closed before the bind.

use std::path::PathBuf;

use um_gateway::config::is_loopback_host;
pub use um_gateway::config::BindMode;

use crate::display_relay::DisplayConfig;

pub const CLOUD_DEFAULT_PORT: u16 = 8002;
pub const LOCAL_DEFAULT_PORT: u16 = 8001;
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
pub const CONSOLE_LOCK_DIR_ENV: &str = "CONSOLE_LOCK_DIR";
pub const SHARE_STORE_FILE_ENV: &str = "SHARE_STORE_FILE";
pub const SHARE_SECRET_KEY_FILE_ENV: &str = "CHAT2API_SHARE_SECRET_KEY_FILE";
pub const PUBLIC_ORIGIN_ENV: &str = "CONSOLE_PUBLIC_ORIGIN";
pub const SECURITY_STATE_FILE_ENV: &str = "UM_CLOUD_SECURITY_STATE";
pub const CONTROL_TOKEN_FILE_ENV: &str = "USAGE_CONTROL_TOKEN_FILE";
pub const SHARE_BACKEND_DIR_ENV: &str = "CONSOLE_SHARE_BACKEND_DIR";
pub const USAGE_SESSION_CODE_FILE_ENV: &str = "USAGE_SESSION_CODE_FILE";
pub const USAGE_LOG_ENV: &str = "CHAT2API_USAGE_LOG";
pub const DSH_SNAPSHOT_ENV: &str = "DSH_LEDGER_SNAPSHOT_FILE";
pub const SERVICE_LOG_ENV: &str = "CHAT2API_SERVICE_LOG";
pub const UPSTREAM_FAULT_LOG_ENV: &str = "CHAT2API_UPSTREAM_FAULT_LOG";
pub const WATCH_DIR_ENV: &str = "SELFUSEAI_GATEWAY_WATCH_DIR";
pub const DIST_DIR_ENV: &str = "CONSOLE_DIST_DIR";
pub const REPO_ROOT_ENV: &str = "CONSOLE_REPO_ROOT";

pub const DEFAULT_DIST_DIR: &str = "web/dist";
pub const DEFAULT_REPO_ROOT: &str = ".";
pub const DEFAULT_USAGE_LOG: &str = "data/usage-chat2api.jsonl";
pub const DEFAULT_DSH_SNAPSHOT: &str = "data/dsh-ledger-20260902.json";
pub const DEFAULT_SERVICE_LOG: &str = "chat2api/chat2api.log";
pub const DEFAULT_UPSTREAM_FAULT_LOG: &str = "data/upstream-faults.json";
pub const DEFAULT_CONSOLE_STATE_FILE: &str = "data/console-state.sqlite3";
pub const DEFAULT_CONSOLE_LOCK_DIR: &str = "data/console-locks";
pub const DEFAULT_SHARE_STORE_FILE: &str = "data/share-store.db";
pub const DEFAULT_CONTROL_TOKEN_FILE: &str = "data/admin-control-token";
pub const DEFAULT_USAGE_SESSION_CODE_FILE: &str = "data/usage-session-code";

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

    /// The report port this role owns: 8001 local, 8002 cloud.
    pub fn default_port(self) -> u16 {
        match self {
            Self::Local => LOCAL_DEFAULT_PORT,
            Self::Cloud => CLOUD_DEFAULT_PORT,
        }
    }

    /// The production bind port this role owns: 8001 local, 8002 cloud.
    pub fn default_bind_port(self) -> u16 {
        self.default_port()
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
    /// Candidate mode (the default); `false` selects the explicit production
    /// binding policy.
    pub candidate: bool,
    pub console_state_file: Option<PathBuf>,
    pub console_lock_dir: Option<PathBuf>,
    pub gateway_pid_file: PathBuf,
    pub gateway_state_dir: PathBuf,
    pub gateway_port: u16,
    pub gateway_entry: String,
    pub gateway_entry_kind: EntryKind,
    pub proc_root: PathBuf,
    pub expected_uid: Option<u32>,
    pub share_store_file: Option<PathBuf>,
    pub share_secret_key_file: Option<PathBuf>,
    pub public_origin: Option<String>,
    pub security_state_file: Option<PathBuf>,
    pub control_token_file: Option<PathBuf>,
    pub share_backend_dir: Option<PathBuf>,
    pub usage_session_code_file: Option<PathBuf>,
    /// Local role inputs: the private chat2api usage ledger, the frozen DSH
    /// day snapshot and the chat2api run log. Cloud role ignores them.
    pub usage_log: PathBuf,
    pub dsh_snapshot: PathBuf,
    pub service_log: PathBuf,
    /// #103: the durable UM upstream fault record written by the local 8000
    /// gateway and read here without probing or writing.
    pub upstream_fault_log: PathBuf,
    pub watch_dir: Option<PathBuf>,
    /// The built console page tree hosted by the local role
    /// (`usagesrv.py::CONSOLE_DIST_DIR`).
    pub console_dist_dir: PathBuf,
    /// The repo root the local role reads its PowerShell runtime entry from. The
    /// service, account and model-sync actions all run without a Python child
    /// process (#92/#93/#94).
    pub repo_root: PathBuf,
    pub display: DisplayConfig,
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
            console_lock_dir: None,
            gateway_pid_file: PathBuf::from(DEFAULT_GATEWAY_PID_FILE),
            gateway_state_dir: PathBuf::from(DEFAULT_GATEWAY_STATE_DIR),
            gateway_port: DEFAULT_GATEWAY_PORT,
            gateway_entry: DEFAULT_GATEWAY_ENTRY.to_string(),
            gateway_entry_kind: EntryKind::Python,
            proc_root: PathBuf::from(DEFAULT_PROC_ROOT),
            expected_uid: None,
            share_store_file: None,
            share_secret_key_file: None,
            public_origin: None,
            security_state_file: None,
            control_token_file: None,
            share_backend_dir: None,
            usage_session_code_file: None,
            usage_log: PathBuf::from(DEFAULT_USAGE_LOG),
            dsh_snapshot: PathBuf::from(DEFAULT_DSH_SNAPSHOT),
            service_log: PathBuf::from(DEFAULT_SERVICE_LOG),
            upstream_fault_log: PathBuf::from(DEFAULT_UPSTREAM_FAULT_LOG),
            watch_dir: None,
            console_dist_dir: PathBuf::from(DEFAULT_DIST_DIR),
            repo_root: PathBuf::from(DEFAULT_REPO_ROOT),
            display: DisplayConfig::default(),
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

/// Python's local composition root defaults (`usagesrv.py` + `share/management.py`):
/// the local role owns these paths unless an explicit environment value wins.
pub fn apply_local_defaults(config: &mut ConsoleConfig) {
    if config.console_state_file.is_none() {
        config.console_state_file = Some(PathBuf::from(DEFAULT_CONSOLE_STATE_FILE));
    }
    if config.console_lock_dir.is_none() {
        config.console_lock_dir = Some(PathBuf::from(DEFAULT_CONSOLE_LOCK_DIR));
    }
    if config.share_store_file.is_none() {
        config.share_store_file = Some(PathBuf::from(DEFAULT_SHARE_STORE_FILE));
    }
    if config.control_token_file.is_none() {
        config.control_token_file = Some(PathBuf::from(DEFAULT_CONTROL_TOKEN_FILE));
    }
    if config.usage_session_code_file.is_none() {
        config.usage_session_code_file = Some(PathBuf::from(DEFAULT_USAGE_SESSION_CODE_FILE));
    }
    if config.share_backend_dir.is_none() {
        if let Some(local) = std::env::var_os("LOCALAPPDATA").filter(|value| !value.is_empty()) {
            config.share_backend_dir = Some(
                PathBuf::from(local)
                    .join("SelfUseAI")
                    .join("ai-gateway")
                    .join("share-backend"),
            );
        }
    }
    if config.watch_dir.is_none() {
        if let Some(local) = std::env::var_os("LOCALAPPDATA").filter(|value| !value.is_empty()) {
            config.watch_dir = Some(
                PathBuf::from(local)
                    .join("SelfUseAI")
                    .join("ai-gateway")
                    .join("watch"),
            );
        }
    }
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
        config.port = config.role.default_port();
        if config.role == Role::Local {
            apply_local_defaults(&mut config);
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
        if let Ok(value) = std::env::var(CONSOLE_LOCK_DIR_ENV) {
            if !value.trim().is_empty() {
                config.console_lock_dir = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(SHARE_STORE_FILE_ENV) {
            if !value.trim().is_empty() {
                config.share_store_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(SHARE_SECRET_KEY_FILE_ENV) {
            if !value.trim().is_empty() {
                config.share_secret_key_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(PUBLIC_ORIGIN_ENV) {
            config.public_origin = Some(value);
        }
        if let Ok(value) = std::env::var(SECURITY_STATE_FILE_ENV) {
            if !value.trim().is_empty() {
                config.security_state_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(CONTROL_TOKEN_FILE_ENV) {
            if !value.trim().is_empty() {
                config.control_token_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(SHARE_BACKEND_DIR_ENV) {
            if !value.trim().is_empty() {
                config.share_backend_dir = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(USAGE_SESSION_CODE_FILE_ENV) {
            if !value.trim().is_empty() {
                config.usage_session_code_file = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(USAGE_LOG_ENV) {
            if !value.trim().is_empty() {
                config.usage_log = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(DSH_SNAPSHOT_ENV) {
            if !value.trim().is_empty() {
                config.dsh_snapshot = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(SERVICE_LOG_ENV) {
            if !value.trim().is_empty() {
                config.service_log = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(UPSTREAM_FAULT_LOG_ENV) {
            if !value.trim().is_empty() {
                config.upstream_fault_log = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(WATCH_DIR_ENV) {
            if !value.trim().is_empty() {
                config.watch_dir = Some(PathBuf::from(value));
            }
        }
        if let Ok(value) = std::env::var(DIST_DIR_ENV) {
            if !value.trim().is_empty() {
                config.console_dist_dir = PathBuf::from(value);
            }
        }
        if let Ok(value) = std::env::var(REPO_ROOT_ENV) {
            if !value.trim().is_empty() {
                config.repo_root = PathBuf::from(value);
            }
        }
        config.display = DisplayConfig::from_env();
        Ok(config)
    }

    /// The selected binding policy. Candidate is the default.
    pub fn bind_mode(&self) -> BindMode {
        if self.candidate {
            BindMode::Candidate
        } else {
            BindMode::Production
        }
    }

    /// Fail closed before binding a socket. The role selects the production
    /// port: cloud 8002, local 8001. Candidate binds only an ephemeral
    /// loopback port.
    pub fn validate_bind_policy(&self) -> Result<(), ConfigError> {
        if !is_loopback_host(&self.bind_host) {
            return Err(ConfigError(format!(
                "{} bind host is not loopback: {}",
                self.bind_mode().label(),
                self.bind_host
            )));
        }
        if self.candidate {
            if self.bind_port != 0 {
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
        } else {
            let expected = self.role.default_bind_port();
            if self.bind_port != expected {
                return Err(ConfigError(format!(
                    "production {} role must bind {expected} on loopback, got {}",
                    self.role.as_str(),
                    self.bind_port
                )));
            }
        }
        if self.port == 0 {
            return Err(ConfigError(
                "the reported console port must be 1..65535".to_string(),
            ));
        }
        Ok(())
    }
}
