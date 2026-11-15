//! AC1: POSIX AF_UNIX endpoint policy.
//!
//! Mirrors the Python contract in `share/broker_client.py`:
//!
//! * the socket path is ABSOLUTE and fits the Linux `sun_path` bound (107 bytes);
//! * the parent directory is created 0700 and an existing wider directory is an
//!   explicit access refusal;
//! * the socket file is 0600 and a wider endpoint is an access refusal before a
//!   client connects;
//! * a LIVE endpoint is refused and only a DEAD socket file is reclaimed.
//!
//! The decision is a pure function over observed facts so the policy is testable
//! on every platform; the real filesystem adapter lives in
//! `transport::unix` behind `cfg(unix)`.

use std::fmt;
use std::path::{Path, PathBuf};

use crate::error::ResultCode;

/// Linux AF_UNIX `sun_path` is 108 bytes including the trailing NUL.
pub const UNIX_SOCKET_MAX_PATH_BYTES: usize = 107;
pub const UNIX_SOCKET_DIRECTORY_MODE: u32 = 0o700;
pub const UNIX_SOCKET_FILE_MODE: u32 = 0o600;
pub const GROUP_OR_OTHER_ACCESS: u32 = 0o077;

/// A refused endpoint policy decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SocketPolicyError {
    InvalidPath(String),
    AccessDenied(String),
    NotADirectory(String),
    NotASocket(String),
    EndpointBusy(String),
    EndpointServed(String),
    Io(String),
}

impl SocketPolicyError {
    /// The wire result a caller reports for this refusal.
    pub fn result_code(&self) -> ResultCode {
        match self {
            Self::InvalidPath(_) => ResultCode::BadRequest,
            Self::AccessDenied(_) => ResultCode::AccessDenied,
            Self::NotADirectory(_) | Self::NotASocket(_) => ResultCode::NotInstalled,
            Self::EndpointBusy(_) | Self::EndpointServed(_) => ResultCode::AccessDenied,
            Self::Io(_) => ResultCode::NotInstalled,
        }
    }
}

impl fmt::Display for SocketPolicyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidPath(message)
            | Self::AccessDenied(message)
            | Self::NotADirectory(message)
            | Self::NotASocket(message)
            | Self::EndpointBusy(message)
            | Self::EndpointServed(message)
            | Self::Io(message) => formatter.write_str(message),
        }
    }
}

impl std::error::Error for SocketPolicyError {}

/// Validate the POSIX control-endpoint path contract (client + server).
pub fn validate_unix_socket_path(path: &Path) -> Result<PathBuf, SocketPolicyError> {
    let bytes = path_bytes(path);
    if bytes.is_empty() || !bytes.starts_with(b"/") {
        return Err(SocketPolicyError::InvalidPath(
            "unix socket path must be an absolute path".to_string(),
        ));
    }
    if bytes.starts_with(br"\\.\pipe") {
        return Err(SocketPolicyError::InvalidPath(
            "unix socket path must not be a Windows named-pipe path".to_string(),
        ));
    }
    if bytes.contains(&0) {
        return Err(SocketPolicyError::InvalidPath(
            "unix socket path must not contain NUL bytes".to_string(),
        ));
    }
    if bytes.len() > UNIX_SOCKET_MAX_PATH_BYTES {
        return Err(SocketPolicyError::InvalidPath(format!(
            "unix socket path exceeds the AF_UNIX bound of \
             {UNIX_SOCKET_MAX_PATH_BYTES} bytes"
        )));
    }
    Ok(path.to_path_buf())
}

#[cfg(unix)]
fn path_bytes(path: &Path) -> Vec<u8> {
    use std::os::unix::ffi::OsStrExt;
    path.as_os_str().as_bytes().to_vec()
}

#[cfg(not(unix))]
fn path_bytes(path: &Path) -> Vec<u8> {
    path.as_os_str().to_string_lossy().into_owned().into_bytes()
}

/// Observed facts about the endpoint's parent directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirectoryFacts {
    pub path: PathBuf,
    pub exists: bool,
    pub is_directory: bool,
    pub mode: u32,
}

/// What the server must do with the parent directory.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DirectoryPlan {
    /// The directory is absent and must be created with mode 0700.
    Create,
    /// The directory exists and is private.
    Ready,
}

pub fn plan_private_directory(facts: &DirectoryFacts) -> Result<DirectoryPlan, SocketPolicyError> {
    if !facts.exists {
        return Ok(DirectoryPlan::Create);
    }
    if !facts.is_directory {
        return Err(SocketPolicyError::NotADirectory(format!(
            "unix socket parent is not a directory: {}",
            facts.path.display()
        )));
    }
    if facts.mode & GROUP_OR_OTHER_ACCESS != 0 {
        return Err(SocketPolicyError::AccessDenied(format!(
            "unix socket parent directory must be 0700: {}",
            facts.path.display()
        )));
    }
    Ok(DirectoryPlan::Ready)
}

/// Observed facts about the endpoint itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EndpointFacts {
    pub path: PathBuf,
    pub exists: bool,
    pub is_socket: bool,
    pub mode: u32,
}

/// The result of probing an existing endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EndpointLiveness {
    /// A short connect succeeded: another server owns the endpoint.
    Live,
    /// A connect was refused or the path disappeared: the file is stale.
    Stale,
    /// A connect timed out: the endpoint is in use but unconfirmed.
    Busy,
}

/// What a caller does with the endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EndpointPlan {
    /// No endpoint exists yet; bind it.
    Bind,
    /// The endpoint is a private 0600 socket; connect to it.
    Connect,
    /// The endpoint is a DEAD socket file; unlink it and bind.
    Reclaim,
}

/// The client-side endpoint policy: refuse a wider endpoint BEFORE connecting.
pub fn plan_connect_endpoint(facts: &EndpointFacts) -> Result<EndpointPlan, SocketPolicyError> {
    if !facts.exists || !facts.is_socket {
        return Err(SocketPolicyError::NotASocket(format!(
            "unix socket endpoint is not a socket: {}",
            facts.path.display()
        )));
    }
    if facts.mode != UNIX_SOCKET_FILE_MODE {
        return Err(SocketPolicyError::AccessDenied(format!(
            "unix socket file must be 0600: {}",
            facts.path.display()
        )));
    }
    Ok(EndpointPlan::Connect)
}

/// The server-side bind policy: reclaim only a DEAD socket file.
pub fn plan_bind_endpoint(
    facts: &EndpointFacts,
    liveness: Option<EndpointLiveness>,
) -> Result<EndpointPlan, SocketPolicyError> {
    if !facts.exists {
        return Ok(EndpointPlan::Bind);
    }
    if !facts.is_socket {
        return Err(SocketPolicyError::NotASocket(format!(
            "unix socket path already exists and is not a socket: {}",
            facts.path.display()
        )));
    }
    match liveness {
        Some(EndpointLiveness::Stale) => Ok(EndpointPlan::Reclaim),
        Some(EndpointLiveness::Live) => Err(SocketPolicyError::EndpointServed(format!(
            "unix socket endpoint is already served: {}",
            facts.path.display()
        ))),
        Some(EndpointLiveness::Busy) | None => Err(SocketPolicyError::EndpointBusy(format!(
            "unix socket endpoint is busy: {}",
            facts.path.display()
        ))),
    }
}
