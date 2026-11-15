//! S6b: the console usage-session bootstrap (`usagesrv.py:665-743`).
//!
//! A one-time bootstrap code is generated on demand, published to the private
//! code file and consumed atomically by `POST /api/session`; a successful
//! exchange starts the 12-hour HttpOnly session. `POST /api/session/reissue`
//! needs the local control token and invalidates the previous code. The code and
//! the session value are never logged, recorded or echoed in an error.

use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

pub const SESSION_COOKIE: &str = "usage_session";
pub const SESSION_TTL_SECONDS: u64 = 12 * 3600;
pub const SESSION_CODE_TTL_SECONDS: u64 = 600;
pub const SESSION_CODE_MAX_BYTES: u64 = 1024;

#[derive(Debug, Default)]
struct SessionInner {
    code: Option<String>,
    code_expires: u64,
    session: Option<String>,
    session_expires: u64,
}

/// `usagesrv.py::_session`: one bootstrap code and one live session.
#[derive(Debug, Default)]
pub struct SessionState {
    code_file: Option<PathBuf>,
    inner: Mutex<SessionInner>,
}

impl SessionState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_code_file(code_file: Option<PathBuf>) -> Self {
        Self {
            code_file,
            inner: Mutex::new(SessionInner::default()),
        }
    }

    /// Test seam: install one live session without an exchange.
    pub fn issue(&self, token: String) {
        self.issue_at(token, epoch_seconds());
    }

    pub fn issue_at(&self, token: String, now: u64) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.session = Some(token);
            inner.session_expires = now + SESSION_TTL_SECONDS;
        }
    }

    pub fn clear(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.session = None;
            inner.session_expires = 0;
        }
    }

    /// `session_bootstrap_code`: generate a code only when absent/expired.
    pub fn bootstrap_code(&self) -> String {
        self.bootstrap_code_at(epoch_seconds())
    }

    pub fn bootstrap_code_at(&self, now: u64) -> String {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if inner.code.is_none() || now > inner.code_expires {
            self.generate_code_locked(&mut inner, now);
        }
        inner.code.clone().unwrap_or_default()
    }

    /// `reissue_session_code`: the control token was already verified by the
    /// route; a new code invalidates the previous one even when unexpired.
    pub fn reissue(&self, control_token_ok: bool) -> Option<String> {
        self.reissue_at(control_token_ok, epoch_seconds())
    }

    pub fn reissue_at(&self, control_token_ok: bool, now: u64) -> Option<String> {
        if !control_token_ok {
            return None;
        }
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        self.generate_code_locked(&mut inner, now);
        inner.code.clone()
    }

    /// `session_exchange`: a correct, unexpired code is consumed exactly once;
    /// the session TTL starts at this redemption.
    pub fn exchange(&self, code: Option<&str>) -> Option<String> {
        self.exchange_at(code, epoch_seconds())
    }

    pub fn exchange_at(&self, code: Option<&str>, now: u64) -> Option<String> {
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let current = inner.code.as_ref()?;
        if now > inner.code_expires {
            return None;
        }
        let supplied = code?;
        if !constant_time_eq(supplied.as_bytes(), current.as_bytes()) {
            return None;
        }
        inner.code = None;
        inner.code_expires = 0;
        let token = um_store::random_token_urlsafe(32);
        inner.session = Some(token.clone());
        inner.session_expires = now + SESSION_TTL_SECONDS;
        Some(token)
    }

    /// `session_valid`: the cookie must carry this process's live session.
    pub fn valid(&self, cookie_header: Option<&str>) -> bool {
        self.valid_at(cookie_header, epoch_seconds())
    }

    pub fn valid_at(&self, cookie_header: Option<&str>, now: u64) -> bool {
        let expected = {
            let inner = self
                .inner
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            match inner.session.as_ref() {
                Some(token) if now <= inner.session_expires => token.clone(),
                _ => return false,
            }
        };
        let Some(supplied) = cookie_header.and_then(|header| cookie_value(header, SESSION_COOKIE))
        else {
            return false;
        };
        constant_time_eq(supplied.as_bytes(), expected.as_bytes())
    }

    /// The live session token, used as a display-grant subject.
    pub fn session_token(&self, cookie_header: Option<&str>) -> Option<String> {
        if !self.valid(cookie_header) {
            return None;
        }
        let inner = self
            .inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        inner.session.clone()
    }

    fn generate_code_locked(&self, inner: &mut SessionInner, now: u64) {
        let code = um_store::random_token_urlsafe(24);
        inner.code = Some(code.clone());
        inner.code_expires = now + SESSION_CODE_TTL_SECONDS;
        // Python ignores a write failure: only the console bootstrap is
        // affected, the route still refuses missing sessions.
        if let Some(path) = self.code_file.as_deref() {
            write_private_secret(path, code.as_bytes());
        }
    }
}

fn epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}

/// One cookie value from a Cookie header; a malformed header yields `None`.
pub fn cookie_value(header: &str, name: &str) -> Option<String> {
    for part in header.split(';') {
        let Some((key, value)) = part.trim().split_once('=') else {
            continue;
        };
        if key == name {
            return Some(value.trim_matches('"').to_string());
        }
    }
    None
}

/// `share/secret_protection.py::write_private_secret_file` on POSIX: an
/// existing file must be a regular file owned by this uid and is chmod-ed to
/// 0600; a new file is created 0600. Any refusal keeps the old contents.
pub fn write_private_secret(path: &Path, data: &[u8]) {
    let Some(parent) = path.parent() else {
        return;
    };
    if std::fs::create_dir_all(parent).is_err() {
        return;
    }
    #[cfg(unix)]
    {
        use std::io::Write as _;
        use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
        use std::os::unix::io::AsRawFd;
        if path.exists() {
            let Ok(metadata) = std::fs::metadata(path) else {
                return;
            };
            // SAFETY: `geteuid` has no preconditions.
            if !metadata.is_file() || metadata.uid() != unsafe { libc::geteuid() } {
                return;
            }
            let Ok(mut file) = std::fs::OpenOptions::new().write(true).open(path) else {
                return;
            };
            // SAFETY: the file descriptor is open for the lifetime of `file`.
            if unsafe { libc::fchmod(file.as_raw_fd(), 0o600) } != 0 {
                return;
            }
            let _ = file.set_len(0);
            let _ = file.write_all(data);
            let _ = file.flush();
            return;
        }
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
        {
            let _ = file.write_all(data);
            let _ = file.flush();
            let _ = std::fs::set_permissions(path, PermissionsExt::from_mode(0o600));
        }
    }
    #[cfg(not(unix))]
    {
        let _ = std::fs::write(path, data);
    }
}

/// A constant-time byte comparison (never early-exit on a mismatch).
pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut difference = 0u8;
    for (left, right) in left.iter().zip(right.iter()) {
        difference |= left ^ right;
    }
    difference == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state() -> SessionState {
        SessionState::new()
    }

    #[test]
    fn expired_bootstrap_code_is_rejected_and_regenerated() {
        let state = state();
        let code = state.bootstrap_code_at(1_000);
        assert!(state
            .exchange_at(Some(&code), 1_000 + SESSION_CODE_TTL_SECONDS + 5)
            .is_none());
        let fresh = state.bootstrap_code_at(1_000 + SESSION_CODE_TTL_SECONDS + 5);
        assert_ne!(fresh, code);
        assert!(state
            .exchange_at(Some(&fresh), 1_000 + SESSION_CODE_TTL_SECONDS + 5)
            .is_some());
    }

    #[test]
    fn exchange_consumes_the_code_and_rejects_a_replay() {
        let state = state();
        let code = state.bootstrap_code_at(10);
        let session = state.exchange_at(Some(&code), 10).expect("session");
        assert!(state.exchange_at(Some(&code), 10).is_none());
        assert!(state.valid_at(Some(&format!("{SESSION_COOKIE}={session}")), 10));
    }

    #[test]
    fn concurrent_exchanges_admit_exactly_one() {
        use std::sync::Arc;
        let state = Arc::new(state());
        let code = state.bootstrap_code_at(10);
        let mut handles = Vec::new();
        for _ in 0..8 {
            let state = Arc::clone(&state);
            let code = code.clone();
            handles.push(std::thread::spawn(move || {
                state.exchange_at(Some(&code), 10).is_some()
            }));
        }
        let winners = handles
            .into_iter()
            .map(|handle| handle.join().unwrap_or(false))
            .filter(|won| *won)
            .count();
        assert_eq!(winners, 1);
    }

    #[test]
    fn the_session_clock_starts_at_redemption() {
        let state = state();
        let code = state.bootstrap_code_at(10);
        let session = state
            .exchange_at(Some(&code), 10 + SESSION_CODE_TTL_SECONDS - 5)
            .expect("session");
        let cookie = format!("{SESSION_COOKIE}={session}");
        assert!(state.valid_at(Some(&cookie), 10 + SESSION_CODE_TTL_SECONDS - 5));
        assert!(state.valid_at(
            Some(&cookie),
            10 + SESSION_CODE_TTL_SECONDS - 5 + SESSION_TTL_SECONDS - 60
        ));
        assert!(!state.valid_at(
            Some(&cookie),
            10 + SESSION_CODE_TTL_SECONDS - 5 + SESSION_TTL_SECONDS + 60
        ));
    }

    #[test]
    fn reissue_requires_the_token_and_invalidates_the_old_code() {
        let state = state();
        let old = state.bootstrap_code_at(10);
        assert!(state.reissue_at(false, 10).is_none());
        let fresh = state.reissue_at(true, 10).expect("reissued code");
        assert_ne!(fresh, old);
        assert!(state.exchange_at(Some(&old), 10).is_none());
        assert!(state.exchange_at(Some(&fresh), 10).is_some());
    }

    #[test]
    fn reissue_after_consumption_restores_login() {
        let state = state();
        let code = state.bootstrap_code_at(10);
        assert!(state.exchange_at(Some(&code), 10).is_some());
        let fresh = state.reissue_at(true, 10).expect("reissued code");
        let session = state.exchange_at(Some(&fresh), 10).expect("session");
        assert!(state.valid_at(Some(&format!("{SESSION_COOKIE}={session}")), 10));
    }

    #[test]
    fn a_wrong_code_does_not_consume_the_bootstrap_code() {
        let state = state();
        let code = state.bootstrap_code_at(10);
        assert!(state.exchange_at(Some("wrong-code"), 10).is_none());
        assert!(state.exchange_at(Some(&code), 10).is_some());
    }
}
