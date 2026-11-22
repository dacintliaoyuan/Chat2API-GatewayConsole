//! UM account credentials and authentication state (#125).
//!
//! The app owns `%LOCALAPPDATA%\Chat2API\credentials.json` (plain text, current
//! user ACL) and hands the pair to its own gateway through
//! `CHAT2API_UM_ACCOUNT` / `CHAT2API_UM_PASSWORD`. The gateway performs the
//! automatic re-login; this module only stores the credentials and reads the
//! gateway's `/admin/auth/status`. Neither the password nor the token is ever
//! logged.

use std::fmt;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::gateway::ControlToken;
use crate::overview::Tone;

pub const CREDENTIALS_VERSION: i64 = 1;
pub const CREDENTIALS_FILE: &str = "credentials.json";
pub const ACCOUNT_ENV: &str = "CHAT2API_UM_ACCOUNT";
pub const PASSWORD_ENV: &str = "CHAT2API_UM_PASSWORD";

/// One saved UM account pair.
///
/// `Debug` never prints the account or the password; only the account card may
/// show the account, and only the gateway subprocess receives the password.
#[derive(Clone, PartialEq, Eq)]
pub struct Credentials {
    pub account: String,
    pub password: String,
}

impl fmt::Debug for Credentials {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("Credentials(<redacted>)")
    }
}

/// The result of reading the credentials file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadOutcome {
    /// No file yet: a legal first open, not a configured account.
    Missing,
    Loaded(Credentials),
    /// The file exists but is unreadable, corrupt or half-filled. The account is
    /// not configured and the user must re-enter it.
    Unusable(String),
}

pub fn credentials_path(app_data_root: &Path) -> PathBuf {
    app_data_root.join(CREDENTIALS_FILE)
}

/// Parses the versioned plain-text document.
///
/// Empty account or password fields are rejected: a half-filled file must never be
/// mistaken for a configured account.
pub fn parse_credentials(text: &str) -> Result<Credentials, String> {
    let body: Value =
        serde_json::from_str(text).map_err(|error| format!("凭据不是有效 JSON：{error}"))?;
    let object = body
        .as_object()
        .ok_or_else(|| "凭据不是 JSON 对象".to_string())?;
    let version = object
        .get("version")
        .and_then(Value::as_i64)
        .ok_or_else(|| "凭据缺少 version".to_string())?;
    if version != CREDENTIALS_VERSION {
        return Err(format!(
            "凭据版本不受支持：{version}（需要 {CREDENTIALS_VERSION}）"
        ));
    }
    let account = object
        .get("account")
        .and_then(Value::as_str)
        .ok_or_else(|| "凭据 account 不是字符串".to_string())?
        .trim()
        .to_string();
    if account.is_empty() {
        return Err("凭据缺少账号".to_string());
    }
    let password = object
        .get("password")
        .and_then(Value::as_str)
        .ok_or_else(|| "凭据 password 不是字符串".to_string())?
        .to_string();
    if password.is_empty() {
        return Err("凭据缺少密码".to_string());
    }
    Ok(Credentials { account, password })
}

pub fn load_from(path: &Path) -> LoadOutcome {
    match std::fs::read_to_string(path) {
        Ok(text) => match parse_credentials(&text) {
            Ok(credentials) => LoadOutcome::Loaded(credentials),
            Err(error) => LoadOutcome::Unusable(error),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => LoadOutcome::Missing,
        Err(error) => LoadOutcome::Unusable(format!("读取凭据失败：{error}")),
    }
}

/// Writes the credentials atomically and reduces the file ACL to the current user.
///
/// The temp file is hardened before the rename, so the final path never exists
/// with the inherited profile ACL. A failed hardening removes the temp file and
/// reports the error instead of leaving a half-protected credential behind.
pub fn save_to(path: &Path, credentials: &Credentials) -> Result<(), String> {
    let validated = Credentials {
        account: credentials.account.trim().to_string(),
        password: credentials.password.clone(),
    };
    if validated.account.is_empty() {
        return Err("账号不能为空".to_string());
    }
    if validated.password.is_empty() {
        return Err("密码不能为空".to_string());
    }
    let parent = path
        .parent()
        .ok_or_else(|| "凭据路径没有父目录".to_string())?;
    std::fs::create_dir_all(parent).map_err(|error| format!("创建凭据目录失败：{error}"))?;
    let text = serde_json::to_string_pretty(&serde_json::json!({
        "version": CREDENTIALS_VERSION,
        "account": validated.account,
        "password": validated.password,
    }))
    .map_err(|error| format!("序列化凭据失败：{error}"))?;
    let temp = parent.join(format!(".credentials.json.{}.tmp", std::process::id()));
    std::fs::write(&temp, format!("{text}\n"))
        .map_err(|error| format!("写入临时凭据失败：{error}"))?;
    if let Err(error) = crate::config::harden_private_file(&temp) {
        let _ = std::fs::remove_file(&temp);
        return Err(error);
    }
    if let Err(error) = std::fs::rename(&temp, path) {
        let _ = std::fs::remove_file(&temp);
        return Err(format!("替换凭据文件失败：{error}"));
    }
    Ok(())
}

/// Removes the saved credentials. A missing file is a legal cleared state.
pub fn clear(path: &Path) -> Result<bool, String> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(format!("清除凭据失败：{error}")),
    }
}

/// How the password field echoes its text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PasswordInput {
    Masked,
    Visible,
}

/// The independent credential form.
///
/// The saved account is prefilled, the password always starts empty, and the
/// password stays masked until the user explicitly reveals it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountForm {
    account: String,
    password: String,
    password_visible: bool,
}

impl AccountForm {
    pub fn new(saved_account: Option<&str>) -> Self {
        Self {
            account: saved_account.unwrap_or_default().to_string(),
            password: String::new(),
            password_visible: false,
        }
    }

    pub fn account(&self) -> &str {
        &self.account
    }

    pub fn password(&self) -> &str {
        &self.password
    }

    pub fn set_account(&mut self, account: &str) {
        self.account = account.to_string();
    }

    pub fn set_password(&mut self, password: &str) {
        self.password = password.to_string();
    }

    pub fn password_visible(&self) -> bool {
        self.password_visible
    }

    pub fn toggle_password_visible(&mut self) {
        self.password_visible = !self.password_visible;
    }

    pub fn password_input_type(&self) -> PasswordInput {
        if self.password_visible {
            PasswordInput::Visible
        } else {
            PasswordInput::Masked
        }
    }

    pub fn validate(&self) -> Result<Credentials, FormError> {
        let account = self.account.trim().to_string();
        if account.is_empty() {
            return Err(FormError::AccountRequired);
        }
        if self.password.is_empty() {
            return Err(FormError::PasswordRequired);
        }
        Ok(Credentials {
            account,
            password: self.password.clone(),
        })
    }
}

/// The two empty-field errors the form can report, so the shell can mark the
/// matching field invalid and place the hint directly below it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FormError {
    AccountRequired,
    PasswordRequired,
}

impl FormError {
    pub fn message(self) -> &'static str {
        match self {
            Self::AccountRequired => "请输入 UM 账号",
            Self::PasswordRequired => "请输入 UM 密码",
        }
    }
}

/// The gateway's authentication states (`/admin/auth/status`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthState {
    Valid,
    NeedsLogin,
    LoggingIn,
    Failed,
    Cancelled,
    Unknown,
}

impl AuthState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Valid => "valid",
            Self::NeedsLogin => "needs_login",
            Self::LoggingIn => "logging_in",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::Unknown => "unknown",
        }
    }

    pub fn from_gateway(value: &str) -> Self {
        match value {
            "valid" => Self::Valid,
            "needs_login" => Self::NeedsLogin,
            "logging_in" => Self::LoggingIn,
            "failed" => Self::Failed,
            "cancelled" => Self::Cancelled,
            _ => Self::Unknown,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Valid => "认证有效",
            Self::NeedsLogin => "需要登录",
            Self::LoggingIn => "登录中",
            Self::Failed => "登录失败",
            Self::Cancelled => "已取消登录",
            Self::Unknown => "认证暂时无法核验",
        }
    }
}

/// One `/admin/auth/status` answer, reduced to the fields the app displays.
///
/// The gateway's `login.manual_reason` is the concrete manual-takeover reason;
/// `error` may carry the same reason behind `manual_takeover_required:`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthStatus {
    pub state: AuthState,
    pub label: String,
    pub error: Option<String>,
    pub manual_reason: Option<String>,
    pub phase: Option<String>,
    pub detail: Option<String>,
}

impl AuthStatus {
    pub fn parse(body: &Value) -> Result<Self, String> {
        let object = body
            .as_object()
            .ok_or_else(|| "认证状态不是 JSON 对象".to_string())?;
        let state = object
            .get("state")
            .and_then(Value::as_str)
            .filter(|state| !state.is_empty())
            .ok_or_else(|| "认证状态缺少 state".to_string())?;
        let label = object
            .get("label")
            .and_then(Value::as_str)
            .filter(|label| !label.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| AuthState::from_gateway(state).label().to_string());
        let error = text_field(object.get("error"));
        let login = object.get("login").and_then(Value::as_object);
        let phase = login
            .and_then(|login| login.get("phase"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let manual_reason = login
            .and_then(|login| login.get("manual_reason"))
            .and_then(Value::as_str)
            .map(str::to_string)
            .or_else(|| {
                error
                    .as_deref()
                    .and_then(|error| error.strip_prefix("manual_takeover_required:"))
                    .map(str::to_string)
            });
        let detail = login
            .and_then(|login| login.get("detail"))
            .and_then(Value::as_str)
            .map(str::to_string);
        Ok(Self {
            state: AuthState::from_gateway(state),
            label,
            error,
            manual_reason,
            phase,
            detail,
        })
    }

    pub fn manual_reason_label(&self) -> Option<String> {
        self.manual_reason.as_deref().map(manual_reason_label)
    }
}

fn text_field(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .map(str::to_string)
}

/// Explains a gateway manual-takeover reason without echoing credentials.
pub fn manual_reason_label(reason: &str) -> String {
    match reason {
        "duo_prompt_waiting" => "等待本人确认 Duo 推送".to_string(),
        "umpass_prefill_missing" => "UMPASS 登录表单缺少账号或密码，需要人工补填".to_string(),
        "umpass_credentials_untrusted_origin" => {
            "登录页不是可信的 UMPASS 官方页，已暂停自动填写".to_string()
        }
        "umpass_submit_unrecognized" => "未能唯一识别 UMPASS 登录按钮".to_string(),
        "umpass_click_swallowed" => "UMPASS 登录按钮点击后页面未响应".to_string(),
        "umpass_account_mismatch" => {
            "UMPASS 页面账号与已保存账号不一致，请确认使用哪个账号".to_string()
        }
        "openwebui_button_unrecognized" => "未能识别 Open WebUI 登录按钮".to_string(),
        "openwebui_click_swallowed" => "Open WebUI 登录按钮点击后页面未响应".to_string(),
        "timeout" => "登录窗口已超时".to_string(),
        other => format!("需要人工处理：{other}"),
    }
}

/// Whether a save must open the visible Edge for the first login.
///
/// A running automatic re-login (`logging_in`) is never started again; the app only
/// tracks the same round. Every other non-valid state is an explicit first login.
pub fn first_login_needed(status: &AuthStatus) -> bool {
    !matches!(status.state, AuthState::Valid | AuthState::LoggingIn)
}

/// Applies the first-login policy to one already-read status.
///
/// A valid or already-running round is returned unchanged; any other state opens
/// exactly one visible login through the gateway's shared round.
pub fn first_login(
    auth_admin: &dyn AuthAdmin,
    base_url: &str,
    token: &ControlToken,
    status: &AuthStatus,
) -> AuthRead {
    if !first_login_needed(status) {
        return AuthRead::Status(status.clone());
    }
    match auth_admin.start_visible_login(base_url, token) {
        Ok(status) => AuthRead::Status(status),
        Err(error) => AuthRead::Unavailable(error),
    }
}

/// The gateway's control-token auth face.
pub trait AuthAdmin: Send + Sync {
    fn read_status(&self, base_url: &str, token: &ControlToken) -> Result<AuthStatus, String>;
    fn start_visible_login(
        &self,
        base_url: &str,
        token: &ControlToken,
    ) -> Result<AuthStatus, String>;
}

pub struct HttpAuthAdmin {
    agent: ureq::Agent,
}

impl HttpAuthAdmin {
    pub fn new() -> Self {
        Self::with_timeout(std::time::Duration::from_secs(5))
    }

    pub fn with_timeout(timeout: std::time::Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }

    fn read(&self, base_url: &str, token: &ControlToken) -> Result<AuthStatus, String> {
        let url = format!("{}/admin/auth/status", base_url.trim_end_matches('/'));
        let response = self
            .agent
            .get(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map_err(|error| admin_error(&url, error))?;
        parse_response(&url, response)
    }

    fn start(&self, base_url: &str, token: &ControlToken) -> Result<AuthStatus, String> {
        let url = format!("{}/admin/auth/login", base_url.trim_end_matches('/'));
        let response = self
            .agent
            .post(&url)
            .set("X-Admin-Control", token.expose())
            .send_json(serde_json::json!({ "visible": true }))
            .map_err(|error| admin_error(&url, error))?;
        parse_response(&url, response)
    }
}

impl Default for HttpAuthAdmin {
    fn default() -> Self {
        Self::new()
    }
}

impl AuthAdmin for HttpAuthAdmin {
    fn read_status(&self, base_url: &str, token: &ControlToken) -> Result<AuthStatus, String> {
        self.read(base_url, token)
    }

    fn start_visible_login(
        &self,
        base_url: &str,
        token: &ControlToken,
    ) -> Result<AuthStatus, String> {
        self.start(base_url, token)
    }
}

/// Maps a transport/HTTP failure without relaying the response body.
///
/// Only the gateway's short error code is kept, so a response that echoes a
/// credential cannot reach the page.
fn admin_error(url: &str, error: ureq::Error) -> String {
    match error {
        ureq::Error::Status(code, response) => {
            let detail = response
                .into_json::<Value>()
                .ok()
                .and_then(|body| {
                    body.get("error")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .filter(|detail| safe_reason(detail));
            match detail {
                Some(detail) => format!("{url}: 本实例网关返回 HTTP {code}（{detail}）"),
                None => format!("{url}: 本实例网关返回 HTTP {code}"),
            }
        }
        // The transport error repeats the URL and the OS text; keep one short
        // Chinese reason so the card stays readable.
        ureq::Error::Transport(error) => match error.kind() {
            ureq::ErrorKind::ConnectionFailed => {
                format!("{url}: 无法连接本实例网关（连接被拒绝）")
            }
            _ => format!("{url}: 无法连接本实例网关"),
        },
    }
}

/// A gateway reason code is short and contains no credential text.
fn safe_reason(reason: &str) -> bool {
    !reason.is_empty()
        && reason.len() <= 120
        && reason
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-.:".contains(&byte))
}

fn parse_response(url: &str, response: ureq::Response) -> Result<AuthStatus, String> {
    let body: Value = response
        .into_json()
        .map_err(|error| format!("{url}: 认证状态不是有效 JSON：{error}"))?;
    AuthStatus::parse(&body).map_err(|error| format!("{url}: {error}"))
}

/// One status read: either the gateway's answer or why it is unavailable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthRead {
    Status(AuthStatus),
    Unavailable(String),
}

/// What the account card and the overview row show.
///
/// The saved account is the only place the account appears; the password never
/// enters this view.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountView {
    pub saved_account: String,
    pub saved_detail: String,
    pub state_value: String,
    pub state_detail: String,
    pub state_tone: Tone,
    pub can_clear: bool,
    pub can_continue_login: bool,
    /// True when the gateway is logged in or already in a login round, so stale
    /// failure text from an earlier round must be cleared from both the overview
    /// `login-status` and the settings `credential-status`.
    pub clears_old_failure: bool,
    pub overview_label: String,
    pub overview_detail: String,
    pub overview_tone: Tone,
    /// True while the account row is still reading or in a login round; the
    /// shell passes this to the status dot instead of the page matching labels.
    pub pulse: bool,
}

/// The window fields the account card and the overview row own.
///
/// The Slint shell implements this for its `MainWindow`; tests implement it with a
/// recording double so [`apply_account`] is asserted field by field without a live
/// window.
pub trait AccountSurface {
    fn set_saved_account(&self, value: &str);
    fn set_saved_detail(&self, value: &str);
    fn set_can_clear(&self, value: bool);
    fn set_can_continue_login(&self, value: bool);
    fn set_overview_label(&self, value: &str);
    fn set_overview_detail(&self, value: &str);
    fn set_overview_tone(&self, tone: Tone);
    fn set_overview_pulse(&self, value: bool);
    fn set_login_status(&self, value: &str);
    fn set_credential_status(&self, value: &str);
}

/// Applies one account view to the overview row and the settings card.
///
/// A valid login or an in-flight login round clears the stale failure text from
/// both the overview `login-status` and the settings `credential-status`; a manual
/// takeover keeps its reason until the user acts.
pub fn apply_account(surface: &dyn AccountSurface, view: &AccountView) {
    surface.set_saved_account(&view.saved_account);
    surface.set_saved_detail(&view.saved_detail);
    surface.set_can_clear(view.can_clear);
    surface.set_can_continue_login(view.can_continue_login);
    surface.set_overview_label(&view.overview_label);
    surface.set_overview_detail(&view.overview_detail);
    surface.set_overview_tone(view.overview_tone);
    surface.set_overview_pulse(view.pulse);
    if view.clears_old_failure {
        surface.set_login_status("");
        surface.set_credential_status("");
    }
}

pub fn account_view(saved: &LoadOutcome, auth: Option<&AuthRead>) -> AccountView {
    let (saved_account, saved_detail, can_clear) = match saved {
        LoadOutcome::Missing => ("未配置账号".to_string(), String::new(), false),
        LoadOutcome::Loaded(credentials) => (credentials.account.clone(), String::new(), true),
        LoadOutcome::Unusable(reason) => {
            ("已保存的账号信息无法读取".to_string(), reason.clone(), true)
        }
    };
    let (state_value, state_detail, state_tone, can_continue, clears_old_failure) = match auth {
        None => (
            "认证状态读取中".to_string(),
            String::new(),
            Tone::Warn,
            false,
            false,
        ),
        Some(AuthRead::Unavailable(reason)) => (
            "认证未知".to_string(),
            reason.clone(),
            Tone::Warn,
            false,
            false,
        ),
        Some(AuthRead::Status(status)) => {
            let detail = status
                .manual_reason_label()
                .or_else(|| status.detail.clone())
                .unwrap_or_default();
            let awaiting_duo = status.phase.as_deref() == Some("awaiting_duo")
                || status.manual_reason.as_deref() == Some("duo_prompt_waiting");
            let (label, tone, next) = if status.state == AuthState::Valid {
                ("已登录", Tone::Good, false)
            } else if awaiting_duo {
                // Duo is progress in the same round. The gateway keeps the
                // visible window open and finishes this round after approval, so
                // this is not a manual takeover and must not open another round.
                ("正在完成 Duo 验证", Tone::Warn, false)
            } else {
                match status.state {
                    AuthState::Valid => unreachable!("valid is handled above"),
                    AuthState::LoggingIn if status.phase.as_deref() == Some("verifying_login") => {
                        ("正在核验登录结果", Tone::Warn, false)
                    }
                    AuthState::LoggingIn => ("登录中", Tone::Warn, status.manual_reason.is_some()),
                    AuthState::NeedsLogin => ("尚未登录", Tone::Warn, true),
                    AuthState::Failed => ("登录失败", Tone::Bad, true),
                    AuthState::Cancelled => ("已取消登录", Tone::Warn, true),
                    AuthState::Unknown => {
                        ("暂时无法核验", Tone::Warn, status.manual_reason.is_some())
                    }
                }
            };
            let detail = if status.state == AuthState::Valid {
                String::new()
            } else if status.label == "UM 已拒绝当前凭据（尚未到期）" {
                status.label.clone()
            } else if detail.is_empty() && status.error.as_deref() == Some("timeout") {
                "登录窗口已超时，可重新登录。".to_string()
            } else {
                detail
            };
            let clears_old_failure =
                matches!(status.state, AuthState::Valid | AuthState::LoggingIn);
            (label.to_string(), detail, tone, next, clears_old_failure)
        }
    };
    let can_continue_login = can_continue && matches!(saved, LoadOutcome::Loaded(_));
    // The status dot pulses only while a read or a login round is in flight;
    // the shell consumes this flag instead of matching display text.
    let pulse = match auth {
        None => true,
        Some(AuthRead::Unavailable(_)) => false,
        Some(AuthRead::Status(status)) => matches!(status.state, AuthState::LoggingIn),
    };
    let (overview_label, overview_detail) = match saved {
        LoadOutcome::Unusable(_) => (
            state_value.clone(),
            "已保存的账号信息无法读取，请重新录入。".to_string(),
        ),
        LoadOutcome::Missing if auth.is_none() => ("尚未登录".to_string(), String::new()),
        _ => (state_value.clone(), state_detail.clone()),
    };
    AccountView {
        saved_account,
        saved_detail,
        state_value,
        state_detail: state_detail.clone(),
        state_tone,
        can_clear,
        can_continue_login,
        clears_old_failure,
        overview_label,
        overview_detail,
        overview_tone: state_tone,
        pulse,
    }
}

/// Whether the first-open intro is shown.
///
/// Only a legal first open without saved credentials shows it, and only until the
/// user skips or saves the account. A corrupt file is a re-entry case, not a first
/// open.
pub fn intro_visible(intro_dismissed: bool, saved: &LoadOutcome) -> bool {
    !intro_dismissed && matches!(saved, LoadOutcome::Missing)
}

/// The tray tooltip: the login state only, never an account or password.
pub fn tray_tooltip(auth: Option<&AuthRead>, paused: bool) -> String {
    let state = match auth {
        Some(AuthRead::Status(status)) => match status.state {
            AuthState::Valid => "已登录",
            AuthState::LoggingIn => "登录中",
            _ => "尚未登录",
        },
        _ => "尚未登录",
    };
    if paused {
        format!("本机控制台 · {state} · 日志已暂停")
    } else {
        format!("本机控制台 · {state}")
    }
}
