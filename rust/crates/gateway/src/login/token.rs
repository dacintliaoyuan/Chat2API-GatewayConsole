//! Python-compatible UM credential storage.
//!
//! `chat2api/chat2api.py::TokenStore` writes the same JSON document
//! (`url`, `token`, `api_key`, `saved_at`) through a sibling temp file and an
//! atomic replace, so a crash mid-write never truncates the only valid
//! credential. POSIX keeps mode `0600`; the replace is what protects the
//! previous document on every platform.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// One saved credential document.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TokenDocument {
    pub url: String,
    #[serde(default)]
    pub token: String,
    #[serde(default)]
    pub api_key: Option<String>,
    #[serde(default)]
    pub saved_at: String,
}

/// The persisted UM session token.
#[derive(Debug, Clone)]
pub struct TokenStore {
    path: PathBuf,
    url: String,
}

impl TokenStore {
    pub fn new(path: PathBuf, url: impl Into<String>) -> Self {
        Self {
            path,
            url: url.into(),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Load the saved document only when it belongs to this upstream URL.
    ///
    /// Any unreadable or malformed document reads as "no credential", exactly
    /// like Python's `TokenStore.load`; a document for another URL is not this
    /// instance's credential.
    pub fn load(&self) -> Option<TokenDocument> {
        let raw = fs::read_to_string(&self.path).ok()?;
        let document: TokenDocument = serde_json::from_str(&raw).ok()?;
        (document.url == self.url).then_some(document)
    }

    /// Persist the credential through a controlled atomic replace.
    ///
    /// A login round has no new API key to publish; `None` keeps the API key the
    /// operator saved into the same document instead of erasing it.
    pub fn save(&self, token: &str, api_key: Option<&str>) -> io::Result<TokenDocument> {
        let api_key = api_key
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .or_else(|| self.load().and_then(|document| document.api_key))
            .filter(|value| !value.trim().is_empty());
        let document = TokenDocument {
            url: self.url.clone(),
            token: token.to_string(),
            api_key,
            saved_at: chrono::Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
        };
        let raw = serde_json::to_vec_pretty(&document)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let parent = self
            .path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&parent)?;
        let tmp_path = sibling_temp_path(&self.path);
        let result = write_temp(&tmp_path, &raw)
            .and_then(|()| set_private_mode(&tmp_path))
            .and_then(|()| replace_file(&tmp_path, &self.path));
        if result.is_err() {
            let _ = fs::remove_file(&tmp_path);
        }
        result.map(|()| document)
    }
}

fn sibling_temp_path(path: &Path) -> PathBuf {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_else(|| "token.json".to_string());
    path.with_file_name(format!("{}.{}.tmp", name, std::process::id()))
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
    // POSIX modes are best-effort, like Python: the atomic replace is what
    // protects the previous valid document.
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
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir() -> PathBuf {
        static COUNTER: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or_default();
        let sequence = COUNTER.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let path = std::env::temp_dir().join(format!(
            "um-login-token-{}-{stamp}-{sequence}",
            std::process::id()
        ));
        fs::create_dir_all(&path).expect("temp dir");
        path
    }

    #[test]
    fn a_saved_document_round_trips_with_the_python_keys() {
        let dir = temp_dir();
        let store = TokenStore::new(dir.join("token.json"), "http://127.0.0.1:8000");
        let saved = store.save("jwt-token", Some("api-key")).expect("save");
        assert_eq!(saved.url, "http://127.0.0.1:8000");
        let raw = fs::read_to_string(store.path()).expect("read token");
        let value: serde_json::Value = serde_json::from_str(&raw).expect("json");
        assert_eq!(value["url"], "http://127.0.0.1:8000");
        assert_eq!(value["token"], "jwt-token");
        assert_eq!(value["api_key"], "api-key");
        assert!(value["saved_at"]
            .as_str()
            .is_some_and(|value| value.len() == 19));
        assert!(!raw.contains("jwt-token") || raw.contains("\"token\""));
        let loaded = store.load().expect("load");
        assert_eq!(loaded.token, "jwt-token");
        assert_eq!(loaded.api_key.as_deref(), Some("api-key"));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_document_for_another_url_is_not_this_instances_credential() {
        let dir = temp_dir();
        let other = TokenStore::new(dir.join("token.json"), "http://other:8000");
        other.save("other-token", None).expect("save");
        let store = TokenStore::new(dir.join("token.json"), "http://127.0.0.1:8000");
        assert!(store.load().is_none());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn saving_a_new_token_preserves_a_manually_saved_api_key() {
        let dir = temp_dir();
        let store = TokenStore::new(dir.join("token.json"), "http://127.0.0.1:8000");
        store
            .save("first-token", Some("manual-api-key"))
            .expect("save");
        let saved = store.save("second-token", None).expect("save");
        assert_eq!(saved.api_key.as_deref(), Some("manual-api-key"));
        let raw = fs::read_to_string(store.path()).expect("read token");
        let value: serde_json::Value = serde_json::from_str(&raw).expect("json");
        assert_eq!(value["token"], "second-token");
        assert_eq!(value["api_key"], "manual-api-key");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn an_empty_api_key_never_clears_the_manually_saved_one() {
        let dir = temp_dir();
        let store = TokenStore::new(dir.join("token.json"), "http://127.0.0.1:8000");
        store
            .save("first-token", Some("manual-api-key"))
            .expect("save");
        let saved = store.save("second-token", Some("")).expect("save");
        assert_eq!(saved.api_key.as_deref(), Some("manual-api-key"));
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn an_explicit_api_key_replaces_the_saved_one() {
        let dir = temp_dir();
        let store = TokenStore::new(dir.join("token.json"), "http://127.0.0.1:8000");
        store.save("first-token", Some("old-key")).expect("save");
        let saved = store.save("second-token", Some("new-key")).expect("save");
        assert_eq!(saved.api_key.as_deref(), Some("new-key"));
        assert_eq!(
            store.load().expect("load").api_key.as_deref(),
            Some("new-key")
        );
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_save_failure_keeps_the_previous_document() {
        let dir = temp_dir();
        let store = TokenStore::new(dir.join("token.json"), "http://127.0.0.1:8000");
        store.save("first-token", None).expect("save");
        // A directory at the temp sibling path makes the temp write fail.
        fs::create_dir_all(dir.join(format!("token.json.{}.tmp", std::process::id())))
            .expect("blocking dir");
        assert!(store.save("second-token", None).is_err());
        assert_eq!(store.load().expect("previous").token, "first-token");
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_corrupt_document_reads_as_no_credential() {
        let dir = temp_dir();
        fs::write(dir.join("token.json"), "{not json").expect("write");
        let store = TokenStore::new(dir.join("token.json"), "http://127.0.0.1:8000");
        assert!(store.load().is_none());
        let _ = fs::remove_dir_all(dir);
    }
}
