//! AC1: kernel peer-identity evidence, mirroring `LinuxInstanceVerifier`.
//!
//! The peer pid comes from `SO_PEERCRED` (never a status file). Identity is the
//! REAL `/proc/<pid>` command line (the expected entry marker and the exact
//! `--port <port>` pair), the exe symlink target, the process uid and the
//! creation epoch (PID-reuse detection). Every failed or unreadable check is a
//! refusal; `None` credentials are never a verified peer.

use std::path::{Path, PathBuf};

use crate::error::{ProtocolError, ResultCode};

/// The kernel's `struct ucred` for a connected AF_UNIX socket.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCredentials {
    pub pid: i32,
    pub uid: u32,
    pub gid: u32,
}

/// The process evidence the kernel and `/proc/<pid>` supplied for a peer.
#[derive(Debug, Clone, PartialEq)]
pub struct PeerEvidence {
    pub pid: i32,
    pub uid: Option<u32>,
    pub cmdline: Vec<String>,
    pub image: Option<PathBuf>,
    pub creation_epoch: Option<f64>,
}

/// What a deployment expects of the instance-channel peer.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct PeerExpectation {
    pub expected_uid: Option<u32>,
    pub expected_image: Option<PathBuf>,
    pub expected_cmdline_marker: Option<String>,
    pub expected_port: Option<u16>,
    pub expected_creation_epoch: Option<f64>,
}

/// Missing `SO_PEERCRED` evidence is an access refusal, never a peer.
pub fn require_peer_credentials(
    credentials: Option<PeerCredentials>,
) -> Result<PeerCredentials, ProtocolError> {
    match credentials {
        Some(credentials) if credentials.pid > 0 => Ok(credentials),
        Some(_) | None => Err(ProtocolError::new(
            ResultCode::AccessDenied,
            "peer credentials unavailable (refused)",
        )),
    }
}

/// Verify kernel evidence against the deployment expectation.
pub fn verify_peer_evidence(
    evidence: &PeerEvidence,
    expectation: &PeerExpectation,
) -> Result<PeerEvidence, ProtocolError> {
    let refuse = |message: &str| {
        Err(ProtocolError::new(ResultCode::AccessDenied, message))
    };
    if evidence.cmdline.is_empty() {
        return refuse("process command line unreadable (refused)");
    }
    if let Some(marker) = expectation.expected_cmdline_marker.as_deref() {
        let matches = evidence
            .cmdline
            .iter()
            .any(|part| basename(part) == marker);
        if !matches {
            return refuse("instance entry marker mismatch (refused)");
        }
    }
    if let Some(port) = expectation.expected_port {
        let port = port.to_string();
        let paired = evidence.cmdline.windows(2).any(|pair| {
            pair[0] == "--port" && pair[1] == port
        });
        if !paired {
            return refuse("instance port mismatch (refused)");
        }
    }
    if let Some(image) = expectation.expected_image.as_deref() {
        let Some(observed) = evidence.image.as_deref() else {
            return refuse("process image unreadable (refused)");
        };
        if observed != image {
            return refuse("process image mismatch (refused)");
        }
    }
    let Some(uid) = evidence.uid else {
        return refuse("process uid unreadable (refused)");
    };
    if let Some(expected) = expectation.expected_uid {
        if uid != expected {
            return refuse("process uid mismatch (refused)");
        }
    }
    let Some(creation) = evidence.creation_epoch else {
        return refuse("process creation time unreadable (refused)");
    };
    if let Some(expected) = expectation.expected_creation_epoch {
        if (creation - expected).abs() > 2.0 {
            return refuse("process creation time mismatch (pid reuse) (refused)");
        }
    }
    Ok(evidence.clone())
}

fn basename(part: &str) -> &str {
    Path::new(part)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or(part)
}
