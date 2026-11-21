//! Real UM login executor for Rust 8003.
//!
//! This is the Rust port of the login/recovery half of
//! `chat2api/chat2api.py`: headless-first, one shared re-authentication
//! session, an explicit visible round only when the operator asks for it, the
//! per-profile OS lock, Python-compatible token storage, the manual-takeover
//! reasons, operator cancel, `BrowserDisabled`, and the rolling global sign-in
//! start limit. The browser, the profile filesystem, the credential probe and
//! the credential sink are seams: tests use fakes and never open a browser.

pub mod browser;
pub mod history;
#[cfg(windows)]
mod playwright;
pub mod probe;
pub mod profile;
pub mod token;

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::auth_state::{self, AuthStateFile};
use crate::config::{GatewayConfig, Role};
use crate::reauth::{ReauthProviderResult, ReauthTrigger, ReauthenticationProvider};
use crate::watch::WatchStatus;

use self::profile::{acquire_profile_lock, manual_display, visible_login_supported};
use self::token::TokenStore;

pub use self::browser::{
    BrowserUnavailable, CredentialField, LaunchRequest, LoginBrowser, LoginBrowserFactory,
    LoginFailure, LoginPage, SystemBrowserFactory, VisibleTarget,
};
pub use self::probe::{CredentialProbe, CredentialVerdict, HttpCredentialProbe};
pub use self::profile::DisplayEnvironment;
pub use self::token::{TokenDocument, TokenStore as TokenStoreSeam};

pub(crate) fn production_browser_factory(config: &GatewayConfig) -> Arc<dyn LoginBrowserFactory> {
    #[cfg(windows)]
    if !config.candidate && config.role == crate::config::Role::Local {
        if let Some(python) =
            std::env::var_os("CHAT2API_LOGIN_PLAYWRIGHT_PYTHON").filter(|python| !python.is_empty())
        {
            return Arc::new(playwright::PlaywrightFactory(python.into()));
        }
    }
    let _ = config;
    Arc::new(SystemBrowserFactory)
}

pub const MANUAL_TAKEOVER_REQUIRED_PREFIX: &str = "manual_takeover_required:";
const PROTECTED_AUTOFILL_REASON: &str = "umpass_autofill_protected";
const PROTECTED_AUTOFILL_HINT: &str = "Edge 已预填，请在 UMPASS 页面点击登录";
const MISSING_FIELDS_HINT: &str = "UMPASS 表单未就绪，请在 Edge 窗口中完成登录";
const AMBIGUOUS_SUBMIT_HINT: &str = "UMPASS 页面有多个提交按钮，请在 Edge 窗口中手动登录";
const UNRECOGNIZED_SUBMIT_HINT: &str = "UMPASS 页面未识别到唯一提交按钮，请在 Edge 窗口中手动登录";
const SUBMITTED_HINT: &str = "已自动提交 UMPASS 登录，正在等待结果";
const UNTRUSTED_ORIGIN_HINT: &str = "UMPASS 页面来源不可信，已停止自动提交";
const REJECTED_HINT: &str = "上游拒绝了当前账号密码，请人工处理";
const FORM_READY: &str = "ready";
const FORM_PROTECTED_AUTOFILL: &str = "protected_autofill";
const FORM_MISSING_FIELDS: &str = "missing_fields";
const FORM_SUBMIT_AMBIGUOUS: &str = "submit_ambiguous";
const FORM_SUBMIT_UNRECOGNIZED: &str = "submit_unrecognized";
const FORM_SUBMITTED: &str = "submitted";
const FORM_UNTRUSTED_ORIGIN: &str = "untrusted_origin";
const FORM_REJECTED: &str = "rejected";
pub const LOGIN_TERMINAL_MANUAL_REASONS: [&str; 4] = [
    "umpass_mfa_required",
    "umpass_prefill_missing",
    PROTECTED_AUTOFILL_REASON,
    "unrecognized_page",
];
pub const REAUTH_RECOVERY_BACKOFF: [Duration; 5] = [
    Duration::ZERO,
    Duration::from_secs(30),
    Duration::from_secs(60),
    Duration::from_secs(300),
    Duration::from_secs(600),
];
pub const LOGIN_DISPLAY_FILE_ENV: &str = "CHAT2API_LOGIN_DISPLAY_FILE";
pub const LOGIN_DISPLAY_FILE: &str = "/run/um-share/login-display.env";
pub const BROWSER_PATH_ENV: &str = "CHAT2API_BROWSER_PATH";
pub const TOKEN_FILE_ENV: &str = "CHAT2API_TOKEN_FILE";
pub const PROFILE_DIR_ENV: &str = "CHAT2API_PROFILE_DIR";
pub const AUTH_WAIT_RECHECK: Duration = Duration::from_secs(1);

pub const OPENWEBUI_SSO_BUTTON_SELECTORS: [&str; 2] = [
    "button:has-text(\"使用 UMPASS 继续\")",
    "button:has-text(\"Continue with UMPASS\")",
];
pub const UMPASS_SSO_HOSTS: [&str; 2] = ["websso.um.edu.mo", "login.um.edu.mo"];
pub const UMPASS_ACCOUNT_SELECTORS: [&str; 4] = [
    "input[name=\"UserName\"]",
    "input[id=\"userNameInput\"]",
    "input[name=\"username\"]",
    "input[id=\"username\"]",
];
pub const UMPASS_PASSWORD_SELECTORS: [&str; 5] = [
    "input[name=\"Password\"]",
    "input[id=\"passwordInput\"]",
    "input[name=\"password\"]",
    "input[id=\"password\"]",
    "input[type=\"password\"]",
];
pub const UMPASS_SUBMIT_SELECTORS: [&str; 4] = [
    "input[type=\"submit\"]",
    "button[type=\"submit\"]",
    "span#submitButton[role=\"button\"]",
    "#kc-login",
];
pub const UMPASS_MFA_SELECTORS: [&str; 7] = [
    "input[autocomplete=\"one-time-code\"]",
    "input[name*=\"otp\" i]",
    "input[id*=\"otp\" i]",
    "input[name*=\"code\" i]",
    "input[id*=\"code\" i]",
    "text=\"验证码\"",
    "text=\"Verification code\"",
];
pub const UMPASS_ERROR_SELECTORS: [&str; 8] = [
    "[role=\"alert\"]",
    "#errorText",
    ".error",
    "#usernameError",
    "#passwordError",
    "#input-error",
    "[id$=\"-error\"]",
    ".pf-c-form__helper-text.pf-m-error",
];
pub const DUO_WAIT_HOSTS: [&str; 1] = ["duosecurity.com"];

const AUTH_LABELS: [(&str, &str); 6] = [
    ("valid", "认证有效"),
    ("unknown", "认证暂时无法核验"),
    ("needs_login", "需要登录"),
    ("logging_in", "登录中"),
    ("failed", "登录失败"),
    ("cancelled", "已取消登录"),
];

/// The upstream authentication state the console reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthState {
    Valid,
    Unknown,
    NeedsLogin,
    LoggingIn,
    Failed,
    Cancelled,
}

impl AuthState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Valid => "valid",
            Self::Unknown => "unknown",
            Self::NeedsLogin => "needs_login",
            Self::LoggingIn => "logging_in",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }

    fn label(self) -> &'static str {
        AUTH_LABELS
            .iter()
            .find(|(state, _)| *state == self.as_str())
            .map(|(_, label)| *label)
            .unwrap_or("需要登录")
    }
}

/// One login round's terminal outcome.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoginOutcome {
    Valid,
    VerificationUnknown { reason: String },
    Token { token: String },
    Manual { reason: String },
    Rejected { reason: String },
    Unavailable { reason: String },
    Busy { reason: String },
    Transient { reason: String },
    Cancelled { reason: String },
    Terminal { reason: String },
}

impl LoginOutcome {
    fn reason(&self) -> Option<&str> {
        match self {
            Self::Valid => None,
            Self::Token { .. } => None,
            Self::Manual { reason, .. }
            | Self::VerificationUnknown { reason }
            | Self::Rejected { reason }
            | Self::Unavailable { reason }
            | Self::Busy { reason }
            | Self::Transient { reason }
            | Self::Cancelled { reason }
            | Self::Terminal { reason } => Some(reason),
        }
    }
}

/// The credential evidence the durable auth state file records.
///
/// It never carries the token, the API key or a full `jti`; only the fields the
/// operator needs to distinguish "token expired" from "upstream rejected a token
/// that was still inside its `exp`".
#[derive(Debug, Clone, Default, PartialEq)]
pub struct CredentialEvidence {
    pub checked_epoch: Option<f64>,
    pub verdict: Option<String>,
    pub reason: Option<String>,
    pub upstream_status: Option<u16>,
    pub last_valid_epoch: Option<f64>,
    pub upstream_401_summary: Option<String>,
    pub api_key_present: bool,
    pub api_key_rejected: bool,
}

/// The token/API-key sink a successful login publishes into.
pub trait CredentialSink: Send + Sync {
    fn publish_credential(&self, token: &str, api_key: Option<&str>);

    /// The latest credential probe evidence, if the sink records any.
    fn credential_evidence(&self) -> CredentialEvidence {
        CredentialEvidence::default()
    }

    /// Rejection of the current bearer, optionally matched to a browser candidate.
    fn bearer_rejection(&self, _candidate: Option<&str>) -> Option<(u16, bool)> {
        None
    }
}

/// Resolved login paths and budgets.
#[derive(Clone)]
pub struct LoginConfig {
    pub role: Role,
    pub base_url: String,
    pub token_file: PathBuf,
    pub profile_dir: PathBuf,
    pub display_file: PathBuf,
    pub login_timeout: Duration,
    pub idle_timeout: Duration,
    pub poll_interval: Duration,
    pub click_observe_timeout: Duration,
    pub stage_settle_timeout: Duration,
    pub max_clicks_per_stage: usize,
    pub navigation_attempts: usize,
    pub attempt_window: Duration,
    pub max_attempts_per_window: usize,
    pub max_sessions: usize,
    pub browser_disabled: bool,
    /// The bounded automatic re-open interval after a retryable exhausted round.
    pub recovery_retry_interval: Duration,
    /// Automatic re-open rounds allowed inside one rolling day.
    pub recovery_retry_max_per_day: usize,
    /// Durable auth evidence file; `None` keeps the executor in memory only.
    pub auth_state_file: Option<PathBuf>,
    /// Durable login-round history file (#115); `None` keeps it in memory only.
    pub login_history_file: Option<PathBuf>,
    /// UMPASS account and password the login executor may type into an empty
    /// ADFS form; both empty keeps the browser-managed prefill path.
    pub um_account: String,
    pub um_password: String,
}

impl std::fmt::Debug for LoginConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LoginConfig")
            .field("base_url", &self.base_url)
            .field("profile_dir", &self.profile_dir)
            .finish_non_exhaustive()
    }
}

impl LoginConfig {
    pub fn from_gateway(config: &GatewayConfig) -> Self {
        Self {
            role: config.role,
            base_url: config.upstream_base_url.clone(),
            token_file: config.token_file.clone().unwrap_or_else(default_token_file),
            profile_dir: config
                .profile_dir
                .clone()
                .unwrap_or_else(default_profile_dir),
            display_file: config
                .login_display_file
                .clone()
                .unwrap_or_else(default_display_file),
            login_timeout: config.login_timeout,
            idle_timeout: config.login_idle_timeout,
            poll_interval: config.login_poll_interval,
            click_observe_timeout: config.login_click_observe_timeout,
            stage_settle_timeout: config.login_stage_settle_timeout,
            max_clicks_per_stage: config.login_max_clicks_per_stage,
            navigation_attempts: config.login_navigation_attempts,
            attempt_window: config.reauth_attempt_window,
            max_attempts_per_window: config.reauth_max_attempts_per_window,
            max_sessions: config.login_max_sessions,
            browser_disabled: config.browser_disabled,
            recovery_retry_interval: config.recovery_retry_interval,
            recovery_retry_max_per_day: config.recovery_retry_max_per_day,
            auth_state_file: config.effective_auth_state_file(),
            login_history_file: config.effective_login_history_file(),
            um_account: std::env::var("CHAT2API_UM_ACCOUNT")
                .unwrap_or_default()
                .trim()
                .to_string(),
            um_password: std::env::var("CHAT2API_UM_PASSWORD")
                .unwrap_or_default()
                .to_string(),
        }
    }
}

/// The Python production token path.
pub fn default_token_file() -> PathBuf {
    if let Ok(value) = std::env::var(TOKEN_FILE_ENV) {
        if !value.trim().is_empty() {
            return PathBuf::from(value);
        }
    }
    #[cfg(windows)]
    {
        let root = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        root.join("chat2api").join("token.json")
    }
    #[cfg(not(windows))]
    {
        PathBuf::from("/var/lib/um-share-console/share-backend/token.json")
    }
}

/// Load the saved credential without assembling a login executor.
///
/// The gateway needs both the JWT bearer and the operator-saved API key at
/// startup, even when a test injects a scripted re-authentication provider.
pub fn load_saved_token(config: &GatewayConfig) -> Option<TokenDocument> {
    let path = config.token_file.clone().unwrap_or_else(default_token_file);
    TokenStore::new(path, config.upstream_base_url.clone()).load()
}

/// The Python production profile path.
pub fn default_profile_dir() -> PathBuf {
    if let Ok(value) = std::env::var(PROFILE_DIR_ENV) {
        if !value.trim().is_empty() {
            return PathBuf::from(value);
        }
    }
    #[cfg(windows)]
    {
        let root = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
        root.join("chat2api").join(".edge-profile")
    }
    #[cfg(not(windows))]
    {
        PathBuf::from("/var/lib/um-share-console/share-backend/.edge-profile")
    }
}

/// The on-demand manual display marker path.
pub fn default_display_file() -> PathBuf {
    if let Ok(value) = std::env::var(LOGIN_DISPLAY_FILE_ENV) {
        if !value.trim().is_empty() {
            return PathBuf::from(value);
        }
    }
    PathBuf::from(LOGIN_DISPLAY_FILE)
}

#[derive(Default)]
struct Evidence {
    verdict: Option<String>,
    reason: Option<String>,
    upstream_status: Option<u16>,
    checked_epoch: Option<f64>,
    last_valid_epoch: Option<f64>,
    source: Option<String>,
}

impl Evidence {
    fn as_json(&self) -> Value {
        json!({
            "verdict": self.verdict,
            "reason": self.reason,
            "upstream_status": self.upstream_status,
            "checked_epoch": self.checked_epoch,
            "last_valid_epoch": self.last_valid_epoch,
            "source": self.source,
        })
    }
}

#[derive(Debug, Clone, Default)]
struct LoginProgress {
    phase: Option<String>,
    origin: Option<String>,
    manual_reason: Option<String>,
    detail: Option<String>,
    form: Option<String>,
    submit_count: usize,
}

impl LoginProgress {
    fn as_json(&self) -> Value {
        json!({
            "phase": self.phase,
            "origin": self.origin,
            "manual_reason": self.manual_reason,
            "detail": self.detail,
            "form": self.form,
            "submit_count": self.submit_count,
        })
    }
}

#[derive(Debug, Clone, Default)]
struct Recovery {
    state: String,
    round: u64,
    /// Why the current (or last) round started; null when no round started.
    trigger_source: Option<String>,
    /// When the current (or last) round started.
    trigger_epoch: Option<f64>,
    /// The terminal result of the last round, including the concrete reason.
    last_outcome: Option<String>,
    session_starts: usize,
    max_sessions: usize,
    next_attempt_epoch: Option<f64>,
    last_failure_epoch: Option<f64>,
    last_failure_reason: Option<String>,
    pause_reason: Option<String>,
    visible: bool,
    /// A retryable terminal round scheduled its bounded automatic re-open.
    retry_scheduled: bool,
    /// Automatic re-open rounds already used inside the rolling day.
    retry_count: usize,
    /// The last UMPASS form state and automatic submission count observed in
    /// the current (or last) round.
    last_form: Option<String>,
    last_submit_count: usize,
}

impl Recovery {
    fn as_json(&self) -> Value {
        json!({
            "state": self.state,
            "round": self.round,
            "trigger_source": self.trigger_source,
            "trigger_epoch": self.trigger_epoch,
            "last_outcome": self.last_outcome,
            "session_starts": self.session_starts,
            "max_sessions": self.max_sessions,
            "next_attempt_epoch": self.next_attempt_epoch,
            "last_failure_epoch": self.last_failure_epoch,
            "last_failure_reason": self.last_failure_reason,
            "pause_reason": self.pause_reason,
            "retry_scheduled": self.retry_scheduled,
            "retry_count": self.retry_count,
            "visible": self.visible,
            "last_form": self.last_form,
            "last_submit_count": self.last_submit_count,
        })
    }
}

#[derive(Debug, Clone)]
struct LoginWindow {
    window_s: f64,
    deadline_epoch: f64,
}

struct LoginState {
    auth_state: AuthState,
    error: Option<String>,
    primary: Option<String>,
    evidence: Evidence,
    login: Option<LoginProgress>,
    gate: Option<Value>,
    recovery: Recovery,
    login_window: Option<LoginWindow>,
    round: u64,
    session: Option<Arc<LoginSession>>,
    shutdown: bool,
    browser_catalog_valid: bool,
}

struct LoginSession {
    deadline: Instant,
    cancel: AtomicBool,
    result: Mutex<Option<LoginOutcome>>,
    condvar: Condvar,
}

struct LoginInner {
    config: LoginConfig,
    factory: Arc<dyn LoginBrowserFactory>,
    probe: Arc<dyn CredentialProbe>,
    credentials: Arc<dyn CredentialSink>,
    token_store: TokenStore,
    auth_state: AuthStateFile,
    history: history::LoginHistory,
    state: Mutex<LoginState>,
    condvar: Condvar,
    attempts: Mutex<VecDeque<Instant>>,
    retry_attempts: Mutex<VecDeque<Instant>>,
    watch: Arc<WatchStatus>,
}

/// One shared UM login executor.
#[derive(Clone)]
pub struct LoginService {
    inner: Arc<LoginInner>,
}

impl LoginService {
    pub fn new(
        config: LoginConfig,
        factory: Arc<dyn LoginBrowserFactory>,
        probe: Arc<dyn CredentialProbe>,
        credentials: Arc<dyn CredentialSink>,
    ) -> Self {
        Self::new_with_watch(
            config,
            factory,
            probe,
            credentials,
            Arc::new(WatchStatus::new()),
        )
    }

    /// Construct with the auth-watch state shared with the HTTP surface.
    pub fn new_with_watch(
        config: LoginConfig,
        factory: Arc<dyn LoginBrowserFactory>,
        probe: Arc<dyn CredentialProbe>,
        credentials: Arc<dyn CredentialSink>,
        watch: Arc<WatchStatus>,
    ) -> Self {
        let token_store = TokenStore::new(config.token_file.clone(), config.base_url.clone());
        let saved = token_store.load();
        let auth_state = if saved
            .as_ref()
            .is_some_and(|document| !document.token.is_empty())
        {
            AuthState::Valid
        } else {
            AuthState::NeedsLogin
        };
        let recovery = Recovery {
            state: "idle".to_string(),
            max_sessions: config.max_sessions,
            ..Recovery::default()
        };
        let auth_state_file = AuthStateFile::open(config.auth_state_file.clone());
        let history = history::LoginHistory::open_with_driver(
            config.login_history_file.clone(),
            Some(factory.driver_name().to_string()),
        );
        let service = Self {
            inner: Arc::new(LoginInner {
                config,
                factory,
                probe,
                credentials,
                token_store,
                auth_state: auth_state_file,
                history,
                state: Mutex::new(LoginState {
                    auth_state,
                    error: None,
                    primary: None,
                    evidence: Evidence::default(),
                    login: None,
                    gate: None,
                    recovery,
                    login_window: None,
                    round: 0,
                    session: None,
                    shutdown: false,
                    browser_catalog_valid: false,
                }),
                condvar: Condvar::new(),
                attempts: Mutex::new(VecDeque::new()),
                retry_attempts: Mutex::new(VecDeque::new()),
                watch,
            }),
        };
        service.persist_auth_state();
        service
    }

    pub fn from_gateway(
        config: &GatewayConfig,
        factory: Arc<dyn LoginBrowserFactory>,
        probe: Arc<dyn CredentialProbe>,
        credentials: Arc<dyn CredentialSink>,
    ) -> Self {
        Self::new(
            LoginConfig::from_gateway(config),
            factory,
            probe,
            credentials,
        )
    }

    /// Construct from the gateway config with the shared auth-watch state.
    pub fn from_gateway_with_watch(
        config: &GatewayConfig,
        factory: Arc<dyn LoginBrowserFactory>,
        probe: Arc<dyn CredentialProbe>,
        credentials: Arc<dyn CredentialSink>,
        watch: Arc<WatchStatus>,
    ) -> Self {
        Self::new_with_watch(
            LoginConfig::from_gateway(config),
            factory,
            probe,
            credentials,
            watch,
        )
    }

    /// The saved credential this instance starts with, if any.
    pub fn saved_token(&self) -> Option<TokenDocument> {
        self.inner.token_store.load()
    }

    pub fn token_store(&self) -> &TokenStore {
        &self.inner.token_store
    }

    pub fn config(&self) -> &LoginConfig {
        &self.inner.config
    }

    /// The auth-watch state shared with the server's watch thread.
    pub fn watch(&self) -> Arc<WatchStatus> {
        Arc::clone(&self.inner.watch)
    }

    /// The console-visible authentication status, with the same keys as Python.
    pub fn status(&self) -> Value {
        let state = self.inner.state.lock().expect("login state poisoned");
        status_from_locked(&state, &self.inner)
    }

    /// Write the durable auth state document once.
    fn persist_auth_state(&self) {
        let state = self.inner.state.lock().expect("login state poisoned");
        persist_auth_state_locked(&self.inner, &state);
    }

    /// The explicit `/admin/auth/login` entry: start or join the ONE round.
    pub fn start_explicit(&self, visible: bool) -> Value {
        {
            let mut state = self.inner.state.lock().expect("login state poisoned");
            if self.inner.config.browser_disabled {
                state.auth_state = AuthState::Failed;
                state.error = Some("BrowserDisabled".to_string());
                state.primary = None;
                state.recovery.state = "exhausted".to_string();
                state.recovery.pause_reason = Some("BrowserDisabled".to_string());
                state.recovery.next_attempt_epoch = None;
                state.recovery.retry_scheduled = false;
                self.inner.condvar.notify_all();
                persist_auth_state_locked(&self.inner, &state);
                return status_from_locked(&state, &self.inner);
            }
            // Only a live login task owns the round. A refusal gate also leaves
            // `recovery.state = waiting`, but without a session; it must fall
            // through and re-check the budget so a released window accepts the
            // explicit request directly.
            if active_round(&state) && state.session.is_some() {
                if visible && !state.recovery.visible {
                    state.recovery.visible = true;
                    self.inner.history.note_visible(state.round);
                }
                self.inner.condvar.notify_all();
                return status_from_locked(&state, &self.inner);
            }
            if state.auth_state == AuthState::Failed || state.auth_state == AuthState::Cancelled {
                state.error = None;
                state.primary = None;
                state.login = None;
            }
            if let Err(retry_after) = self.inner.allow_start() {
                state.auth_state = AuthState::NeedsLogin;
                state.error = Some("reauth_refused:global_attempt_cap".to_string());
                state.recovery.state = "waiting".to_string();
                state.recovery.next_attempt_epoch = Some(profile::epoch_now() + retry_after as f64);
                state.recovery.pause_reason = None;
                state.recovery.retry_scheduled = false;
                state.gate = Some(self.inner.refused_gate("global_attempt_cap"));
                self.inner.condvar.notify_all();
                persist_auth_state_locked(&self.inner, &state);
                return status_from_locked(&state, &self.inner);
            }
            begin_round_locked(
                &self.inner,
                &mut state,
                visible,
                Some(ReauthTrigger::Explicit),
            );
        }
        self.status()
    }

    /// Cancel THIS round; the service keeps running.
    pub fn cancel(&self) -> Value {
        let mut state = self.inner.state.lock().expect("login state poisoned");
        let active = active_round(&state) || state.auth_state == AuthState::LoggingIn;
        if !active {
            if state.auth_state == AuthState::Cancelled {
                return json!({
                    "cancelled": false,
                    "state": "cancelled",
                    "detail": "本轮登录已取消；服务继续运行",
                });
            }
            return json!({
                "cancelled": false,
                "state": "no_login_in_progress",
                "detail": "当前没有进行中的登录",
            });
        }
        if let Some(session) = &state.session {
            session.cancel.store(true, Ordering::SeqCst);
            session.condvar.notify_all();
        }
        // Invalidate the round so a worker that is still unwinding cannot
        // overwrite the cancelled state with its own outcome.
        let round = state.round;
        self.inner.history.finish(
            round,
            "cancelled",
            Some("cancelled"),
            state.evidence.upstream_status,
            state.evidence.verdict.as_deref(),
        );
        state.round += 1;
        state.recovery.round = state.round;
        state.session = None;
        state.auth_state = AuthState::Cancelled;
        state.error = Some("cancelled".to_string());
        state.primary = None;
        state.login = None;
        state.recovery.state = "exhausted".to_string();
        state.recovery.next_attempt_epoch = None;
        state.recovery.pause_reason = Some("cancelled".to_string());
        state.recovery.last_outcome = Some("cancelled".to_string());
        state.recovery.retry_scheduled = false;
        self.inner.condvar.notify_all();
        persist_auth_state_locked(&self.inner, &state);
        json!({
            "cancelled": true,
            "state": "cancelled",
            "detail": "已取消本轮登录；服务继续运行，普通请求不会自动重开",
        })
    }

    /// Record one auth-watch probe so `/admin/auth/status` carries evidence
    /// without a login round. Python records the same fields from its watch.
    pub fn note_probe(&self, verdict: &str, reason: Option<&str>, upstream_status: Option<u16>) {
        let mut state = self.inner.state.lock().expect("login state poisoned");
        if self.inner.config.role == Role::Local
            && state
                .login
                .as_ref()
                .and_then(|login| login.phase.as_deref())
                == Some("verifying_login")
        {
            // 巡检查的是已发布的凭据，进行中的轮次需要保留候选凭据的核验证据。
            return;
        }
        let now = profile::epoch_now();
        let last_valid_epoch = if verdict == "valid" {
            Some(now)
        } else {
            state.evidence.last_valid_epoch
        };
        state.evidence = Evidence {
            verdict: Some(verdict.to_string()),
            reason: reason.map(str::to_string),
            upstream_status,
            checked_epoch: Some(now),
            last_valid_epoch,
            source: Some("auth_watch".to_string()),
        };
        if active_round(&state)
            || matches!(state.auth_state, AuthState::Failed | AuthState::Cancelled)
        {
            persist_auth_state_locked(&self.inner, &state);
            return;
        }
        match verdict {
            "valid" => {
                state.auth_state = AuthState::Valid;
                state.error = None;
                state.primary = None;
                state.gate = None;
                state.recovery.state = "idle".to_string();
                state.recovery.next_attempt_epoch = None;
                state.recovery.pause_reason = None;
                state.recovery.retry_scheduled = false;
            }
            // Python only lets these two verdicts move the state to
            // `needs_login`; an unknown probe only records evidence.
            "needs_login" | "rejected" => {
                state.auth_state = AuthState::NeedsLogin;
                state.error = None;
            }
            _ => {}
        }
        persist_auth_state_locked(&self.inner, &state);
    }

    /// Stop the executor for shutdown: the current round is cancelled.
    pub fn shutdown(&self) {
        let mut state = self.inner.state.lock().expect("login state poisoned");
        state.shutdown = true;
        if let Some(session) = &state.session {
            session.cancel.store(true, Ordering::SeqCst);
            session.condvar.notify_all();
        }
        state.recovery.state = "exhausted".to_string();
        state.recovery.next_attempt_epoch = None;
        state.recovery.pause_reason = Some("stopped".to_string());
        state.recovery.retry_scheduled = false;
        self.inner.condvar.notify_all();
        persist_auth_state_locked(&self.inner, &state);
    }

    /// Whether shutdown already stopped the executor (test/observability seam).
    pub fn is_shutdown(&self) -> bool {
        self.inner
            .state
            .lock()
            .expect("login state poisoned")
            .shutdown
    }
}

impl ReauthenticationProvider for LoginService {
    fn browser_available(&self) -> bool {
        !self.inner.config.browser_disabled
    }

    fn unavailable_reason(&self) -> &'static str {
        "BrowserDisabled"
    }

    fn manual_wait_reason(&self) -> Option<String> {
        let state = self.inner.state.lock().expect("login state poisoned");
        state
            .login
            .as_ref()
            .and_then(|login| login.manual_reason.clone())
            .filter(|reason| reason == "duo_prompt_waiting")
    }

    fn reauthenticate(&self, deadline: Instant) -> ReauthProviderResult {
        self.reauthenticate_round(deadline, None)
    }

    fn reauthenticate_for(
        &self,
        deadline: Instant,
        trigger: ReauthTrigger,
    ) -> ReauthProviderResult {
        self.reauthenticate_round(deadline, Some(trigger))
    }
}

impl LoginService {
    fn reauthenticate_round(
        &self,
        deadline: Instant,
        trigger: Option<ReauthTrigger>,
    ) -> ReauthProviderResult {
        let session = {
            let mut state = self.inner.state.lock().expect("login state poisoned");
            if self.inner.config.browser_disabled {
                return ReauthProviderResult::Unavailable {
                    reason: "BrowserDisabled".to_string(),
                    retry_after: None,
                };
            }
            if state.auth_state == AuthState::Failed || state.auth_state == AuthState::Cancelled {
                let retry_due = state.auth_state == AuthState::Failed
                    && state.recovery.retry_scheduled
                    && state
                        .recovery
                        .next_attempt_epoch
                        .is_some_and(|next| profile::epoch_now() >= next);
                if !retry_due {
                    let reason = state
                        .error
                        .clone()
                        .unwrap_or_else(|| "login_failed".to_string());
                    return ReauthProviderResult::Failed { reason };
                }
                // A retryable exhausted round is due: the schedule already
                // checked the daily bound; clear the terminal failure and fall
                // through to the normal start gate.
                state.auth_state = AuthState::NeedsLogin;
                state.error = None;
                state.primary = None;
                state.login = None;
                state.session = None;
                state.recovery.retry_scheduled = false;
                state.recovery.next_attempt_epoch = None;
            }
            if let Some(session) = state.session.clone() {
                session
            } else if let Some(reason) = state
                .error
                .as_deref()
                .filter(|error| error.starts_with("reauth_refused:"))
            {
                let reason = reason
                    .split_once(':')
                    .map(|(_, reason)| reason)
                    .unwrap_or("recovery_in_progress");
                let retry_after = state
                    .recovery
                    .next_attempt_epoch
                    .map(|next| ((next - profile::epoch_now()).max(1.0).ceil() as u64).min(900));
                return ReauthProviderResult::Unavailable {
                    reason: reason.to_string(),
                    retry_after,
                };
            } else if state.recovery.state == "waiting" {
                let retry_after = state
                    .recovery
                    .next_attempt_epoch
                    .map(|next| ((next - profile::epoch_now()).max(1.0).ceil() as u64).min(900));
                return ReauthProviderResult::Unavailable {
                    reason: "recovery_in_progress".to_string(),
                    retry_after,
                };
            } else {
                if let Err(retry_after) = self.inner.allow_start() {
                    state.auth_state = AuthState::NeedsLogin;
                    state.error = Some("reauth_refused:global_attempt_cap".to_string());
                    state.recovery.state = "waiting".to_string();
                    state.recovery.next_attempt_epoch =
                        Some(profile::epoch_now() + retry_after as f64);
                    state.recovery.pause_reason = None;
                    state.gate = Some(self.inner.refused_gate("global_attempt_cap"));
                    self.inner.condvar.notify_all();
                    return ReauthProviderResult::Unavailable {
                        reason: "global_attempt_cap".to_string(),
                        retry_after: Some(retry_after),
                    };
                }
                begin_round_locked(&self.inner, &mut state, false, trigger)
            }
        };
        let mut result = session.result.lock().expect("login session poisoned");
        loop {
            if let Some(outcome) = result.clone() {
                return outcome_to_provider(outcome);
            }
            let now = Instant::now();
            if now >= deadline {
                // ADR 0011: the request's own wait budget does not end the
                // round. A live Duo manual wait is reported as such so the
                // client retries while the account holder is still confirming.
                if let Some(reason) = self.manual_wait_reason() {
                    return ReauthProviderResult::Unavailable {
                        reason,
                        retry_after: Some(30),
                    };
                }
                return ReauthProviderResult::Unavailable {
                    reason: "auth_wait_timeout".to_string(),
                    retry_after: None,
                };
            }
            let timeout = (deadline - now).min(AUTH_WAIT_RECHECK);
            let (guard, _) = session
                .condvar
                .wait_timeout(result, timeout)
                .expect("login session poisoned");
            result = guard;
        }
    }
}

fn outcome_to_provider(outcome: LoginOutcome) -> ReauthProviderResult {
    match outcome {
        LoginOutcome::Valid => ReauthProviderResult::Restored,
        LoginOutcome::Token { .. } => ReauthProviderResult::Unavailable {
            reason: "login_uncommitted".to_string(),
            retry_after: None,
        },
        LoginOutcome::Busy { reason } => ReauthProviderResult::Unavailable {
            reason,
            retry_after: None,
        },
        LoginOutcome::Transient { reason }
        | LoginOutcome::VerificationUnknown { reason }
        | LoginOutcome::Unavailable { reason } => ReauthProviderResult::Unavailable {
            reason,
            retry_after: None,
        },
        other => ReauthProviderResult::Failed {
            reason: other.reason().unwrap_or("login_failed").to_string(),
        },
    }
}

impl LoginInner {
    fn allow_start(&self) -> Result<usize, u64> {
        let now = Instant::now();
        let mut attempts = self.attempts.lock().expect("login attempts poisoned");
        while attempts.front().is_some_and(|oldest| {
            now.saturating_duration_since(*oldest) >= self.config.attempt_window
        }) {
            attempts.pop_front();
        }
        if attempts.len() >= self.config.max_attempts_per_window {
            let retry_after = attempts
                .front()
                .map(|oldest| {
                    let wait =
                        (*oldest + self.config.attempt_window).saturating_duration_since(now);
                    wait.as_secs().clamp(1, 900)
                })
                .unwrap_or(1);
            return Err(retry_after);
        }
        attempts.push_back(now);
        Ok(attempts.len())
    }

    fn refund_start(&self, start: Instant) {
        let mut attempts = self.attempts.lock().expect("login attempts poisoned");
        if let Some(position) = attempts.iter().position(|attempt| *attempt == start) {
            attempts.remove(position);
        }
    }

    fn attempts_in_window(&self) -> usize {
        let now = Instant::now();
        let attempts = self.attempts.lock().expect("login attempts poisoned");
        attempts
            .iter()
            .filter(|attempt| now.saturating_duration_since(**attempt) < self.config.attempt_window)
            .count()
    }

    /// Automatic re-open rounds used inside the rolling day.
    fn retry_count(&self) -> usize {
        let now = Instant::now();
        let mut attempts = self
            .retry_attempts
            .lock()
            .expect("recovery retry attempts poisoned");
        while attempts.front().is_some_and(|oldest| {
            now.saturating_duration_since(*oldest) >= Duration::from_secs(86_400)
        }) {
            attempts.pop_front();
        }
        attempts.len()
    }

    /// Whether one more retryable round may be scheduled today.
    fn retry_schedule_allowed(&self) -> bool {
        self.retry_count() < self.config.recovery_retry_max_per_day
    }

    /// Record one automatic re-open round and return the rolling-day count.
    fn record_retry(&self) -> usize {
        let now = Instant::now();
        let mut attempts = self
            .retry_attempts
            .lock()
            .expect("recovery retry attempts poisoned");
        while attempts.front().is_some_and(|oldest| {
            now.saturating_duration_since(*oldest) >= Duration::from_secs(86_400)
        }) {
            attempts.pop_front();
        }
        attempts.push_back(now);
        attempts.len()
    }

    fn refused_gate(&self, reason: &str) -> Value {
        json!({
            "granted": false,
            "reason": reason,
            "owner_pid": Value::Null,
            "expires_epoch": Value::Null,
            "attempts_in_window": self.attempts_in_window(),
            "max_attempts": self.config.max_attempts_per_window,
            "window_s": self.config.attempt_window.as_secs_f64(),
            "checked_epoch": profile::epoch_now(),
        })
    }
}

fn begin_round_locked(
    inner: &Arc<LoginInner>,
    state: &mut LoginState,
    visible: bool,
    trigger: Option<ReauthTrigger>,
) -> Arc<LoginSession> {
    let timeout = inner.config.login_timeout;
    let deadline = Instant::now() + timeout;
    let started_epoch = profile::epoch_now();
    state.login_window = Some(LoginWindow {
        window_s: timeout.as_secs_f64(),
        deadline_epoch: profile::epoch_now() + timeout.as_secs_f64(),
    });
    state.round += 1;
    state.recovery.round = state.round;
    state.recovery.state = "running".to_string();
    state.browser_catalog_valid = false;
    state.recovery.trigger_source = trigger.map(|trigger| trigger.as_str().to_string());
    state.recovery.trigger_epoch = Some(started_epoch);
    state.recovery.last_outcome = None;
    state.recovery.session_starts = 1;
    state.recovery.next_attempt_epoch = None;
    state.recovery.last_failure_epoch = None;
    state.recovery.last_failure_reason = None;
    state.recovery.pause_reason = None;
    state.recovery.visible = visible;
    state.recovery.last_form = None;
    state.recovery.last_submit_count = 0;
    state.recovery.retry_scheduled = false;
    state.recovery.max_sessions = inner.config.max_sessions;
    state.auth_state = AuthState::LoggingIn;
    state.error = None;
    state.primary = None;
    state.login = None;
    state.gate = Some(json!({
        "granted": true,
        "reason": Value::Null,
        "owner_pid": std::process::id(),
        "expires_epoch": Value::Null,
        "attempts_in_window": inner.attempts_in_window(),
        "max_attempts": inner.config.max_attempts_per_window,
        "window_s": inner.config.attempt_window.as_secs_f64(),
        "checked_epoch": profile::epoch_now(),
    }));
    let session = Arc::new(LoginSession {
        deadline,
        cancel: AtomicBool::new(false),
        result: Mutex::new(None),
        condvar: Condvar::new(),
    });
    state.session = Some(Arc::clone(&session));
    inner
        .history
        .begin_at(state.round, trigger, visible, started_epoch);
    if let Some((status, true)) = inner.credentials.bearer_rejection(None) {
        inner.history.note_early_rejection(state.round, status);
    }
    persist_auth_state_locked(inner, state);
    let worker = Arc::clone(inner);
    let round = state.round;
    let session_worker = Arc::clone(&session);
    thread::spawn(move || run_round(worker, round, session_worker));
    session
}

fn active_round(state: &LoginState) -> bool {
    state.recovery.state == "running" || state.recovery.state == "waiting"
}

/// Compose and write the durable auth state document while the login state lock
/// is held. A write failure only logs; the login state machine never fails.
fn persist_auth_state_locked(inner: &LoginInner, state: &LoginState) {
    if !inner.auth_state.is_persistent() {
        return;
    }
    let evidence = state.evidence.as_json();
    let credential = inner.credentials.credential_evidence();
    let token = inner.token_store.load();
    let claims = auth_state::token_claims(token.as_ref());
    let recovery = &state.recovery;
    let document = json!({
        "schema_version": 1,
        "checked_epoch": evidence.get("checked_epoch").cloned().unwrap_or(Value::Null),
        "verdict": evidence.get("verdict").cloned().unwrap_or(Value::Null),
        "reason": evidence.get("reason").cloned().unwrap_or(Value::Null),
        "upstream_status": evidence.get("upstream_status").cloned().unwrap_or(Value::Null),
        "source": evidence.get("source").cloned().unwrap_or(Value::Null),
        "last_valid_epoch": evidence.get("last_valid_epoch").cloned().unwrap_or(Value::Null),
        "trigger_epoch": recovery.trigger_epoch,
        "trigger_source": recovery.trigger_source,
        "round": recovery.round,
        "recovery": {
            "state": recovery.state,
            "last_outcome": recovery.last_outcome,
            "last_form": recovery.last_form,
            "last_submit_count": recovery.last_submit_count,
            "session_starts": recovery.session_starts,
            "pause_reason": recovery.pause_reason,
            "last_failure_epoch": recovery.last_failure_epoch,
            "last_failure_reason": recovery.last_failure_reason,
            "next_attempt_epoch": recovery.next_attempt_epoch,
            "retry_scheduled": recovery.retry_scheduled,
            "retry_count": recovery.retry_count,
        },
        "saved_at": claims.get("saved_at").cloned().unwrap_or(Value::Null),
        "token_iat": claims.get("token_iat").cloned().unwrap_or(Value::Null),
        "token_exp": claims.get("token_exp").cloned().unwrap_or(Value::Null),
        "token_jti_sha12": claims.get("token_jti_sha12").cloned().unwrap_or(Value::Null),
        "token_id_sha12": claims.get("token_id_sha12").cloned().unwrap_or(Value::Null),
        "api_key_present": credential.api_key_present,
        "api_key_rejected": credential.api_key_rejected,
        "upstream_401_summary": credential.upstream_401_summary,
    });
    inner.auth_state.write(&document);
}

/// Schedule the bounded automatic re-open after a retryable exhausted round.
fn schedule_recovery_retry(inner: &LoginInner, state: &mut LoginState, reason: &str) {
    if inner.retry_schedule_allowed() {
        state.recovery.retry_count = inner.record_retry();
        state.recovery.retry_scheduled = true;
        state.recovery.next_attempt_epoch =
            Some(profile::epoch_now() + inner.config.recovery_retry_interval.as_secs_f64());
        state.recovery.pause_reason = Some(reason.to_string());
    } else {
        state.recovery.retry_count = inner.retry_count();
        state.recovery.retry_scheduled = false;
        state.recovery.next_attempt_epoch = None;
        state.recovery.pause_reason = Some("retry_exhausted".to_string());
        state.recovery.last_outcome = Some("retry_exhausted".to_string());
    }
}

fn status_from_locked(state: &LoginState, inner: &LoginInner) -> Value {
    let manual_reason = state
        .login
        .as_ref()
        .and_then(|login| login.manual_reason.clone());
    let phase = state
        .login
        .as_ref()
        .and_then(|login| login.phase.as_deref());
    let verifying = phase == Some("verifying_login");
    let early_rejection = inner
        .credentials
        .bearer_rejection(None)
        .and_then(|(status, early)| early.then_some(status));
    let mut label = state.auth_state.label().to_string();
    if state.error.as_deref() == Some("timeout") {
        label = "登录窗口已超时".to_string();
    } else if let Some(reason) = &manual_reason {
        label = format!("{label}（需人工：{reason}）");
    } else if verifying {
        label = if inner.config.role == Role::Local {
            "正在核验登录结果".to_string()
        } else {
            format!("{label}（正在核验登录结果）")
        };
    } else if phase == Some("awaiting_duo") {
        label = "正在完成 Duo 验证".to_string();
    } else if early_rejection.is_some() && matches!(state.auth_state, AuthState::NeedsLogin) {
        label = "UM 已拒绝当前凭据（尚未到期）".to_string();
    } else if inner.config.role == Role::Local && state.auth_state == AuthState::Unknown {
        label = "暂时无法核验".to_string();
    } else if state.evidence.verdict.as_deref() == Some("unknown")
        && !matches!(state.auth_state, AuthState::Cancelled | AuthState::Failed)
    {
        label = format!(
            "{label}（最近一次检查无法判断：{}）",
            state.evidence.reason.as_deref().unwrap_or("unknown")
        );
    }
    let window = state.login_window.as_ref();
    json!({
        "state": state.auth_state.as_str(),
        "label": label,
        "error": state.error,
        "primary": state.primary,
        "evidence": state.evidence.as_json(),
        "login": state.login.as_ref().map(LoginProgress::as_json),
        "gate": state.gate,
        "watch": inner.watch.snapshot(),
        "recovery": state.recovery.as_json(),
        "rejected_before_expiry_status": early_rejection,
        "browser_catalog_valid": verifying && state.browser_catalog_valid,
        "login_window_deadline_epoch": window.map(|window| window.deadline_epoch),
        "login_window_s": window.map(|window| window.window_s),
    })
}

fn run_round(inner: Arc<LoginInner>, round: u64, session: Arc<LoginSession>) {
    let mut session_starts = 0usize;
    loop {
        if inner.state.lock().expect("login state poisoned").shutdown {
            finish(
                &inner,
                round,
                &session,
                LoginOutcome::Cancelled {
                    reason: "stopped".to_string(),
                },
            );
            return;
        }
        if session.cancel.load(Ordering::SeqCst) {
            finish(
                &inner,
                round,
                &session,
                LoginOutcome::Cancelled {
                    reason: "cancelled".to_string(),
                },
            );
            return;
        }
        if Instant::now() >= session.deadline {
            finish(
                &inner,
                round,
                &session,
                LoginOutcome::Manual {
                    reason: "timeout".to_string(),
                },
            );
            return;
        }
        if session_starts >= inner.config.max_sessions {
            finish(
                &inner,
                round,
                &session,
                LoginOutcome::Terminal {
                    reason: "attempts_exhausted".to_string(),
                },
            );
            return;
        }
        let start = Instant::now();
        session_starts += 1;
        let visible = {
            let mut state = inner.state.lock().expect("login state poisoned");
            if state.round != round {
                return;
            }
            state.recovery.session_starts = session_starts;
            state.recovery.state = "running".to_string();
            state.recovery.next_attempt_epoch = None;
            state.auth_state = AuthState::LoggingIn;
            state.error = None;
            inner.condvar.notify_all();
            state.recovery.visible
        };
        let outcome = run_browser_session(&inner, round, &session, visible);
        match outcome {
            LoginOutcome::Busy { reason } => {
                inner.refund_start(start);
                let mut state = inner.state.lock().expect("login state poisoned");
                if state.round != round {
                    return;
                }
                state.auth_state = AuthState::NeedsLogin;
                state.error = Some(reason.clone());
                state.recovery.state = "waiting".to_string();
                state.recovery.next_attempt_epoch = None;
                state.recovery.last_failure_epoch = Some(profile::epoch_now());
                state.recovery.last_failure_reason = Some(reason);
                state.recovery.pause_reason = None;
                session_starts = session_starts.saturating_sub(1);
                state.recovery.session_starts = session_starts;
                inner.condvar.notify_all();
                drop(state);
                if !wait_for_retry(&inner, &session, Duration::from_secs(1)) {
                    finish(
                        &inner,
                        round,
                        &session,
                        LoginOutcome::Cancelled {
                            reason: "cancelled".to_string(),
                        },
                    );
                    return;
                }
            }
            LoginOutcome::Transient { reason } => {
                let mut state = inner.state.lock().expect("login state poisoned");
                if state.round != round {
                    return;
                }
                state.recovery.last_failure_epoch = Some(profile::epoch_now());
                state.recovery.last_failure_reason = Some(reason.clone());
                if session_starts >= inner.config.max_sessions {
                    drop(state);
                    finish(
                        &inner,
                        round,
                        &session,
                        LoginOutcome::Terminal {
                            reason: "attempts_exhausted".to_string(),
                        },
                    );
                    return;
                }
                let backoff = REAUTH_RECOVERY_BACKOFF
                    .get(session_starts.saturating_sub(1))
                    .copied()
                    .unwrap_or(Duration::from_secs(600));
                state.recovery.state = "waiting".to_string();
                state.recovery.next_attempt_epoch =
                    Some(profile::epoch_now() + backoff.as_secs_f64());
                state.auth_state = AuthState::NeedsLogin;
                state.error = None;
                state.recovery.pause_reason = None;
                inner.condvar.notify_all();
                drop(state);
                let woke = wait_for_retry(&inner, &session, backoff);
                if !woke {
                    finish(
                        &inner,
                        round,
                        &session,
                        LoginOutcome::Cancelled {
                            reason: "cancelled".to_string(),
                        },
                    );
                    return;
                }
            }
            LoginOutcome::Manual { reason, .. } if reason == "duo_prompt_waiting" && !visible => {
                // ADR 0011: a background round cannot show the Duo prompt and
                // must not open Edge on its own. Keep this round waiting until
                // the account holder explicitly chooses the visible continue.
                let mut state = inner.state.lock().expect("login state poisoned");
                if state.round != round {
                    return;
                }
                state.recovery.state = "waiting".to_string();
                state.recovery.next_attempt_epoch = None;
                state.recovery.last_failure_epoch = None;
                state.recovery.last_failure_reason = None;
                state.recovery.pause_reason = None;
                state.auth_state = AuthState::LoggingIn;
                state.error = None;
                session_starts = session_starts.saturating_sub(1);
                state.recovery.session_starts = session_starts;
                inner.condvar.notify_all();
                drop(state);
                if !wait_for_visible_continue(&inner, &session) {
                    finish(
                        &inner,
                        round,
                        &session,
                        LoginOutcome::Cancelled {
                            reason: "cancelled".to_string(),
                        },
                    );
                    return;
                }
            }
            outcome => {
                finish(&inner, round, &session, outcome);
                return;
            }
        }
    }
}

fn wait_for_retry(inner: &Arc<LoginInner>, session: &Arc<LoginSession>, wait: Duration) -> bool {
    let deadline = (Instant::now() + wait).min(session.deadline);
    while Instant::now() < deadline {
        if session.cancel.load(Ordering::SeqCst) {
            return false;
        }
        if inner.state.lock().expect("login state poisoned").shutdown {
            return false;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        let result = session.result.lock().expect("login session poisoned");
        let _ = session
            .condvar
            .wait_timeout(result, remaining.min(Duration::from_secs(1)))
            .expect("login session poisoned");
    }
    true
}

/// Wait until the account holder chooses the visible continue, cancels the
/// round, or the service shuts down.
fn wait_for_visible_continue(inner: &Arc<LoginInner>, session: &Arc<LoginSession>) -> bool {
    loop {
        if session.cancel.load(Ordering::SeqCst) {
            return false;
        }
        let state = inner.state.lock().expect("login state poisoned");
        if state.shutdown || state.recovery.visible {
            return !state.shutdown;
        }
        let remaining = session.deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            // The round loop publishes the expired deadline.
            return true;
        }
        let (state, _) = inner
            .condvar
            .wait_timeout(state, remaining.min(Duration::from_millis(250)))
            .expect("login state poisoned");
        drop(state);
    }
}

fn session_finish(session: &Arc<LoginSession>, outcome: LoginOutcome) {
    *session.result.lock().expect("login session poisoned") = Some(outcome);
    session.condvar.notify_all();
}

fn finish(inner: &Arc<LoginInner>, round: u64, session: &Arc<LoginSession>, outcome: LoginOutcome) {
    let session_outcome = outcome.clone();
    let mut state = inner.state.lock().expect("login state poisoned");
    if state.round != round {
        drop(state);
        session_finish(session, outcome);
        return;
    }
    // An inconclusive network check never carried an upstream HTTP status; do not
    // borrow the previous round's status for this round's record.
    let upstream_status = match &outcome {
        LoginOutcome::VerificationUnknown { .. } => None,
        _ => state.evidence.upstream_status,
    };
    let evidence_verdict = match &outcome {
        LoginOutcome::Valid => Some("valid".to_string()),
        LoginOutcome::VerificationUnknown { .. } => Some("unknown".to_string()),
        LoginOutcome::Rejected { .. } => Some("rejected".to_string()),
        _ => state.evidence.verdict.clone(),
    };
    let (outcome_category, outcome_reason) = history_outcome(&outcome);
    let outcome_reason = if outcome_reason.as_deref() == Some("verification_timeout") {
        state.evidence.reason.as_ref().or(outcome_reason.as_ref())
    } else {
        outcome_reason.as_ref()
    };
    // The durable record is written before the outcome becomes visible to
    // `status()` or the waiting provider, so a caller that observed the finished
    // round can read its history immediately.
    inner.history.finish_at(
        round,
        outcome_category,
        outcome_reason.map(String::as_str),
        upstream_status,
        evidence_verdict.as_deref(),
        profile::epoch_now(),
    );
    if let Some((last_form, last_submit_count)) = state
        .login
        .as_ref()
        .map(|login| (login.form.clone(), login.submit_count))
    {
        state.recovery.last_form = last_form;
        state.recovery.last_submit_count = last_submit_count;
    }
    match outcome {
        LoginOutcome::VerificationUnknown { reason } => {
            state.auth_state = AuthState::Unknown;
            state.error = None;
            state.primary = None;
            state.login = None;
            state.login_window = None;
            state.gate = None;
            state.recovery.state = "idle".to_string();
            state.recovery.next_attempt_epoch = None;
            state.recovery.pause_reason = None;
            state.recovery.last_outcome = Some(format!("verification_unknown:{reason}"));
            if reason == "verification_timeout" {
                state.recovery.last_failure_reason = state.evidence.reason.clone();
            }
            let now = profile::epoch_now();
            state.evidence = Evidence {
                verdict: Some("unknown".to_string()),
                reason: Some(reason),
                upstream_status: None,
                checked_epoch: Some(now),
                last_valid_epoch: state.evidence.last_valid_epoch,
                source: Some("login".to_string()),
            };
        }
        LoginOutcome::Valid => {
            state.auth_state = AuthState::Valid;
            state.error = None;
            state.primary = None;
            state.login = None;
            state.recovery.state = "idle".to_string();
            state.recovery.last_outcome = Some("restored".to_string());
            state.recovery.next_attempt_epoch = None;
            state.recovery.retry_scheduled = false;
            state.recovery.last_failure_epoch = None;
            state.recovery.last_failure_reason = None;
            state.recovery.pause_reason = None;
        }
        LoginOutcome::Cancelled { reason } => {
            state.auth_state = AuthState::Cancelled;
            state.error = Some(reason.clone());
            state.primary = None;
            state.login = None;
            state.recovery.state = "exhausted".to_string();
            state.recovery.last_outcome = Some(format!("cancelled:{reason}"));
            state.recovery.next_attempt_epoch = None;
            state.recovery.retry_scheduled = false;
            state.recovery.pause_reason = Some("cancelled".to_string());
        }
        LoginOutcome::Busy { .. } => {
            // The round loop keeps waiting; this branch is not reached.
        }
        LoginOutcome::Transient { .. } | LoginOutcome::Token { .. } => {
            // The round loop keeps waiting; this branch is not reached.
        }
        LoginOutcome::Manual { reason, .. } => {
            state.auth_state = AuthState::Failed;
            state.error = Some(reason.clone());
            state.primary = None;
            state.recovery.state = "exhausted".to_string();
            state.recovery.last_outcome = Some(reason.clone());
            state.recovery.next_attempt_epoch = None;
            state.recovery.last_failure_epoch = Some(profile::epoch_now());
            state.recovery.last_failure_reason = Some(reason.clone());
            state.recovery.pause_reason = Some(reason.clone());
            state.recovery.retry_scheduled = false;
            if reason == "timeout" {
                // A total-budget timeout is retryable inside the daily bound;
                // a manual takeover reason is not.
                schedule_recovery_retry(inner, &mut state, &reason);
            }
        }
        LoginOutcome::Rejected { reason }
        | LoginOutcome::Unavailable { reason }
        | LoginOutcome::Terminal { reason } => {
            state.auth_state = AuthState::Failed;
            state.error = Some(reason.clone());
            state.primary = None;
            state.login = None;
            state.recovery.state = "exhausted".to_string();
            state.recovery.last_outcome = Some(format!("failed:{reason}"));
            state.recovery.next_attempt_epoch = None;
            state.recovery.last_failure_epoch = Some(profile::epoch_now());
            state.recovery.last_failure_reason = Some(reason.clone());
            state.recovery.pause_reason = Some(reason.clone());
            state.recovery.retry_scheduled = false;
            if reason == "attempts_exhausted" {
                schedule_recovery_retry(inner, &mut state, &reason);
            }
        }
    }
    state.session = None;
    persist_auth_state_locked(inner, &state);
    inner.condvar.notify_all();
    drop(state);
    session_finish(session, session_outcome);
}

/// The bounded result category and concrete reason one finished round records.
fn history_outcome(outcome: &LoginOutcome) -> (&'static str, Option<String>) {
    match outcome {
        LoginOutcome::Valid => ("restored", None),
        LoginOutcome::VerificationUnknown { reason } => {
            ("verification_unknown", Some(reason.clone()))
        }
        LoginOutcome::Manual { reason } => {
            let reason = reason
                .strip_prefix(MANUAL_TAKEOVER_REQUIRED_PREFIX)
                .unwrap_or(reason);
            ("manual_takeover", Some(reason.to_string()))
        }
        LoginOutcome::Rejected { reason } => ("rejected", Some(reason.clone())),
        LoginOutcome::Unavailable { reason } => ("unavailable", Some(reason.clone())),
        LoginOutcome::Terminal { reason } => ("terminal", Some(reason.clone())),
        LoginOutcome::Cancelled { reason } => ("cancelled", Some(reason.clone())),
        LoginOutcome::Busy { reason } => ("busy", Some(reason.clone())),
        LoginOutcome::Transient { reason } => ("transient", Some(reason.clone())),
        LoginOutcome::Token { .. } => ("token_uncommitted", None),
    }
}

fn run_browser_session(
    inner: &Arc<LoginInner>,
    round: u64,
    session: &Arc<LoginSession>,
    visible: bool,
) -> LoginOutcome {
    let lock = match acquire_profile_lock(&inner.config.profile_dir) {
        Ok(lock) => lock,
        Err(_busy) => {
            return LoginOutcome::Busy {
                reason: "login_already_running".to_string(),
            }
        }
    };
    if session.cancel.load(Ordering::SeqCst) {
        return LoginOutcome::Cancelled {
            reason: "cancelled".to_string(),
        };
    }
    let browser_path = match inner.factory.resolve_browser() {
        Ok(path) => path,
        Err(error) => {
            return LoginOutcome::Unavailable {
                reason: error.reason,
            }
        }
    };
    inner.factory.cleanup_profile(&inner.config.profile_dir);
    let display = manual_display(&inner.config.display_file);
    if visible && display.is_none() && !visible_login_supported() {
        return LoginOutcome::Unavailable {
            reason: "visible_login_unavailable".to_string(),
        };
    }
    let headful = visible || display.is_some();
    let request = LaunchRequest {
        executable: browser_path,
        profile_dir: inner.config.profile_dir.clone(),
        headful,
        display,
        initial_url: inner.config.base_url.clone(),
    };
    let mut browser = match inner.factory.launch(&request) {
        Ok(browser) => browser,
        Err(error) => {
            return LoginOutcome::Unavailable {
                reason: error.reason,
            }
        }
    };
    let outcome = drive_login(inner, round, session, browser.page(), visible);
    let close_error = browser.close().err();
    drop(browser);
    drop(lock);
    match outcome {
        LoginOutcome::Token { token } => match close_error {
            None => commit_token(inner, round, session, &token),
            Some(detail) => LoginOutcome::Terminal {
                reason: format!("browser_cleanup_failed: {detail}"),
            },
        },
        outcome => match close_error {
            None => outcome,
            Some(detail) => LoginOutcome::Terminal {
                reason: format!(
                    "{}; primary outcome: {detail}",
                    outcome.reason().unwrap_or("login_failed")
                ),
            },
        },
    }
}

fn drive_login(
    inner: &Arc<LoginInner>,
    round: u64,
    session: &Arc<LoginSession>,
    page: &dyn LoginPage,
    visible: bool,
) -> LoginOutcome {
    let mut navigation_error = None;
    for _ in 0..inner.config.navigation_attempts.max(1) {
        if page.bring_to_front().is_err() {
            // A missing front-most window is not a credential verdict.
        }
        match page.goto(&inner.config.base_url) {
            Ok(()) => {
                navigation_error = None;
                break;
            }
            Err(error) => navigation_error = Some(error),
        }
    }
    if navigation_error.is_some() {
        // The page did not load on any attempt: keep polling (it may still
        // arrive) but record the failure as visible evidence.
        note_progress(
            inner,
            round,
            "navigation_failed",
            Some(&inner.config.base_url),
            None,
            None,
        );
    }

    let deadline = session.deadline;
    let mut idle_deadline = Instant::now() + inner.config.idle_timeout;
    let mut last_url = page.url();
    let mut last_token: Option<String> = None;
    let mut rejected_tokens: HashSet<String> = HashSet::new();
    let mut verifying: Option<String> = None;
    let mut verification_retries = 0;
    let mut next_verification = Instant::now();
    let recheck_headless = !visible && inner.config.role == Role::Local;
    let mut clicks = HashMap::new();
    let mut stage_seen: HashMap<String, Instant> = HashMap::new();
    let mut manual_reason: Option<String> = None;
    let mut duo_manual_since: Option<Instant> = None;
    let mut duo_manual_published = false;
    // #101: the UMPASS form may be submitted automatically at most once per
    // round. The submit count and the observed form state are part of the status.
    let mut auto_submissions = 0usize;
    let mut last_click_swallowed = false;
    let mut saw_umpass_form = false;

    while Instant::now() < deadline {
        if session.cancel.load(Ordering::SeqCst) {
            return LoginOutcome::Cancelled {
                reason: "cancelled".to_string(),
            };
        }
        if let Some(outcome) = check_login_window(page, visible) {
            return outcome;
        }
        if Instant::now() >= idle_deadline {
            return LoginOutcome::Manual {
                reason: "idle_timeout".to_string(),
            };
        }
        let token = page.read_token();
        if let Some(token) = token.as_ref() {
            if last_token.as_ref() != Some(token) {
                last_token = Some(token.clone());
                idle_deadline = Instant::now() + inner.config.idle_timeout;
                verification_retries = 0;
                next_verification = Instant::now();
                if verifying.is_some() {
                    note_browser_catalog(inner, round, false);
                }
            }
            if let Some((status, early)) = inner.credentials.bearer_rejection(Some(token)) {
                if rejected_tokens.insert(token.clone()) {
                    note_login_evidence(
                        inner,
                        round,
                        "rejected",
                        Some("upstream_rejected"),
                        Some(status),
                    );
                    if early {
                        inner.history.note_early_rejection(round, status);
                    }
                    let _ = page.drop_token();
                    let _ = page.reload();
                    verifying = None;
                    continue;
                }
            }
            if !rejected_tokens.contains(token) && Instant::now() >= next_verification {
                let verdict = if recheck_headless {
                    inner.probe.classify_with_budget(
                        token,
                        deadline.saturating_duration_since(Instant::now()),
                    )
                } else {
                    inner.probe.classify(token)
                };
                if recheck_headless && Instant::now() >= deadline {
                    return LoginOutcome::VerificationUnknown {
                        reason: "verification_timeout".to_string(),
                    };
                }
                match verdict.verdict {
                    "valid" => {
                        // Keep the valid probe's upstream status in the round
                        // evidence; the history record reads it before the round
                        // finishes.
                        note_login_evidence(
                            inner,
                            round,
                            "valid",
                            verdict.reason.as_deref(),
                            verdict.upstream_status,
                        );
                        return LoginOutcome::Token {
                            token: token.clone(),
                        };
                    }
                    "rejected" => {
                        rejected_tokens.insert(token.clone());
                        note_login_evidence(
                            inner,
                            round,
                            "rejected",
                            verdict.reason,
                            verdict.upstream_status,
                        );
                        if let Some(status) = verdict.upstream_status.filter(|_| {
                            auth_state::rejected_before_expiry(token, profile::epoch_now())
                        }) {
                            inner.history.note_early_rejection(round, status);
                        }
                        if verifying.is_some() {
                            note_browser_catalog(inner, round, false);
                        }
                        let _ = page.drop_token();
                        let _ = page.reload();
                        verifying = None;
                        continue;
                    }
                    _ if visible
                        || (recheck_headless
                            && matches!(
                                verdict.reason,
                                Some("Timeout" | "timeout" | "ConnectionError" | "network_error")
                            )) =>
                    {
                        let first_check_for_candidate = verifying.as_ref() != Some(token);
                        verifying = Some(token.clone());
                        note_login_evidence(
                            inner,
                            round,
                            "unknown",
                            verdict.reason,
                            verdict.upstream_status,
                        );
                        note_progress(
                            inner,
                            round,
                            "verifying_login",
                            Some(&page.url()),
                            None,
                            None,
                        );
                        if first_check_for_candidate && inner.config.role == Role::Local {
                            let budget = deadline
                                .saturating_duration_since(Instant::now())
                                .min(Duration::from_secs(3));
                            if !budget.is_zero() {
                                let accessible =
                                    page.catalog_access(&inner.config.base_url, token, budget);
                                note_browser_catalog(inner, round, accessible);
                            }
                        }
                        idle_deadline = Instant::now() + inner.config.idle_timeout;
                        if recheck_headless {
                            // 暂时断连复用现有恢复退避，期间继续观察取消和页面变化。
                            let delay = REAUTH_RECOVERY_BACKOFF[verification_retries];
                            verification_retries =
                                (verification_retries + 1).min(REAUTH_RECOVERY_BACKOFF.len() - 1);
                            next_verification = Instant::now() + delay;
                        }
                    }
                    _ => {
                        // 后台会话没有可见窗口可保留；结束本次会话，后续认证
                        // 巡检负责重新核验。
                        return LoginOutcome::VerificationUnknown {
                            reason: verdict
                                .reason
                                .unwrap_or("credential_probe_inconclusive")
                                .to_string(),
                        };
                    }
                }
            }
        }
        if verifying.is_some() && verifying.as_ref() != token.as_ref() {
            // 页面换上了别的 token 或清掉了 token：旧的核验目标已消失。
            verifying = None;
            idle_deadline = Instant::now() + inner.config.idle_timeout;
            note_browser_catalog(inner, round, false);
        }
        if recheck_headless && verifying.is_some() {
            idle_deadline = deadline;
        }

        let url = page.url();
        if url != last_url {
            // The SSO broker and the credential form share a stage name.
            // Give the next page its own rendering and autofill budget.
            if origin_without_query(&url) != origin_without_query(&last_url) {
                stage_seen.remove("umpass");
            }
            last_url = url.clone();
            idle_deadline = Instant::now() + inner.config.idle_timeout;
        }
        if let Some(outcome) = check_login_window(page, visible) {
            return outcome;
        }
        let host = url_host(&url);
        let stage = if !url.is_empty() && url_origin(&url) == url_origin(&inner.config.base_url) {
            "openwebui".to_string()
        } else if UMPASS_SSO_HOSTS
            .iter()
            .any(|allowed| host_matches(&host, allowed))
        {
            "umpass".to_string()
        } else if DUO_WAIT_HOSTS
            .iter()
            .any(|allowed| host_matches(&host, allowed))
        {
            "duo".to_string()
        } else {
            "unknown".to_string()
        };

        if stage == "duo" {
            stage_seen.entry(stage.clone()).or_insert_with(Instant::now);
            idle_deadline = Instant::now() + inner.config.idle_timeout;
            // ADR 0011: entering Duo only publishes progress. A trusted
            // device passes through without any interaction, so a manual reason
            // requires positive page evidence that persists past the short
            // transition window; the request-side auth wait must never mistake
            // the transition for a live manual wait.
            if page.duo_requires_confirmation() {
                let since = *duo_manual_since.get_or_insert_with(Instant::now);
                if since.elapsed() >= inner.config.click_observe_timeout {
                    if !duo_manual_published {
                        duo_manual_published = true;
                        note_progress(
                            inner,
                            round,
                            "awaiting_duo",
                            Some(&url),
                            Some("duo_prompt_waiting"),
                            Some("请在 Duo 页面确认本次登录"),
                        );
                    }
                    if !visible {
                        return LoginOutcome::Manual {
                            reason: "duo_prompt_waiting".to_string(),
                        };
                    }
                } else {
                    note_progress(
                        inner,
                        round,
                        "awaiting_duo",
                        Some(&url),
                        None,
                        Some("正在完成 Duo 验证"),
                    );
                }
            } else {
                duo_manual_since = None;
                duo_manual_published = false;
                note_progress(
                    inner,
                    round,
                    "awaiting_duo",
                    Some(&url),
                    None,
                    Some("正在完成 Duo 验证"),
                );
            }
        } else if stage == "unknown" && verifying.is_none() {
            stage_seen.entry(stage.clone()).or_insert_with(Instant::now);
            return stop_for_manual(inner, round, page, "unrecognized_page");
        } else if manual_reason.is_some() || verifying.is_some() {
            // The automatic flow already stopped (an inconclusive probe, a
            // human-only page, a changed site): never act on a page again,
            // only keep polling for the token or a decisive verdict.
        } else if stage == "openwebui" {
            let count = clicks.entry(stage.clone()).or_insert(0);
            if *count >= inner.config.max_clicks_per_stage {
                manual_reason = Some("openwebui_click_swallowed".to_string());
                note_progress(
                    inner,
                    round,
                    "manual_takeover",
                    Some(&url),
                    manual_reason.as_deref(),
                    None,
                );
            } else if let Some(target) = page.one_visible(&OPENWEBUI_SSO_BUTTON_SELECTORS) {
                *count += 1;
                match click_and_observe(inner, session, page, &target, &url) {
                    ClickOutcome::Cancelled => {
                        return LoginOutcome::Cancelled {
                            reason: "cancelled".to_string(),
                        }
                    }
                    ClickOutcome::Advanced => {
                        idle_deadline = Instant::now() + inner.config.idle_timeout;
                    }
                    ClickOutcome::Swallowed => {}
                }
                note_progress(inner, round, "openwebui_clicked", Some(&url), None, None);
            } else {
                let settled = stage_seen.entry(stage.clone()).or_insert_with(Instant::now);
                if settled.elapsed() >= inner.config.stage_settle_timeout {
                    manual_reason = Some("openwebui_button_unrecognized".to_string());
                    note_progress(
                        inner,
                        round,
                        "manual_takeover",
                        Some(&url),
                        manual_reason.as_deref(),
                        None,
                    );
                }
            }
        } else if stage == "umpass" {
            let seen = *stage_seen.entry(stage.clone()).or_insert_with(Instant::now);
            let fields = [
                page.credential_field(&UMPASS_ACCOUNT_SELECTORS),
                page.credential_field(&UMPASS_PASSWORD_SELECTORS),
            ];
            if fields.iter().any(|field| {
                matches!(field, CredentialField::Filled | CredentialField::ProtectedAutofill)
            }) {
                saw_umpass_form = true;
            }
            // A rejection is only acted on once a UMPASS form was recognized in
            // this round (automatically submitted or completed by the operator).
            // A page that only prompts for credentials must stay a form wait.
            let rejection = umpass_credential_rejected(page);
            let rejection_visible = rejection.is_some();
            let manual_rejection = auto_submissions == 0
                && saw_umpass_form
                && rejection_visible
                && fields[1] != CredentialField::ProtectedAutofill;
            if (auto_submissions > 0 && rejection_visible) || manual_rejection {
                note_form(
                    inner,
                    round,
                    "manual_takeover",
                    Some(&url),
                    None,
                    FormState {
                        form: FORM_REJECTED,
                        detail: Some(REJECTED_HINT),
                        submit_count: auto_submissions,
                    },
                );
                if let Some(selector) = rejection {
                    inner.history.note_detail(round, Some(selector));
                }
                return LoginOutcome::Rejected {
                    reason: "upstream_credential_rejected".to_string(),
                };
            }
            if page.any_visible(&UMPASS_MFA_SELECTORS) {
                return stop_for_manual(inner, round, page, "umpass_mfa_required");
            }
            if auto_submissions > 0 {
                // #101: one automatic submission per round. A swallowed submit
                // must not type and submit the same old credentials again.
                if manual_reason.is_none() && last_click_swallowed {
                    manual_reason = Some("umpass_click_swallowed".to_string());
                    note_form(
                        inner,
                        round,
                        "manual_takeover",
                        Some(&url),
                        manual_reason.as_deref(),
                        FormState {
                            form: FORM_SUBMITTED,
                            detail: Some(SUBMITTED_HINT),
                            submit_count: auto_submissions,
                        },
                    );
                }
            } else if fields != [CredentialField::Filled; 2] {
                if seen.elapsed() < inner.config.stage_settle_timeout {
                    // 页面渲染和浏览器已保存密码的自动填充都是异步的。
                    // 使用既有稳定预算等待，不能因没有明文环境配置而提前判缺失。
                } else {
                    // Only type credentials into the exact HTTPS SSO origins.
                    if !trusted_umpass_credential_origin(&url) {
                        note_form(
                            inner,
                            round,
                            "manual_takeover",
                            Some(&url),
                            Some("umpass_credentials_untrusted_origin"),
                            FormState {
                                form: FORM_UNTRUSTED_ORIGIN,
                                detail: Some(UNTRUSTED_ORIGIN_HINT),
                                submit_count: auto_submissions,
                            },
                        );
                        return LoginOutcome::Terminal {
                            reason: "umpass_credentials_untrusted_origin".to_string(),
                        };
                    }
                    let protected_autofill = fields.contains(&CredentialField::ProtectedAutofill);
                    if inner.config.um_account.is_empty() || inner.config.um_password.is_empty() {
                        note_umpass_fields(inner, round, fields, "not_attempted_no_credentials");
                        let (reason, form, detail) = if protected_autofill {
                            (
                                PROTECTED_AUTOFILL_REASON,
                                FORM_PROTECTED_AUTOFILL,
                                PROTECTED_AUTOFILL_HINT,
                            )
                        } else {
                            (
                                "umpass_prefill_missing",
                                FORM_MISSING_FIELDS,
                                MISSING_FIELDS_HINT,
                            )
                        };
                        note_form(
                            inner,
                            round,
                            "waiting_for_credentials",
                            Some(&url),
                            Some(reason),
                            FormState {
                                form,
                                detail: Some(detail),
                                submit_count: auto_submissions,
                            },
                        );
                        if !visible {
                            return stop_for_manual(inner, round, page, reason);
                        }
                    } else {
                        // Explicit environment credentials are a pair. Never combine a
                        // browser-prefilled account with another password.
                        let failure = if page
                            .fill(&UMPASS_ACCOUNT_SELECTORS, &inner.config.um_account)
                            .is_err()
                        {
                            Some("account_fill_failed")
                        } else if page
                            .fill(&UMPASS_PASSWORD_SELECTORS, &inner.config.um_password)
                            .is_err()
                        {
                            Some("password_fill_failed")
                        } else {
                            None
                        };
                        note_umpass_fields(
                            inner,
                            round,
                            fields,
                            failure.unwrap_or("fill_calls_succeeded"),
                        );
                        if failure.is_some() {
                            return stop_for_manual(inner, round, page, "umpass_prefill_missing");
                        }
                    }
                }
            } else if !trusted_umpass_credential_origin(&url) {
                // A filled form is only submitted on the exact HTTPS SSO origin.
                note_form(
                    inner,
                    round,
                    "manual_takeover",
                    Some(&url),
                    Some("umpass_credentials_untrusted_origin"),
                    FormState {
                        form: FORM_UNTRUSTED_ORIGIN,
                        detail: Some(UNTRUSTED_ORIGIN_HINT),
                        submit_count: auto_submissions,
                    },
                );
                return LoginOutcome::Terminal {
                    reason: "umpass_credentials_untrusted_origin".to_string(),
                };
            } else if let Some(target) = page.one_visible(&UMPASS_SUBMIT_SELECTORS) {
                auto_submissions += 1;
                note_submission(inner, round, Some(&url), auto_submissions);
                last_click_swallowed = match click_and_observe(inner, session, page, &target, &url)
                {
                    ClickOutcome::Cancelled => {
                        return LoginOutcome::Cancelled {
                            reason: "cancelled".to_string(),
                        }
                    }
                    ClickOutcome::Advanced => {
                        idle_deadline = Instant::now() + inner.config.idle_timeout;
                        false
                    }
                    ClickOutcome::Swallowed => true,
                };
                if let Some(outcome) = check_login_window(page, visible) {
                    return outcome;
                }
                if let Some(selector) = umpass_credential_rejected(page) {
                    note_form(
                        inner,
                        round,
                        "manual_takeover",
                        Some(&url),
                        None,
                        FormState {
                            form: FORM_REJECTED,
                            detail: Some(REJECTED_HINT),
                            submit_count: auto_submissions,
                        },
                    );
                    inner.history.note_detail(round, Some(selector));
                    return LoginOutcome::Rejected {
                        reason: "upstream_credential_rejected".to_string(),
                    };
                }
            } else {
                let (form, detail) = if page.any_visible(&UMPASS_SUBMIT_SELECTORS) {
                    (FORM_SUBMIT_AMBIGUOUS, AMBIGUOUS_SUBMIT_HINT)
                } else {
                    (FORM_SUBMIT_UNRECOGNIZED, UNRECOGNIZED_SUBMIT_HINT)
                };
                let settled = stage_seen.entry(stage.clone()).or_insert_with(Instant::now);
                if settled.elapsed() >= inner.config.stage_settle_timeout && manual_reason.is_none()
                {
                    manual_reason = Some("umpass_submit_unrecognized".to_string());
                    note_form(
                        inner,
                        round,
                        "manual_takeover",
                        Some(&url),
                        manual_reason.as_deref(),
                        FormState {
                            form,
                            detail: Some(detail),
                            submit_count: auto_submissions,
                        },
                    );
                }
            }
        }

        let poll = inner
            .config
            .poll_interval
            .min(deadline.saturating_duration_since(Instant::now()));
        if let Err(error) = page.wait(poll) {
            if let Some(outcome) = check_login_window(page, visible) {
                return outcome;
            }
            return LoginOutcome::Transient { reason: error };
        }
    }

    if verifying.is_some() {
        // 本轮截止时仍未核验出结论：凭据保持未发布、未落盘。
        return LoginOutcome::VerificationUnknown {
            reason: "verification_timeout".to_string(),
        };
    }
    // A round that runs out of its total budget reports the real result. A Duo
    // prompt that was still waiting is progress evidence, not a guess that the
    // account holder failed to confirm.
    let reason = manual_reason.unwrap_or_else(|| "timeout".to_string());
    LoginOutcome::Manual {
        reason: reason.clone(),
    }
}

fn note_browser_catalog(inner: &LoginInner, round: u64, valid: bool) {
    let mut state = inner.state.lock().expect("login state poisoned");
    if state.round == round {
        state.browser_catalog_valid = valid;
    }
}

fn note_login_evidence(
    inner: &Arc<LoginInner>,
    round: u64,
    verdict: &str,
    reason: Option<&str>,
    upstream_status: Option<u16>,
) {
    let mut state = inner.state.lock().expect("login state poisoned");
    if state.round != round {
        return;
    }
    let now = profile::epoch_now();
    let last_valid_epoch = if verdict == "valid" {
        Some(now)
    } else {
        state.evidence.last_valid_epoch
    };
    state.evidence = Evidence {
        verdict: Some(verdict.to_string()),
        reason: reason.map(str::to_string),
        upstream_status,
        checked_epoch: Some(now),
        last_valid_epoch,
        source: Some("login".to_string()),
    };
}

fn commit_token(
    inner: &Arc<LoginInner>,
    round: u64,
    session: &Arc<LoginSession>,
    token: &str,
) -> LoginOutcome {
    if session.cancel.load(Ordering::SeqCst) {
        return LoginOutcome::Cancelled {
            reason: "cancelled".to_string(),
        };
    }
    {
        let state = inner.state.lock().expect("login state poisoned");
        if state.round != round {
            return LoginOutcome::Terminal {
                reason: "superseded".to_string(),
            };
        }
    }
    let document = match inner.token_store.save(token, None) {
        Ok(document) => document,
        Err(_) => {
            return LoginOutcome::Terminal {
                reason: "token_save_failed".to_string(),
            }
        }
    };
    inner
        .credentials
        .publish_credential(&document.token, document.api_key.as_deref());
    let now = profile::epoch_now();
    let mut state = inner.state.lock().expect("login state poisoned");
    let upstream_status = state.evidence.upstream_status;
    state.evidence = Evidence {
        verdict: Some("valid".to_string()),
        reason: None,
        upstream_status,
        checked_epoch: Some(now),
        last_valid_epoch: Some(now),
        source: Some("login".to_string()),
    };
    LoginOutcome::Valid
}

fn check_login_window(page: &dyn LoginPage, visible: bool) -> Option<LoginOutcome> {
    if !page.is_closed() {
        return None;
    }
    if visible {
        return Some(LoginOutcome::Cancelled {
            reason: "window_closed".to_string(),
        });
    }
    Some(LoginOutcome::Transient {
        reason: "window_closed".to_string(),
    })
}

fn note_umpass_fields(
    inner: &LoginInner,
    round: u64,
    [account, password]: [CredentialField; 2],
    fallback: &'static str,
) {
    let detail = format!(
        "umpass_form account:{} password:{} fallback:{fallback}",
        account.diagnostic_name(),
        password.diagnostic_name()
    );
    inner.history.note_detail(round, Some(&detail));
}

fn stop_for_manual(
    inner: &Arc<LoginInner>,
    round: u64,
    page: &dyn LoginPage,
    reason: &str,
) -> LoginOutcome {
    let url = page.url();
    note_progress(
        inner,
        round,
        "manual_takeover",
        Some(&url),
        Some(reason),
        None,
    );
    LoginOutcome::Manual {
        reason: format!("{MANUAL_TAKEOVER_REQUIRED_PREFIX}{reason}"),
    }
}

fn note_progress(
    inner: &Arc<LoginInner>,
    round: u64,
    phase: &str,
    origin: Option<&str>,
    manual_reason: Option<&str>,
    detail: Option<&str>,
) {
    let mut state = inner.state.lock().expect("login state poisoned");
    if state.round != round {
        return;
    }
    let previous = state.login.take().unwrap_or_default();
    let origin = origin
        .filter(|origin| !origin.is_empty())
        .map(origin_without_query);
    state.login = Some(LoginProgress {
        phase: Some(phase.to_string()),
        origin,
        manual_reason: manual_reason.map(str::to_string),
        detail: detail.map(str::to_string),
        form: previous.form,
        submit_count: previous.submit_count,
    });
    if let Some(reason) = manual_reason {
        if LOGIN_TERMINAL_MANUAL_REASONS.contains(&reason) {
            state.error = Some(format!("{MANUAL_TAKEOVER_REQUIRED_PREFIX}{reason}"));
        }
    }
    inner.condvar.notify_all();
}

#[derive(Clone, Copy)]
struct FormState<'a> {
    form: &'a str,
    detail: Option<&'a str>,
    submit_count: usize,
}

fn note_form(
    inner: &Arc<LoginInner>,
    round: u64,
    phase: &str,
    origin: Option<&str>,
    manual_reason: Option<&str>,
    form: FormState<'_>,
) {
    let mut state = inner.state.lock().expect("login state poisoned");
    if state.round != round {
        return;
    }
    let origin = origin
        .filter(|origin| !origin.is_empty())
        .map(origin_without_query);
    state.login = Some(LoginProgress {
        phase: Some(phase.to_string()),
        origin,
        manual_reason: manual_reason.map(str::to_string),
        detail: form.detail.map(str::to_string),
        form: Some(form.form.to_string()),
        submit_count: form.submit_count,
    });
    if let Some(reason) = manual_reason {
        if LOGIN_TERMINAL_MANUAL_REASONS.contains(&reason) {
            state.error = Some(format!("{MANUAL_TAKEOVER_REQUIRED_PREFIX}{reason}"));
        }
    }
    inner.history.note_submission(round, form.submit_count);
    inner.condvar.notify_all();
}

fn note_submission(inner: &Arc<LoginInner>, round: u64, origin: Option<&str>, submit_count: usize) {
    note_form(
        inner,
        round,
        "umpass_submitted",
        origin,
        None,
        FormState {
            form: FORM_READY,
            detail: Some(SUBMITTED_HINT),
            submit_count,
        },
    );
}

fn umpass_credential_rejected(page: &dyn LoginPage) -> Option<&'static str> {
    let host = url_host(&page.url());
    if !UMPASS_SSO_HOSTS
        .iter()
        .any(|allowed| host_matches(&host, allowed))
    {
        return None;
    }
    page.first_visible(&UMPASS_ERROR_SELECTORS)
}

enum ClickOutcome {
    Advanced,
    Swallowed,
    Cancelled,
}

fn click_and_observe(
    inner: &Arc<LoginInner>,
    session: &Arc<LoginSession>,
    page: &dyn LoginPage,
    target: &VisibleTarget,
    before_url: &str,
) -> ClickOutcome {
    let _ = page.click(target);
    let deadline = Instant::now() + inner.config.click_observe_timeout;
    while Instant::now() < deadline {
        if session.cancel.load(Ordering::SeqCst) {
            return ClickOutcome::Cancelled;
        }
        if page.url() != before_url {
            return ClickOutcome::Advanced;
        }
        if !page.any_visible(&[target.selector.as_str()]) {
            return ClickOutcome::Advanced;
        }
        if let Err(error) = page.wait(Duration::from_millis(250)) {
            let _ = error;
            return ClickOutcome::Swallowed;
        }
    }
    ClickOutcome::Swallowed
}

fn url_origin(url: &str) -> String {
    let Some((scheme, rest)) = url.split_once("://") else {
        return String::new();
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    if scheme.is_empty() || authority.is_empty() {
        return String::new();
    }
    format!(
        "{}://{}",
        scheme.to_ascii_lowercase(),
        authority.to_ascii_lowercase()
    )
}

fn url_host(url: &str) -> String {
    let Some((_, rest)) = url.split_once("://") else {
        return String::new();
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or_default();
    let host = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    let host = host.split(':').next().unwrap_or(host);
    host.to_ascii_lowercase()
}

fn trusted_umpass_credential_origin(url: &str) -> bool {
    let origin = url_origin(url);
    UMPASS_SSO_HOSTS
        .iter()
        .any(|host| origin == format!("https://{host}") || origin == format!("https://{host}:443"))
}

fn host_matches(host: &str, allowed: &str) -> bool {
    host == allowed || host.ends_with(&format!(".{allowed}"))
}

fn origin_without_query(url: &str) -> String {
    let Some((scheme, rest)) = url.split_once("://") else {
        return String::new();
    };
    let authority_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..authority_end];
    let path = rest[authority_end..]
        .split(['?', '#'])
        .next()
        .unwrap_or_default();
    if scheme.is_empty() || authority.is_empty() {
        return String::new();
    }
    format!(
        "{}://{}{}",
        scheme.to_ascii_lowercase(),
        authority.to_ascii_lowercase(),
        path
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hosts_match_only_the_exact_host_or_a_subdomain() {
        assert!(host_matches("websso.um.edu.mo", "websso.um.edu.mo"));
        assert!(host_matches("login.websso.um.edu.mo", "websso.um.edu.mo"));
        assert!(!host_matches(
            "websso.um.edu.mo.evil.example",
            "websso.um.edu.mo"
        ));
    }

    #[test]
    fn the_url_origin_and_host_ignore_userinfo_and_query() {
        assert_eq!(
            url_origin("https://chat.example/path?code=secret"),
            "https://chat.example"
        );
        assert_eq!(
            url_host("https://user@chat.example:8443/path"),
            "chat.example"
        );
        assert_eq!(
            origin_without_query("https://chat.example/path?code=secret"),
            "https://chat.example/path"
        );
        assert_eq!(origin_without_query("not a url"), "");
    }
}
