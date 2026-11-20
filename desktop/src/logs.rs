//! Bounded run-log view over the app instance's own service log.
//!
//! The gateway writes JSONL events to `%LOCALAPPDATA%\Chat2API\instance\gateway.log`.
//! This ticket reads the bounded tail and shows the most recent records; live
//! incremental reads and backfill belong to #126. A missing or unreadable file keeps
//! its own failure reason and is never shown as a legal empty list.

use std::path::Path;

use serde_json::Value;

use crate::overview::Tone;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogEntry {
    pub at: Option<i64>,
    pub level: String,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LogsView {
    Loaded(Vec<LogEntry>),
    Failed(String),
}

/// The largest tail the log page reads before splitting lines.
const MAX_LOG_BYTES: u64 = 256 * 1024;
/// The number of newest lines the log page shows.
const MAX_LOG_LINES: usize = 80;

pub fn parse_log_line(line: &str) -> LogEntry {
    let line = line.trim();
    let Ok(body) = serde_json::from_str::<Value>(line) else {
        return LogEntry {
            at: None,
            level: "raw".to_string(),
            text: line.to_string(),
        };
    };
    let event = body.get("event").and_then(Value::as_str);
    let at = body.get("time").and_then(|time| {
        time.as_i64().or_else(|| {
            time.as_str()
                .and_then(|text| chrono::DateTime::parse_from_rfc3339(text).ok())
                .map(|time| time.timestamp_millis())
        })
    });
    let level = match body.get("level").and_then(Value::as_str) {
        Some(level) => level.to_string(),
        None if event == Some("request_summary") => "error".to_string(),
        None => "info".to_string(),
    };
    let text = if event == Some("request_summary") {
        format!(
            "request_summary {} {} final={} tries={}",
            body.get("request_id")
                .and_then(Value::as_str)
                .unwrap_or("?"),
            body.get("protocol").and_then(Value::as_str).unwrap_or("?"),
            body.get("final").and_then(Value::as_str).unwrap_or("?"),
            body.get("tries").and_then(Value::as_u64).unwrap_or(0),
        )
    } else {
        line.to_string()
    };
    LogEntry { at, level, text }
}

pub fn read_log_file(path: &Path) -> LogsView {
    let text = match crate::tail::read_tail(path, MAX_LOG_BYTES) {
        Ok(text) => text,
        Err(error) => return LogsView::Failed(format!("读取日志失败：{error}")),
    };
    let mut lines: Vec<&str> = text
        .lines()
        .filter(|line| !line.trim().is_empty())
        .collect();
    if lines.len() > MAX_LOG_LINES {
        lines = lines.split_off(lines.len() - MAX_LOG_LINES);
    }
    LogsView::Loaded(lines.into_iter().map(parse_log_line).collect())
}

pub fn logs_status(view: &LogsView) -> (String, Tone) {
    match view {
        LogsView::Loaded(rows) if rows.is_empty() => {
            ("暂无日志记录（本实例）".to_string(), Tone::Warn)
        }
        LogsView::Loaded(rows) => (
            format!(
                "最近 {} 条 · 本实例运行日志（#126 接入实时增量）",
                rows.len()
            ),
            Tone::Good,
        ),
        LogsView::Failed(reason) => (format!("日志读取失败：{reason}"), Tone::Bad),
    }
}
