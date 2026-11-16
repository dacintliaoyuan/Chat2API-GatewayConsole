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

use crate::config::GatewayConfig;
use crate::reauth::{ReauthProviderResult, ReauthenticationProvider};

use self::profile::{acquire_profile_lock, manual_display, visible_login_supported};
use self::token::TokenStore;

pub use self::browser::{
    BrowserUnavailable, LaunchRequest, LoginBrowser, LoginBrowserFactory, LoginFailure, LoginPage,
    SystemBrowserFactory, VisibleTarget,
};
pub use self::probe::{CredentialProbe, CredentialVerdict, HttpCredentialProbe};
pub use self::profile::DisplayEnvironment;
pub use self::token::{TokenDocument, TokenStore as TokenStoreSeam};

pub const MANUAL_TAKEOVER_REQUIRED_PREFIX: &str = "manual_takeover_required:";
pub const LOGIN_TERMINAL_MANUAL_REASONS: [&str; 3] = [
    "umpass_mfa_required",
    "umpass_prefill_missing",
    "unrecognized_page",
];
pub const DEFAULT_LOGIN_TIMEOUT: Duration = Duration::from_secs(1800);
pub const DEFAULT_LOGIN_IDLE_TIMEOUT: Duration = Duration::from_secs(300);
pub const DEFAULT_LOGIN_POLL_INTERVAL: Duration = Duration::from_secs(1);
pub const DEFAULT_CLICK_OBSERVE_TIMEOUT: Duration = Duration::from_secs(8);
pub const DEFAULT_STAGE_SETTLE_TIMEOUT: Duration = Duration::from_secs(20);
pub const DEFAULT_MAX_CLICKS_PER_STAGE: usize = 2;
pub const DEFAULT_NAVIGATION_ATTEMPTS: usize = 2;
pub const DEFAULT_MAX_SESSIONS_PER_ROUND: usize = 6;
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

const AUTH_LABELS: [(&str, &str); 5] = [
    ("valid", "认证有效"),
    ("needs_login", "需要登录"),
    ("logging_in", "登录中"),
    ("failed", "登录失败"),
    ("cancelled", "已取消登录"),
];

/// The upstream authentication state the console reads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuthState {
    Valid,
    NeedsLogin,
    LoggingIn,
    Failed,
    Cancelled,
}

impl AuthState {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Valid => "valid",
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
            | Self::Rejected { reason }
            | Self::Unavailable { reason }
            | Self::Busy { reason }
            | Self::Transient { reason }
            | Self::Cancelled { reason }
            | Self::Terminal { reason } => Some(reason),
        }
    }
}

/// The token/API-key sink a successful login publishes into.
pub trait CredentialSink: Send + Sync {
    fn publish_credential(&self, token: &str, api_key: Option<&str>);
}

/// Resolved login paths and budgets.
#[derive(Debug, Clone)]
pub struct LoginConfig {
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
    /// UMPASS account and password the login executor may type into an empty
    /// ADFS form; both empty keeps the historical manual prefill path.
    pub um_account: String,
    pub um_password: String,
}

impl LoginConfig {
    pub fn from_gateway(config: &GatewayConfig) -> Self {
        Self {
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
            um_account: std::env::var("CHAT2API_UM_ACCOUNT")
                .unwrap_or_default()
                .trim()
                .to_string(),
            um_password: std::env::var("CHAT2API_UM_PASSWORD")
                .unwrap_or_default()
                .trim()
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
}

impl LoginProgress {
    fn as_json(&self) -> Value {
        json!({
            "phase": self.phase,
            "origin": self.origin,
            "manual_reason": self.manual_reason,
            "detail": self.detail,
        })
    }
}

#[derive(Debug, Clone, Default)]
struct Recovery {
    state: String,
    round: u64,
    session_starts: usize,
    max_sessions: usize,
    next_attempt_epoch: Option<f64>,
    last_failure_epoch: Option<f64>,
    last_failure_reason: Option<String>,
    pause_reason: Option<String>,
    visible: bool,
}

impl Recovery {
    fn as_json(&self) -> Value {
        json!({
            "state": self.state,
            "round": self.round,
            "session_starts": self.session_starts,
            "max_sessions": self.max_sessions,
            "next_attempt_epoch": self.next_attempt_epoch,
            "last_failure_epoch": self.last_failure_epoch,
            "last_failure_reason": self.last_failure_reason,
            "pause_reason": self.pause_reason,
            "visible": self.visible,
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
}

struct LoginSession {
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
    state: Mutex<LoginState>,
    condvar: Condvar,
    attempts: Mutex<VecDeque<Instant>>,
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
        Self {
            inner: Arc::new(LoginInner {
                config,
                factory,
                probe,
                credentials,
                token_store,
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
                }),
                condvar: Condvar::new(),
                attempts: Mutex::new(VecDeque::new()),
            }),
        }
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

    /// The console-visible authentication status, with the same keys as Python.
    pub fn status(&self) -> Value {
        let state = self.inner.state.lock().expect("login state poisoned");
        status_from_locked(&state)
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
                self.inner.condvar.notify_all();
                return status_from_locked(&state);
            }
            if active_round(&state) {
                if visible {
                    state.recovery.visible = true;
                }
                begin_login_window(&mut state, &self.inner.config);
                return status_from_locked(&state);
            }
            if state.auth_state == AuthState::Failed || state.auth_state == AuthState::Cancelled {
                state.error = None;
                state.primary = None;
                state.login = None;
            }
            begin_login_window(&mut state, &self.inner.config);
            if let Err(retry_after) = self.inner.allow_start() {
                state.auth_state = AuthState::NeedsLogin;
                state.error = Some("reauth_refused:global_attempt_cap".to_string());
                state.recovery.state = "waiting".to_string();
                state.recovery.next_attempt_epoch = Some(profile::epoch_now() + retry_after as f64);
                state.recovery.pause_reason = None;
                state.gate = Some(self.inner.refused_gate("global_attempt_cap"));
                self.inner.condvar.notify_all();
                return status_from_locked(&state);
            }
            begin_round_locked(&self.inner, &mut state, visible);
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
        self.inner.condvar.notify_all();
        json!({
            "cancelled": true,
            "state": "cancelled",
            "detail": "已取消本轮登录；服务继续运行，普通请求不会自动重开",
        })
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
        self.inner.condvar.notify_all();
    }
}

impl ReauthenticationProvider for LoginService {
    fn browser_available(&self) -> bool {
        !self.inner.config.browser_disabled
    }

    fn unavailable_reason(&self) -> &'static str {
        "BrowserDisabled"
    }

    fn reauthenticate(&self, deadline: Instant) -> ReauthProviderResult {
        let session = {
            let mut state = self.inner.state.lock().expect("login state poisoned");
            if self.inner.config.browser_disabled {
                return ReauthProviderResult::Unavailable {
                    reason: "BrowserDisabled".to_string(),
                    retry_after: None,
                };
            }
            if state.auth_state == AuthState::Failed || state.auth_state == AuthState::Cancelled {
                let reason = state
                    .error
                    .clone()
                    .unwrap_or_else(|| "login_failed".to_string());
                return ReauthProviderResult::Failed { reason };
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
                begin_round_locked(&self.inner, &mut state, false)
            }
        };
        let mut result = session.result.lock().expect("login session poisoned");
        loop {
            if let Some(outcome) = result.clone() {
                return outcome_to_provider(outcome);
            }
            let now = Instant::now();
            if now >= deadline {
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
        LoginOutcome::Transient { reason } | LoginOutcome::Unavailable { reason } => {
            ReauthProviderResult::Unavailable {
                reason,
                retry_after: None,
            }
        }
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
) -> Arc<LoginSession> {
    state.round += 1;
    state.recovery.round = state.round;
    state.recovery.state = "running".to_string();
    state.recovery.session_starts = 1;
    state.recovery.next_attempt_epoch = None;
    state.recovery.last_failure_epoch = None;
    state.recovery.last_failure_reason = None;
    state.recovery.pause_reason = None;
    state.recovery.visible = visible;
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
        cancel: AtomicBool::new(false),
        result: Mutex::new(None),
        condvar: Condvar::new(),
    });
    state.session = Some(Arc::clone(&session));
    let worker = Arc::clone(inner);
    let round = state.round;
    let session_worker = Arc::clone(&session);
    thread::spawn(move || run_round(worker, round, session_worker));
    session
}

fn active_round(state: &LoginState) -> bool {
    state.recovery.state == "running" || state.recovery.state == "waiting"
}

fn begin_login_window(state: &mut LoginState, config: &LoginConfig) {
    if state
        .login_window
        .as_ref()
        .is_some_and(|window| profile::epoch_now() < window.deadline_epoch)
        && active_round(state)
    {
        return;
    }
    let started = profile::epoch_now();
    let window_s = config.login_timeout.as_secs_f64();
    state.login_window = Some(LoginWindow {
        window_s,
        deadline_epoch: started + window_s,
    });
}

fn status_from_locked(state: &LoginState) -> Value {
    let manual_reason = state
        .login
        .as_ref()
        .and_then(|login| login.manual_reason.clone());
    let mut label = state.auth_state.label().to_string();
    if let Some(reason) = &manual_reason {
        label = format!("{label}（需人工：{reason}）");
    } else if state.evidence.verdict.as_deref() == Some("unknown") {
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
        "watch": {
            "thread_alive": false,
            "last_round_epoch": Value::Null,
            "last_round_outcome": Value::Null,
            "next_round_due": Value::Null,
            "rounds": 0,
        },
        "recovery": state.recovery.as_json(),
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
            outcome => {
                finish(&inner, round, &session, outcome);
                return;
            }
        }
    }
}

fn wait_for_retry(inner: &Arc<LoginInner>, session: &Arc<LoginSession>, wait: Duration) -> bool {
    let deadline = Instant::now() + wait;
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

fn session_finish(session: &Arc<LoginSession>, outcome: LoginOutcome) {
    *session.result.lock().expect("login session poisoned") = Some(outcome);
    session.condvar.notify_all();
}

fn finish(inner: &Arc<LoginInner>, round: u64, session: &Arc<LoginSession>, outcome: LoginOutcome) {
    session_finish(session, outcome.clone());
    let mut state = inner.state.lock().expect("login state poisoned");
    if state.round != round {
        return;
    }
    match outcome {
        LoginOutcome::Valid => {
            state.auth_state = AuthState::Valid;
            state.error = None;
            state.primary = None;
            state.login = None;
            state.recovery.state = "idle".to_string();
            state.recovery.next_attempt_epoch = None;
            state.recovery.last_failure_epoch = None;
            state.recovery.last_failure_reason = None;
            state.recovery.pause_reason = None;
        }
        LoginOutcome::Cancelled { reason } => {
            state.auth_state = AuthState::Cancelled;
            state.error = Some(reason);
            state.primary = None;
            state.login = None;
            state.recovery.state = "exhausted".to_string();
            state.recovery.next_attempt_epoch = None;
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
            state.recovery.next_attempt_epoch = None;
            state.recovery.last_failure_epoch = Some(profile::epoch_now());
            state.recovery.last_failure_reason = Some(reason.clone());
            state.recovery.pause_reason = Some(reason);
        }
        LoginOutcome::Rejected { reason }
        | LoginOutcome::Unavailable { reason }
        | LoginOutcome::Terminal { reason } => {
            state.auth_state = AuthState::Failed;
            state.error = Some(reason.clone());
            state.primary = None;
            state.login = None;
            state.recovery.state = "exhausted".to_string();
            state.recovery.next_attempt_epoch = None;
            state.recovery.last_failure_epoch = Some(profile::epoch_now());
            state.recovery.last_failure_reason = Some(reason.clone());
            state.recovery.pause_reason = Some(reason);
        }
    }
    state.session = None;
    inner.condvar.notify_all();
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
    let _ = navigation_error;

    let deadline = Instant::now() + inner.config.login_timeout;
    let mut idle_deadline = Instant::now() + inner.config.idle_timeout;
    let mut last_url = page.url();
    let mut last_token: Option<String> = None;
    let mut rejected_tokens: HashSet<String> = HashSet::new();
    let mut clicks = HashMap::new();
    let mut stage_seen: HashMap<String, Instant> = HashMap::new();
    let mut manual_reason: Option<String> = None;
    let mut stage = String::new();

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
            }
            if !rejected_tokens.contains(token) {
                let verdict = inner.probe.classify(token);
                if verdict.verdict == "valid" {
                    return LoginOutcome::Token {
                        token: token.clone(),
                    };
                }
                if verdict.verdict == "rejected" {
                    rejected_tokens.insert(token.clone());
                    let _ = page.drop_token();
                    let _ = page.reload();
                    continue;
                }
                manual_reason = Some("credential_probe_inconclusive".to_string());
                note_progress(
                    inner,
                    round,
                    "manual_takeover",
                    Some(&page.url()),
                    Some("credential_probe_inconclusive"),
                );
            }
        }

        let url = page.url();
        if url != last_url {
            last_url = url.clone();
            idle_deadline = Instant::now() + inner.config.idle_timeout;
        }
        if let Some(outcome) = check_login_window(page, visible) {
            return outcome;
        }
        let host = url_host(&url);
        stage = if !url.is_empty() && url_origin(&url) == url_origin(&inner.config.base_url) {
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
            note_progress(inner, round, "awaiting_duo", Some(&url), None);
        } else if stage == "unknown" {
            stage_seen.entry(stage.clone()).or_insert_with(Instant::now);
            return stop_for_manual(inner, round, page, "unrecognized_page");
        } else if manual_reason.is_some() {
            // The automatic flow already stopped; keep polling for a token.
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
                note_progress(inner, round, "openwebui_clicked", Some(&url), None);
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
                    );
                }
            }
        } else if stage == "umpass" {
            let count = clicks.entry(stage.clone()).or_insert(0);
            if *count >= inner.config.max_clicks_per_stage {
                manual_reason = Some("umpass_click_swallowed".to_string());
                note_progress(
                    inner,
                    round,
                    "manual_takeover",
                    Some(&url),
                    manual_reason.as_deref(),
                );
            } else if page.any_visible(&UMPASS_MFA_SELECTORS) {
                return stop_for_manual(inner, round, page, "umpass_mfa_required");
            } else if !page.field_filled(&UMPASS_ACCOUNT_SELECTORS)
                || !page.field_filled(&UMPASS_PASSWORD_SELECTORS)
            {
                if inner.config.um_account.is_empty() || inner.config.um_password.is_empty() {
                    return stop_for_manual(inner, round, page, "umpass_prefill_missing");
                }
                let filled = page
                    .fill(&UMPASS_ACCOUNT_SELECTORS, &inner.config.um_account)
                    .and_then(|_| page.fill(&UMPASS_PASSWORD_SELECTORS, &inner.config.um_password));
                if filled.is_err() {
                    return stop_for_manual(inner, round, page, "umpass_prefill_missing");
                }
            } else if let Some(target) = page.one_visible(&UMPASS_SUBMIT_SELECTORS) {
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
                note_progress(inner, round, "umpass_submitted", Some(&url), None);
                if let Some(outcome) = check_login_window(page, visible) {
                    return outcome;
                }
                if umpass_credential_rejected(page) {
                    return LoginOutcome::Rejected {
                        reason: "upstream_credential_rejected".to_string(),
                    };
                }
            } else {
                let settled = stage_seen.entry(stage.clone()).or_insert_with(Instant::now);
                if settled.elapsed() >= inner.config.stage_settle_timeout {
                    manual_reason = Some("umpass_submit_unrecognized".to_string());
                    note_progress(
                        inner,
                        round,
                        "manual_takeover",
                        Some(&url),
                        manual_reason.as_deref(),
                    );
                }
            }
        }

        if let Err(error) = page.wait(inner.config.poll_interval) {
            if let Some(outcome) = check_login_window(page, visible) {
                return outcome;
            }
            return LoginOutcome::Transient { reason: error };
        }
    }

    if manual_reason.is_none() && stage == "duo" {
        manual_reason = Some("duo_prompt_waiting".to_string());
    }
    let reason = manual_reason.unwrap_or_else(|| "timeout".to_string());
    LoginOutcome::Manual {
        reason: reason.clone(),
    }
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
    if inner.token_store.save(token, None).is_err() {
        return LoginOutcome::Terminal {
            reason: "token_save_failed".to_string(),
        };
    }
    inner.credentials.publish_credential(token, None);
    let now = profile::epoch_now();
    let mut state = inner.state.lock().expect("login state poisoned");
    state.evidence = Evidence {
        verdict: Some("valid".to_string()),
        reason: None,
        upstream_status: None,
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

fn stop_for_manual(
    inner: &Arc<LoginInner>,
    round: u64,
    page: &dyn LoginPage,
    reason: &str,
) -> LoginOutcome {
    let url = page.url();
    note_progress(inner, round, "manual_takeover", Some(&url), Some(reason));
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
        detail: None,
    });
    if let Some(reason) = manual_reason {
        if LOGIN_TERMINAL_MANUAL_REASONS.contains(&reason) {
            state.error = Some(format!("{MANUAL_TAKEOVER_REQUIRED_PREFIX}{reason}"));
        }
    }
    inner.condvar.notify_all();
}

fn umpass_credential_rejected(page: &dyn LoginPage) -> bool {
    let host = url_host(&page.url());
    if !UMPASS_SSO_HOSTS
        .iter()
        .any(|allowed| host_matches(&host, allowed))
    {
        return false;
    }
    if !page.any_visible(&UMPASS_ERROR_SELECTORS) {
        return false;
    }
    page.field_filled(&UMPASS_PASSWORD_SELECTORS)
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
