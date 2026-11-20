use std::cell::RefCell;

use serde_json::{json, Value};
use um_desktop::logs::{logs_status, parse_logs, read_logs, LogEntry, LogsView};
use um_desktop::overview::{Probe, Tone};

struct FakeProbe {
    body: Result<Value, String>,
    urls: RefCell<Vec<String>>,
}

impl FakeProbe {
    fn new(body: Result<Value, String>) -> Self {
        Self {
            body,
            urls: RefCell::new(Vec::new()),
        }
    }
}

impl Probe for FakeProbe {
    fn get_json(&self, url: &str) -> Result<Value, String> {
        self.urls.borrow_mut().push(url.to_string());
        self.body.clone()
    }
}

#[test]
fn ok_response_with_rows_is_loaded() {
    let view = parse_logs(&json!({
        "status": "ok",
        "lines": [
            {"at": 1700000000000_i64, "level": "info", "text": "hello"},
            {"at": 1700000001000_i64, "level": "error", "text": "boom"},
        ],
    }));
    assert_eq!(
        view,
        LogsView::Loaded(vec![
            LogEntry {
                at: Some(1700000000000),
                level: "info".to_string(),
                text: "hello".to_string(),
            },
            LogEntry {
                at: Some(1700000001000),
                level: "error".to_string(),
                text: "boom".to_string(),
            },
        ])
    );
}

#[test]
fn ok_response_with_no_rows_is_a_legal_empty_log() {
    let view = parse_logs(&json!({"status": "ok", "lines": []}));
    assert_eq!(view, LogsView::Loaded(Vec::new()));
    let (status, tone) = logs_status(&view);
    assert_eq!(tone, Tone::Warn);
    assert!(status.contains("暂无日志"));
}

#[test]
fn missing_log_file_is_a_failure_not_an_empty_log() {
    let view = parse_logs(&json!({
        "status": "missing",
        "error": "file_missing",
        "lines": [],
    }));
    match view {
        LogsView::Failed(reason) => {
            assert!(reason.contains("missing"), "{reason}");
            assert!(reason.contains("file_missing"), "{reason}");
        }
        other => panic!("missing log must be failed, got {other:?}"),
    }
}

#[test]
fn ok_response_without_lines_is_a_failure() {
    let view = parse_logs(&json!({"status": "ok"}));
    match view {
        LogsView::Failed(reason) => assert!(reason.contains("lines"), "{reason}"),
        other => panic!("missing lines must be failed, got {other:?}"),
    }
}

#[test]
fn unreadable_log_is_a_failure_with_its_reason() {
    let view = parse_logs(&json!({
        "status": "denied",
        "error": "permission_denied",
        "lines": [],
    }));
    let (status, tone) = logs_status(&view);
    assert_eq!(tone, Tone::Bad);
    assert!(status.contains("permission_denied"), "{status}");
}

#[test]
fn read_logs_calls_the_read_only_8001_route() {
    let probe = FakeProbe::new(Ok(json!({"status": "ok", "lines": []})));
    let _ = read_logs(&probe, "http://127.0.0.1:8001");
    let urls = probe.urls.borrow();
    assert_eq!(
        urls.as_slice(),
        ["http://127.0.0.1:8001/api/console/um/logs?window=today&source=service&limit=80"]
    );
}

#[test]
fn probe_failure_keeps_the_transport_reason() {
    let view = read_logs(
        &FakeProbe::new(Err("8001: connection refused".to_string())),
        "http://127.0.0.1:8001",
    );
    match view {
        LogsView::Failed(reason) => assert!(reason.contains("connection refused"), "{reason}"),
        other => panic!("transport failure must be failed, got {other:?}"),
    }
}

#[test]
fn loaded_rows_are_reported_as_read_only_recent_entries() {
    let view = LogsView::Loaded(vec![LogEntry {
        at: Some(1700000000000),
        level: "info".to_string(),
        text: "hello".to_string(),
    }]);
    let (status, tone) = logs_status(&view);
    assert_eq!(tone, Tone::Good);
    assert!(status.contains("最近 1 条"), "{status}");
    assert!(status.contains("只读"), "{status}");
}

#[test]
fn rows_without_a_timestamp_keep_none_instead_of_zero() {
    let view = parse_logs(&json!({
        "status": "ok",
        "lines": [{"level": "info", "text": "undated"}],
    }));
    assert_eq!(
        view,
        LogsView::Loaded(vec![LogEntry {
            at: None,
            level: "info".to_string(),
            text: "undated".to_string(),
        }])
    );
}
