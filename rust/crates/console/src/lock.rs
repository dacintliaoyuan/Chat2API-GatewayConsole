//! The console operation resource lock (`console/operations.py::ResourceFileLock`).
//!
//! The lock file name is derived from a sanitized resource id; the open handle
//! (Windows, share mode 0) or the `flock` descriptor (POSIX) IS the lock. Process
//! death releases it, so there is no stale-marker heuristic. Owner info is written
//! only for diagnostics and never decides ownership.

use std::fs::{File, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::operation_manager::OperationError;

#[derive(Debug)]
pub enum LockError {
    Busy,
    Cancelled,
    Open(String),
}

pub struct ResourceLock {
    file: Option<File>,
}

impl ResourceLock {
    /// Acquire `<lock_dir>/<sanitized resource>.lock` within `timeout`.
    pub fn acquire(
        lock_dir: &Path,
        resource: &str,
        owner: &str,
        timeout: Duration,
        cancel: &AtomicBool,
    ) -> Result<Self, LockError> {
        let name = match sanitize_resource(resource) {
            Ok(name) => format!("{name}.lock"),
            Err(error) => return Err(LockError::Open(error.message)),
        };
        let path = lock_dir.join(name);
        std::fs::create_dir_all(lock_dir)
            .map_err(|error| LockError::Open(format!("console lock open failed: {error}")))?;
        let deadline = Instant::now() + timeout;
        let file = loop {
            if cancel.load(Ordering::SeqCst) {
                return Err(LockError::Cancelled);
            }
            match try_open_exclusive(&path) {
                Ok(file) => break file,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    if Instant::now() >= deadline {
                        return Err(LockError::Busy);
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
                Err(error) => {
                    return Err(LockError::Open(format!(
                        "console lock open failed: {error}"
                    )))
                }
            }
        };
        let info = serde_json::json!({
            "pid": std::process::id(),
            "owner": owner,
            "resource": resource,
            "created_epoch": epoch_millis(),
        })
        .to_string();
        let mut file = file;
        let _ = file.set_len(0);
        let _ = file.seek(SeekFrom::Start(0));
        let _ = file.write_all(info.as_bytes());
        Ok(Self { file: Some(file) })
    }
}

impl Drop for ResourceLock {
    fn drop(&mut self) {
        // Closing the handle/descriptor releases the lock.
        let _ = self.file.take();
    }
}

/// `console/operations.py::_sanitize_resource`: a bounded lock name that can
/// never traverse out of the lock directory.
pub fn sanitize_resource(resource: &str) -> Result<String, OperationError> {
    let cleaned: String = resource
        .chars()
        .map(|character| {
            if character.is_ascii()
                && (character.is_ascii_alphanumeric() || character == '_' || character == '-')
            {
                character
            } else {
                '_'
            }
        })
        .collect();
    if resource.is_empty() || resource.chars().count() > 128 || cleaned.chars().count() > 64 {
        return Err(OperationError::new(
            "invalid_resource",
            "resource id is not usable",
        ));
    }
    Ok(cleaned)
}

#[cfg(windows)]
fn try_open_exclusive(path: &Path) -> std::io::Result<File> {
    use std::os::windows::fs::OpenOptionsExt;
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .share_mode(0)
        .open(path)
}

#[cfg(unix)]
fn try_open_exclusive(path: &Path) -> std::io::Result<File> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)?;
    use std::os::unix::io::AsRawFd;
    // SAFETY: the fd is owned by `file` for the call.
    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result == 0 {
        return Ok(file);
    }
    let error = std::io::Error::last_os_error();
    match error.raw_os_error() {
        Some(libc::EACCES) | Some(libc::EAGAIN) => Err(std::io::Error::new(
            std::io::ErrorKind::WouldBlock,
            "resource lock is held",
        )),
        _ => Err(error),
    }
}

#[cfg(not(any(windows, unix)))]
fn try_open_exclusive(path: &Path) -> std::io::Result<File> {
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
}

fn epoch_millis() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or_default()
}

/// Test seam: the lock file path one resource maps to.
pub fn lock_path(lock_dir: &Path, resource: &str) -> PathBuf {
    lock_dir.join(format!(
        "{}.lock",
        sanitize_resource(resource).unwrap_or_default()
    ))
}
