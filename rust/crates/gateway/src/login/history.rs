//! Durable UM login-round history (#115/#116).
//!
//! The local 8000 and cloud 8003 gateways each own one JSONL record per actually
//! started login round, whichever trigger started it. Concurrent triggers and
//! internal browser retries belong to that round and never add another record. The
//! console reads the same file without probing the upstream, starting a login or
//! writing a record.
//!
//! The file is append-only with bounded rotation: the newest records survive,
//! the active file rotates at [`ROTATE_BYTES`], records older than
//! [`RETENTION_SECONDS`] are dropped, and the active file plus every rotation
//! never exceeds [`MAX_TOTAL_BYTES`]. A write failure is logged without the
//! record contents and never fails a login or a request.

use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use serde_json::{json, Value};

use crate::login::profile::epoch_now;
use crate::reauth::ReauthTrigger;

pub const LOGIN_HISTORY_FILE_ENV: &str = "CHAT2API_LOGIN_HISTORY_FILE";
pub const DEFAULT_LOGIN_HISTORY_FILE: &str = "data/login-history.jsonl";
/// The cloud 8003 process keeps its history beside the other writable share state.
pub const DEFAULT_CLOUD_LOGIN_HISTORY_FILE: &str = "/var/lib/um-share/login-history.jsonl";
/// How long a round stays queryable.
pub const RETENTION_SECONDS: f64 = 7.0 * 86_400.0;
/// The active file rotates once it would grow past this size.
pub const ROTATE_BYTES: u64 = 1024 * 1024;
/// Rotation files kept beside the active file (`.1` .. `.3`).
pub const MAX_ROTATIONS: u32 = 3;
/// The active file plus every rotation stays under this bound.
pub const MAX_TOTAL_BYTES: u64 = 4 * 1024 * 1024;
/// One stored reason is a bounded summary, never a whole upstream response.
pub const MAX_REASON_CHARS: usize = 160;

const ACTIVE_NAME: &str = "login-history.jsonl";

/// The role default: local 8000 keeps its history beside its fault record;
/// cloud 8003 keeps it in the writable share state.
pub fn default_path(role: crate::config::Role) -> PathBuf {
    match role {
        crate::config::Role::Local => PathBuf::from(DEFAULT_LOGIN_HISTORY_FILE),
        crate::config::Role::Cloud => PathBuf::from(DEFAULT_CLOUD_LOGIN_HISTORY_FILE),
    }
}

/// Resolve the effective path. An explicit value wins even in candidate mode; a
/// candidate without one never writes a production file.
pub fn effective_path(
    explicit: Option<PathBuf>,
    role: crate::config::Role,
    candidate: bool,
) -> Option<PathBuf> {
    explicit.or_else(|| (!candidate).then(|| default_path(role)))
}

/// One login round as the console reads it.
#[derive(Debug, Clone, PartialEq)]
pub struct LoginRoundRecord {
    /// The process run that owns the round; two processes may reuse a round
    /// number and stay distinguishable.
    pub run_id: String,
    pub round: u64,
    /// `running`, `completed` or `incomplete`.
    pub state: String,
    pub trigger_source: Option<String>,
    pub visible: bool,
    pub started_epoch: f64,
    pub ended_epoch: Option<f64>,
    pub submit_count: usize,
    pub outcome: Option<String>,
    pub reason: Option<String>,
    /// The last upstream HTTP status this round actually obtained.
    pub upstream_status: Option<u16>,
    /// The last credential-probe verdict this round observed.
    pub evidence_verdict: Option<String>,
    pub rejected_before_expiry_status: Option<u16>,
}

impl LoginRoundRecord {
    fn as_json(&self) -> Value {
        json!({
            "schema": 1,
            "run_id": self.run_id,
            "round": self.round,
            "state": self.state,
            "trigger_source": self.trigger_source,
            "visible": self.visible,
            "started_epoch": self.started_epoch,
            "ended_epoch": self.ended_epoch,
            "submit_count": self.submit_count,
            "outcome": self.outcome,
            "reason": self.reason,
            "upstream_status": self.upstream_status,
            "evidence_verdict": self.evidence_verdict,
            "rejected_before_expiry_status": self.rejected_before_expiry_status,
        })
    }

    fn from_json(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        let state = object.get("state")?.as_str()?;
        if !matches!(state, "running" | "completed" | "incomplete") {
            return None;
        }
        Some(Self {
            run_id: object.get("run_id")?.as_str()?.to_string(),
            round: object.get("round")?.as_u64()?,
            state: state.to_string(),
            trigger_source: object
                .get("trigger_source")
                .and_then(Value::as_str)
                .map(str::to_string),
            visible: object
                .get("visible")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            started_epoch: object.get("started_epoch")?.as_f64()?,
            ended_epoch: object.get("ended_epoch").and_then(Value::as_f64),
            submit_count: object
                .get("submit_count")
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize,
            outcome: object
                .get("outcome")
                .and_then(Value::as_str)
                .map(str::to_string),
            reason: object
                .get("reason")
                .and_then(Value::as_str)
                .map(sanitize_reason),
            upstream_status: object
                .get("upstream_status")
                .and_then(Value::as_u64)
                .map(|status| status as u16),
            evidence_verdict: object
                .get("evidence_verdict")
                .and_then(Value::as_str)
                .map(str::to_string),
            rejected_before_expiry_status: object
                .get("rejected_before_expiry_status")
                .and_then(Value::as_u64)
                .filter(|status| matches!(status, 401 | 403))
                .map(|status| status as u16),
        })
    }
}

/// One writer for the local 8000 gateway.
pub struct LoginHistory {
    path: Option<PathBuf>,
    run_id: String,
    limits: HistoryLimits,
    active: Mutex<Option<LoginRoundRecord>>,
    write_lock: Mutex<()>,
}

/// The bounded retention of one history file set.
#[derive(Debug, Clone, Copy)]
pub struct HistoryLimits {
    pub rotate_bytes: u64,
    pub max_total_bytes: u64,
    pub retention_seconds: f64,
}

impl Default for HistoryLimits {
    fn default() -> Self {
        Self {
            rotate_bytes: ROTATE_BYTES,
            max_total_bytes: MAX_TOTAL_BYTES,
            retention_seconds: RETENTION_SECONDS,
        }
    }
}

impl LoginHistory {
    pub fn open(path: Option<PathBuf>) -> Self {
        Self::open_at(path, epoch_now())
    }

    /// Open and mark every round left `running` by an earlier process as
    /// `incomplete`, preserving its recorded progress.
    pub fn open_at(path: Option<PathBuf>, now: f64) -> Self {
        Self::open_with_limits(path, HistoryLimits::default(), now)
    }

    /// The explicit-limits constructor: production uses [`HistoryLimits::default`];
    /// tests use smaller bounds without changing the retention behavior.
    pub fn open_with_limits(path: Option<PathBuf>, limits: HistoryLimits, now: f64) -> Self {
        let history = Self {
            path,
            run_id: new_run_id(),
            limits,
            active: Mutex::new(None),
            write_lock: Mutex::new(()),
        };
        history.mark_previous_incomplete_at(now);
        history.prune_at(now);
        history
    }

    pub fn run_id(&self) -> &str {
        &self.run_id
    }

    pub fn is_persistent(&self) -> bool {
        self.path.is_some()
    }

    /// Record one actually started round.
    pub fn begin_at(
        &self,
        round: u64,
        trigger: Option<ReauthTrigger>,
        visible: bool,
        started_epoch: f64,
    ) {
        let Some(_) = &self.path else {
            return;
        };
        let record = LoginRoundRecord {
            run_id: self.run_id.clone(),
            round,
            state: "running".to_string(),
            trigger_source: trigger.map(|trigger| trigger.as_str().to_string()),
            visible,
            started_epoch,
            ended_epoch: None,
            submit_count: 0,
            outcome: None,
            reason: None,
            upstream_status: None,
            evidence_verdict: None,
            rejected_before_expiry_status: None,
        };
        let mut active = self.active.lock().expect("login history active poisoned");
        *active = Some(record.clone());
        drop(active);
        self.append(&record, started_epoch);
    }

    /// Record a progress update without adding a round.
    pub fn note_submission(&self, round: u64, submit_count: usize) {
        let Some(record) = self.update_active(round, |record| {
            if submit_count <= record.submit_count {
                return false;
            }
            record.submit_count = submit_count;
            true
        }) else {
            return;
        };
        self.append(&record, epoch_now());
    }

    /// A background round became the visible one: same round, updated mode.
    pub fn note_visible(&self, round: u64) {
        let Some(record) = self.update_active(round, |record| {
            if record.visible {
                return false;
            }
            record.visible = true;
            true
        }) else {
            return;
        };
        self.append(&record, epoch_now());
    }

    pub fn note_early_rejection(&self, round: u64, status: u16) {
        let Some(record) = self.update_active(round, |record| {
            if record.rejected_before_expiry_status.is_some() {
                return false;
            }
            record.rejected_before_expiry_status = Some(status);
            true
        }) else {
            return;
        };
        self.append(&record, epoch_now());
    }

    /// Finish the round; a round invalidated by cancel is not written twice.
    pub fn finish(
        &self,
        round: u64,
        outcome: &str,
        reason: Option<&str>,
        upstream_status: Option<u16>,
        evidence_verdict: Option<&str>,
    ) {
        self.finish_at(
            round,
            outcome,
            reason,
            upstream_status,
            evidence_verdict,
            epoch_now(),
        );
    }

    pub fn finish_at(
        &self,
        round: u64,
        outcome: &str,
        reason: Option<&str>,
        upstream_status: Option<u16>,
        evidence_verdict: Option<&str>,
        ended_epoch: f64,
    ) {
        let Some(_) = &self.path else {
            return;
        };
        let mut active = self.active.lock().expect("login history active poisoned");
        let Some(mut record) = active.take() else {
            return;
        };
        if record.round != round {
            *active = Some(record);
            return;
        }
        record.state = "completed".to_string();
        record.ended_epoch = Some(ended_epoch);
        record.outcome = Some(outcome.to_string());
        record.reason = reason.map(sanitize_reason);
        record.upstream_status = upstream_status;
        record.evidence_verdict = evidence_verdict.map(str::to_string);
        drop(active);
        self.append(&record, ended_epoch);
    }

    /// Mark every round left `running` by another process run as `incomplete`.
    pub fn mark_previous_incomplete_at(&self, now: f64) {
        let Some(path) = &self.path else {
            return;
        };
        let records = match read_records(path) {
            Ok(records) => records,
            Err(_) => return,
        };
        for record in records {
            if record.state != "running" || record.run_id == self.run_id {
                continue;
            }
            let mut record = record;
            record.state = "incomplete".to_string();
            record.reason = Some("process_exit".to_string());
            record.ended_epoch = None;
            self.append(&record, now);
        }
    }

    /// Apply the seven-day and total-size bounds now.
    pub fn prune_at(&self, now: f64) {
        let Some(_) = &self.path else {
            return;
        };
        let _guard = self
            .write_lock
            .lock()
            .expect("login history write poisoned");
        if let Err(error) = self.prune_locked(now) {
            self.log_failure(&error);
        }
    }

    fn update_active(
        &self,
        round: u64,
        update: impl FnOnce(&mut LoginRoundRecord) -> bool,
    ) -> Option<LoginRoundRecord> {
        let Some(_) = &self.path else {
            return None;
        };
        let mut active = self.active.lock().expect("login history active poisoned");
        let record = active.as_mut()?;
        if record.round != round || !update(record) {
            return None;
        }
        Some(record.clone())
    }

    fn append(&self, record: &LoginRoundRecord, now: f64) {
        let Some(_) = &self.path else {
            return;
        };
        let _guard = self
            .write_lock
            .lock()
            .expect("login history write poisoned");
        if let Err(error) = self.append_locked(record, now) {
            self.log_failure(&error);
        }
    }

    fn log_failure(&self, error: &io::Error) {
        let path = self
            .path
            .as_ref()
            .map(|path| path.display().to_string())
            .unwrap_or_default();
        // 只记录脱敏的路径与错误类别，不写记录内容、凭据或上游响应。
        eprintln!("um-gateway: login history write failed ({path}): {error}");
    }

    fn append_locked(&self, record: &LoginRoundRecord, now: f64) -> io::Result<()> {
        let path = self.path.as_ref().expect("login history path");
        let parent = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&parent)?;
        self.prune_if_needed_locked(now)?;
        let raw = serde_json::to_string(&record.as_json())
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let line = format!("{raw}\n");
        let active_size = fs::metadata(path)
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        if active_size > 0 && active_size + line.len() as u64 > self.limits.rotate_bytes {
            self.rotate_locked()?;
        }
        let mut file = OpenOptions::new().create(true).append(true).open(path)?;
        file.write_all(line.as_bytes())?;
        file.flush()?;
        if self.total_bytes_locked()? > self.limits.max_total_bytes {
            self.prune_locked(now)?;
        }
        Ok(())
    }

    fn rotate_locked(&self) -> io::Result<()> {
        let path = self.path.as_ref().expect("login history path");
        let oldest = path.with_file_name(format!("{ACTIVE_NAME}.{MAX_ROTATIONS}"));
        let _ = fs::remove_file(oldest);
        for index in (1..MAX_ROTATIONS).rev() {
            let from = path.with_file_name(format!("{ACTIVE_NAME}.{index}"));
            let to = path.with_file_name(format!("{ACTIVE_NAME}.{}", index + 1));
            match fs::rename(&from, &to) {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
        if path.exists() {
            fs::rename(path, path.with_file_name(format!("{ACTIVE_NAME}.1")))?;
        }
        Ok(())
    }

    fn prune_if_needed_locked(&self, now: f64) -> io::Result<()> {
        let over_size = self.total_bytes_locked()? > self.limits.max_total_bytes;
        let oldest = self.oldest_started_locked()?;
        if over_size || oldest.is_some_and(|started| started < now - self.limits.retention_seconds)
        {
            self.prune_locked(now)?;
        }
        Ok(())
    }

    /// Drop records older than the retention bound and the oldest records until
    /// the active file plus rotations fits the total-size bound.
    fn prune_locked(&self, now: f64) -> io::Result<()> {
        let path = self.path.as_ref().expect("login history path");
        let cutoff = now - self.limits.retention_seconds;
        let mut lines: Vec<String> = Vec::new();
        for file in self.ordered_files_locked()? {
            let text = match fs::read_to_string(&file) {
                Ok(text) => text,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            };
            for line in text.lines() {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                let Some(record) = serde_json::from_str::<Value>(trimmed)
                    .ok()
                    .and_then(|value| LoginRoundRecord::from_json(&value))
                else {
                    continue;
                };
                if record.started_epoch < cutoff {
                    continue;
                }
                lines.push(trimmed.to_string());
            }
        }
        let mut total: u64 = lines.iter().map(|line| line.len() as u64 + 1).sum::<u64>();
        while total > self.limits.max_total_bytes && !lines.is_empty() {
            let dropped = lines.remove(0);
            total -= dropped.len() as u64 + 1;
        }
        let parent = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        fs::create_dir_all(&parent)?;
        let temporary = path.with_file_name(format!("{ACTIVE_NAME}.{}.tmp", std::process::id()));
        let mut file = fs::File::create(&temporary)?;
        for line in &lines {
            writeln!(file, "{line}")?;
        }
        file.flush()?;
        file.sync_all()?;
        drop(file);
        for index in 1..=MAX_ROTATIONS {
            let rotated = path.with_file_name(format!("{ACTIVE_NAME}.{index}"));
            match fs::remove_file(rotated) {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
        replace_file(&temporary, path)
    }

    fn oldest_started_locked(&self) -> io::Result<Option<f64>> {
        for file in self.ordered_files_locked()? {
            let text = match fs::read_to_string(&file) {
                Ok(text) => text,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            };
            for line in text.lines() {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                if let Some(record) = serde_json::from_str::<Value>(trimmed)
                    .ok()
                    .and_then(|value| LoginRoundRecord::from_json(&value))
                {
                    return Ok(Some(record.started_epoch));
                }
            }
        }
        Ok(None)
    }

    fn total_bytes_locked(&self) -> io::Result<u64> {
        let mut total = 0;
        for file in self.all_files_locked() {
            match fs::metadata(&file) {
                Ok(metadata) => total += metadata.len(),
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
        }
        Ok(total)
    }

    /// Oldest rotation first, active file last.
    fn ordered_files_locked(&self) -> io::Result<Vec<PathBuf>> {
        let path = self.path.as_ref().expect("login history path");
        let mut files = Vec::new();
        for index in (1..=MAX_ROTATIONS).rev() {
            files.push(path.with_file_name(format!("{ACTIVE_NAME}.{index}")));
        }
        files.push(path.clone());
        Ok(files)
    }

    fn all_files_locked(&self) -> Vec<PathBuf> {
        let path = self.path.as_ref().expect("login history path");
        let mut files = vec![path.clone()];
        for index in 1..=MAX_ROTATIONS {
            files.push(path.with_file_name(format!("{ACTIVE_NAME}.{index}")));
        }
        files
    }
}

/// Read every retained round, newest first. A missing file is a legal empty
/// history; an unreadable file is an error, never an empty list.
pub fn read_records(path: &Path) -> io::Result<Vec<LoginRoundRecord>> {
    let mut files = Vec::new();
    for index in (1..=MAX_ROTATIONS).rev() {
        files.push(path.with_file_name(format!("{ACTIVE_NAME}.{index}")));
    }
    files.push(path.to_path_buf());
    let mut latest: HashMap<(String, u64), LoginRoundRecord> = HashMap::new();
    let mut found = false;
    for file in files {
        let text = match fs::read_to_string(&file) {
            Ok(text) => text,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        found = true;
        for line in text.lines() {
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let Some(record) = serde_json::from_str::<Value>(trimmed)
                .ok()
                .and_then(|value| LoginRoundRecord::from_json(&value))
            else {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "invalid login history record",
                ));
            };
            latest.insert((record.run_id.clone(), record.round), record);
        }
    }
    if !found {
        return Ok(Vec::new());
    }
    let mut records: Vec<LoginRoundRecord> = latest.into_values().collect();
    records.sort_by(|left, right| {
        right
            .started_epoch
            .partial_cmp(&left.started_epoch)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left.run_id.cmp(&right.run_id))
            .then_with(|| left.round.cmp(&right.round))
    });
    Ok(records)
}

fn new_run_id() -> String {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let millis = (epoch_now() * 1000.0).round() as u64;
    format!(
        "{millis}-{}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::SeqCst)
    )
}

/// A bounded, single-line reason. It drops control characters and markup,
/// redacts bearer and token-like values, and never stores an upstream body.
pub fn sanitize_reason(raw: &str) -> String {
    let mut output = String::new();
    let mut in_tag = false;
    for character in raw.chars() {
        match character {
            '<' => in_tag = true,
            '>' if in_tag => in_tag = false,
            _ if in_tag => {}
            '\r' | '\n' | '\t' => output.push(' '),
            character if character.is_control() => {}
            character => output.push(character),
        }
    }
    let collapsed = output.split_whitespace().collect::<Vec<_>>().join(" ");
    let redacted = redact_secrets(&collapsed);
    redacted.chars().take(MAX_REASON_CHARS).collect()
}

fn redact_secrets(text: &str) -> String {
    let mut output = String::new();
    let mut rest = text;
    loop {
        let lowered = rest.to_ascii_lowercase();
        let mut best: Option<(usize, usize)> = None;
        for marker in ["bearer ", "token=", "password=", "api_key=", "cookie="] {
            if let Some(index) = lowered.find(marker) {
                let end = index + marker.len();
                let value_end = rest[end..]
                    .find(char::is_whitespace)
                    .map(|offset| end + offset)
                    .unwrap_or(rest.len());
                if best.map_or(true, |(start, _)| index < start) {
                    best = Some((index, value_end));
                }
            }
        }
        let Some((start, end)) = best else {
            output.push_str(rest);
            break;
        };
        output.push_str(&rest[..start]);
        output.push_str("[redacted]");
        rest = &rest[end..];
    }
    output
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
    fn a_reason_is_bounded_and_never_keeps_a_bearer_or_markup() {
        let reason = format!("<html>Bearer secret-token</html>{}", "x".repeat(300));
        let sanitized = sanitize_reason(&reason);
        assert!(!sanitized.contains("secret-token"));
        assert!(!sanitized.contains('<'));
        assert!(sanitized.chars().count() <= MAX_REASON_CHARS);
    }

    #[test]
    fn the_default_path_follows_the_role_and_candidates_stay_memory_only() {
        assert_eq!(
            effective_path(None, crate::config::Role::Local, false),
            Some(PathBuf::from(DEFAULT_LOGIN_HISTORY_FILE))
        );
        assert_eq!(
            effective_path(None, crate::config::Role::Cloud, false),
            Some(PathBuf::from(DEFAULT_CLOUD_LOGIN_HISTORY_FILE))
        );
        assert_eq!(effective_path(None, crate::config::Role::Local, true), None);
        assert_eq!(effective_path(None, crate::config::Role::Cloud, true), None);
        assert_eq!(
            effective_path(
                Some(PathBuf::from("tmp/history.jsonl")),
                crate::config::Role::Cloud,
                true
            ),
            Some(PathBuf::from("tmp/history.jsonl"))
        );
    }
}
