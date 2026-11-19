//! Durable authentication evidence (#114).
//!
//! The gateway writes one small JSON document after startup, every credential
//! probe, every login round transition and shutdown. It explains why the upstream
//! rejected a credential that was still inside its JWT `exp`: the saved Open WebUI
//! JWT failed upstream signature validation (`401 {"detail":"401 Unauthorized"}`),
//! not JWT expiry. Only bounded summaries and hash prefixes are stored; the token,
//! the API key and the full `jti` never leave their original storage.
//!
//! A write failure is logged and never fails a request or a login round.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::config::Role;
use crate::login::token::TokenDocument;

pub const AUTH_STATE_FILE_ENV: &str = "CHAT2API_AUTH_STATE_FILE";
pub const DEFAULT_LOCAL_AUTH_STATE_FILE: &str = "data/auth-state.json";
pub const DEFAULT_CLOUD_AUTH_STATE_FILE: &str = "/var/lib/um-share/auth-state.json";

/// The role default: local 8000 keeps the file beside its fault record; cloud
/// 8003 keeps it in the writable share state.
pub fn default_path(role: Role) -> PathBuf {
    match role {
        Role::Local => PathBuf::from(DEFAULT_LOCAL_AUTH_STATE_FILE),
        Role::Cloud => PathBuf::from(DEFAULT_CLOUD_AUTH_STATE_FILE),
    }
}

/// Resolve the effective path. An explicit value wins even in candidate mode; a
/// candidate without one never writes a production file.
pub fn effective_path(explicit: Option<PathBuf>, role: Role, candidate: bool) -> Option<PathBuf> {
    explicit.or_else(|| (!candidate).then(|| default_path(role)))
}

/// The atomic writer of the durable auth state document.
pub struct AuthStateFile {
    path: Option<PathBuf>,
    write_lock: Mutex<()>,
}

impl AuthStateFile {
    /// Open one writer; `None` keeps every write in memory only (no-op).
    pub fn open(path: Option<PathBuf>) -> Self {
        Self {
            path,
            write_lock: Mutex::new(()),
        }
    }

    pub fn is_persistent(&self) -> bool {
        self.path.is_some()
    }

    /// Write the document atomically; a failure only logs and never propagates.
    pub fn write(&self, document: &Value) {
        let Some(path) = &self.path else {
            return;
        };
        let _guard = self
            .write_lock
            .lock()
            .expect("auth state write lock poisoned");
        if let Err(error) = write_atomic(path, document) {
            // 证据文件写失败不能影响请求或登录轮次；保留原文件并记录日志。
            eprintln!(
                "um-gateway: auth state write failed ({}): {error}",
                path.display()
            );
        }
    }
}

/// The bounded JWT claims stored in the auth state file.
///
/// `iat`, `exp`, `jti` and `id` are read only to explain a rejection before
/// `exp`; the `jti`/`id` values themselves are never written, only the first
/// twelve hex characters of their SHA-256 digest.
pub fn token_claims(document: Option<&TokenDocument>) -> Value {
    let payload = document
        .map(|document| document.token.as_str())
        .and_then(|token| token.split('.').nth(1))
        .and_then(base64url_decode)
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok());
    let claim = |name: &str| {
        payload
            .as_ref()
            .and_then(|payload| payload.get(name))
            .cloned()
            .unwrap_or(Value::Null)
    };
    let hashed = |name: &str| {
        payload
            .as_ref()
            .and_then(|payload| payload.get(name))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(sha12)
            .map(Value::String)
            .unwrap_or(Value::Null)
    };
    json!({
        "saved_at": document.map(|document| document.saved_at.clone()).unwrap_or_default(),
        "token_iat": claim("iat"),
        "token_exp": claim("exp"),
        "token_jti_sha12": hashed("jti"),
        "token_id_sha12": hashed("id"),
    })
}

fn sha12(value: &str) -> String {
    let digest = Sha256::digest(value.as_bytes());
    digest
        .iter()
        .take(6)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn base64url_decode(text: &str) -> Option<Vec<u8>> {
    let mut output = Vec::new();
    let mut buffer = 0u32;
    let mut bits = 0u32;
    for byte in text.bytes() {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            b'=' => break,
            _ => return None,
        } as u32;
        buffer = (buffer << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            output.push((buffer >> bits) as u8);
            buffer &= (1 << bits) - 1;
        }
    }
    Some(output)
}

fn write_atomic(path: &Path, document: &Value) -> io::Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    fs::create_dir_all(&parent)?;
    let raw = serde_json::to_vec_pretty(document)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "auth-state.json".to_string());
    let temporary = path.with_file_name(format!("{name}.{}.tmp", std::process::id()));
    let result = write_temp(&temporary, &raw)
        .and_then(|()| set_private_mode(&temporary))
        .and_then(|()| replace_file(&temporary, path));
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn write_temp(path: &Path, raw: &[u8]) -> io::Result<()> {
    let mut file = File::create(path)?;
    file.write_all(raw)?;
    file.flush()?;
    file.sync_all()
}

#[cfg(unix)]
fn set_private_mode(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    // POSIX mode is best effort, like the token store; the atomic replace keeps
    // the previous valid document on failure.
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o600));
    Ok(())
}

#[cfg(not(unix))]
fn set_private_mode(_path: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(not(windows))]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    fs::rename(source, target)
}

#[cfg(windows)]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;

    #[link(name = "kernel32")]
    extern "system" {
        fn MoveFileExW(existing: *const u16, replacement: *const u16, flags: u32) -> i32;
    }

    const MOVEFILE_REPLACE_EXISTING: u32 = 0x1;
    const MOVEFILE_WRITE_THROUGH: u32 = 0x8;
    let source_wide: Vec<u16> = source
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let target_wide: Vec<u16> = target
        .as_os_str()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    let ok = unsafe {
        MoveFileExW(
            source_wide.as_ptr(),
            target_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if ok == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_claims_are_bounded_to_hashes_and_never_the_raw_claims() {
        let payload =
            "{\"id\":\"user-1\",\"jti\":\"jti-secret\",\"iat\":1700000000,\"exp\":1700032400}";
        let token = format!("header.{}.signature", base64url_encode(payload.as_bytes()));
        let document = TokenDocument {
            url: "http://127.0.0.1:8000".to_string(),
            token,
            api_key: Some("api-key-secret".to_string()),
            saved_at: "2026-11-19 20:35:03".to_string(),
        };
        let claims = token_claims(Some(&document));
        assert_eq!(claims["saved_at"], json!("2026-11-19 20:35:03"));
        assert_eq!(claims["token_iat"], json!(1700000000));
        assert_eq!(claims["token_exp"], json!(1700032400));
        assert_eq!(claims["token_jti_sha12"], json!(sha12("jti-secret")));
        assert_eq!(claims["token_id_sha12"], json!(sha12("user-1")));
        let text = claims.to_string();
        assert!(!text.contains("jti-secret"));
        assert!(!text.contains("user-1"));
        assert!(!text.contains("api-key-secret"));
    }

    #[test]
    fn a_candidate_never_writes_a_default_auth_state_file() {
        assert_eq!(effective_path(None, Role::Local, true), None);
        assert_eq!(effective_path(None, Role::Cloud, true), None);
        assert_eq!(
            effective_path(None, Role::Local, false),
            Some(PathBuf::from(DEFAULT_LOCAL_AUTH_STATE_FILE))
        );
        assert_eq!(
            effective_path(None, Role::Cloud, false),
            Some(PathBuf::from(DEFAULT_CLOUD_AUTH_STATE_FILE))
        );
        assert_eq!(
            effective_path(Some(PathBuf::from("tmp/state.json")), Role::Cloud, true),
            Some(PathBuf::from("tmp/state.json"))
        );
    }

    fn base64url_encode(raw: &[u8]) -> String {
        const ALPHABET: &[u8; 64] =
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut output = String::new();
        for chunk in raw.chunks(3) {
            let first = chunk[0] as u32;
            let second = chunk.get(1).copied().unwrap_or(0) as u32;
            let third = chunk.get(2).copied().unwrap_or(0) as u32;
            let combined = (first << 16) | (second << 8) | third;
            output.push(ALPHABET[((combined >> 18) & 0x3f) as usize] as char);
            output.push(ALPHABET[((combined >> 12) & 0x3f) as usize] as char);
            if chunk.len() > 1 {
                output.push(ALPHABET[((combined >> 6) & 0x3f) as usize] as char);
            }
            if chunk.len() > 2 {
                output.push(ALPHABET[(combined & 0x3f) as usize] as char);
            }
        }
        output
    }
}
