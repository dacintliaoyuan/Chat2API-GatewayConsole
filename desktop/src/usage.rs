//! Slint view models over the instance ledger and service log.

use std::path::Path;

use chrono::{FixedOffset, TimeZone};
use serde_json::Value;
use um_console::local_um::{desktop_trend, um_requests};

use crate::logs::{parse_log_line, sanitize_text};
use crate::logstream::{DEFAULT_MAX_LINES, DEFAULT_TAIL_BYTES};
use crate::tail::read_tail;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Range {
    Today,
    Week,
    Month,
}

impl Range {
    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Week,
            2 => Self::Month,
            _ => Self::Today,
        }
    }

    fn key(self) -> &'static str {
        match self {
            Self::Today => "today",
            Self::Week => "7d",
            Self::Month => "30d",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Coverage {
    Complete,
    Partial,
    Empty,
    Unavailable,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Bucket {
    pub label: String,
    pub tokens: String,
    pub requests: String,
    pub token_fraction: Option<f32>,
    pub request_fraction: Option<f32>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct UsageView {
    pub buckets: Vec<Bucket>,
    pub token_total: String,
    pub request_total: String,
    pub token_ticks: Vec<String>,
    pub request_ticks: Vec<String>,
    pub coverage: Coverage,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestRow {
    pub key: String,
    pub request_id: Option<String>,
    pub at: String,
    pub model: String,
    pub result: String,
    pub input: String,
    pub output: String,
    pub duration: String,
    pub detail: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestPage {
    pub rows: Vec<RequestRow>,
    pub earlier: Option<String>,
    pub coverage: Coverage,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestLogs {
    pub lines: Vec<String>,
    pub message: String,
}

fn text(value: &Value, fallback: &str) -> String {
    sanitize_text(value.as_str().unwrap_or(fallback))
}

fn format_at(value: Option<i64>) -> String {
    let beijing = FixedOffset::east_opt(8 * 3600).expect("valid Beijing UTC offset");
    value
        .and_then(|time| beijing.timestamp_millis_opt(time).single())
        .map(|time| time.format("%m-%d %H:%M:%S").to_string())
        .unwrap_or_else(|| "—".to_string())
}

fn amount(reading: &Value) -> Option<i64> {
    reading["value"].as_i64().filter(|value| *value >= 0)
}

fn display(reading: &Value) -> String {
    let Some(value) = amount(reading) else {
        return "—".to_string();
    };
    let prefix = match reading["kind"].as_str() {
        Some("estimate") => "约 ",
        Some("atLeast") => "≥ ",
        _ => "",
    };
    format!("{prefix}{value}")
}

fn plot_axis(max: Option<i64>) -> (u64, Vec<String>) {
    let Some(max) = max else {
        return (1, Vec::new());
    };
    let ideal_step = (max as u64).div_ceil(4).max(1);
    let rounding = 10u64.pow(ideal_step.ilog10().saturating_sub(1));
    let step = ideal_step.div_ceil(rounding) * rounding;
    let ticks = (0..=4)
        .rev()
        .map(|index| axis_label(index * step))
        .collect();
    (step * 4, ticks)
}

fn axis_label(value: u64) -> String {
    for (unit, suffix) in [(1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")] {
        if value >= unit {
            let decimal = format!("{:.2}", value as f64 / unit as f64);
            let number = decimal.trim_end_matches('0').trim_end_matches('.');
            return format!("{number}{suffix}");
        }
    }
    value.to_string()
}

fn coverage(value: &Value, empty: bool, empty_message: &str) -> (Coverage, String) {
    match value["status"].as_str() {
        Some("ok") if empty => (Coverage::Empty, empty_message.to_string()),
        Some("ok") => (Coverage::Complete, String::new()),
        Some("partial") => (
            Coverage::Partial,
            text(&value["note"], "仅显示可读取的记录"),
        ),
        Some("missing") => (
            Coverage::Empty,
            "尚未生成本实例账本。发送模型请求后可查看记录。".into(),
        ),
        _ => (
            Coverage::Unavailable,
            text(&value["note"], "无法读取本实例账本"),
        ),
    }
}

pub fn read_usage(ledger: &Path, range: Range, now_ms: i64) -> UsageView {
    let value = match desktop_trend(ledger, range.key(), now_ms as f64) {
        Ok(value) => value,
        Err(error) => {
            return UsageView {
                buckets: Vec::new(),
                token_total: "—".into(),
                request_total: "—".into(),
                token_ticks: Vec::new(),
                request_ticks: Vec::new(),
                coverage: Coverage::Unavailable,
                message: format!("无法读取选定时间的用量：{error}"),
            };
        }
    };
    let rows = value["buckets"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let (coverage, message) = coverage(
        &value["coverage"],
        value["coverage"]["scanned"].as_u64() == Some(0),
        "所选时间没有已记录的模型请求。",
    );
    let max_for = |key: &str| rows.iter().filter_map(|row| amount(&row[key])).max();
    let token_max = max_for("tokens");
    let request_max = max_for("requests");
    let (token_axis, token_ticks) = plot_axis(token_max);
    let (request_axis, request_ticks) = plot_axis(request_max);
    let fraction =
        |reading: &Value, axis: u64| amount(reading).map(|value| value as f32 / axis as f32);
    let buckets = rows
        .iter()
        .map(|row| Bucket {
            label: text(&row["label"], "—"),
            tokens: display(&row["tokens"]),
            requests: display(&row["requests"]),
            token_fraction: fraction(&row["tokens"], token_axis),
            request_fraction: fraction(&row["requests"], request_axis),
        })
        .collect();
    UsageView {
        buckets,
        token_total: display(&value["total"]["tokens"]),
        request_total: display(&value["total"]["requests"]),
        token_ticks,
        request_ticks,
        coverage,
        message,
    }
}

pub fn read_requests(
    ledger: &Path,
    range: Range,
    result_index: i32,
    search: &str,
    cursor: Option<&str>,
    now_ms: i64,
) -> RequestPage {
    let result = match result_index {
        1 => "success",
        2 => "failed",
        3 => "truncated",
        4 => "cancelled",
        5 => "client_disconnected",
        _ => "all",
    };
    let value = match um_requests(
        ledger,
        range.key(),
        None,
        result,
        search,
        cursor,
        60,
        now_ms as f64,
    ) {
        Ok(value) => value,
        Err(error) => {
            return RequestPage {
                rows: Vec::new(),
                earlier: None,
                coverage: Coverage::Unavailable,
                message: format!("无法读取所选请求：{error}"),
            };
        }
    };
    let page = &value["page"];
    let rows = page["rows"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let (coverage, message) = coverage(&page["coverage"], rows.is_empty(), "没有符合条件的请求。");
    RequestPage {
        rows: rows.iter().map(request_row).collect(),
        earlier: page["earlier"].as_str().map(str::to_owned),
        coverage,
        message,
    }
}

fn request_row(row: &Value) -> RequestRow {
    let duration = display(&row["durationMs"]);
    let duration = if duration == "—" {
        duration
    } else {
        format!("{duration} ms")
    };
    let result = match row["result"].as_str() {
        Some("success") => "成功",
        Some("failed") => "失败",
        Some("truncated") => "回复截断",
        Some("cancelled") => "已取消",
        Some("client_disconnected") => "客户端断开",
        _ => "结果未知",
    };
    let note = text(&row["resultNote"], "未知");
    let retries = row["retries"].as_array().map(Vec::len);
    let detail = match retries {
        Some(count) => format!("{note}\n已关联的物理重试 {count} 次"),
        None => note,
    };
    RequestRow {
        key: text(&row["key"], ""),
        request_id: row["requestId"].as_str().map(str::to_string),
        at: format_at(row["at"].as_i64()),
        model: text(&row["model"], "未知模型"),
        result: result.into(),
        input: display(&row["input"]),
        output: display(&row["output"]),
        duration,
        detail,
    }
}

pub fn read_request_logs(
    service_log: &Path,
    request_id: Option<&str>,
    _now_ms: i64,
) -> RequestLogs {
    let Some(id) = request_id.filter(|id| {
        id.len() == 32
            && id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    }) else {
        return RequestLogs {
            lines: Vec::new(),
            message: "这条账本记录没有可关联的请求 ID。".into(),
        };
    };
    let Ok(tail) = read_tail(service_log, DEFAULT_TAIL_BYTES) else {
        return RequestLogs {
            lines: Vec::new(),
            message: "本实例运行日志暂不可读。".into(),
        };
    };
    let complete = tail.rsplit_once('\n').map_or("", |(complete, _)| complete);
    let lines = complete
        .lines()
        .rev()
        .take(DEFAULT_MAX_LINES)
        .filter_map(|line| {
            let value: Value = serde_json::from_str(line).ok()?;
            (value["request_id"].as_str() == Some(id)).then(|| parse_log_line(line))
        })
        .take(20)
        .map(|entry| format!("{}  {}", format_at(entry.at), entry.text))
        .collect::<Vec<_>>();
    let message = if lines.is_empty() {
        "近期运行日志里没有匹配的记录。"
    } else {
        "仅列出近期运行日志中的匹配事件。"
    };
    RequestLogs {
        lines,
        message: message.into(),
    }
}

/// Detail text for one ledger row. `logs` is `None` while the matching service
/// log events are still being read; the finished text only ever includes the
/// sanitized lines already filtered by [`read_request_logs`].
pub fn request_detail(row: &RequestRow, logs: Option<&RequestLogs>) -> String {
    let mut detail = format!(
        "{}  {}\n模型  {}\n请求 ID  {}\n耗时  {}\n输入  {} Token  ·  输出  {} Token\n\n{}",
        row.at,
        row.result,
        row.model,
        row.request_id.as_deref().unwrap_or("—"),
        row.duration,
        row.input,
        row.output,
        row.detail
    );
    match logs {
        Some(logs) => {
            detail.push_str("\n\n运行日志\n");
            detail.push_str(&logs.message);
            if !logs.lines.is_empty() {
                detail.push('\n');
                detail.push_str(&logs.lines.join("\n"));
            }
        }
        None => detail.push_str("\n\n正在读取关联运行日志…"),
    }
    detail
}
