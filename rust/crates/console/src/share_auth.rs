//! S5b: the legacy `/api/share/*` management face auth.
//!
//! Reads require the HttpOnly usage session or the local control token
//! (`usagesrv.py::_management_auth`). Writes (S6b) accept the control token,
//! or the usage session plus the same-origin CSRF marker. The public ingress
//! never reverse-proxies this face.

use std::path::Path;

pub use crate::session::{constant_time_eq, SessionState};

pub const CONTROL_TOKEN_MAX_BYTES: u64 = 1024;

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

/// `usagesrv.py::_management_auth(write=True)`: a write is allowed with the
/// control token, or with the usage session plus the same-origin JS marker.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ManagementAuth {
    Control,
    Session,
    Missing,
    CsrfRequired,
}

pub fn management_write_auth(
    session: &SessionState,
    control_token_file: Option<&Path>,
    cookie_header: Option<&str>,
    supplied_token: Option<&str>,
    requested_with: Option<&str>,
    write: bool,
) -> ManagementAuth {
    if control_token_ok(control_token_file, supplied_token) {
        return ManagementAuth::Control;
    }
    if !session.valid(cookie_header) {
        return ManagementAuth::Missing;
    }
    if write && requested_with != Some("XMLHttpRequest") {
        return ManagementAuth::CsrfRequired;
    }
    ManagementAuth::Session
}
