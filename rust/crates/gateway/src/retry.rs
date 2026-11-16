//! Shared retry matrix for one upstream candidate.
//!
//! The gateway retries the same upstream request only while the client has not
//! received the first substantive UM output. The matrix follows
//! `docs/adr/0003-protocol-aware-error-contract.md` and spec #58:
//!
//! * 408, 429, 500, 502, 503, 504, 529, 520-524, transport failures and
//!   gateway waits use the full fast/slow budget;
//! * 402 and 403 are probed once each, counted separately from the fast budget;
//! * 400/422 are probed once only when the upstream text names a transient
//!   condition;
//! * 404/405/409/413/415 and 401 never enter transient retry.
//!
//! `Retry-After` at most 8 seconds replaces the local backoff; a longer value
//! stops retrying this candidate and is returned to the client. `X-Should-Retry:
//! false` also stops. Probe retries consume the twelve physical attempts but not
//! the five fast or two slow retries.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, Utc};

use crate::config::GatewayConfig;
use crate::error::{ErrorProtocol, GatewayError};
use crate::http::Response;
use crate::upstream::UpstreamFailure;

/// A `Retry-After` above this bound stops retrying the same candidate.
///
/// This mirrors Magpie's `longestPause=8s`: the value is not a maximum sleep
/// before retrying, it is the point where the wait is handed to the client.
pub const RETRY_AFTER_MAX: Duration = Duration::from_secs(8);

/// Upstream text that makes a 400/422 worth one conditional probe.
pub const TRANSIENT_TEXTS: [&str; 8] = [
    "rate limit",
    "too many requests",
    "overloaded",
    "try again",
    "temporarily",
    "unavailable",
    "cloudflare",
    "challenge",
];

/// Upstream text that marks a 403 as a missing/expired UM session.
///
/// #63 owns the actual re-authentication session; this module only classifies the
/// trigger so the retry loop has one clear interception point.
pub const REAUTHENTICATION_TEXTS: [&str; 5] = [
    "not authenticated",
    "sign in",
    "session expired",
    "authentication required",
    "please log in",
];

/// The retry limits of one logical request.
#[derive(Debug, Clone, Copy)]
pub struct RetryLimits {
    pub max_fast_retries: u32,
    pub max_slow_retries: u32,
    pub max_physical_attempts: u32,
    pub retry_delay_base: Duration,
    pub slow_retry_threshold: Duration,
}

impl RetryLimits {
    pub fn from_config(config: &GatewayConfig) -> Self {
        Self {
            max_fast_retries: config.max_fast_retries,
            max_slow_retries: config.max_slow_retries,
            max_physical_attempts: config.max_physical_attempts,
            retry_delay_base: config.retry_delay_base,
            slow_retry_threshold: config.slow_retry_threshold,
        }
    }
}

/// How one status participates in the retry matrix.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RetryClass {
    /// A same-candidate transient failure with the full fast/slow budget.
    Full,
    /// A one-shot probe outside the fast/slow counters.
    Probe(ProbeKind),
    /// A status that is returned to the client as-is.
    None,
}

/// The probe class of one status.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeKind {
    /// 402: the upstream may be reporting a transient quota condition.
    Quota,
    /// 403 with ordinary permission text.
    Forbidden,
    /// 403 whose text names a missing/expired UM session.
    ForbiddenAuthentication,
    /// 400/422 whose text names a transient condition.
    Conditional,
}

/// The #63 interception point for a 403 authentication-text probe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReauthenticationTrigger {
    ForbiddenAuthenticationText,
}

/// One attempt outcome that may be retried.
#[derive(Debug, Clone, Copy)]
pub enum RetryOutcome<'a> {
    /// A connection/DNS/TLS/proxy failure or a gateway wait timeout.
    Transport,
    /// A stream failed before the 200 commit.
    Stream,
    /// A complete non-200 upstream response.
    Status {
        status: u16,
        headers: &'a HashMap<String, String>,
        /// Present when the status classification needs the upstream text.
        body_text: Option<&'a str>,
    },
}

/// The retry action for one attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RetryPlan {
    pub delay: Duration,
    /// #63 starts the shared re-authentication session here and resends once.
    pub reauthentication: Option<ReauthenticationTrigger>,
}

/// The retry counters of one logical request.
#[derive(Debug, Default)]
pub struct RetryState {
    pub fast_retries: u32,
    pub slow_retries: u32,
    pub physical_attempts: u32,
    quota_probes: u32,
    forbidden_probes: u32,
    conditional_probes: u32,
}

impl RetryState {
    /// Record one attempt that reached the upstream client.
    pub fn record_attempt(&mut self) {
        self.physical_attempts += 1;
    }

    /// Decide whether one attempt may be retried and how long to wait first.
    pub fn plan(
        &mut self,
        outcome: RetryOutcome<'_>,
        limits: &RetryLimits,
        deadline: Instant,
        elapsed: Duration,
    ) -> Option<RetryPlan> {
        if Instant::now() >= deadline || self.physical_attempts >= limits.max_physical_attempts {
            return None;
        }
        match outcome {
            RetryOutcome::Transport | RetryOutcome::Stream => {
                let slow = elapsed >= limits.slow_retry_threshold;
                if !self.transient_available(limits, slow) {
                    return None;
                }
                self.consume_transient(slow);
                Some(RetryPlan {
                    delay: self.transient_delay(limits, slow),
                    reauthentication: None,
                })
            }
            RetryOutcome::Status {
                status,
                headers,
                body_text,
            } => {
                if stop_retrying(headers) {
                    return None;
                }
                let retry_after = parse_retry_after(headers.get("retry-after").map(String::as_str));
                if retry_after.is_some_and(|delay| delay > RETRY_AFTER_MAX) {
                    return None;
                }
                match classify_status(status, body_text) {
                    RetryClass::Full => {
                        let slow = elapsed >= limits.slow_retry_threshold;
                        if !self.transient_available(limits, slow) {
                            return None;
                        }
                        self.consume_transient(slow);
                        Some(RetryPlan {
                            delay: retry_after
                                .unwrap_or_else(|| self.transient_delay(limits, slow)),
                            reauthentication: None,
                        })
                    }
                    RetryClass::Probe(kind) => {
                        if !self.probe_available(kind) {
                            return None;
                        }
                        self.consume_probe(kind);
                        let slow = elapsed >= limits.slow_retry_threshold;
                        Some(RetryPlan {
                            delay: retry_after.unwrap_or_else(|| self.probe_delay(limits, slow)),
                            reauthentication: (kind == ProbeKind::ForbiddenAuthentication)
                                .then_some(ReauthenticationTrigger::ForbiddenAuthenticationText),
                        })
                    }
                    RetryClass::None => None,
                }
            }
        }
    }

    fn transient_available(&self, limits: &RetryLimits, slow: bool) -> bool {
        self.fast_retries < limits.max_fast_retries
            && (!slow || self.slow_retries < limits.max_slow_retries)
    }

    fn consume_transient(&mut self, slow: bool) {
        self.fast_retries += 1;
        if slow {
            self.slow_retries += 1;
        }
    }

    fn probe_available(&self, kind: ProbeKind) -> bool {
        match kind {
            ProbeKind::Quota => self.quota_probes < 1,
            ProbeKind::Forbidden | ProbeKind::ForbiddenAuthentication => self.forbidden_probes < 1,
            ProbeKind::Conditional => self.conditional_probes < 1,
        }
    }

    fn consume_probe(&mut self, kind: ProbeKind) {
        match kind {
            ProbeKind::Quota => self.quota_probes += 1,
            ProbeKind::Forbidden | ProbeKind::ForbiddenAuthentication => self.forbidden_probes += 1,
            ProbeKind::Conditional => self.conditional_probes += 1,
        }
    }

    fn transient_delay(&self, limits: &RetryLimits, slow: bool) -> Duration {
        if slow {
            return slow_delay(limits);
        }
        let exponent = self.fast_retries.saturating_sub(1).min(4);
        (limits.retry_delay_base * (1u32 << exponent)).min(Duration::from_secs(2))
    }

    fn probe_delay(&self, limits: &RetryLimits, slow: bool) -> Duration {
        if slow {
            return slow_delay(limits);
        }
        limits.retry_delay_base
    }
}

/// The last upstream outcome of one logical request.
///
/// Retry exhaustion returns this outcome instead of folding it into a synthetic
/// 502: a status keeps its status/body, a transport failure stays 502, and a
/// deadline that stops the next attempt keeps the last status in
/// `detail.last_upstream_status`.
#[derive(Debug, Default, Clone)]
pub struct LastAttempt {
    status: Option<u16>,
    text: Option<String>,
    retry_after: Option<String>,
    failure: Option<UpstreamFailure>,
}

impl LastAttempt {
    pub fn record_status(
        &mut self,
        status: u16,
        text: Option<String>,
        retry_after: Option<String>,
    ) {
        self.status = Some(status);
        self.text = text;
        self.retry_after = retry_after;
        self.failure = None;
    }

    pub fn record_transport(&mut self, failure: UpstreamFailure) {
        self.status = None;
        self.text = None;
        self.retry_after = None;
        self.failure = Some(failure);
    }

    pub fn status(&self) -> Option<u16> {
        self.status
    }

    pub fn error(&self) -> GatewayError {
        if let Some(status) = self.status {
            GatewayError::upstream_status_error(
                status,
                self.text.as_deref(),
                self.retry_after.as_deref(),
            )
        } else if let Some(failure) = self.failure {
            GatewayError::upstream_failure(failure)
        } else {
            GatewayError::new(
                502,
                "Upstream request could not start another attempt",
                "upstream_attempt_budget_exhausted",
            )
        }
    }

    pub fn response(&self, protocol: ErrorProtocol) -> Response {
        self.error().response_for(protocol)
    }

    pub fn deadline_error(&self) -> GatewayError {
        GatewayError::request_deadline_exceeded(self.status)
    }
}

/// Classify one non-200 status for the retry matrix.
pub fn classify_status(status: u16, body_text: Option<&str>) -> RetryClass {
    match status {
        408 | 429 | 500 | 502 | 503 | 504 | 529 | 520..=524 => RetryClass::Full,
        402 => RetryClass::Probe(ProbeKind::Quota),
        403 if body_text.is_some_and(is_reauthentication_text) => {
            RetryClass::Probe(ProbeKind::ForbiddenAuthentication)
        }
        403 => RetryClass::Probe(ProbeKind::Forbidden),
        400 | 422 if body_text.is_some_and(is_transient_text) => {
            RetryClass::Probe(ProbeKind::Conditional)
        }
        _ => RetryClass::None,
    }
}

/// Whether a status needs its upstream text before the matrix can classify it.
pub fn needs_body_for_classification(status: u16) -> bool {
    matches!(status, 400 | 402 | 403 | 422)
}

/// The 400/422 transient-text whitelist.
pub fn is_transient_text(text: &str) -> bool {
    let text = text.to_ascii_lowercase();
    TRANSIENT_TEXTS.iter().any(|needle| text.contains(needle))
}

/// The 403 authentication-text trigger of #63.
pub fn is_reauthentication_text(text: &str) -> bool {
    let text = text.to_ascii_lowercase();
    REAUTHENTICATION_TEXTS
        .iter()
        .any(|needle| text.contains(needle))
}

/// Parse `Retry-After` as seconds or an HTTP date.
pub fn parse_retry_after(raw: Option<&str>) -> Option<Duration> {
    let raw = raw?.trim();
    if raw.is_empty() {
        return None;
    }
    if let Ok(seconds) = raw.parse::<f64>() {
        if seconds.is_finite() {
            // A negative value is "now"; a huge upstream value would overflow
            // Duration and is not going to be waited for anyway.
            return Some(Duration::from_secs_f64(seconds.clamp(0.0, 86_400.0)));
        }
    }
    let target = DateTime::parse_from_rfc2822(raw).ok()?;
    let delta = target.with_timezone(&Utc).timestamp() - Utc::now().timestamp();
    Some(Duration::from_secs(delta.max(0) as u64))
}

/// Whether the upstream explicitly asked not to retry.
pub fn stop_retrying(headers: &HashMap<String, String>) -> bool {
    headers
        .get("x-should-retry")
        .is_some_and(|value| value.trim().eq_ignore_ascii_case("false"))
}

/// Sleep at most until the request deadline; `false` means the budget is gone.
pub fn sleep_with_deadline(delay: Duration, deadline: Instant) -> bool {
    if delay.is_zero() {
        return Instant::now() < deadline;
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return false;
    }
    std::thread::sleep(delay.min(remaining));
    Instant::now() < deadline
}

fn slow_delay(limits: &RetryLimits) -> Duration {
    // A zero base is the test seam for an immediate retry. Production keeps the
    // Python 1-3 second jitter so a wave of ~50 second failures does not hit UM
    // in lockstep.
    if limits.retry_delay_base.is_zero() {
        return Duration::ZERO;
    }
    Duration::from_secs_f64(1.0 + 2.0 * pseudo_random_fraction())
}

fn pseudo_random_fraction() -> f64 {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos() as u64)
        .unwrap_or(0);
    let mixed = nanos
        ^ COUNTER
            .fetch_add(1, Ordering::Relaxed)
            .wrapping_mul(0x9E37_79B9_7F4A_7C15);
    (mixed % 1_000_000) as f64 / 1_000_000.0
}
