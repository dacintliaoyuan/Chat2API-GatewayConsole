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

use chrono::TimeZone;
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

/// The gateway's verdict from `/admin/auth/verify`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VerifyVerdict {
    Valid,
    Rejected,
    NeedsLogin,
    Unknown,
}

impl VerifyVerdict {
    pub fn from_gateway(value: &str) -> Self {
        match value {
            "valid" => Self::Valid,
            "rejected" => Self::Rejected,
            "needs_login" => Self::NeedsLogin,
            _ => Self::Unknown,
        }
    }
}

/// One `/admin/auth/verify` answer, reduced to the fields the app displays.
///
/// The probe never publishes a credential or changes the gateway's auth state; a
/// failed verify therefore keeps its own reason instead of becoming a login.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifyReport {
    pub verdict: VerifyVerdict,
    pub reason: Option<String>,
    pub upstream_status: Option<u16>,
    /// Unix epoch seconds reported by the gateway.
    pub checked_epoch: i64,
    pub note: Option<String>,
}

impl VerifyReport {
    pub fn parse(body: &Value) -> Result<Self, String> {
        let object = body
            .as_object()
            .ok_or_else(|| "核验结果不是 JSON 对象".to_string())?;
        let verify = object
            .get("verify")
            .and_then(Value::as_str)
            .filter(|verify| !verify.is_empty())
            .ok_or_else(|| "核验结果缺少 verify".to_string())?;
        let checked_epoch = object
            .get("checked_epoch")
            .and_then(Value::as_u64)
            .ok_or_else(|| "核验结果缺少 checked_epoch".to_string())?;
        let upstream_status = object
            .get("upstream_status")
            .and_then(Value::as_u64)
            .and_then(|status| u16::try_from(status).ok());
        Ok(Self {
            verdict: VerifyVerdict::from_gateway(verify),
            reason: text_field(object.get("reason")),
            upstream_status,
            checked_epoch: i64::try_from(checked_epoch)
                .map_err(|_| "核验结果的 checked_epoch 超出范围".to_string())?,
            note: text_field(object.get("note")),
        })
    }

    /// The concrete verify reason, explained for the page.
    pub fn reason_label(&self) -> Option<String> {
        self.reason.as_deref().map(verify_reason_label)
    }
}

/// Explains a gateway verify reason without echoing credentials.
pub fn verify_reason_label(reason: &str) -> String {
    match reason {
        "no_local_credential" => "本实例没有可核验的 UM 凭据".to_string(),
        "upstream_rejected" => "上游拒绝了当前凭据".to_string(),
        "timeout" => "核验上游时超时".to_string(),
        "network_error" => "无法连接上游".to_string(),
        "upstream_5xx" => "上游返回 5xx".to_string(),
        "unexpected_status" => "上游返回非预期状态".to_string(),
        "invalid_catalog_shape" => "上游模型目录结构不符合预期".to_string(),
        "credential_changed" => "核验期间凭据已变化".to_string(),
        other => format!("核验原因：{other}"),
    }
}

/// One verify call: the gateway's report or why it is unavailable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VerifyOutcome {
    Report(VerifyReport),
    Unavailable(String),
}

impl VerifyReport {
    /// The concrete reason plus the upstream status when the gateway reported one.
    fn reason_detail(&self) -> String {
        match (self.reason_label(), self.upstream_status) {
            (Some(label), Some(status)) => format!("{label}（HTTP {status}）"),
            (Some(label), None) => label,
            (None, Some(status)) => format!("上游核验结果：HTTP {status}"),
            (None, None) => self
                .note
                .clone()
                .unwrap_or_else(|| "核验没有给出可用结论".to_string()),
        }
    }
}

/// What the overview's 「最近核验」 card shows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifyView {
    /// Empty until the first probe.
    pub at: String,
    pub result: String,
    pub detail: String,
    pub tone: Tone,
    pub action_label: String,
}

/// Maps one verify outcome to the card. A failed verify keeps its own result
/// and reason; it never becomes a passed verification.
pub fn verify_view(outcome: Option<&VerifyOutcome>) -> VerifyView {
    match outcome {
        None => VerifyView {
            at: String::new(),
            result: "从未核验".to_string(),
            detail: "还没有向上游确认过当前登录凭据。".to_string(),
            tone: Tone::Neutral,
            action_label: "立即核验".to_string(),
        },
        Some(VerifyOutcome::Unavailable(reason)) => VerifyView {
            at: String::new(),
            result: "结果未知".to_string(),
            detail: reason.clone(),
            tone: Tone::Warn,
            action_label: "重新核验".to_string(),
        },
        Some(VerifyOutcome::Report(report)) => {
            let at = format!("核验于 {}", verify_clock(report.checked_epoch));
            let (result, detail, tone) = match report.verdict {
                VerifyVerdict::Valid => (
                    "通过".to_string(),
                    "上游确认当前登录有效（未调用模型）".to_string(),
                    Tone::Good,
                ),
                VerifyVerdict::Rejected | VerifyVerdict::NeedsLogin => {
                    ("未通过".to_string(), report.reason_detail(), Tone::Bad)
                }
                VerifyVerdict::Unknown => {
                    ("结果未知".to_string(), report.reason_detail(), Tone::Warn)
                }
            };
            VerifyView {
                at,
                result,
                detail,
                tone,
                action_label: "重新核验".to_string(),
            }
        }
    }
}

fn verify_clock(epoch_seconds: i64) -> String {
    chrono::Local
        .timestamp_opt(epoch_seconds, 0)
        .single()
        .map(|time| time.format("%H:%M:%S").to_string())
        .unwrap_or_else(|| "—".to_string())
}

/// The window fields the 「最近核验」 card owns.
pub trait VerifySurface {
    fn set_verify_at_label(&self, value: &str);
    fn set_verify_result_label(&self, value: &str);
    fn set_verify_detail_text(&self, value: &str);
    fn set_verify_tone_index(&self, tone: Tone);
    fn set_verify_action_text(&self, value: &str);
}

pub fn apply_verify(surface: &dyn VerifySurface, view: &VerifyView) {
    surface.set_verify_at_label(&view.at);
    surface.set_verify_result_label(&view.result);
    surface.set_verify_detail_text(&view.detail);
    surface.set_verify_tone_index(view.tone);
    surface.set_verify_action_text(&view.action_label);
}

/// Whether the gateway is waiting for an operator to finish a manual step.
///
/// `duo_prompt_waiting` is progress in the same round, not a takeover, so it
/// must not open another window or skip the on-demand verify.
pub fn manual_takeover_status(status: &AuthStatus) -> bool {
    let awaiting_duo = status.phase.as_deref() == Some("awaiting_duo")
        || status.manual_reason.as_deref() == Some("duo_prompt_waiting");
    status.manual_reason.is_some() && !awaiting_duo
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

/// What one 「按需登录」 click did.
///
/// A valid verify only refreshes the displayed state; the visible window opens
/// only when the verify rejects the current credential. ADR 0011 keeps the manual
/// takeover on the explicit visible login and never submits credentials here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OnDemandLogin {
    /// Verify passed; no window was opened and the auth state was re-read.
    AlreadyValid {
        report: VerifyReport,
        status: Option<AuthStatus>,
    },
    /// Verify rejected the credential and one visible login round was started.
    LoginStarted {
        report: Option<VerifyReport>,
        status: AuthStatus,
    },
    /// Verify could not decide; no window was opened.
    Inconclusive(VerifyReport),
    /// The verify call itself failed; no window was opened.
    VerifyUnavailable(String),
    /// Verify rejected the credential but the visible login could not start.
    LoginUnavailable {
        report: Option<VerifyReport>,
        error: String,
    },
}

impl OnDemandLogin {
    pub fn report(&self) -> Option<&VerifyReport> {
        match self {
            Self::AlreadyValid { report, .. } => Some(report),
            Self::LoginStarted { report, .. } | Self::LoginUnavailable { report, .. } => {
                report.as_ref()
            }
            Self::Inconclusive(report) => Some(report),
            Self::VerifyUnavailable(_) => None,
        }
    }
}

/// Runs the on-demand login: verify first, and only a rejected credential opens
/// the visible window. A manual takeover is the operator's explicit choice, so it
/// opens the window without a verify that could skip it.
pub fn on_demand_login(
    auth_admin: &dyn AuthAdmin,
    base_url: &str,
    token: &ControlToken,
    status: Option<&AuthStatus>,
) -> OnDemandLogin {
    if status.is_some_and(manual_takeover_status) {
        return match auth_admin.start_visible_login(base_url, token) {
            Ok(status) => OnDemandLogin::LoginStarted {
                report: None,
                status,
            },
            Err(error) => OnDemandLogin::LoginUnavailable {
                report: None,
                error,
            },
        };
    }
    let report = match auth_admin.verify(base_url, token) {
        Ok(report) => report,
        Err(error) => return OnDemandLogin::VerifyUnavailable(error),
    };
    match report.verdict {
        VerifyVerdict::Valid => {
            // The credential is valid, so no login round is started; only the
            // displayed status is refreshed.
            let status = auth_admin.read_status(base_url, token).ok();
            OnDemandLogin::AlreadyValid { report, status }
        }
        VerifyVerdict::Rejected | VerifyVerdict::NeedsLogin => {
            match auth_admin.start_visible_login(base_url, token) {
                Ok(status) => OnDemandLogin::LoginStarted {
                    report: Some(report),
                    status,
                },
                Err(error) => OnDemandLogin::LoginUnavailable {
                    report: Some(report),
                    error,
                },
            }
        }
        VerifyVerdict::Unknown => OnDemandLogin::Inconclusive(report),
    }
}

/// The status line for one on-demand login, with its reason kept visible.
pub fn on_demand_message(outcome: &OnDemandLogin) -> (String, Tone) {
    match outcome {
        OnDemandLogin::AlreadyValid { .. } => {
            ("核验通过：当前凭据有效，无需登录".to_string(), Tone::Good)
        }
        OnDemandLogin::LoginStarted {
            report: Some(report),
            ..
        } => (
            format!(
                "核验未通过：{}；已打开可见登录窗口",
                on_demand_reason(report)
            ),
            Tone::Warn,
        ),
        OnDemandLogin::LoginStarted { report: None, .. } => {
            ("正在打开可见登录窗口…".to_string(), Tone::Warn)
        }
        OnDemandLogin::Inconclusive(report) => (
            format!("暂时无法核验：{}；未打开登录窗口", on_demand_reason(report)),
            Tone::Warn,
        ),
        OnDemandLogin::VerifyUnavailable(error) => (format!("无法核验：{error}"), Tone::Bad),
        OnDemandLogin::LoginUnavailable {
            report: Some(report),
            error,
        } => (
            format!(
                "核验未通过：{}；打开可见登录失败：{error}",
                on_demand_reason(report)
            ),
            Tone::Bad,
        ),
        OnDemandLogin::LoginUnavailable {
            report: None,
            error,
        } => (format!("打开可见登录失败：{error}"), Tone::Bad),
    }
}

fn on_demand_reason(report: &VerifyReport) -> String {
    report
        .reason_label()
        .unwrap_or_else(|| "核验没有给出可用结论".to_string())
}

/// The gateway's control-token auth face.
pub trait AuthAdmin: Send + Sync {
    fn read_status(&self, base_url: &str, token: &ControlToken) -> Result<AuthStatus, String>;
    /// One single-shot `/admin/auth/verify` probe; it never changes the auth state.
    fn verify(&self, base_url: &str, token: &ControlToken) -> Result<VerifyReport, String>;
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

    fn verify_probe(&self, base_url: &str, token: &ControlToken) -> Result<VerifyReport, String> {
        let url = format!("{}/admin/auth/verify", base_url.trim_end_matches('/'));
        let response = self
            .agent
            .get(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map_err(|error| admin_error(&url, error))?;
        let body: Value = response
            .into_json()
            .map_err(|error| format!("{url}: 核验结果不是有效 JSON：{error}"))?;
        VerifyReport::parse(&body).map_err(|error| format!("{url}: {error}"))
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

    fn verify(&self, base_url: &str, token: &ControlToken) -> Result<VerifyReport, String> {
        self.verify_probe(base_url, token)
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
                Some(detail) => format!("{url}: 本机网关返回 HTTP {code}（{detail}）"),
                None => format!("{url}: 本机网关返回 HTTP {code}"),
            }
        }
        // The transport error repeats the URL and the OS text; keep one short
        // Chinese reason so the card stays readable.
        ureq::Error::Transport(error) => match error.kind() {
            ureq::ErrorKind::ConnectionFailed => {
                format!("{url}: 无法连接本机网关（连接被拒绝）")
            }
            _ => format!("{url}: 无法连接本机网关"),
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
    /// ADR 0011: a manual-takeover state opens the visible window directly
    /// instead of running the on-demand verify first.
    pub manual_takeover: bool,
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
    fn set_manual_takeover(&self, value: bool);
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
    surface.set_manual_takeover(view.manual_takeover);
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
            "登录状态未知".to_string(),
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
    let manual_takeover = matches!(
        auth,
        Some(AuthRead::Status(status)) if manual_takeover_status(status)
    );
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
        manual_takeover,
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
