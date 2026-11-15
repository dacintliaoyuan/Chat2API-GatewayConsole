//! S5b: the legacy `/api/share/*` read-only management face auth.
//!
//! Reads require the HttpOnly usage session or the local control token
//! (`usagesrv.py::_management_auth`). Writes belong to S6; this candidate
//! never performs them. The public ingress never reverse-proxies this face.

use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

pub const SESSION_COOKIE: &str = "usage_session";
pub const SESSION_TTL_SECONDS: u64 = 12 * 3600;
pub const CONTROL_TOKEN_MAX_BYTES: u64 = 1024;

#[derive(Debug, Default)]
pub struct SessionState {
    inner: Mutex<Option<(String, u64)>>,
}

impl SessionState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn issue(&self, token: String) {
        let expires = epoch_seconds() + SESSION_TTL_SECONDS;
        if let Ok(mut inner) = self.inner.lock() {
            *inner = Some((token, expires));
        }
    }

    pub fn clear(&self) {
        if let Ok(mut inner) = self.inner.lock() {
            *inner = None;
        }
    }

    pub fn valid(&self, cookie_header: Option<&str>) -> bool {
        let Some(cookie_header) = cookie_header else {
            return false;
        };
        let expected = match self.inner.lock() {
            Ok(inner) => match inner.as_ref() {
                Some((token, expires)) if epoch_seconds() <= *expires => token.clone(),
                _ => return false,
            },
            Err(_) => return false,
        };
        let supplied = match cookie_value(cookie_header, SESSION_COOKIE) {
            Some(value) => value,
            None => return false,
        };
        constant_time_eq(supplied.as_bytes(), expected.as_bytes())
    }
}

fn epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}

/// One cookie value from a Cookie header; a malformed header yields `None`.
fn cookie_value(header: &str, name: &str) -> Option<String> {
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

/// `usagesrv.py::control_token_ok`: the control token file is read with the
/// private-secret gate, and the supplied value must hmac-match it. The value is
/// never copied into an error.
pub fn control_token_ok(path: Option<&Path>, supplied: Option<&str>) -> bool {
    let Some(path) = path else {
        return false;
    };
    let supplied = match supplied {
        Some(value) if !value.is_empty() => value,
        _ => return false,
    };
    let raw = match read_private_secret(path, CONTROL_TOKEN_MAX_BYTES) {
        Ok(raw) => raw,
        Err(_) => return false,
    };
    let expected = match String::from_utf8(raw) {
        Ok(text) => text.trim().to_string(),
        Err(_) => return false,
    };
    !expected.is_empty() && constant_time_eq(supplied.as_bytes(), expected.as_bytes())
}

/// `usagesrv.py::read_private_secret_path` on POSIX: 0600 and the current uid.
fn read_private_secret(path: &Path, max_bytes: u64) -> Result<Vec<u8>, std::io::Error> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = std::fs::metadata(path)?;
        if !metadata.is_file() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "control token is not a regular file",
            ));
        }
        if metadata.mode() & 0o777 != 0o600 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "control token must have mode 0600",
            ));
        }
        // SAFETY: `geteuid` has no preconditions.
        if metadata.uid() != unsafe { libc::geteuid() } {
            return Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "control token is not owned by the current uid",
            ));
        }
    }
    let raw = std::fs::read(path)?;
    if raw.len() as u64 > max_bytes {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "control token is unexpectedly large",
        ));
    }
    Ok(raw)
}

pub fn management_auth(
    session: &SessionState,
    control_token_file: Option<&Path>,
    cookie_header: Option<&str>,
    supplied_token: Option<&str>,
) -> bool {
    control_token_ok(control_token_file, supplied_token) || session.valid(cookie_header)
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
