//! Read-only run-log view over the local 8001 console.
//!
//! The ticket reads the bounded `/api/console/um/logs` tail and shows the most
//! recent records. It does not implement incremental reads or backfill (#126).
//! Missing/unreadable logs keep their own failure reason and are never shown as a
//! legal empty list.

use serde_json::Value;

use crate::overview::{Probe, Tone};

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

pub fn parse_logs(body: &Value) -> LogsView {
    let status = body
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    if status != "ok" {
        let error = body
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown_error");
        return LogsView::Failed(format!("日志读取状态 {status}：{error}"));
    }
    let Some(lines) = body.get("lines").and_then(Value::as_array) else {
        return LogsView::Failed("日志响应缺少 lines 数组".to_string());
    };
    LogsView::Loaded(
        lines
            .iter()
            .map(|row| LogEntry {
                at: row.get("at").and_then(Value::as_i64),
                level: row
                    .get("level")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown")
                    .to_string(),
                text: row
                    .get("text")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            })
            .collect(),
    )
}

pub fn read_logs(probe: &dyn Probe, console_base: &str) -> LogsView {
    let url = format!(
        "{}/api/console/um/logs?window=today&source=service&limit=80",
        console_base.trim_end_matches('/')
    );
    match probe.get_json(&url) {
        Ok(body) => parse_logs(&body),
        Err(error) => LogsView::Failed(error),
    }
}

pub fn logs_status(view: &LogsView) -> (String, Tone) {
    match view {
        LogsView::Loaded(rows) if rows.is_empty() => {
            ("暂无日志记录（只读）".to_string(), Tone::Warn)
        }
        LogsView::Loaded(rows) => (
            format!("最近 {} 条 · 只读 8001 运行日志", rows.len()),
            Tone::Good,
        ),
        LogsView::Failed(reason) => (format!("日志读取失败：{reason}"), Tone::Bad),
    }
}
