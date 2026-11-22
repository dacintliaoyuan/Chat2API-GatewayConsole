//! Supervises the desktop app's own gateway process.
//!
//! The app owns one loopback gateway instance (default `127.0.0.1:7999`, test
//! instance `127.0.0.1:7998`) and never touches the existing 8000/8001 pair. The
//! gateway binary is resolved, spawned with the existing CLI, waited on through
//! `/health`, stopped through `POST /admin/shutdown`, and never killed unless the
//! app itself spawned it and the graceful stop timed out.
//!
//! The process and HTTP edges are traits so the state machine is testable without a
//! real gateway. Tokens are never printed: [`ControlToken`] redacts its `Debug`
//! output and the supervision log only records events and paths.
//!
//! Port changes first drain admitted model requests through the desktop-only
//! control route. A normal exit still uses immediate shutdown.

use std::ffi::OsString;
use std::fmt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const DEFAULT_INSTANCE_PORT: u16 = 7999;
pub const TEST_INSTANCE_PORT: u16 = 7998;
pub const UPSTREAM_BASE_URL: &str = "https://chat.genai.um.edu.mo";
/// The existing production pair and the cloud pair are never touched.
pub const GOVERNED_PORTS: [u16; 4] = [8000, 8001, 8002, 8003];
pub const HEALTH_TIMEOUT: Duration = Duration::from_secs(20);
pub const STOP_TIMEOUT: Duration = Duration::from_secs(10);
pub const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(100);
const DRAIN_POLL_INTERVAL: Duration = Duration::from_millis(250);
const DRAIN_TIMEOUT: Duration = Duration::from_secs(600);

pub const GATEWAY_BIN_ENV: &str = "UM_DESKTOP_GATEWAY_BIN";
pub const LOGIN_HISTORY_ENV: &str = "CHAT2API_LOGIN_HISTORY_FILE";

/// Every writable runtime file of one app instance.
///
/// Credentials and the desktop settings stay at the app-data root (#120/#123);
/// the token, browser profile, auth evidence, login history and both logs live in
/// `instance/` so the app instance can be started and stopped without touching the
/// existing 8000/8001 data. The app's own supervision events go to `logs/`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstancePaths {
    pub app_data: PathBuf,
    pub instance_dir: PathBuf,
    pub control_token: PathBuf,
    pub token: PathBuf,
    pub profile_dir: PathBuf,
    pub auth_state: PathBuf,
    pub login_history: PathBuf,
    pub usage_log: PathBuf,
    pub service_log: PathBuf,
    pub upstream_faults: PathBuf,
    pub stderr_log: PathBuf,
    pub supervision_log: PathBuf,
}

impl InstancePaths {
    pub fn under(app_data: &Path) -> Self {
        let instance_dir = app_data.join("instance");
        let logs_dir = app_data.join("logs");
        Self {
            app_data: app_data.to_path_buf(),
            control_token: instance_dir.join("admin-control-token"),
            token: instance_dir.join("token.json"),
            profile_dir: instance_dir.join("profile"),
            auth_state: instance_dir.join("auth-state.json"),
            login_history: instance_dir.join("login-history.jsonl"),
            usage_log: instance_dir.join("usage.jsonl"),
            service_log: instance_dir.join("gateway.log"),
            upstream_faults: instance_dir.join("upstream-faults.json"),
            stderr_log: instance_dir.join("gateway-stderr.log"),
            supervision_log: logs_dir.join("desktop.log"),
            instance_dir,
        }
    }

    pub fn ensure_dirs(&self) -> Result<(), String> {
        for directory in [&self.instance_dir, &self.profile_dir] {
            std::fs::create_dir_all(directory)
                .map_err(|error| format!("创建实例目录失败（{}）：{error}", directory.display()))?;
        }
        if let Some(parent) = self.supervision_log.parent() {
            std::fs::create_dir_all(parent).map_err(|error| {
                format!("创建监督日志目录失败（{}）：{error}", parent.display())
            })?;
        }
        Ok(())
    }

    pub fn base_url(&self, port: u16) -> String {
        format!("http://127.0.0.1:{port}")
    }
}

/// A 32-byte hex control token.
///
/// `Debug` never prints the value; only [`ControlToken::expose`] returns it for the
/// `X-Admin-Control` header.
#[derive(Clone, PartialEq, Eq)]
pub struct ControlToken(String);

impl ControlToken {
    pub fn generate() -> Result<Self, String> {
        let mut bytes = [0u8; 32];
        getrandom::getrandom(&mut bytes).map_err(|error| format!("生成控制令牌失败：{error}"))?;
        Ok(Self(hex(&bytes)))
    }

    pub fn parse(raw: &str) -> Result<Self, String> {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return Err("控制令牌为空".to_string());
        }
        if trimmed.len() != 64 || !trimmed.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err("控制令牌格式无效：需要 64 位十六进制".to_string());
        }
        Ok(Self(trimmed.to_string()))
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for ControlToken {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("ControlToken(<redacted>)")
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write;
    let mut text = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(text, "{byte:02x}");
    }
    text
}

/// Reads the control token or creates it with a current-user-only ACL.
///
/// A missing file is the legal first start and is created atomically: the temp file
/// is hardened before the rename, so the final path never exists with the inherited
/// profile ACL. An empty or malformed existing file is reported, never replaced
/// silently, because it would invalidate the running instance's token.
pub fn ensure_control_token(path: &Path) -> Result<ControlToken, String> {
    match std::fs::read_to_string(path) {
        Ok(raw) => ControlToken::parse(&raw)
            .map_err(|error| format!("控制令牌不可用（{}）：{error}", path.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let token = ControlToken::generate()?;
            write_control_token(path, &token)
        }
        Err(error) => Err(format!("读取控制令牌失败（{}）：{error}", path.display())),
    }
}

/// Reads the existing control token without creating one.
///
/// The status read runs before the supervisor's first start may have created the
/// token; a missing or malformed file keeps its own reason instead of being
/// replaced, because a replacement would invalidate the running instance.
pub fn load_control_token(path: &Path) -> Result<ControlToken, String> {
    match std::fs::read_to_string(path) {
        Ok(raw) => ControlToken::parse(&raw)
            .map_err(|error| format!("控制令牌不可用（{}）：{error}", path.display())),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            Err(format!("控制令牌不存在（{}）", path.display()))
        }
        Err(error) => Err(format!("读取控制令牌失败（{}）：{error}", path.display())),
    }
}

fn write_control_token(path: &Path, token: &ControlToken) -> Result<ControlToken, String> {
    let parent = path
        .parent()
        .ok_or_else(|| "控制令牌路径没有父目录".to_string())?;
    std::fs::create_dir_all(parent)
        .map_err(|error| format!("创建实例目录失败（{}）：{error}", parent.display()))?;
    let temp = parent.join(format!(".admin-control-token.{}.tmp", std::process::id()));
    std::fs::write(&temp, format!("{}\n", token.expose()))
        .map_err(|error| format!("写入控制令牌失败：{error}"))?;
    if let Err(error) = crate::config::harden_private_file(&temp) {
        let _ = std::fs::remove_file(&temp);
        return Err(error);
    }
    if let Err(error) = std::fs::rename(&temp, path) {
        let _ = std::fs::remove_file(&temp);
        // A concurrent first start may have won the rename. Reuse its token
        // instead of failing with a token the gateway never read.
        if let Ok(raw) = std::fs::read_to_string(path) {
            if let Ok(existing) = ControlToken::parse(&raw) {
                return Ok(existing);
            }
        }
        return Err(format!("替换控制令牌失败：{error}"));
    }
    Ok(token.clone())
}

/// The resolved gateway executable plus any interpreter prefix used by tests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayProgram {
    pub program: PathBuf,
    pub prefix_args: Vec<OsString>,
}

impl GatewayProgram {
    pub fn plain(program: impl Into<PathBuf>) -> Self {
        Self {
            program: program.into(),
            prefix_args: Vec::new(),
        }
    }

    pub fn with_prefix(program: impl Into<PathBuf>, prefix_args: Vec<OsString>) -> Self {
        Self {
            program: program.into(),
            prefix_args,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BinaryError {
    Missing { source: String, path: PathBuf },
    NotExecutable { path: PathBuf, reason: String },
    NotFound { tried: Vec<PathBuf> },
}

impl fmt::Display for BinaryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Missing { source, path } => {
                write!(formatter, "未找到网关二进制：{source}={}", path.display())
            }
            Self::NotExecutable { path, reason } => {
                write!(
                    formatter,
                    "网关二进制不可执行：{}（{reason}）",
                    path.display()
                )
            }
            Self::NotFound { tried } => {
                let paths = tried
                    .iter()
                    .map(|path| path.display().to_string())
                    .collect::<Vec<_>>()
                    .join("；");
                write!(formatter, "未找到网关二进制；已尝试：{paths}")
            }
        }
    }
}

impl std::error::Error for BinaryError {}

/// Resolves `UM_DESKTOP_GATEWAY_BIN`, then `um-gateway.exe` beside the app, then
/// the development `rust/target/release/um-gateway-candidate.exe` in an ancestor.
pub fn resolve_gateway_binary(
    env_override: Option<PathBuf>,
    app_dir: &Path,
) -> Result<GatewayProgram, BinaryError> {
    if let Some(path) = env_override {
        return match executable(&path) {
            Ok(()) => Ok(GatewayProgram::plain(path)),
            Err(ExecutableProblem::Missing) => Err(BinaryError::Missing {
                source: GATEWAY_BIN_ENV.to_string(),
                path,
            }),
            Err(ExecutableProblem::NotExecutable(reason)) => {
                Err(BinaryError::NotExecutable { path, reason })
            }
        };
    }

    let app_binary = app_dir.join("um-gateway.exe");
    let mut tried = vec![app_binary.clone()];
    match executable(&app_binary) {
        Ok(()) => return Ok(GatewayProgram::plain(app_binary)),
        Err(ExecutableProblem::NotExecutable(reason)) => {
            return Err(BinaryError::NotExecutable {
                path: app_binary,
                reason,
            })
        }
        Err(ExecutableProblem::Missing) => {}
    }

    for ancestor in app_dir.ancestors() {
        let dev_binary = ancestor
            .join("rust")
            .join("target")
            .join("release")
            .join("um-gateway-candidate.exe");
        if tried.contains(&dev_binary) {
            continue;
        }
        tried.push(dev_binary.clone());
        match executable(&dev_binary) {
            Ok(()) => return Ok(GatewayProgram::plain(dev_binary)),
            Err(ExecutableProblem::NotExecutable(reason)) => {
                return Err(BinaryError::NotExecutable {
                    path: dev_binary,
                    reason,
                })
            }
            Err(ExecutableProblem::Missing) => {}
        }
    }

    Err(BinaryError::NotFound { tried })
}

enum ExecutableProblem {
    Missing,
    NotExecutable(String),
}

fn executable(path: &Path) -> Result<(), ExecutableProblem> {
    let metadata = std::fs::metadata(path).map_err(|error| {
        if error.kind() == std::io::ErrorKind::NotFound {
            ExecutableProblem::Missing
        } else {
            ExecutableProblem::NotExecutable(error.to_string())
        }
    })?;
    if !metadata.is_file() {
        return Err(ExecutableProblem::NotExecutable("不是普通文件".to_string()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            return Err(ExecutableProblem::NotExecutable(
                "缺少可执行权限".to_string(),
            ));
        }
    }
    Ok(())
}

/// The existing gateway CLI. The gateway has no `--login-history-file` flag, so the
/// login history path travels through `CHAT2API_LOGIN_HISTORY_FILE`.
pub fn gateway_args(port: u16, paths: &InstancePaths) -> Vec<OsString> {
    let mut args = Vec::new();
    for value in [
        "--mode",
        "production",
        "--desktop-instance",
        "--port",
        &port.to_string(),
        "--role",
        "local",
        "--upstream-base-url",
        UPSTREAM_BASE_URL,
        "--control-token-file",
    ] {
        args.push(OsString::from(value));
    }
    args.push(paths.control_token.clone().into_os_string());
    for (flag, path) in [
        ("--usage-log", &paths.usage_log),
        ("--service-log", &paths.service_log),
        ("--token-file", &paths.token),
        ("--profile-dir", &paths.profile_dir),
        ("--auth-state-file", &paths.auth_state),
    ] {
        args.push(OsString::from(flag));
        args.push(path.clone().into_os_string());
    }
    args
}

pub fn gateway_env(
    paths: &InstancePaths,
    credentials: Option<&crate::account::Credentials>,
) -> Vec<(OsString, OsString)> {
    let mut envs = vec![
        (
            OsString::from(LOGIN_HISTORY_ENV),
            paths.login_history.clone().into_os_string(),
        ),
        (
            OsString::from("CHAT2API_UPSTREAM_FAULT_LOG"),
            paths.upstream_faults.clone().into_os_string(),
        ),
    ];
    if let Some(credentials) = credentials {
        envs.push((
            OsString::from(crate::account::ACCOUNT_ENV),
            OsString::from(&credentials.account),
        ));
        envs.push((
            OsString::from(crate::account::PASSWORD_ENV),
            OsString::from(&credentials.password),
        ));
    }
    envs
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DrainAction {
    Wait,
    Commit,
    Cancel,
}

/// The HTTP edge of the gateway's health and authenticated lifecycle routes.
pub trait GatewayHttp: Send + Sync {
    fn health(&self, base_url: &str) -> Result<(), String>;
    fn shutdown(&self, base_url: &str, token: &ControlToken) -> Result<(), String>;
    fn port_switch_drain(
        &self,
        base_url: &str,
        token: &ControlToken,
        lease: &str,
        action: DrainAction,
    ) -> Result<usize, String>;
    fn listener_down(&self, base_url: &str) -> bool;
}

pub struct HttpGateway {
    agent: ureq::Agent,
}

impl HttpGateway {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_secs(2))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpGateway {
    fn default() -> Self {
        Self::new()
    }
}

impl GatewayHttp for HttpGateway {
    fn health(&self, base_url: &str) -> Result<(), String> {
        let url = format!("{}/health", base_url.trim_end_matches('/'));
        let response = self
            .agent
            .get(&url)
            .call()
            .map_err(|error| format!("{url}: {error}"))?;
        let body: serde_json::Value = response
            .into_json()
            .map_err(|error| format!("{url}: {error}"))?;
        if body.get("chat2api_version").is_none() {
            return Err(format!("{url}: 不是本实例网关"));
        }
        let status = body
            .get("status")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("unknown");
        if status == "ok" {
            Ok(())
        } else {
            Err(format!("{url}: status={status}"))
        }
    }

    fn shutdown(&self, base_url: &str, token: &ControlToken) -> Result<(), String> {
        let url = format!("{}/admin/shutdown", base_url.trim_end_matches('/'));
        self.agent
            .post(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map(|_| ())
            .map_err(|error| format!("{url}: {error}"))
    }

    fn port_switch_drain(
        &self,
        base_url: &str,
        token: &ControlToken,
        lease: &str,
        action: DrainAction,
    ) -> Result<usize, String> {
        let url = format!(
            "{}/admin/desktop/port-switch",
            base_url.trim_end_matches('/')
        );
        let (verb, expected_status) = match action {
            DrainAction::Wait => ("wait", 200),
            DrainAction::Commit => ("commit", 202),
            DrainAction::Cancel => ("cancel", 200),
        };
        let response = self
            .agent
            .post(&url)
            .set("X-Admin-Control", token.expose())
            .set("X-Desktop-Port-Switch", lease)
            .set("X-Desktop-Port-Switch-Action", verb)
            .call()
            .map_err(|error| format!("{url}: {error}"))?;
        if response.status() != expected_status {
            return Err(format!("{url}: 意外的 HTTP {}", response.status()));
        }
        let body: serde_json::Value = response
            .into_json()
            .map_err(|error| format!("{url}: {error}"))?;
        match action {
            DrainAction::Wait => body
                .get("in_flight")
                .and_then(serde_json::Value::as_u64)
                .and_then(|count| usize::try_from(count).ok())
                .ok_or_else(|| "网关未返回有效的在途请求数".to_string()),
            DrainAction::Commit if body["status"] == "stopping" => Ok(0),
            DrainAction::Cancel if body["status"] == "accepting" => Ok(0),
            _ => Err("网关未返回预期的服务状态".to_string()),
        }
    }

    fn listener_down(&self, base_url: &str) -> bool {
        let Some(port) = base_url
            .strip_prefix("http://")
            .and_then(|addr| addr.parse::<std::net::SocketAddr>().ok())
            .map(|addr| addr.port())
        else {
            return false;
        };
        // A successful loopback bind proves the old listener is gone. A
        // connect probe can time out on a firewall-dropped SYN even after the
        // process closed the port, which made the desktop switch wait ten
        // seconds and then report a stop failure.
        port_available(port).is_ok()
    }
}

pub trait Sleeper: Send + Sync {
    fn sleep(&self, duration: Duration);
}

pub struct ThreadSleeper;

impl Sleeper for ThreadSleeper {
    fn sleep(&self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

/// One observed process exit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ProcessExit {
    pub code: Option<i32>,
}

impl ProcessExit {
    pub fn describe(&self) -> String {
        match self.code {
            Some(code) => format!("退出码 {code}"),
            None => "退出码未知".to_string(),
        }
    }
}

pub trait ChildProcess: Send {
    fn id(&self) -> u32;
    fn try_wait(&mut self) -> Result<Option<ProcessExit>, String>;
    fn kill(&mut self) -> Result<(), String>;
}

/// The process edge; tests inject a fake so no real gateway is needed.
pub trait ProcessLauncher: Send {
    fn spawn(
        &self,
        program: &GatewayProgram,
        args: &[OsString],
        envs: &[(OsString, OsString)],
        stderr_log: &Path,
    ) -> Result<Box<dyn ChildProcess>, String>;
}

pub struct SystemLauncher;

impl ProcessLauncher for SystemLauncher {
    fn spawn(
        &self,
        program: &GatewayProgram,
        args: &[OsString],
        envs: &[(OsString, OsString)],
        stderr_log: &Path,
    ) -> Result<Box<dyn ChildProcess>, String> {
        use std::process::{Command, Stdio};

        if let Some(parent) = stderr_log.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("创建网关 stderr 目录失败：{error}"))?;
        }
        let stderr = std::fs::File::create(stderr_log).map_err(|error| {
            format!(
                "创建网关 stderr 日志失败（{}）：{error}",
                stderr_log.display()
            )
        })?;
        let mut command = Command::new(&program.program);
        command.args(&program.prefix_args).args(args);
        for (key, value) in envs {
            command.env(key, value);
        }
        if let Some(instance_dir) = stderr_log.parent() {
            command.current_dir(instance_dir);
        }
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::from(stderr));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let child = command.spawn().map_err(|error| {
            format!("启动网关进程失败（{}）：{error}", program.program.display())
        })?;
        Ok(Box::new(SystemChild { child }))
    }
}

struct SystemChild {
    child: std::process::Child,
}

impl ChildProcess for SystemChild {
    fn id(&self) -> u32 {
        self.child.id()
    }

    fn try_wait(&mut self) -> Result<Option<ProcessExit>, String> {
        self.child
            .try_wait()
            .map(|exit| {
                exit.map(|status| ProcessExit {
                    code: status.code(),
                })
            })
            .map_err(|error| error.to_string())
    }

    fn kill(&mut self) -> Result<(), String> {
        self.child.kill().map_err(|error| error.to_string())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Ownership {
    Managed,
    Attached,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StopReason {
    UserRequested,
    PortSwitch,
    CredentialsChanged,
    Shutdown,
}

impl StopReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::UserRequested => "user",
            Self::PortSwitch => "port_switch",
            Self::CredentialsChanged => "credentials",
            Self::Shutdown => "shutdown",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GatewayState {
    Starting,
    Stopped,
    Running { ownership: Ownership },
    PortInUse { port: u16, reason: String },
    StartFailed { reason: String },
    HealthTimeout { reason: String },
    StopFailed { reason: String },
    SetupFailed { reason: String },
}

impl GatewayState {
    pub fn value(&self) -> &'static str {
        match self {
            Self::Starting => "正在启动",
            Self::Stopped => "已停止",
            Self::Running { .. } => "运行中",
            Self::PortInUse { .. } => "端口被占用",
            Self::StartFailed { .. } => "启动失败",
            Self::HealthTimeout { .. } => "启动未就绪",
            Self::StopFailed { .. } => "停止失败",
            Self::SetupFailed { .. } => "监督不可用",
        }
    }

    pub fn detail(&self) -> String {
        match self {
            Self::Starting => "正在等待本实例网关的 /health".to_string(),
            Self::Stopped => "本实例网关未运行".to_string(),
            Self::Running {
                ownership: Ownership::Managed,
            } => "App 拉起并监督".to_string(),
            Self::Running {
                ownership: Ownership::Attached,
            } => "接入已有实例，未重复启动".to_string(),
            Self::PortInUse { reason, .. }
            | Self::StartFailed { reason }
            | Self::HealthTimeout { reason }
            | Self::StopFailed { reason }
            | Self::SetupFailed { reason } => reason.clone(),
        }
    }

    pub fn tone(&self) -> crate::overview::Tone {
        match self {
            Self::Running { .. } => crate::overview::Tone::Good,
            Self::Starting | Self::Stopped => crate::overview::Tone::Warn,
            Self::PortInUse { .. }
            | Self::StartFailed { .. }
            | Self::HealthTimeout { .. }
            | Self::StopFailed { .. }
            | Self::SetupFailed { .. } => crate::overview::Tone::Bad,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayStatus {
    pub state: GatewayState,
    pub port: u16,
    pub base_url: String,
    pub supervision_log: PathBuf,
}

impl GatewayStatus {
    pub fn card(&self) -> crate::overview::Card {
        let detail = if matches!(self.state, GatewayState::Stopped) {
            self.state.detail()
        } else {
            format!(
                "{} · 日志 {}",
                self.state.detail(),
                self.supervision_log.display()
            )
        };
        crate::overview::Card {
            label: "网关监督",
            value: self.state.value().to_string(),
            detail,
            tone: self.state.tone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PortSwitch {
    Unchanged {
        port: u16,
    },
    Switched {
        from: u16,
        to: u16,
    },
    Rejected {
        from: u16,
        to: u16,
        reason: String,
    },
    Restored {
        from: u16,
        to: u16,
        reason: String,
    },
    RestoreFailed {
        from: u16,
        to: u16,
        reason: String,
        restore_error: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PortSwitchProgress {
    Waiting { in_flight: usize },
    Switching,
    Restoring,
}

enum HealthWait {
    Healthy,
    Exited(ProcessExit),
    Timeout,
    WaitFailed(String),
}

enum ExitWait {
    Exited(ProcessExit),
    TimedOut,
    Failed(String),
}

const KILL_TIMEOUT: Duration = Duration::from_secs(2);

/// Owns the single app-instance gateway child.
pub struct GatewaySupervisor {
    port: u16,
    paths: InstancePaths,
    program: Result<GatewayProgram, BinaryError>,
    http: std::sync::Arc<dyn GatewayHttp>,
    launcher: Box<dyn ProcessLauncher>,
    sleeper: Box<dyn Sleeper>,
    token: Option<ControlToken>,
    credentials: Option<crate::account::Credentials>,
    child: Option<Box<dyn ChildProcess>>,
    state: GatewayState,
}

impl GatewaySupervisor {
    pub fn new(
        port: u16,
        paths: InstancePaths,
        program: Result<GatewayProgram, BinaryError>,
        http: std::sync::Arc<dyn GatewayHttp>,
        launcher: Box<dyn ProcessLauncher>,
        sleeper: Box<dyn Sleeper>,
    ) -> Self {
        Self {
            port,
            paths,
            program,
            http,
            launcher,
            sleeper,
            token: None,
            credentials: None,
            child: None,
            state: GatewayState::Stopped,
        }
    }

    /// The credentials handed to the next spawned child.
    ///
    /// Changing them does not touch a running child; call [`Self::restart`] to
    /// apply them to this instance.
    pub fn set_credentials(&mut self, credentials: Option<crate::account::Credentials>) {
        self.credentials = credentials;
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn paths(&self) -> &InstancePaths {
        &self.paths
    }

    pub fn child_count(&self) -> usize {
        usize::from(self.child.is_some())
    }

    pub fn status(&self) -> GatewayStatus {
        GatewayStatus {
            state: self.state.clone(),
            port: self.port,
            base_url: self.base_url(),
            supervision_log: self.paths.supervision_log.clone(),
        }
    }

    fn base_url(&self) -> String {
        self.paths.base_url(self.port)
    }

    /// Attaches to a healthy instance or starts exactly one managed child.
    pub fn start_or_attach(&mut self) -> GatewayStatus {
        if desktop_port_reserved(self.port) {
            self.state = GatewayState::SetupFailed {
                reason: format!("127.0.0.1:{} 为保留端口", self.port),
            };
            return self.status();
        }
        if let Err(error) = self.paths.ensure_dirs() {
            self.state = GatewayState::SetupFailed {
                reason: error.clone(),
            };
            self.append_log("setup_failed", serde_json::json!({ "reason": error }));
            return self.status();
        }
        if self.child.is_some() {
            let exited = match self.child.as_mut() {
                Some(child) => matches!(child.try_wait(), Ok(Some(_))),
                None => false,
            };
            if !exited {
                return self.probe_managed();
            }
            self.child = None;
        }
        if self.http.health(&self.base_url()).is_ok() {
            self.state = GatewayState::Running {
                ownership: Ownership::Attached,
            };
            self.append_log("attach", serde_json::json!({ "port": self.port }));
            return self.status();
        }
        if let Err(reason) = port_available(self.port) {
            self.state = GatewayState::PortInUse {
                port: self.port,
                reason: reason.clone(),
            };
            self.append_log(
                "port_in_use",
                serde_json::json!({ "port": self.port, "reason": reason }),
            );
            return self.status();
        }
        let program = match &self.program {
            Ok(program) => program.clone(),
            Err(error) => {
                let reason = error.to_string();
                self.state = GatewayState::SetupFailed {
                    reason: reason.clone(),
                };
                self.append_log("setup_failed", serde_json::json!({ "reason": reason }));
                return self.status();
            }
        };
        let token = match ensure_control_token(&self.paths.control_token) {
            Ok(token) => token,
            Err(error) => {
                self.state = GatewayState::SetupFailed {
                    reason: error.clone(),
                };
                self.append_log("setup_failed", serde_json::json!({ "reason": error }));
                return self.status();
            }
        };
        self.token = Some(token);
        let args = gateway_args(self.port, &self.paths);
        let envs = gateway_env(&self.paths, self.credentials.as_ref());
        let child = match self
            .launcher
            .spawn(&program, &args, &envs, &self.paths.stderr_log)
        {
            Ok(child) => child,
            Err(error) => {
                let reason = format!("启动网关进程失败：{error}");
                self.state = GatewayState::StartFailed {
                    reason: reason.clone(),
                };
                self.append_log(
                    "start_failed",
                    serde_json::json!({ "port": self.port, "reason": reason }),
                );
                return self.status();
            }
        };
        self.child = Some(child);
        self.state = GatewayState::Starting;
        self.append_log(
            "start",
            serde_json::json!({ "port": self.port, "binary": program.program }),
        );
        match self.wait_for_health(HEALTH_TIMEOUT) {
            HealthWait::Healthy => {
                self.state = GatewayState::Running {
                    ownership: Ownership::Managed,
                };
                self.append_log("healthy", serde_json::json!({ "port": self.port }));
            }
            HealthWait::Exited(exit) => {
                self.child = None;
                let reason = format!(
                    "网关进程退出（{}）；stderr：{}",
                    exit.describe(),
                    self.stderr_tail()
                );
                self.state = GatewayState::StartFailed {
                    reason: reason.clone(),
                };
                self.append_log(
                    "start_failed",
                    serde_json::json!({ "port": self.port, "reason": reason }),
                );
            }
            HealthWait::Timeout => {
                let reason = format!("20 秒内 127.0.0.1:{} 的 /health 未返回 200", self.port);
                self.state = GatewayState::HealthTimeout {
                    reason: reason.clone(),
                };
                self.append_log(
                    "health_timeout",
                    serde_json::json!({ "port": self.port, "reason": reason }),
                );
            }
            HealthWait::WaitFailed(error) => {
                let reason = format!("等待网关进程失败：{error}");
                self.state = GatewayState::StartFailed {
                    reason: reason.clone(),
                };
                self.append_log(
                    "start_failed",
                    serde_json::json!({ "port": self.port, "reason": reason }),
                );
            }
        }
        self.status()
    }

    fn probe_managed(&mut self) -> GatewayStatus {
        if self.http.health(&self.base_url()).is_ok() {
            self.state = GatewayState::Running {
                ownership: Ownership::Managed,
            };
        } else {
            self.state = GatewayState::HealthTimeout {
                reason: format!("127.0.0.1:{} 的 /health 未返回 200", self.port),
            };
        }
        self.status()
    }

    fn wait_for_health(&mut self, timeout: Duration) -> HealthWait {
        let mut waited = Duration::ZERO;
        loop {
            if self.http.health(&self.base_url()).is_ok() {
                return HealthWait::Healthy;
            }
            if let Some(child) = self.child.as_mut() {
                match child.try_wait() {
                    Ok(Some(exit)) => return HealthWait::Exited(exit),
                    Ok(None) => {}
                    Err(error) => return HealthWait::WaitFailed(error),
                }
            }
            if waited >= timeout {
                return HealthWait::Timeout;
            }
            self.sleeper.sleep(HEALTH_POLL_INTERVAL);
            waited += HEALTH_POLL_INTERVAL;
        }
    }

    /// Stops the instance, waiting for the child before killing it.
    pub fn stop(&mut self, reason: StopReason) -> GatewayStatus {
        let managed = self.child.is_some();
        let attached = matches!(
            self.state,
            GatewayState::Running {
                ownership: Ownership::Attached
            }
        );
        if !managed && !attached {
            return self.status();
        }
        let token = self
            .token
            .clone()
            .or_else(|| ensure_control_token(&self.paths.control_token).ok());
        let Some(token) = token else {
            let reason = "控制令牌不可用，无法停止网关".to_string();
            self.state = GatewayState::StopFailed {
                reason: reason.clone(),
            };
            self.append_log(
                "stop_failed",
                serde_json::json!({ "port": self.port, "reason": reason }),
            );
            return self.status();
        };
        let shutdown_error = self.http.shutdown(&self.base_url(), &token).err();
        self.wait_until_stopped(reason, shutdown_error, true)
    }

    fn wait_until_stopped(
        &mut self,
        reason: StopReason,
        shutdown_error: Option<String>,
        allow_force: bool,
    ) -> GatewayStatus {
        let managed = self.child.is_some();
        let wait = if managed {
            self.wait_for_exit(STOP_TIMEOUT)
        } else if self.wait_for_health_down(STOP_TIMEOUT, reason == StopReason::PortSwitch) {
            ExitWait::Exited(ProcessExit { code: None })
        } else {
            ExitWait::TimedOut
        };
        match wait {
            ExitWait::Exited(exit) => {
                self.child = None;
                self.state = GatewayState::Stopped;
                self.append_log(
                    "stop",
                    serde_json::json!({
                        "port": self.port,
                        "reason": reason.as_str(),
                        "forced": false,
                        "exit": exit.describe(),
                    }),
                );
            }
            ExitWait::TimedOut if managed && allow_force => match self.force_kill() {
                Ok(()) => {
                    self.child = None;
                    self.state = GatewayState::Stopped;
                    self.append_log(
                        "stop",
                        serde_json::json!({
                            "port": self.port,
                            "reason": reason.as_str(),
                            "forced": true,
                        }),
                    );
                }
                Err(error) => {
                    let reason = format!("停止网关失败：{error}");
                    self.state = GatewayState::StopFailed {
                        reason: reason.clone(),
                    };
                    self.append_log(
                        "stop_failed",
                        serde_json::json!({ "port": self.port, "reason": reason }),
                    );
                }
            },
            ExitWait::TimedOut => {
                let shutdown = shutdown_error
                    .map(|error| format!("；shutdown 请求失败：{error}"))
                    .unwrap_or_default();
                let reason = format!("10 秒内未停止 127.0.0.1:{}{shutdown}", self.port);
                self.state = GatewayState::StopFailed {
                    reason: reason.clone(),
                };
                self.append_log(
                    "stop_failed",
                    serde_json::json!({ "port": self.port, "reason": reason }),
                );
            }
            ExitWait::Failed(error) => {
                let reason = format!("等待网关进程退出失败：{error}");
                self.state = GatewayState::StopFailed {
                    reason: reason.clone(),
                };
                self.append_log(
                    "stop_failed",
                    serde_json::json!({ "port": self.port, "reason": reason }),
                );
            }
        }
        self.status()
    }

    /// Replaces the running instance so the current credentials environment
    /// applies to the new child. A failed stop leaves the old child untouched.
    pub fn restart(&mut self, reason: StopReason) -> GatewayStatus {
        let has_instance = self.child.is_some()
            || matches!(
                self.state,
                GatewayState::Running { .. }
                    | GatewayState::Starting
                    | GatewayState::HealthTimeout { .. }
            );
        if has_instance {
            let stopped = self.stop(reason);
            if !matches!(stopped.state, GatewayState::Stopped) {
                return stopped;
            }
        }
        let started = self.start_or_attach();
        self.append_log(
            "restart",
            serde_json::json!({ "port": self.port, "reason": reason.as_str() }),
        );
        started
    }

    fn wait_for_exit(&mut self, timeout: Duration) -> ExitWait {
        let mut waited = Duration::ZERO;
        loop {
            let Some(child) = self.child.as_mut() else {
                return ExitWait::Exited(ProcessExit { code: None });
            };
            match child.try_wait() {
                Ok(Some(exit)) => return ExitWait::Exited(exit),
                Ok(None) => {}
                Err(error) => return ExitWait::Failed(error),
            }
            if waited >= timeout {
                return ExitWait::TimedOut;
            }
            self.sleeper.sleep(HEALTH_POLL_INTERVAL);
            waited += HEALTH_POLL_INTERVAL;
        }
    }

    fn wait_for_health_down(&mut self, timeout: Duration, verify_listener: bool) -> bool {
        let mut waited = Duration::ZERO;
        loop {
            let base = self.base_url();
            let down = if verify_listener {
                self.http.listener_down(&base)
            } else {
                self.http.health(&base).is_err()
            };
            if down {
                return true;
            }
            if waited >= timeout {
                return false;
            }
            self.sleeper.sleep(HEALTH_POLL_INTERVAL);
            waited += HEALTH_POLL_INTERVAL;
        }
    }

    fn force_kill(&mut self) -> Result<(), String> {
        let kill_error = match self.child.as_mut() {
            Some(child) => child.kill().err(),
            None => None,
        };
        match self.wait_for_exit(KILL_TIMEOUT) {
            ExitWait::Exited(_) => Ok(()),
            ExitWait::TimedOut => Err(match kill_error {
                Some(error) => format!("kill 失败：{error}"),
                None => "kill 后进程仍未退出".to_string(),
            }),
            ExitWait::Failed(error) => Err(error),
        }
    }

    pub fn switch_port(
        &mut self,
        new_port: u16,
        save: &mut dyn FnMut(u16) -> Result<(), String>,
    ) -> PortSwitch {
        self.switch_port_with_progress(new_port, save, &mut |_| {}, &|| false)
    }

    /// Stops admission, drains the old listener, then changes the saved port.
    /// The caller can preempt the wait with a separate explicit quit action.
    pub fn switch_port_with_progress(
        &mut self,
        new_port: u16,
        save: &mut dyn FnMut(u16) -> Result<(), String>,
        progress: &mut dyn FnMut(PortSwitchProgress),
        cancelled: &dyn Fn() -> bool,
    ) -> PortSwitch {
        let from = self.port;
        let reject = |reason| PortSwitch::Rejected {
            from,
            to: new_port,
            reason,
        };
        if new_port == from {
            return PortSwitch::Unchanged { port: from };
        }
        if desktop_port_reserved(new_port) {
            return reject(format!("127.0.0.1:{new_port} 为保留端口"));
        }
        if let Err(reason) = port_available(new_port) {
            return reject(reason);
        }
        if cancelled() {
            return reject("已取消端口切换".to_string());
        }
        let has_listener = self.child.is_some()
            || matches!(
                self.state,
                GatewayState::Running { .. } | GatewayState::StopFailed { .. }
            );
        if has_listener {
            if let Err(reason) = self.drain_before_switch(new_port, progress, cancelled) {
                // A committed drain closes the old listener before the stop
                // wait finishes. If it is already gone, bring the old port
                // back before reporting the failure; an explicit quit keeps
                // the stopped state instead.
                if !cancelled() && self.http.listener_down(&self.base_url()) {
                    return self.restore_port(from, new_port, reason, Ok(()), progress);
                }
                return reject(reason);
            }
        } else {
            progress(PortSwitchProgress::Switching);
        }
        if cancelled() {
            return reject("已取消端口切换".to_string());
        }
        if let Err(error) = save(new_port) {
            let reason = format!("保存新端口失败：{error}");
            return if has_listener {
                self.restore_port(from, new_port, reason, Ok(()), progress)
            } else {
                reject(reason)
            };
        }
        self.port = new_port;
        let started = self.start_or_attach();
        if matches!(&started.state, GatewayState::Running { .. }) {
            self.append_log(
                "switch",
                serde_json::json!({ "from": from, "to": new_port }),
            );
            return PortSwitch::Switched { from, to: new_port };
        }
        let reason = started.state.detail();
        if self.child.is_some() {
            let stopped = self.stop(StopReason::Shutdown);
            if !matches!(stopped.state, GatewayState::Stopped) {
                // Retain the current port and ownership until the managed
                // child is confirmed stopped. Restoring now can fork a
                // second child against the same persistent files.
                return PortSwitch::RestoreFailed {
                    from,
                    to: new_port,
                    reason,
                    restore_error: stopped.state.detail(),
                };
            }
        }
        let config_restore = save(from);
        self.restore_port(from, new_port, reason, config_restore, progress)
    }

    fn drain_before_switch(
        &mut self,
        new_port: u16,
        progress: &mut dyn FnMut(PortSwitchProgress),
        cancelled: &dyn Fn() -> bool,
    ) -> Result<(), String> {
        let base = self.base_url();
        if self.child.is_none() && self.http.listener_down(&base) {
            self.state = GatewayState::Stopped;
            progress(PortSwitchProgress::Switching);
            return Ok(());
        }
        if let Some(child) = self.child.as_mut() {
            if child.try_wait()?.is_some() {
                self.child = None;
                self.state = GatewayState::Stopped;
                progress(PortSwitchProgress::Switching);
                return Ok(());
            }
        }
        let token = self
            .token
            .clone()
            .map(Ok)
            .unwrap_or_else(|| load_control_token(&self.paths.control_token))?;
        let mut entropy = [0u8; 16];
        getrandom::getrandom(&mut entropy)
            .map_err(|error| format!("创建端口切换租约失败：{error}"))?;
        let lease = entropy
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let started = Instant::now();
        let mut previous = None;
        let result = loop {
            if cancelled() {
                break Err("已取消端口切换".to_string());
            }
            if started.elapsed() >= DRAIN_TIMEOUT {
                break Err("等候当前模型请求结束超时，旧端口继续服务".to_string());
            }
            match self
                .http
                .port_switch_drain(&base, &token, &lease, DrainAction::Wait)
            {
                Ok(0) => break port_available(new_port),
                Ok(in_flight) => {
                    if previous != Some(in_flight) {
                        previous = Some(in_flight);
                        progress(PortSwitchProgress::Waiting { in_flight });
                    }
                    self.sleeper.sleep(DRAIN_POLL_INTERVAL);
                }
                Err(error) => break Err(format!("无法确认当前请求已经结束：{error}")),
            }
        };
        if result.is_err() || cancelled() {
            let _ = self
                .http
                .port_switch_drain(&base, &token, &lease, DrainAction::Cancel);
            return Err(result.err().unwrap_or_else(|| "已取消端口切换".to_string()));
        }
        progress(PortSwitchProgress::Switching);
        let commit = self
            .http
            .port_switch_drain(&base, &token, &lease, DrainAction::Commit);
        if commit.is_err() {
            // The server might have committed before the reply was lost. A
            // committed lease cannot be cancelled. Check actual termination
            // and never force-kill after an ambiguous response.
            let _ = self
                .http
                .port_switch_drain(&base, &token, &lease, DrainAction::Cancel);
        }
        let committed = commit.is_ok();
        let stopped = self.wait_until_stopped(StopReason::PortSwitch, commit.err(), committed);
        if matches!(stopped.state, GatewayState::Stopped) {
            Ok(())
        } else {
            Err(format!("停止旧实例失败：{}", stopped.state.detail()))
        }
    }

    fn restore_port(
        &mut self,
        from: u16,
        to: u16,
        reason: String,
        config_restore: Result<(), String>,
        progress: &mut dyn FnMut(PortSwitchProgress),
    ) -> PortSwitch {
        progress(PortSwitchProgress::Restoring);
        self.port = from;
        if let Some(mut child) = self.child.take() {
            // A committed stop may have closed the listener while the old child
            // is still finishing its auth watch. Reap or kill it before starting
            // the replacement; otherwise start_or_attach would probe the stale
            // child and report a HealthTimeout instead of a restored port.
            match child.try_wait() {
                Ok(Some(_)) => {}
                Ok(None) => {
                    let _ = child.kill();
                }
                Err(_) => {}
            }
        }
        let restored = self.start_or_attach();
        if matches!(&restored.state, GatewayState::Running { .. }) {
            if let Err(error) = config_restore {
                return PortSwitch::RestoreFailed {
                    from,
                    to,
                    reason,
                    restore_error: format!("恢复原端口配置失败：{error}"),
                };
            }
            self.append_log(
                "switch_restored",
                serde_json::json!({ "from": from, "to": to, "reason": reason }),
            );
            return PortSwitch::Restored { from, to, reason };
        }
        let mut restore_error = restored.state.detail();
        if let Err(error) = config_restore {
            restore_error.push_str(&format!("；恢复原端口配置失败：{error}"));
        }
        self.append_log(
            "switch_restore_failed",
            serde_json::json!({
                "from": from,
                "to": to,
                "reason": reason,
                "restore_error": restore_error,
            }),
        );
        PortSwitch::RestoreFailed {
            from,
            to,
            reason,
            restore_error,
        }
    }

    fn stderr_tail(&self) -> String {
        let text = crate::tail::read_tail(&self.paths.stderr_log, 8192).unwrap_or_default();
        let text = self.redact(text);
        let lines: Vec<&str> = text
            .lines()
            .filter(|line| !line.trim().is_empty())
            .collect();
        if lines.is_empty() {
            return format!("（stderr 为空，日志 {}）", self.paths.stderr_log.display());
        }
        let start = lines.len().saturating_sub(5);
        lines[start..].join(" | ")
    }

    /// Removes the control token, account and password from any surfaced text.
    fn redact(&self, text: String) -> String {
        let mut text = match &self.token {
            Some(token) => text.replace(token.expose(), "<redacted>"),
            None => text,
        };
        if let Some(credentials) = &self.credentials {
            if !credentials.account.is_empty() {
                text = text.replace(&credentials.account, "<redacted>");
            }
            if !credentials.password.is_empty() {
                text = text.replace(&credentials.password, "<redacted>");
            }
        }
        text
    }

    fn append_log(&self, event: &str, fields: serde_json::Value) {
        use std::io::Write;

        let mut row = serde_json::json!({
            "time": chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false),
            "event": event,
        });
        if let (Some(row), serde_json::Value::Object(fields)) = (row.as_object_mut(), fields) {
            for (key, value) in fields {
                row.insert(key, value);
            }
        }
        let mut text = row.to_string();
        text = self.redact(text);
        text.push('\n');
        if let Some(parent) = self.paths.supervision_log.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let _ = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.paths.supervision_log)
            .and_then(|mut file| file.write_all(text.as_bytes()));
    }
}

fn desktop_port_reserved(port: u16) -> bool {
    matches!(
        port,
        0 | 5173 | 5901 | 8000..=8009 | 8090..=8119 | 8300..=8399 | 18096
    )
}

/// Checks whether a loopback port can be bound right now.
pub fn port_available(port: u16) -> Result<(), String> {
    match std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port)) {
        Ok(listener) => {
            drop(listener);
            Ok(())
        }
        Err(error) if error.kind() == std::io::ErrorKind::AddrInUse => {
            Err(format!("127.0.0.1:{port} 已被占用"))
        }
        Err(error) => Err(format!("无法检查 127.0.0.1:{port}：{error}")),
    }
}

/// Stops the instance before the app exits.
pub fn quit_app(supervisor: &mut GatewaySupervisor, quit: &mut dyn FnMut()) -> GatewayStatus {
    let status = supervisor.stop(StopReason::UserRequested);
    quit();
    status
}
