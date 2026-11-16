//! Gateway runtime event log.
//!
//! Python writes a `request summary` line for every logical request, including
//! `tries`/`physical_attempts` on a final failure (`chat2api/chat2api.py`
//! `log_summary`). The Rust candidate keeps the same additive contract but only
//! records final failures: successful requests already have the usage ledger, and a
//! candidate must not invent a production log path. The optional path comes from
//! `CHAT2API_SERVICE_LOG`; without it the event is not written.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Instant;

use chrono::{Local, SecondsFormat};
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

    /// Record one final failure. This is best-effort by design: the log is
    /// diagnostic, so an unwritable path must not mask the upstream outcome.
    pub fn final_failure(&self, failure: FinalFailure<'_>) {
        let Some(path) = &self.path else {
            return;
        };
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
        let mut line = serde_json::to_string(&row).unwrap_or_default();
        line.push('\n');
        let result = (|| -> std::io::Result<()> {
            if let Some(parent) = path.parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent)?;
                }
            }
            let _guard = self.lock.lock().expect("event log lock poisoned");
            let mut file = OpenOptions::new().create(true).append(true).open(path)?;
            file.write_all(line.as_bytes())
        })();
        if result.is_err() {
            eprintln!("[chat2api] service log write failed");
        }
    }
}
