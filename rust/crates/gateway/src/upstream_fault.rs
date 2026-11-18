//! Durable UM upstream fault records (#103).
//!
//! The local 8000 gateway owns one record for the upstream it proxies. The first
//! observed 5xx, transport failure or timeout opens it; later failures update the
//! same record until contract-valid success evidence closes it. The record is a
//! small JSON document written atomically on every change, so a restart keeps the
//! active fault and the closed history. 8001 reads the same document; a read never
//! probes the upstream or writes the record.
//!
//! Only a bounded, sanitized summary is stored. Credentials, request bodies and
//! whole upstream error pages stay at their original boundary.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

/// Newest closed records kept on disk.
pub const MAX_HISTORY: usize = 50;
/// Bound of one stored summary.
pub const MAX_SUMMARY_CHARS: usize = 200;
/// The recognizable Envoy short phrase that is kept verbatim.
pub const NO_HEALTHY_UPSTREAM: &str = "no healthy upstream";
/// The default local record path; the local production 8000/8001 pair shares it.
pub const DEFAULT_FAULT_LOG: &str = "data/upstream-faults.json";

/// Which observer produced one failure or recovery observation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultSource {
    /// The periodic auth-watch round.
    BackgroundProbe,
    /// `/ready` or the explicit `/admin/auth/verify` probe.
    ActiveProbe,
    /// The final result of one business request.
    BusinessRequest,
}

impl FaultSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BackgroundProbe => "background_probe",
            Self::ActiveProbe => "active_probe",
            Self::BusinessRequest => "business_request",
        }
    }
}

/// How one failure is classified in the record.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultClass {
    /// Any upstream 5xx response.
    Upstream5xx,
    /// A timeout before a usable response.
    Timeout,
    /// A connection/DNS/TLS/protocol failure.
    NetworkError,
    /// A stream that failed or ended without a contract-valid completion.
    StreamError,
}

impl FaultClass {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Upstream5xx => "upstream_5xx",
            Self::Timeout => "timeout",
            Self::NetworkError => "network_error",
            Self::StreamError => "stream_error",
        }
    }
}

/// One failure observation, ready to update the active record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FaultObservation {
    pub class: FaultClass,
    pub upstream_status: Option<u16>,
    pub summary: Option<String>,
    pub source: FaultSource,
}

/// One durable fault log.
pub struct UpstreamFaultLog {
    path: Option<PathBuf>,
    state: Mutex<FaultState>,
    write_lock: Mutex<()>,
}

#[derive(Debug, Clone, Default)]
struct FaultState {
    active: Option<ActiveFault>,
    history: Vec<Value>,
    last_check: Option<Value>,
    next_check_epoch: Option<f64>,
}

#[derive(Debug, Clone)]
struct ActiveFault {
    id: String,
    started_epoch: f64,
    last_epoch: f64,
    source: FaultSource,
    failures: u64,
    class: FaultClass,
    upstream_status: Option<u16>,
    summary: Option<String>,
}

impl ActiveFault {
    fn to_json(&self) -> Value {
        json!({
            "id": self.id,
            "started_epoch": self.started_epoch,
            "last_epoch": self.last_epoch,
            "source": self.source.as_str(),
            "failures": self.failures,
            "classification": self.class.as_str(),
            "upstream_status": self.upstream_status,
            "summary": self.summary,
        })
    }
}

impl UpstreamFaultLog {
    /// Open one log; `None` keeps it in memory only (candidate tests).
    pub fn open(path: Option<PathBuf>) -> Self {
        let state = path
            .as_deref()
            .and_then(read_state)
            .unwrap_or_default();
        Self {
            path,
            state: Mutex::new(state),
            write_lock: Mutex::new(()),
        }
    }

    /// Whether this log has a durable path.
    pub fn is_persistent(&self) -> bool {
        self.path.is_some()
    }

    /// Update the record with one failed observation.
    ///
    /// The first failure opens a new record; every later failure without
    /// recovery evidence updates the same one and increments the count exactly
    /// once per observation.
    pub fn record_failure(&self, observation: FaultObservation) {
        let now = epoch_now();
        let mut state = self.state.lock().expect("fault state poisoned");
        match state.active.as_mut() {
            Some(active) => {
                active.last_epoch = now;
                active.source = observation.source;
                active.failures = active.failures.saturating_add(1);
                active.class = observation.class;
                active.upstream_status = observation.upstream_status;
                if let Some(summary) = observation.summary {
                    active.summary = Some(summary);
                }
            }
            None => {
                state.active = Some(ActiveFault {
                    id: new_fault_id(now),
                    started_epoch: now,
                    last_epoch: now,
                    source: observation.source,
                    failures: 1,
                    class: observation.class,
                    upstream_status: observation.upstream_status,
                    summary: observation.summary,
                });
            }
        }
        drop(state);
        self.persist();
    }

    /// Close the active fault with contract-valid success evidence.
    ///
    /// Without an active fault this is a no-op: a success does not create a
    /// history row on its own.
    pub fn record_recovery(
        &self,
        source: FaultSource,
        upstream_status: Option<u16>,
        detail: &str,
    ) {
        let now = epoch_now();
        let mut state = self.state.lock().expect("fault state poisoned");
        let Some(active) = state.active.take() else {
            return;
        };
        let mut recovered = active.to_json();
        if let Some(object) = recovered.as_object_mut() {
            object.insert("recovered_epoch".to_string(), json!(now));
            object.insert("recovery_source".to_string(), json!(source.as_str()));
            object.insert("recovery_status".to_string(), json!(upstream_status));
            object.insert("recovery_detail".to_string(), json!(detail));
        }
        state.history.insert(0, recovered);
        state.history.truncate(MAX_HISTORY);
        drop(state);
        self.persist();
    }

    /// Record the result of one probe and the time of the next background
    /// check, then apply the failure/recovery rule.
    pub fn record_probe(
        &self,
        source: FaultSource,
        result: &str,
        upstream_status: Option<u16>,
        summary: Option<String>,
        next_check_epoch: Option<f64>,
    ) {
        let mut state = self.state.lock().expect("fault state poisoned");
        state.last_check = Some(json!({
            "epoch": epoch_now(),
            "source": source.as_str(),
            "result": result,
            "upstream_status": upstream_status,
        }));
        if next_check_epoch.is_some() {
            state.next_check_epoch = next_check_epoch;
        }
        let has_active = state.active.is_some();
        drop(state);
        if let Some(class) = fault_class_of_probe_result(result) {
            self.record_failure(FaultObservation {
                class,
                upstream_status,
                summary,
                source,
            });
        } else if result == "valid" {
            if has_active {
                self.record_recovery(source, upstream_status, "credential_probe");
            } else {
                self.persist();
            }
        } else {
            self.persist();
        }
    }

    /// Publish the time of the next periodic check without changing the fault.
    pub fn record_next_check(&self, next_check_epoch: f64) {
        let mut state = self.state.lock().expect("fault state poisoned");
        state.next_check_epoch = Some(next_check_epoch);
        drop(state);
        self.persist();
    }

    /// The side-effect-free read face used by `/health` and 8001.
    pub fn snapshot(&self) -> Value {
        let state = self.state.lock().expect("fault state poisoned");
        let active = state
            .active
            .as_ref()
            .map(ActiveFault::to_json)
            .unwrap_or(Value::Null);
        json!({
            "active": active,
            "history": state.history,
            "last_check": state.last_check,
            "next_check_epoch": state.next_check_epoch,
        })
    }

    fn persist(&self) {
        let Some(path) = &self.path else {
            return;
        };
        let state = self.state.lock().expect("fault state poisoned");
        let document = json!({
            "schema_version": 1,
            "active": state.active.as_ref().map(ActiveFault::to_json).unwrap_or(Value::Null),
            "history": state.history,
            "last_check": state.last_check,
            "next_check_epoch": state.next_check_epoch,
        });
        drop(state);
        let _guard = self.write_lock.lock().expect("fault write lock poisoned");
        let _ = write_atomic(path, &document);
    }
}

/// Read a persisted log for the 8001 read face.
///
/// A missing file is the legal empty record; an unreadable or malformed file is
/// an explicit read error and never folded into a clean empty record.
pub fn read_snapshot(path: &Path) -> Result<Value, ()> {
    match std::fs::read_to_string(path) {
        Ok(text) => {
            let value: Value = serde_json::from_str(&text).map_err(|_| ())?;
            let object = value.as_object().ok_or(())?;
            Ok(json!({
                "active": object.get("active").cloned().unwrap_or(Value::Null),
                "history": object
                    .get("history")
                    .cloned()
                    .unwrap_or_else(|| json!([])),
                "last_check": object.get("last_check").cloned().unwrap_or(Value::Null),
                "next_check_epoch": object
                    .get("next_check_epoch")
                    .cloned()
                    .unwrap_or(Value::Null),
            }))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(json!({
            "active": Value::Null,
            "history": [],
            "last_check": Value::Null,
            "next_check_epoch": Value::Null,
        })),
        Err(_) => Err(()),
    }
}

/// Classify one status response as an upstream fault.
pub fn fault_class_of_status(status: u16) -> Option<FaultClass> {
    (500..=599).contains(&status).then_some(FaultClass::Upstream5xx)
}

/// Classify one transport failure as an upstream fault.
pub fn fault_class_of_failure(failure: crate::upstream::UpstreamFailure) -> FaultClass {
    match failure {
        crate::upstream::UpstreamFailure::Timeout => FaultClass::Timeout,
        crate::upstream::UpstreamFailure::ConnectionError
        | crate::upstream::UpstreamFailure::Protocol => FaultClass::NetworkError,
    }
}

/// Extract the bounded, sanitized summary of one upstream error body.
///
/// The recognizable Envoy phrase is kept exactly. Every other text is collapsed
/// to one line, stripped of control characters and markup, redacted for bearer
/// tokens and bounded to [`MAX_SUMMARY_CHARS`].
pub fn summarize_error_text(raw: &str) -> Option<String> {
    let text = raw.trim();
    if text.is_empty() {
        return None;
    }
    let lowered = text.to_ascii_lowercase();
    if lowered.contains(NO_HEALTHY_UPSTREAM) {
        return Some(NO_HEALTHY_UPSTREAM.to_string());
    }
    let extracted = serde_json::from_str::<Value>(text)
        .ok()
        .and_then(|value| json_error_message(&value))
        .unwrap_or_else(|| text.to_string());
    let sanitized = sanitize_summary(&extracted);
    (!sanitized.is_empty()).then_some(sanitized)
}

fn json_error_message(value: &Value) -> Option<String> {
    let object = value.as_object()?;
    for key in ["detail", "message", "error_description", "title"] {
        if let Some(text) = object.get(key).and_then(Value::as_str) {
            return Some(text.to_string());
        }
    }
    let error = object.get("error")?;
    if let Some(text) = error.as_str() {
        return Some(text.to_string());
    }
    error
        .as_object()
        .and_then(|error| error.get("message"))
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn sanitize_summary(text: &str) -> String {
    let mut output = String::new();
    let mut in_tag = false;
    for character in text.chars() {
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
    let redacted = redact_bearer(&collapsed);
    redacted.chars().take(MAX_SUMMARY_CHARS).collect()
}

fn redact_bearer(text: &str) -> String {
    let mut output = String::new();
    let mut rest = text;
    while let Some(index) = rest.to_ascii_lowercase().find("bearer ") {
        output.push_str(&rest[..index]);
        output.push_str("Bearer [redacted]");
        let after = &rest[index + "bearer ".len()..];
        let end = after
            .find(char::is_whitespace)
            .unwrap_or(after.len());
        if end == 0 {
            output.push_str(after);
            break;
        }
        rest = &after[end..];
    }
    output.push_str(rest);
    output
}

fn fault_class_of_probe_result(result: &str) -> Option<FaultClass> {
    match result {
        "upstream_5xx" => Some(FaultClass::Upstream5xx),
        "timeout" => Some(FaultClass::Timeout),
        "network_error" | "non_json_response" => Some(FaultClass::NetworkError),
        _ => None,
    }
}

fn new_fault_id(now: f64) -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    format!(
        "fault-{}-{}",
        (now * 1000.0).round() as u64,
        COUNTER.fetch_add(1, Ordering::SeqCst)
    )
}

fn epoch_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn read_state(path: &Path) -> Option<FaultState> {
    let text = std::fs::read_to_string(path).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    let object = value.as_object()?;
    let active = object.get("active").and_then(parse_active);
    let history = object
        .get("history")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let last_check = object.get("last_check").cloned();
    let next_check_epoch = object.get("next_check_epoch").and_then(Value::as_f64);
    Some(FaultState {
        active,
        history,
        last_check,
        next_check_epoch,
    })
}

fn parse_active(value: &Value) -> Option<ActiveFault> {
    let object = value.as_object()?;
    let class = match object.get("classification").and_then(Value::as_str)? {
        "upstream_5xx" => FaultClass::Upstream5xx,
        "timeout" => FaultClass::Timeout,
        "network_error" => FaultClass::NetworkError,
        "stream_error" => FaultClass::StreamError,
        _ => return None,
    };
    let source = match object.get("source").and_then(Value::as_str)? {
        "background_probe" => FaultSource::BackgroundProbe,
        "active_probe" => FaultSource::ActiveProbe,
        "business_request" => FaultSource::BusinessRequest,
        _ => return None,
    };
    Some(ActiveFault {
        id: object.get("id").and_then(Value::as_str)?.to_string(),
        started_epoch: object.get("started_epoch").and_then(Value::as_f64)?,
        last_epoch: object.get("last_epoch").and_then(Value::as_f64)?,
        source,
        failures: object.get("failures").and_then(Value::as_u64)?,
        class,
        upstream_status: object
            .get("upstream_status")
            .and_then(Value::as_u64)
            .map(|status| status as u16),
        summary: object
            .get("summary")
            .and_then(Value::as_str)
            .map(str::to_string),
    })
}

fn write_atomic(path: &Path, document: &Value) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }
    let text = serde_json::to_vec(document).unwrap_or_default();
    let temporary = path.with_extension(format!(
        "{}.tmp",
        path.extension()
            .map(|extension| extension.to_string_lossy().into_owned())
            .unwrap_or_default()
    ));
    std::fs::write(&temporary, text)?;
    if std::fs::rename(&temporary, path).is_ok() {
        return Ok(());
    }
    // Windows cannot replace an existing file with `rename`; removing the old
    // record first keeps the write working. The reader tolerates a missing file
    // as the legal empty record, never as a fabricated active fault.
    let _ = std::fs::remove_file(path);
    std::fs::rename(&temporary, path)
}

/// The default durable path for the local production role.
pub fn default_path() -> PathBuf {
    PathBuf::from(DEFAULT_FAULT_LOG)
}

/// Resolve the effective path from an explicit setting and the role/mode.
pub fn effective_path(
    explicit: Option<PathBuf>,
    local_role: bool,
    candidate: bool,
) -> Option<PathBuf> {
    explicit.or_else(|| (local_role && !candidate).then(default_path))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_recognizable_phrase_is_kept_and_other_text_is_bounded() {
        assert_eq!(
            summarize_error_text("{\"detail\":\"no healthy upstream\"}"),
            Some(NO_HEALTHY_UPSTREAM.to_string())
        );
        let hostile = format!(
            "<html><body>Error Bearer secret-token\n{}\n</body></html>",
            "x".repeat(400)
        );
        let summary = summarize_error_text(&hostile).expect("summary");
        assert!(!summary.contains('<'));
        assert!(!summary.contains('\n'));
        assert!(!summary.contains("secret-token"));
        assert!(summary.chars().count() <= MAX_SUMMARY_CHARS);
    }

    #[test]
    fn one_failure_opens_and_later_failures_update_the_same_record() {
        let log = UpstreamFaultLog::open(None);
        log.record_failure(FaultObservation {
            class: FaultClass::Upstream5xx,
            upstream_status: Some(503),
            summary: Some(NO_HEALTHY_UPSTREAM.to_string()),
            source: FaultSource::BackgroundProbe,
        });
        let first = log.snapshot();
        let id = first["active"]["id"].clone();
        log.record_failure(FaultObservation {
            class: FaultClass::Timeout,
            upstream_status: None,
            summary: None,
            source: FaultSource::BusinessRequest,
        });
        let second = log.snapshot();
        assert_eq!(second["active"]["id"], id);
        assert_eq!(second["active"]["failures"], json!(2));
        assert_eq!(second["active"]["classification"], json!("timeout"));
        assert_eq!(second["active"]["summary"], json!(NO_HEALTHY_UPSTREAM));
        log.record_recovery(
            FaultSource::ActiveProbe,
            Some(200),
            "credential_probe",
        );
        let closed = log.snapshot();
        assert_eq!(closed["active"], Value::Null);
        assert_eq!(closed["history"][0]["recovery_source"], json!("active_probe"));
        assert_eq!(closed["history"][0]["recovery_status"], json!(200));
        log.record_recovery(
            FaultSource::ActiveProbe,
            Some(200),
            "credential_probe",
        );
        assert_eq!(log.snapshot()["history"].as_array().unwrap().len(), 1);
    }
}
