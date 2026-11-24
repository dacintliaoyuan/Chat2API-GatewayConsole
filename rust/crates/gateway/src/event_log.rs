//! Gateway runtime event log.
//!
//! Python writes a `request summary` line for every logical request, including
//! `tries`/`physical_attempts` on a final failure (`chat2api/chat2api.py`
//! `log_summary`). The Rust candidate keeps the same additive contract but only
//! records final failures: successful requests already have the usage ledger, and a
//! candidate must not invent a production log path. The optional path comes from
//! `CHAT2API_SERVICE_LOG`; without it the event is not written.

use std::fs::OpenOptions;
use std::io::{self, Cursor, Write};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Instant;

use chrono::{Local, SecondsFormat, Utc};
use serde_json::json;

/// The retry counters of one logical request.
///
/// `tries` counts logical attempts including the first; `physical_attempts`
/// counts actual upstream requests. They coincide until a future authentication
/// retry separates the two layers, but keeping both fields makes a first failure
/// distinguishable from retry exhaustion without changing the log schema.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct AttemptStats {
    pub tries: u32,
    pub physical_attempts: u32,
}

impl AttemptStats {
    /// Record one attempt that reached the upstream client.
    pub fn record_attempt(&mut self) {
        self.tries += 1;
        self.physical_attempts += 1;
    }
}

/// One final upstream failure ready for the runtime event log.
pub struct FinalFailure<'a> {
    pub request_id: &'a str,
    pub protocol: &'static str,
    pub final_status: String,
    pub stats: AttemptStats,
    pub started_at: Instant,
}

/// Safe diagnostic fields for a local Chat attempt. No response text is logged.
pub(crate) struct ChatAttemptEvidence<'a> {
    pub request_id: &'a str,
    pub attempt: u32,
    pub upstream_status: Option<u16>,
    pub upstream_request_id: Option<&'a str>,
    pub transport_error: Option<&'static str>,
    pub reason: Option<&'static str>,
    pub error_body: Option<&'static str>,
    pub stream_options: &'static str,
    pub retry_without_stream_options: bool,
}

/// Append-only runtime event log; a write failure never affects the response.
pub struct EventLog {
    path: Option<PathBuf>,
    lock: Mutex<()>,
}

impl EventLog {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self {
            path,
            lock: Mutex::new(()),
        }
    }

    pub(crate) fn enabled(&self) -> bool {
        self.path.is_some()
    }

    pub(crate) fn chat_attempt(&self, evidence: ChatAttemptEvidence<'_>) {
        self.append_event(json!({
            "time": Local::now().to_rfc3339_opts(SecondsFormat::Secs, false),
            "service": "chat2api",
            "event": "upstream_chat_attempt",
            "protocol": "chat",
            "request_id": evidence.request_id,
            "attempt": evidence.attempt,
            "upstream_status": evidence.upstream_status,
            "upstream_request_id": evidence.upstream_request_id,
            "transport_error": evidence.transport_error,
            "reason": evidence.reason,
            "error_body": evidence.error_body,
            "stream_options": evidence.stream_options,
            "retry_without_stream_options": evidence.retry_without_stream_options,
        }));
    }

    pub(crate) fn listener_failure(&self, component: &str, operation: &str, error: &io::Error) {
        // Log just the operation and numeric OS error. Formatting into a fixed
        // buffer keeps the diagnostic small even during allocation pressure.
        let mut buffer = [0; 256];
        let mut line = Cursor::new(buffer.as_mut_slice());
        if writeln!(
            line,
            "{} WARNING listener_failure component={} operation={} kind={:?} os_error={:?}",
            Utc::now().format("%Y-%m-%dT%H:%M:%SZ"),
            component,
            operation,
            error.kind(),
            error.raw_os_error(),
        )
        .is_ok()
        {
            let len = line.position() as usize;
            if self.append(&buffer[..len]).is_err() || self.path.is_none() {
                let _ = io::stderr().lock().write_all(&buffer[..len]);
            }
        }
    }

    /// Record one final failure. This is best-effort by design: the log is
    /// diagnostic, so an unwritable path must not mask the upstream outcome.
    pub fn final_failure(&self, failure: FinalFailure<'_>) {
        if self.path.is_none() {
            return;
        }
        let row = json!({
            "time": Local::now().to_rfc3339_opts(SecondsFormat::Secs, false),
            "service": "chat2api",
            "event": "request_summary",
            "request_id": failure.request_id,
            "protocol": failure.protocol,
            "final": failure.final_status,
            "tries": failure.stats.tries,
            "physical_attempts": failure.stats.physical_attempts,
            "duration_ms": failure.started_at.elapsed().as_millis() as u64,
        });
        self.append_event(row);
    }

    fn append_event(&self, row: serde_json::Value) {
        let mut line = serde_json::to_string(&row).unwrap_or_default();
        line.push('\n');
        if self.append(line.as_bytes()).is_err() {
            eprintln!("[chat2api] service log write failed");
        }
    }

    fn append(&self, line: &[u8]) -> io::Result<()> {
        let Some(path) = &self.path else {
            return Ok(());
        };
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)?;
        }
        let _guard = self.lock.lock().expect("event log lock poisoned");
        OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?
            .write_all(line)
    }
}
