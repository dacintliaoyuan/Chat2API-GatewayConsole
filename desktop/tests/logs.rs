use serde_json::json;
use um_desktop::logs::{logs_status, parse_log_line, read_log_file, LogEntry, LogsView};
use um_desktop::overview::Tone;

mod common;
use common::TempDir;

fn at(text: &str) -> i64 {
    chrono::DateTime::parse_from_rfc3339(text)
        .expect("rfc3339")
        .timestamp_millis()
}

#[test]
fn request_summary_line_becomes_a_compact_entry() {
    let entry = parse_log_line(
        &json!({
            "time": "2026-11-20T10:00:00+08:00",
            "service": "chat2api",
            "event": "request_summary",
            "request_id": "req-1",
            "protocol": "chat",
            "final": "502",
            "tries": 3,
        })
        .to_string(),
    );
    assert_eq!(
        entry,
        LogEntry {
            at: Some(at("2026-11-20T10:00:00+08:00")),
            level: "error".to_string(),
            text: "request_summary req-1 chat final=502 tries=3".to_string(),
        }
    );
}

#[test]
fn malformed_line_is_kept_raw_not_dropped() {
    let entry = parse_log_line("not json at all");
    assert_eq!(
        entry,
        LogEntry {
            at: None,
            level: "raw".to_string(),
            text: "not json at all".to_string(),
        }
    );
}

#[test]
fn a_line_without_a_time_keeps_none_instead_of_zero() {
    let entry = parse_log_line(&json!({"event": "start", "port": 7999}).to_string());
    assert_eq!(entry.at, None);
    assert_eq!(entry.level, "info");
    assert!(entry.text.contains("\"event\":\"start\""), "{}", entry.text);
}

#[test]
fn missing_file_is_a_legal_empty_log_not_a_failure() {
    let dir = TempDir::new("logs-missing");
    let view = read_log_file(&dir.path().join("gateway.log"));
    assert_eq!(
        view,
        LogsView::Loaded(Vec::new()),
        "a fresh instance has no gateway.log until a request finally fails"
    );
    let (status, tone) = logs_status(&view);
    assert_eq!(tone, Tone::Warn);
    assert!(status.contains("暂无日志"), "{status}");
}

#[test]
fn an_unreadable_path_is_a_failure_not_an_empty_log() {
    let dir = TempDir::new("logs-unreadable");
    let view = read_log_file(dir.path());
    match &view {
        LogsView::Failed(reason) => {
            assert!(reason.contains("logs-unreadable"), "{reason}");
        }
        other => panic!("a directory must be a read failure, got {other:?}"),
    }
    let (status, tone) = logs_status(&view);
    assert_eq!(tone, Tone::Bad);
    assert!(status.contains("日志读取失败"), "{status}");
}

#[test]
fn an_empty_file_is_a_legal_empty_log() {
    let dir = TempDir::new("logs-empty");
    let path = dir.path().join("gateway.log");
    std::fs::write(&path, "").unwrap();
    let view = read_log_file(&path);
    assert_eq!(view, LogsView::Loaded(Vec::new()));
    let (status, tone) = logs_status(&view);
    assert_eq!(tone, Tone::Warn);
    assert!(status.contains("暂无日志"), "{status}");
}

#[test]
fn the_tail_keeps_only_the_newest_400_lines() {
    let dir = TempDir::new("logs-tail");
    let path = dir.path().join("gateway.log");
    let text = (1..=500)
        .map(|index| format!("{{\"event\":\"line-{index}\"}}"))
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    std::fs::write(&path, text).unwrap();
    let view = read_log_file(&path);
    let rows = match &view {
        LogsView::Loaded(rows) => rows,
        other => panic!("expected loaded logs, got {other:?}"),
    };
    assert_eq!(rows.len(), 400);
    assert!(rows[0].text.contains("line-101"), "{}", rows[0].text);
    assert!(rows[399].text.contains("line-500"), "{}", rows[399].text);
}

#[test]
fn loaded_rows_are_reported_as_instance_logs() {
    let view = LogsView::Loaded(vec![LogEntry {
        at: Some(at("2026-11-20T10:00:00+08:00")),
        level: "info".to_string(),
        text: "start".to_string(),
    }]);
    let (status, tone) = logs_status(&view);
    assert_eq!(tone, Tone::Good);
    assert!(status.contains("最近 1 条"), "{status}");
    assert!(!status.contains("实时"), "the Tag already names the layer: {status}");
}
