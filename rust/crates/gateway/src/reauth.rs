//! Re-authentication trigger, shared session and authentication-wait budget.
//!
//! #63 turns one upstream 401 or one 403 whose text names a missing UM session
//! into at most ONE re-authentication session per logical request. The session is
//! shared by every concurrent trigger, and each request waits for its terminal
//! result inside a 30 second cumulative budget, at most 128 concurrent waiters and
//! with a one second recheck interval. The wait also counts toward the request's
//! 540 second total budget because the caller's absolute deadline bounds it.
//!
//! Rust 8003 assembles the real [`crate::login::LoginService`] as the default
//! provider. [`MissingLoginProvider`] remains the explicit unassembled fallback
//! for a gateway started with a scripted provider; it reports
//! `reauth_unavailable` instead of pretending a browser session ran, and
//! [`BrowserDisabledProvider`] keeps the contract reason `BrowserDisabled`.
//! Tests inject a scripted provider through
//! [`crate::GatewayServer::start_with_reauth`].

use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::config::GatewayConfig;
use crate::error::GatewayError;
use crate::retry::{
    self, is_reauthentication_text, LastAttempt, RetryLimits, RetryOutcome, RetryState,
};
use crate::server::AppState;
use crate::upstream::UpstreamResponse;

pub const DEFAULT_AUTH_WAIT_BUDGET: Duration = Duration::from_secs(30);
pub const DEFAULT_AUTH_WAIT_MAX_CONCURRENT: usize = 128;
pub const DEFAULT_AUTH_WAIT_RECHECK: Duration = Duration::from_secs(1);
pub const DEFAULT_REAUTH_ATTEMPT_WINDOW: Duration = Duration::from_secs(900);
pub const DEFAULT_REAUTH_MAX_ATTEMPTS_PER_WINDOW: usize = 3;
/// A definite next global attempt further away than this is not projected.
pub const AUTH_WAIT_RETRY_AFTER_MAX_SECONDS: u64 = 900;

/// The limits of one re-authentication service.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReauthLimits {
    pub auth_wait_budget: Duration,
    pub auth_wait_max_concurrent: usize,
    pub auth_wait_recheck: Duration,
    pub attempt_window: Duration,
    pub max_attempts_per_window: usize,
}

impl Default for ReauthLimits {
    fn default() -> Self {
        Self {
            auth_wait_budget: DEFAULT_AUTH_WAIT_BUDGET,
            auth_wait_max_concurrent: DEFAULT_AUTH_WAIT_MAX_CONCURRENT,
            auth_wait_recheck: DEFAULT_AUTH_WAIT_RECHECK,
            attempt_window: DEFAULT_REAUTH_ATTEMPT_WINDOW,
            max_attempts_per_window: DEFAULT_REAUTH_MAX_ATTEMPTS_PER_WINDOW,
        }
    }
}

impl ReauthLimits {
    pub fn from_config(config: &GatewayConfig) -> Self {
        Self {
            auth_wait_budget: config.auth_wait_budget,
            auth_wait_max_concurrent: config.auth_wait_max_concurrent,
            auth_wait_recheck: config.auth_wait_recheck,
            attempt_window: config.reauth_attempt_window,
            max_attempts_per_window: config.reauth_max_attempts_per_window,
        }
    }
}

/// The cumulative authentication-wait budget of ONE logical request.
///
/// Only time spent inside [`ReauthenticationService::trigger`] is charged. Normal
/// inference and retry delays between two waits are not part of this budget. The
/// same budget is shared by every hidden retry of one logical request, so a later
/// wait can never reset it. The overall request deadline stays a separate bound.
#[derive(Debug)]
pub struct AuthWaitBudget {
    limit: Duration,
    used: Duration,
    segments: u32,
    segment_started: Option<Instant>,
    deadline: Option<Instant>,
}

impl AuthWaitBudget {
    pub fn new(limit: Duration) -> Self {
        Self {
            limit,
            used: Duration::ZERO,
            segments: 0,
            segment_started: None,
            deadline: None,
        }
    }

    pub fn remaining(&self) -> Duration {
        let mut used = self.used;
        if let Some(started) = self.segment_started {
            used += started.elapsed();
        }
        self.limit.saturating_sub(used)
    }

    /// The absolute instant this request's budget runs out.
    ///
    /// While charged time is below the limit it is the projection from the
    /// active (or most recent) wait segment. Once the limit was crossed it keeps
    /// the FIRST crossing instant so a later segment cannot move the real expiry
    /// forward.
    pub fn deadline(&self) -> Option<Instant> {
        if let Some(started) = self.segment_started {
            if self.used < self.limit {
                return Some(started + (self.limit - self.used));
            }
        }
        self.deadline
    }

    pub fn begin_wait(&mut self) {
        if self.segments == 0 {
            let now = Instant::now();
            self.segment_started = Some(now);
            if self.deadline.is_none() || self.used < self.limit {
                self.deadline = Some(now + self.limit.saturating_sub(self.used));
            }
        }
        self.segments += 1;
    }

    pub fn end_wait(&mut self) {
        if self.segments == 0 {
            return;
        }
        self.segments -= 1;
        if self.segments == 0 {
            if let Some(started) = self.segment_started.take() {
                self.used += started.elapsed();
            }
        }
    }
}

/// The result of one re-authentication session as seen by a request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReauthResult {
    /// A new credential was published; the request may resend once.
    Restored,
    /// The session ran and reported a terminal login failure.
    Failed { reason: String },
    /// The session could not start; no upstream resend is attempted.
    Unavailable {
        reason: String,
        retry_after: Option<u64>,
    },
    /// The request's own total deadline expired before the session finished.
    DeadlineExceeded,
}

/// What one provider reports after running a session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReauthProviderResult {
    Restored,
    Failed {
        reason: String,
    },
    Unavailable {
        reason: String,
        retry_after: Option<u64>,
    },
}

impl From<ReauthProviderResult> for ReauthResult {
    fn from(value: ReauthProviderResult) -> Self {
        match value {
            ReauthProviderResult::Restored => Self::Restored,
            ReauthProviderResult::Failed { reason } => Self::Failed { reason },
            ReauthProviderResult::Unavailable {
                reason,
                retry_after,
            } => Self::Unavailable {
                reason,
                retry_after,
            },
        }
    }
}

/// The seam a real browser login plugs into.
pub trait ReauthenticationProvider: Send + Sync {
    /// Whether a browser login can be attempted at all.
    fn browser_available(&self) -> bool {
        true
    }

    /// The reason reported when `browser_available` is false.
    fn unavailable_reason(&self) -> &'static str {
        "BrowserDisabled"
    }

    /// Run one re-authentication session, bounded by `deadline`.
    fn reauthenticate(&self, deadline: Instant) -> ReauthProviderResult;
}

/// An explicit provider for a gateway assembled without a login executor.
#[derive(Debug, Default)]
pub struct MissingLoginProvider;

impl ReauthenticationProvider for MissingLoginProvider {
    fn browser_available(&self) -> bool {
        false
    }

    fn unavailable_reason(&self) -> &'static str {
        "reauth_unavailable"
    }

    fn reauthenticate(&self, _deadline: Instant) -> ReauthProviderResult {
        ReauthProviderResult::Unavailable {
            reason: self.unavailable_reason().to_string(),
            retry_after: None,
        }
    }
}

/// The explicit `--no-browser` mode.
#[derive(Debug, Default)]
pub struct BrowserDisabledProvider;

impl ReauthenticationProvider for BrowserDisabledProvider {
    fn browser_available(&self) -> bool {
        false
    }

    fn unavailable_reason(&self) -> &'static str {
        "BrowserDisabled"
    }

    fn reauthenticate(&self, _deadline: Instant) -> ReauthProviderResult {
        ReauthProviderResult::Unavailable {
            reason: self.unavailable_reason().to_string(),
            retry_after: None,
        }
    }
}

struct SessionState {
    result: Mutex<Option<ReauthResult>>,
}

struct ServiceState {
    session: Option<Arc<SessionState>>,
    attempts: VecDeque<Instant>,
    waiters: usize,
}

struct ServiceInner {
    provider: Arc<dyn ReauthenticationProvider>,
    limits: ReauthLimits,
    state: Mutex<ServiceState>,
    condvar: Condvar,
}

/// The in-process shared re-authentication session and its admission budgets.
pub struct ReauthenticationService {
    inner: Arc<ServiceInner>,
}

impl ReauthenticationService {
    pub fn new(provider: Arc<dyn ReauthenticationProvider>, limits: ReauthLimits) -> Self {
        Self {
            inner: Arc::new(ServiceInner {
                provider,
                limits,
                state: Mutex::new(ServiceState {
                    session: None,
                    attempts: VecDeque::new(),
                    waiters: 0,
                }),
                condvar: Condvar::new(),
            }),
        }
    }

    pub fn from_config(
        config: &GatewayConfig,
        provider: Arc<dyn ReauthenticationProvider>,
    ) -> Self {
        Self::new(provider, ReauthLimits::from_config(config))
    }

    /// Join or start the ONE shared session and wait for its terminal result.
    ///
    /// The caller's `budget` is charged for the whole call, so the wait is
    /// bounded by both this request's cumulative auth budget and the request's
    /// total deadline.
    pub fn trigger(&self, budget: &mut AuthWaitBudget, request_deadline: Instant) -> ReauthResult {
        if Instant::now() >= request_deadline {
            return ReauthResult::DeadlineExceeded;
        }
        if !self.inner.provider.browser_available() {
            return ReauthResult::Unavailable {
                reason: self.inner.provider.unavailable_reason().to_string(),
                retry_after: None,
            };
        }
        if budget.remaining().is_zero() {
            return ReauthResult::Unavailable {
                reason: "auth_wait_timeout".to_string(),
                retry_after: None,
            };
        }

        let session = {
            let mut state = self.inner.state.lock().expect("reauth state poisoned");
            if let Some(session) = state.session.as_ref() {
                Arc::clone(session)
            } else {
                let now = Instant::now();
                let cutoff = now.checked_sub(self.inner.limits.attempt_window);
                while state
                    .attempts
                    .front()
                    .is_some_and(|oldest| cutoff.is_some_and(|cutoff| *oldest <= cutoff))
                {
                    state.attempts.pop_front();
                }
                if state.attempts.len() >= self.inner.limits.max_attempts_per_window {
                    let retry_after = state.attempts.front().map(|oldest| {
                        let wait = (*oldest + self.inner.limits.attempt_window)
                            .saturating_duration_since(now);
                        wait.as_secs().clamp(1, AUTH_WAIT_RETRY_AFTER_MAX_SECONDS)
                    });
                    return ReauthResult::Unavailable {
                        reason: "global_attempt_cap".to_string(),
                        retry_after,
                    };
                }
                state.attempts.push_back(now);
                let session = Arc::new(SessionState {
                    result: Mutex::new(None),
                });
                state.session = Some(Arc::clone(&session));
                let inner = Arc::clone(&self.inner);
                let worker_session = Arc::clone(&session);
                thread::spawn(move || {
                    let outcome = inner
                        .provider
                        .reauthenticate(Instant::now() + inner.limits.auth_wait_budget);
                    {
                        let mut result = worker_session
                            .result
                            .lock()
                            .expect("reauth session poisoned");
                        *result = Some(outcome.into());
                    }
                    let mut state = inner.state.lock().expect("reauth state poisoned");
                    if state
                        .session
                        .as_ref()
                        .is_some_and(|current| Arc::ptr_eq(current, &worker_session))
                    {
                        state.session = None;
                    }
                    inner.condvar.notify_all();
                });
                session
            }
        };

        {
            let mut state = self.inner.state.lock().expect("reauth state poisoned");
            if state.waiters >= self.inner.limits.auth_wait_max_concurrent {
                return ReauthResult::Unavailable {
                    reason: "auth_wait_capacity".to_string(),
                    retry_after: None,
                };
            }
            state.waiters += 1;
        }

        budget.begin_wait();
        let result = self.wait_for_session(&session, budget, request_deadline);
        budget.end_wait();

        {
            let mut state = self.inner.state.lock().expect("reauth state poisoned");
            state.waiters = state.waiters.saturating_sub(1);
            self.inner.condvar.notify_all();
        }
        result
    }

    fn wait_for_session(
        &self,
        session: &Arc<SessionState>,
        budget: &AuthWaitBudget,
        request_deadline: Instant,
    ) -> ReauthResult {
        let budget_deadline = budget
            .deadline()
            .unwrap_or_else(|| Instant::now() + budget.remaining());
        let wait_deadline = request_deadline.min(budget_deadline);
        let mut state = self.inner.state.lock().expect("reauth state poisoned");
        loop {
            let result = session
                .result
                .lock()
                .expect("reauth session poisoned")
                .clone();
            if let Some(result) = result {
                return result;
            }
            let now = Instant::now();
            if now >= wait_deadline {
                if budget_deadline <= request_deadline {
                    return ReauthResult::Unavailable {
                        reason: "auth_wait_timeout".to_string(),
                        retry_after: None,
                    };
                }
                return ReauthResult::DeadlineExceeded;
            }
            let timeout = (wait_deadline - now).min(self.inner.limits.auth_wait_recheck);
            let (guard, _) = self
                .inner
                .condvar
                .wait_timeout(state, timeout)
                .expect("reauth state poisoned");
            state = guard;
        }
    }
}

/// What one non-200 attempt does next.
#[derive(Debug)]
pub enum StatusAction {
    /// The response was consumed and the request should be resent.
    Retry,
    /// The response was consumed and this is the final error.
    Final {
        error: GatewayError,
        status: Option<u16>,
        deadline_exceeded: bool,
    },
}

/// Apply the retry matrix and the #63 re-authentication trigger to one non-200
/// upstream response.
pub fn handle_non_success(
    state: &AppState,
    retry_state: &mut RetryState,
    last: &mut LastAttempt,
    mut response: UpstreamResponse,
    elapsed: Duration,
    deadline: Instant,
    budget: &mut AuthWaitBudget,
) -> StatusAction {
    let status = response.status;
    let retry_after = response.headers.get("retry-after").cloned();
    let mut text = retry::needs_body_for_classification(status).then(|| response.read_error_text());
    last.record_status(status, text.clone(), retry_after.clone());
    let plan = retry_state.plan(
        RetryOutcome::Status {
            status,
            headers: &response.headers,
            body_text: text.as_deref(),
        },
        &RetryLimits::from_config(&state.config),
        deadline,
        elapsed,
    );
    if let Some(plan) = plan {
        drop(response);
        if plan.reauthentication.is_some() {
            return match state.reauth.trigger(budget, deadline) {
                ReauthResult::Restored => {
                    retry_state.record_reauth_session();
                    StatusAction::Retry
                }
                ReauthResult::Failed { reason } => {
                    retry_state.record_reauth_session();
                    StatusAction::Final {
                        error: reauthentication_required(reason),
                        status: Some(status),
                        deadline_exceeded: false,
                    }
                }
                ReauthResult::Unavailable {
                    reason,
                    retry_after,
                } => StatusAction::Final {
                    error: reauthentication_unavailable(reason, retry_after),
                    status: Some(status),
                    deadline_exceeded: false,
                },
                ReauthResult::DeadlineExceeded => StatusAction::Final {
                    error: last.deadline_error(),
                    status: last.status(),
                    deadline_exceeded: true,
                },
            };
        }
        if !retry::sleep_with_deadline(plan.delay, deadline) {
            return StatusAction::Final {
                error: last.deadline_error(),
                status: last.status(),
                deadline_exceeded: true,
            };
        }
        return StatusAction::Retry;
    }
    if text.is_none() && status != 401 {
        text = Some(response.read_error_text());
        last.record_status(status, text, retry_after);
    }
    StatusAction::Final {
        error: final_error(last, retry_state),
        status: Some(status),
        deadline_exceeded: false,
    }
}

/// The final error of one logical request after the retry matrix stopped.
///
/// A 403 authentication-text response is `reauthentication_failed` only after a
/// session actually ran; otherwise it stays the ordinary 403
/// `upstream_forbidden`/`reauthentication_required` mapping.
pub fn final_error(last: &LastAttempt, retry_state: &RetryState) -> GatewayError {
    let Some(status) = last.status() else {
        return last.error();
    };
    let text = last.text();
    let retry_after = last.retry_after();
    if status == 403 && text.is_some_and(is_reauthentication_text) && retry_state.reauth_attempted()
    {
        let mut detail = Map::new();
        detail.insert("reauthentication_attempted".to_string(), json!(true));
        detail.insert("upstream_status".to_string(), json!(403));
        if let Some(message) = text
            .map(crate::error::bounded_upstream_message)
            .filter(|message| !message.is_empty())
        {
            detail.insert("upstream_message".to_string(), json!(message));
        }
        let mut error = GatewayError::reauthentication_failed()
            .with_upstream_status(403)
            .with_detail(Value::Object(detail));
        if let Some(retry_after) = retry_after.filter(|value| !value.is_empty()) {
            error = error.with_retry_after(retry_after);
        }
        return error;
    }
    GatewayError::upstream_status_error(status, text, retry_after)
}

fn reauthentication_required(reason: String) -> GatewayError {
    let mut error = GatewayError::reauthentication_required(Some("upstream_rejected"));
    if !reason.is_empty() {
        error = error.with_reason(reason);
    }
    error
}

fn reauthentication_unavailable(reason: String, retry_after: Option<u64>) -> GatewayError {
    let mut error = reauthentication_required(reason);
    if let Some(retry_after) = retry_after {
        error = error.with_retry_after(retry_after.to_string());
    }
    error
}
