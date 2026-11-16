//! S6b: one-time display grants and the single-page operator.
//!
//! This is `console/display_proxy.py::DisplayGrants`: only SHA-256 digests of
//! bearer values are stored, a grant is single-use and bound to its exact
//! origin (a wrong origin does not consume it), sessions are origin-bound and
//! short-lived, and one display session at a time owns the monotonic operator
//! generation. The actual `/display/*` relay is S7 and is not assembled here.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub const DISPLAY_SESSION_COOKIE: &str = "um_display_session";
pub const GRANT_TTL_SECONDS: u64 = 60;
pub const DISPLAY_SESSION_TTL_SECONDS: u64 = 300;
pub const MAX_PENDING_GRANTS: usize = 64;
pub const MAX_DISPLAY_SESSIONS: usize = 64;
pub const MAX_GRANT_TOKEN_BYTES: usize = 256;
pub const OPERATOR_OWNER_LABEL: &str = "display-session";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DisplayGrantError {
    pub code: &'static str,
}

impl DisplayGrantError {
    pub fn invalid_binding() -> Self {
        Self {
            code: "display_grant_invalid_binding",
        }
    }

    pub fn capacity() -> Self {
        Self {
            code: "display_grant_capacity",
        }
    }
}

impl std::fmt::Display for DisplayGrantError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(self.code)
    }
}

impl std::error::Error for DisplayGrantError {}

#[derive(Debug, Clone)]
struct Grant {
    subject: String,
    origin: String,
    expires_at: u64,
}

/// One redeemed, origin-bound display session (not single-use).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DisplaySession {
    pub token: String,
    pub subject: String,
    pub origin: String,
    pub expires_at: u64,
}

#[derive(Debug, Clone)]
struct Operator {
    session_digest: [u8; 32],
    generation: u64,
    label: String,
}

#[derive(Debug, Default)]
struct DisplayInner {
    grants: HashMap<[u8; 32], Grant>,
    sessions: HashMap<[u8; 32], DisplaySession>,
    operator: Option<Operator>,
    operator_generation: u64,
}

/// `console/display_proxy.py::DisplayGrants`.
#[derive(Debug)]
pub struct DisplayGrants {
    ttl: u64,
    session_ttl: u64,
    max_pending: usize,
    max_sessions: usize,
    inner: Mutex<DisplayInner>,
}

impl Default for DisplayGrants {
    fn default() -> Self {
        Self::new()
    }
}

impl DisplayGrants {
    pub fn new() -> Self {
        Self {
            ttl: GRANT_TTL_SECONDS,
            session_ttl: DISPLAY_SESSION_TTL_SECONDS,
            max_pending: MAX_PENDING_GRANTS,
            max_sessions: MAX_DISPLAY_SESSIONS,
            inner: Mutex::new(DisplayInner::default()),
        }
    }

    /// Issue one single-use grant bound to `subject` and `origin`.
    pub fn issue(&self, subject: &str, origin: &str) -> Result<(String, u64), DisplayGrantError> {
        self.issue_at(subject, origin, epoch_seconds())
    }

    pub fn issue_at(
        &self,
        subject: &str,
        origin: &str,
        now: u64,
    ) -> Result<(String, u64), DisplayGrantError> {
        let invalid = subject.is_empty()
            || subject.chars().count() > 256
            || origin.is_empty()
            || origin.chars().count() > 256
            || subject
                .chars()
                .chain(origin.chars())
                .any(|character| matches!(character, '\r' | '\n' | '\0'));
        if invalid {
            return Err(DisplayGrantError::invalid_binding());
        }
        let token = um_store::random_token_urlsafe(32);
        let expires_at = now + self.ttl;
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        prune_locked(&mut inner, now);
        if inner.grants.len() >= self.max_pending {
            return Err(DisplayGrantError::capacity());
        }
        inner.grants.insert(
            digest(&token),
            Grant {
                subject: subject.to_string(),
                origin: origin.to_string(),
                expires_at,
            },
        );
        Ok((token, expires_at))
    }

    /// Consume one correct-origin grant exactly once; a wrong origin does not
    /// consume it.
    pub fn redeem(&self, token: &str, origin: &str) -> Option<DisplaySession> {
        self.redeem_at(token, origin, epoch_seconds())
    }

    pub fn redeem_at(&self, token: &str, origin: &str, now: u64) -> Option<DisplaySession> {
        if token.is_empty() || token.len() > MAX_GRANT_TOKEN_BYTES {
            return None;
        }
        let key = digest(token);
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        prune_locked(&mut inner, now);
        let grant = inner.grants.get(&key)?;
        if grant.expires_at <= now || grant.origin != origin {
            return None;
        }
        let grant = inner.grants.remove(&key)?;
        if inner.sessions.len() >= self.max_sessions {
            return None;
        }
        let session_token = um_store::random_token_urlsafe(32);
        let session = DisplaySession {
            token: session_token.clone(),
            subject: grant.subject,
            origin: grant.origin,
            expires_at: now + self.session_ttl,
        };
        inner
            .sessions
            .insert(digest(&session_token), session.clone());
        Some(session)
    }

    pub fn validate_session(&self, token: &str, origin: &str) -> Option<DisplaySession> {
        self.validate_session_at(token, origin, epoch_seconds())
    }

    pub fn validate_session_at(
        &self,
        token: &str,
        origin: &str,
        now: u64,
    ) -> Option<DisplaySession> {
        if token.is_empty() || token.len() > MAX_GRANT_TOKEN_BYTES {
            return None;
        }
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        prune_locked(&mut inner, now);
        let session = inner.sessions.get(&digest(token))?;
        if session.expires_at <= now || session.origin != origin {
            return None;
        }
        Some(session.clone())
    }

    pub fn operator_state(&self, token: Option<&str>, origin: &str) -> Value {
        self.operator_state_at(token, origin, epoch_seconds())
    }

    pub fn operator_state_at(&self, token: Option<&str>, origin: &str, now: u64) -> Value {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        prune_locked(&mut inner, now);
        let Some(operator) = inner.operator.as_ref() else {
            return default_operator();
        };
        let stale = inner
            .sessions
            .get(&operator.session_digest)
            .map(|session| session.expires_at <= now)
            .unwrap_or(true);
        let owned = token
            .and_then(|token| session_locked(&inner, Some(token), origin, now))
            .map(|session| digest(&session.token) == operator.session_digest)
            .unwrap_or(false);
        json!({
            "owned_by_me": owned,
            "owner_present": true,
            "generation": operator.generation,
            "stale": stale,
        })
    }

    /// Acquire or explicitly take over the operator for a live session.
    pub fn acquire_operator(&self, token: Option<&str>, origin: &str, action: &str) -> Value {
        self.acquire_operator_at(token, origin, action, epoch_seconds())
    }

    pub fn acquire_operator_at(
        &self,
        token: Option<&str>,
        origin: &str,
        action: &str,
        now: u64,
    ) -> Value {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        prune_locked(&mut inner, now);
        let Some(session) = session_locked(&inner, token, origin, now) else {
            return json!({"acquired": false, "error": "display_session_required"});
        };
        let digest = digest(&session.token);
        let operator = inner.operator.as_ref();
        if let Some(operator) = operator {
            if operator.session_digest == digest {
                return json!({
                    "acquired": true,
                    "generation": operator.generation,
                    "took_over": false,
                });
            }
            if action == "acquire" {
                let owner_live = inner
                    .sessions
                    .get(&operator.session_digest)
                    .map(|owner| owner.expires_at > now)
                    .unwrap_or(false);
                if owner_live {
                    return json!({
                        "acquired": false,
                        "error": "display_operator_exists",
                        "generation": operator.generation,
                        "owner_label": operator.label,
                    });
                }
            }
        }
        if action == "takeover" || action == "acquire" {
            let took_over = operator.is_some();
            return assign_operator_locked(&mut inner, digest, took_over);
        }
        json!({"acquired": false, "error": "display_operator_action_invalid"})
    }

    pub fn takeover_operator(&self, token: Option<&str>, origin: &str) -> Value {
        self.acquire_operator(token, origin, "takeover")
    }

    pub fn takeover_operator_at(&self, token: Option<&str>, origin: &str, now: u64) -> Value {
        self.acquire_operator_at(token, origin, "takeover", now)
    }

    /// Whether this connection still owns the current generation.
    ///
    /// `display_proxy.py::DisplayGrants.operator_matches`: deliberately
    /// independent of session expiry, so an established WebSocket keeps its
    /// operator through a long login window after the short display session
    /// used at handshake time expires.
    pub fn operator_matches(&self, token: Option<&str>, generation: u64) -> bool {
        let Some(token) = token else {
            return false;
        };
        if token.is_empty() || token.len() > MAX_GRANT_TOKEN_BYTES {
            return false;
        }
        let digest = digest(token);
        let inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match inner.operator.as_ref() {
            Some(operator) => {
                operator.generation == generation && operator.session_digest == digest
            }
            None => false,
        }
    }

    /// Release only this page's own generation, never a newer owner.
    pub fn release_operator(&self, token: Option<&str>, generation: u64) -> bool {
        let Some(token) = token else {
            return false;
        };
        if token.is_empty() || token.len() > MAX_GRANT_TOKEN_BYTES {
            return false;
        }
        let digest = digest(token);
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match inner.operator.as_ref() {
            Some(operator)
                if operator.generation == generation && operator.session_digest == digest =>
            {
                inner.operator = None;
                true
            }
            _ => false,
        }
    }
}

fn session_locked(
    inner: &DisplayInner,
    token: Option<&str>,
    origin: &str,
    now: u64,
) -> Option<DisplaySession> {
    let token = token?;
    if token.is_empty() || token.len() > MAX_GRANT_TOKEN_BYTES {
        return None;
    }
    let session = inner.sessions.get(&digest(token))?;
    if session.expires_at <= now || session.origin != origin {
        return None;
    }
    Some(session.clone())
}

fn assign_operator_locked(
    inner: &mut DisplayInner,
    session_digest: [u8; 32],
    took_over: bool,
) -> Value {
    let generation = inner.operator_generation + 1;
    inner.operator_generation = generation;
    inner.operator = Some(Operator {
        session_digest,
        generation,
        label: OPERATOR_OWNER_LABEL.to_string(),
    });
    json!({"acquired": true, "generation": generation, "took_over": took_over})
}

fn default_operator() -> Value {
    json!({
        "owned_by_me": false,
        "owner_present": false,
        "generation": 0,
        "stale": false,
    })
}

fn prune_locked(inner: &mut DisplayInner, now: u64) {
    inner.grants.retain(|_, grant| grant.expires_at > now);
    inner.sessions.retain(|_, session| session.expires_at > now);
}

fn digest(token: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(token.as_bytes());
    hasher.finalize().into()
}

fn epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_grant_is_single_use_and_bound_to_its_origin() {
        let grants = DisplayGrants::new();
        let (token, expires_at) = grants
            .issue_at("control-token", "http://127.0.0.1:8002", 10)
            .unwrap();
        assert_eq!(expires_at, 10 + GRANT_TTL_SECONDS);
        assert!(grants
            .redeem_at(&token, "http://127.0.0.1:8002.evil", 10)
            .is_none());
        let session = grants
            .redeem_at(&token, "http://127.0.0.1:8002", 10)
            .expect("session");
        assert_eq!(session.expires_at, 10 + DISPLAY_SESSION_TTL_SECONDS);
        assert!(grants
            .redeem_at(&token, "http://127.0.0.1:8002", 10)
            .is_none());
        assert!(grants
            .validate_session_at(&session.token, "http://127.0.0.1:8002", 10)
            .is_some());
        assert!(grants
            .validate_session_at(&session.token, "http://127.0.0.1:8002.evil", 10)
            .is_none());
    }

    #[test]
    fn expired_grants_and_sessions_are_refused() {
        let grants = DisplayGrants::new();
        let (token, _) = grants
            .issue_at("control-token", "http://origin", 10)
            .unwrap();
        assert!(grants
            .redeem_at(&token, "http://origin", 10 + GRANT_TTL_SECONDS)
            .is_none());
        let (token, _) = grants
            .issue_at("control-token", "http://origin", 10)
            .unwrap();
        let session = grants
            .redeem_at(&token, "http://origin", 10)
            .expect("session");
        assert!(grants
            .validate_session_at(
                &session.token,
                "http://origin",
                10 + DISPLAY_SESSION_TTL_SECONDS
            )
            .is_none());
    }

    #[test]
    fn pending_grants_are_bounded() {
        let grants = DisplayGrants::new();
        for _ in 0..MAX_PENDING_GRANTS {
            grants
                .issue_at("control-token", "http://origin", 10)
                .unwrap();
        }
        let error = grants
            .issue_at("control-token", "http://origin", 10)
            .unwrap_err();
        assert_eq!(error.code, "display_grant_capacity");
        assert!(grants.issue_at("", "http://origin", 10).is_err());
        assert!(grants
            .issue_at("control-token", "http://origin\n", 10)
            .is_err());
    }

    #[test]
    fn the_operator_is_single_page_and_takeover_bumps_the_generation() {
        let grants = DisplayGrants::new();
        let (token, _) = grants.issue_at("a", "http://origin", 10).unwrap();
        let first = grants
            .redeem_at(&token, "http://origin", 10)
            .expect("first");
        let (token, _) = grants.issue_at("b", "http://origin", 10).unwrap();
        let second = grants
            .redeem_at(&token, "http://origin", 10)
            .expect("second");

        assert_eq!(
            grants.acquire_operator_at(Some(&first.token), "http://origin", "acquire", 10),
            json!({"acquired": true, "generation": 1, "took_over": false})
        );
        assert_eq!(
            grants.acquire_operator_at(Some(&first.token), "http://origin", "acquire", 10),
            json!({"acquired": true, "generation": 1, "took_over": false})
        );
        assert_eq!(
            grants.acquire_operator_at(Some(&second.token), "http://origin", "acquire", 10),
            json!({
                "acquired": false,
                "error": "display_operator_exists",
                "generation": 1,
                "owner_label": "display-session",
            })
        );
        assert_eq!(
            grants.acquire_operator_at(Some(&second.token), "http://origin", "takeover", 10),
            json!({"acquired": true, "generation": 2, "took_over": true})
        );
        assert_eq!(
            grants.acquire_operator_at(None, "http://origin", "acquire", 10),
            json!({"acquired": false, "error": "display_session_required"})
        );
        assert_eq!(
            grants.acquire_operator_at(Some(&first.token), "http://origin", "steal", 10),
            json!({"acquired": false, "error": "display_operator_action_invalid"})
        );
    }

    #[test]
    fn a_stale_owner_can_be_replaced_without_a_takeover() {
        let grants = DisplayGrants::new();
        let (token, _) = grants.issue_at("a", "http://origin", 10).unwrap();
        let first = grants
            .redeem_at(&token, "http://origin", 10)
            .expect("first");
        assert_eq!(
            grants.acquire_operator_at(Some(&first.token), "http://origin", "acquire", 10),
            json!({"acquired": true, "generation": 1, "took_over": false})
        );
        let second_now = 10 + DISPLAY_SESSION_TTL_SECONDS - 10;
        let (token, _) = grants.issue_at("b", "http://origin", second_now).unwrap();
        let second = grants
            .redeem_at(&token, "http://origin", second_now)
            .expect("second");
        assert_eq!(
            grants.acquire_operator_at(
                Some(&second.token),
                "http://origin",
                "acquire",
                second_now + 10,
            ),
            json!({"acquired": true, "generation": 2, "took_over": true})
        );
        assert_eq!(
            grants.operator_state_at(
                Some(&first.token),
                "http://origin",
                second_now + DISPLAY_SESSION_TTL_SECONDS,
            ),
            json!({
                "owned_by_me": false,
                "owner_present": true,
                "generation": 2,
                "stale": true,
            })
        );
    }

    #[test]
    fn only_the_current_generation_matches_and_releases_its_own_owner() {
        let grants = DisplayGrants::new();
        let (token, _) = grants.issue_at("a", "http://origin", 10).unwrap();
        let first = grants
            .redeem_at(&token, "http://origin", 10)
            .expect("first");
        let (token, _) = grants.issue_at("b", "http://origin", 10).unwrap();
        let second = grants
            .redeem_at(&token, "http://origin", 10)
            .expect("second");
        let generation =
            grants.acquire_operator_at(Some(&first.token), "http://origin", "acquire", 10)
                ["generation"]
                .as_u64()
                .expect("generation");
        assert!(grants.operator_matches(Some(&first.token), generation));
        assert!(!grants.operator_matches(Some(&second.token), generation));
        assert!(!grants.operator_matches(None, generation));
        let taken = grants.takeover_operator_at(Some(&second.token), "http://origin", 10);
        assert!(!grants.operator_matches(Some(&first.token), generation));
        assert!(grants.operator_matches(
            Some(&second.token),
            taken["generation"].as_u64().expect("generation"),
        ));
        // A late close from the old page cannot release the new owner.
        assert!(!grants.release_operator(Some(&first.token), generation));
        assert_eq!(
            grants.operator_state_at(Some(&second.token), "http://origin", 10)["owner_present"],
            json!(true)
        );
        assert!(grants.release_operator(
            Some(&second.token),
            taken["generation"].as_u64().expect("generation"),
        ));
        assert_eq!(
            grants.operator_state_at(Some(&second.token), "http://origin", 10)["owner_present"],
            json!(false)
        );
        assert!(!grants.operator_matches(
            Some(&second.token),
            taken["generation"].as_u64().expect("generation"),
        ));
    }
}
