//! Local UM read faces (`usagesrv.py` + `console/stats.py` + `console/log_reader.py`).
//!
//! The local role reads the private chat2api ledger for today/trend/requests,
//! the bounded chat2api run log for logs/request-logs, and the ORIGINAL console
//! operation records for maintenance. Unknown values stay explicit readings; a
//! missing ledger is never folded into a real zero.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use chrono::{FixedOffset, TimeZone};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::local_summary::{day_start_ms, parse_ts_ms};
use crate::operations::OperationsReader;
use um_gateway::login::history::{read_records, sanitize_reason, LoginRoundRecord};

pub const UM_LEDGER_SOURCE: &str = "chat2api";
pub const UM_WINDOWS: [&str; 4] = ["today", "24h", "7d", "30d"];
pub const UM_TREND_RANGES: [&str; 3] = ["7d", "14d", "30d"];
pub const UM_RESULTS: [&str; 6] = [
    "success",
    "failed",
    "truncated",
    "client_disconnected",
    "cancelled",
    "unknown",
];
pub const UM_RETRY_STATUSES: [&str; 2] = [
    "upstream_transient_retried",
    "degenerate_repetition_retried",
];
pub const UM_TRUNCATED_STATUSES: [&str; 3] = [
    "upstream_missing_finish_reason",
    "upstream_eof_without_done",
    "aggregate_upstream_missing_terminal",
];
pub const UM_MAX_PAGE: i64 = 100;
pub const UM_MAX_SEARCH_CHARS: usize = 120;
pub const UM_MAX_READ_BYTES: u64 = 32 * 1024 * 1024;
pub const UM_LEDGER_RECORD_LIMIT: usize = 200_000;
pub const UM_LOG_CATEGORIES: [&str; 4] = ["service", "console", "console_prev", "watch"];
pub const UM_LOG_LEVELS: [&str; 5] = ["error", "warn", "info", "debug", "unknown"];
pub const UM_LOG_MAX_PAGE: i64 = 200;
pub const UM_LOG_MAX_TAIL_BYTES: u64 = 262_144;
pub const UM_LOG_MAX_ROWS: usize = 400;
pub const UM_MAINTENANCE_ACTIONS: [&str; 3] =
    ["account.um_status", "account.um_verify", "account.login"];
pub const UM_MAINTENANCE_SERVICE_ACTIONS: [&str; 3] =
    ["service.start", "service.stop", "service.restart"];
pub const UM_MAINTENANCE_TARGET: &str = "chat2api";
pub const UM_MAINTENANCE_MAX_READ: i64 = 200;

#[derive(Debug, Clone)]
pub struct UmQueryError(pub String);

impl std::fmt::Display for UmQueryError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for UmQueryError {}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum NumberValue {
    Int(i64),
    Float(f64),
}

impl NumberValue {
    fn as_f64(self) -> f64 {
        match self {
            Self::Int(value) => value as f64,
            Self::Float(value) => value,
        }
    }

    fn to_json(self) -> Value {
        match self {
            Self::Int(value) => json!(value),
            Self::Float(value) => json!(value),
        }
    }

    fn is_float(self) -> bool {
        matches!(self, Self::Float(_))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadingKind {
    Exact,
    AtLeast,
    Estimate,
    Unreported,
    Unknown,
    Missing,
}

impl ReadingKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Exact => "exact",
            Self::AtLeast => "atLeast",
            Self::Estimate => "estimate",
            Self::Unreported => "unreported",
            Self::Unknown => "unknown",
            Self::Missing => "missing",
        }
    }

    fn carries_value(self) -> bool {
        matches!(self, Self::Exact | Self::AtLeast | Self::Estimate)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Reading {
    pub kind: ReadingKind,
    pub value: Option<NumberValue>,
    pub note: Option<String>,
}

impl Reading {
    fn new(kind: ReadingKind, value: Option<NumberValue>, note: Option<String>) -> Self {
        Self { kind, value, note }
    }

    pub fn exact_int(value: i64) -> Self {
        Self::new(ReadingKind::Exact, Some(NumberValue::Int(value)), None)
    }

    pub fn exact_float(value: f64) -> Self {
        Self::new(ReadingKind::Exact, Some(NumberValue::Float(value)), None)
    }

    pub fn at_least_int(value: i64, note: &str) -> Self {
        Self::new(
            ReadingKind::AtLeast,
            Some(NumberValue::Int(value)),
            Some(note.to_string()),
        )
    }

    pub fn at_least_float(value: f64, note: &str) -> Self {
        Self::new(
            ReadingKind::AtLeast,
            Some(NumberValue::Float(value)),
            Some(note.to_string()),
        )
    }

    pub fn estimate_float(value: f64, note: &str) -> Self {
        Self::new(
            ReadingKind::Estimate,
            Some(NumberValue::Float(value)),
            Some(note.to_string()),
        )
    }

    pub fn unreported(note: &str) -> Self {
        Self::new(ReadingKind::Unreported, None, Some(note.to_string()))
    }

    pub fn unknown(note: &str) -> Self {
        Self::new(ReadingKind::Unknown, None, Some(note.to_string()))
    }

    pub fn missing(note: &str) -> Self {
        Self::new(ReadingKind::Missing, None, Some(note.to_string()))
    }

    pub fn to_json(&self) -> Value {
        let mut payload = Map::new();
        payload.insert("kind".to_string(), json!(self.kind.as_str()));
        if let Some(value) = self.value {
            payload.insert("value".to_string(), value.to_json());
        }
        if let Some(note) = &self.note {
            payload.insert("note".to_string(), json!(note));
        }
        Value::Object(payload)
    }
}

fn sum_readings(readings: &[Reading]) -> Reading {
    if readings.is_empty() {
        return Reading::exact_int(0);
    }
    let mut notes: Vec<String> = Vec::new();
    for reading in readings.iter() {
        if let Some(note) = &reading.note {
            if !note.is_empty() && !notes.contains(note) {
                notes.push(note.clone());
            }
        }
    }
    let note = notes.join("；");
    let note = (!note.is_empty()).then(|| note.chars().take(240).collect::<String>());
    let values: Vec<&Reading> = readings
        .iter()
        .filter(|reading| reading.kind.carries_value())
        .map(|reading| reading)
        .collect();
    if values.is_empty() {
        if readings
            .iter()
            .any(|reading| reading.kind == ReadingKind::Unknown)
        {
            return Reading::unknown(note.as_deref().unwrap_or("部分记录没有可用数值"));
        }
        if readings
            .iter()
            .any(|reading| reading.kind == ReadingKind::Unreported)
        {
            return Reading::new(
                ReadingKind::Unreported,
                None,
                note.or_else(|| Some("没有报告该数值".to_string())),
            );
        }
        return Reading::new(
            ReadingKind::Missing,
            None,
            note.or_else(|| Some("没有该数值".to_string())),
        );
    }
    let any_float = values.iter().any(|reading| {
        reading
            .value
            .is_some_and(|value| value.is_float())
    });
    let total: f64 = values
        .iter()
        .filter_map(|reading| reading.value)
        .map(NumberValue::as_f64)
        .sum();
    let total = if any_float {
        NumberValue::Float(total)
    } else {
        NumberValue::Int(total as i64)
    };
    if readings.iter().any(|reading| {
        matches!(
            reading.kind,
            ReadingKind::AtLeast
                | ReadingKind::Unknown
                | ReadingKind::Missing
                | ReadingKind::Unreported
        )
    }) {
        return Reading::new(
            ReadingKind::AtLeast,
            Some(total),
            note.or_else(|| Some("部分记录缺少数值，合计为下界".to_string())),
        );
    }
    if readings
        .iter()
        .any(|reading| reading.kind == ReadingKind::Estimate)
    {
        return Reading::new(
            ReadingKind::Estimate,
            Some(total),
            note.or_else(|| Some("含估算值".to_string())),
        );
    }
    Reading::new(ReadingKind::Exact, Some(total), None)
}

fn lower_bound(reading: &Reading, note: &str) -> Reading {
    if matches!(reading.kind, ReadingKind::Exact | ReadingKind::Estimate) {
        return Reading::new(
            ReadingKind::AtLeast,
            Some(reading.value.unwrap_or(NumberValue::Int(0))),
            Some(note.to_string()),
        );
    }
    reading.clone()
}

fn sample_reading(reading: &Reading) -> Reading {
    if reading.kind == ReadingKind::Exact {
        return Reading::new(
            ReadingKind::Estimate,
            Some(reading.value.unwrap_or(NumberValue::Int(0))),
            Some("账本不完整：按已读样本的整次平均".to_string()),
        );
    }
    reading.clone()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DurationState {
    Ok,
    Missing,
    Invalid,
}

#[derive(Debug, Clone)]
pub struct Event {
    pub ms: Option<f64>,
    pub model: String,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub duration_ms: Option<i64>,
    pub duration_state: DurationState,
    pub status: Option<Value>,
    pub cache_tokens: Option<i64>,
    pub reasoning_tokens: Option<i64>,
    pub cache_report_status: Option<String>,
    pub request_id: Option<String>,
    pub usage_source: Option<String>,
    pub estimated: bool,
}

#[derive(Debug, Default)]
pub struct LedgerRead {
    pub events: Vec<Event>,
    pub ledger_present: bool,
    pub errors: Vec<Value>,
    pub truncated: bool,
    pub trusted: bool,
}

fn int_or_none(value: Option<&Value>) -> Option<i64> {
    match value {
        None | Some(Value::Null) | Some(Value::Bool(_)) => None,
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => text.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn first_int(record: &Map<String, Value>, fields: &[&str]) -> Option<i64> {
    for name in fields {
        if let Some(value) = record.get(*name) {
            if let Some(value) = int_or_none(Some(value)) {
                return Some(value);
            }
        }
    }
    None
}

fn first_value<'a>(record: &'a Map<String, Value>, fields: &[&str]) -> Option<&'a Value> {
    for name in fields {
        if let Some(value) = record.get(*name) {
            if !value.is_null() {
                return Some(value);
            }
        }
    }
    None
}

fn duration_state(record: &Map<String, Value>) -> (Option<i64>, DurationState) {
    let Some(raw) = first_value(record, &["duration_ms"]) else {
        return (None, DurationState::Missing);
    };
    match int_or_none(Some(raw)) {
        Some(value) if value > 0 => (Some(value), DurationState::Ok),
        _ => (None, DurationState::Invalid),
    }
}

fn event_from_record(record: &Map<String, Value>) -> Event {
    let ms = record
        .get("time")
        .and_then(Value::as_str)
        .and_then(parse_ts_ms);
    let (duration_ms, duration_state) = duration_state(record);
    let usage_source = record
        .get("usage_source")
        .and_then(Value::as_str)
        .filter(|value| matches!(*value, "upstream" | "partial" | "estimated" | "none"))
        .map(str::to_string);
    let request_id = record
        .get("request_id")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_string);
    let cache_report_status = record
        .get("cache_report_status")
        .and_then(Value::as_str)
        .filter(|value| matches!(*value, "reported" | "unreported"))
        .map(str::to_string);
    Event {
        ms,
        model: record
            .get("model")
            .map_or_else(|| "unknown".to_string(), stringify),
        input_tokens: first_int(record, &["input_tokens"]),
        output_tokens: first_int(record, &["output_tokens"]),
        duration_ms,
        duration_state,
        status: record.get("status").cloned(),
        cache_tokens: first_int(record, &["cached_input_tokens", "cache_tokens"]),
        reasoning_tokens: first_int(record, &["reasoning_tokens"]),
        cache_report_status,
        request_id,
        usage_source,
        estimated: record.get("estimated") == Some(&Value::Bool(true)),
    }
}

fn stringify(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "unknown".to_string(),
        other => other.to_string(),
    }
}

fn ledger_read_error(detail: String) -> LedgerRead {
    LedgerRead {
        ledger_present: true,
        trusted: false,
        errors: vec![json!({"type": "read_error", "detail": detail})],
        ..LedgerRead::default()
    }
}

pub fn read_ledger(path: &Path) -> LedgerRead {
    use std::io::{Read as _, Seek as _, SeekFrom};

    let mut file = match std::fs::File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return LedgerRead::default()
        }
        Err(error) => return ledger_read_error(error.to_string()),
    };
    let metadata = match file.metadata() {
        Ok(metadata) => metadata,
        Err(error) => return ledger_read_error(error.to_string()),
    };
    let size = metadata.len();
    let offset = size.saturating_sub(UM_MAX_READ_BYTES);
    let mut truncated = offset > 0;
    if !metadata.is_file() {
        return ledger_read_error("ledger not a file".to_string());
    }
    if offset > 0 {
        if let Err(error) = file.seek(SeekFrom::Start(offset)) {
            return ledger_read_error(error.to_string());
        }
    }
    // Bound the snapshot to its observed length even while a writer appends.
    let mut data = Vec::new();
    if let Err(error) = file.take(size - offset).read_to_end(&mut data) {
        return ledger_read_error(error.to_string());
    }
    let text = String::from_utf8_lossy(&data);
    let mut lines = text.lines();
    let mut events = Vec::new();
    let mut bad_lines = 0usize;
    if truncated {
        // Drop the first partial line of a tail-only read.
        let _ = lines.next();
    }
    for line in lines {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(Value::Object(record)) = serde_json::from_str::<Value>(line) else {
            bad_lines += 1;
            continue;
        };
        events.push(event_from_record(&record));
    }
    let mut errors = Vec::new();
    if bad_lines > 0 {
        errors.push(json!({"type": "malformed_lines", "bad_lines": bad_lines}));
    }
    if events.len() > UM_LEDGER_RECORD_LIMIT {
        events.drain(0..events.len() - UM_LEDGER_RECORD_LIMIT);
        truncated = true;
    }
    events.sort_by(|left, right| {
        left.ms
            .unwrap_or(0.0)
            .partial_cmp(&right.ms.unwrap_or(0.0))
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    LedgerRead {
        events,
        ledger_present: true,
        errors,
        truncated,
        trusted: true,
    }
}

fn ledger_status(read: &LedgerRead) -> (&'static str, String) {
    if !read.ledger_present {
        return (
            "missing",
            "账本文件缺席：本来源不出行，不以 0 冒充无用量".to_string(),
        );
    }
    if !read.trusted {
        return (
            "unavailable",
            "账本读取失败或结构损坏：旧缓存不是本次读数".to_string(),
        );
    }
    if !read.errors.is_empty() {
        return (
            "partial",
            "账本有坏行/坏记录，可解析部分照常保留".to_string(),
        );
    }
    if read.truncated {
        return (
            "partial",
            "账本超出有界读取上限，保留部分合计为下界".to_string(),
        );
    }
    ("ok", String::new())
}

fn coverage(read: &LedgerRead) -> Value {
    json!({
        "status": ledger_status(read).0,
        "note": ledger_status(read).1,
        "ledger_present": read.ledger_present,
        "events": read.events.len(),
        "errors": read.errors,
        "truncated": read.truncated,
    })
}

fn beijing() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).expect("+08:00")
}

fn beijing_day(ms: Option<f64>) -> Option<String> {
    let ms = ms?;
    let seconds = (ms / 1000.0).floor() as i64;
    beijing()
        .timestamp_opt(seconds, 0)
        .single()
        .map(|stamp| stamp.format("%Y-%m-%d").to_string())
}

fn iso(ms: f64) -> Option<String> {
    let seconds = (ms / 1000.0).floor() as i64;
    beijing()
        .timestamp_opt(seconds, 0)
        .single()
        .map(|stamp| stamp.to_rfc3339_opts(chrono::SecondsFormat::AutoSi, false))
}

fn window_day_keys(days: i64, now_ms: f64) -> Vec<String> {
    let today = beijing_day(Some(now_ms)).unwrap_or_default();
    let Some(today_ms) = day_start_ms(&today) else {
        return Vec::new();
    };
    (0..days)
        .rev()
        .filter_map(|offset| beijing_day(Some(today_ms - offset as f64 * 86_400_000.0)))
        .collect()
}

fn window_bounds(window: &str, now_ms: f64) -> Result<(f64, f64, String), UmQueryError> {
    if !UM_WINDOWS.contains(&window) {
        return Err(UmQueryError(format!(
            "invalid window {window:?}: expected one of {}",
            UM_WINDOWS.join(", ")
        )));
    }
    if window == "today" {
        let day = window_day_keys(1, now_ms).first().cloned().unwrap_or_default();
        let start = day_start_ms(&day).unwrap_or(now_ms);
        return Ok((start, start + 86_400_000.0, "今天".to_string()));
    }
    if window == "24h" {
        return Ok((now_ms - 86_400_000.0, now_ms + 1.0, "近 24 小时".to_string()));
    }
    let days = if window == "7d" { 7 } else { 30 };
    let start = window_day_keys(days, now_ms)
        .first()
        .and_then(|day| day_start_ms(day))
        .unwrap_or(now_ms);
    let end = window_day_keys(1, now_ms)
        .first()
        .and_then(|day| day_start_ms(day))
        .unwrap_or(now_ms)
        + 86_400_000.0;
    Ok((start, end, format!("近 {days} 天")))
}

fn is_retry(event: &Event) -> bool {
    event
        .status
        .as_ref()
        .and_then(Value::as_str)
        .is_some_and(|status| UM_RETRY_STATUSES.contains(&status.trim().to_lowercase().as_str()))
}

fn http_status(status: Option<&Value>) -> Option<i64> {
    match status {
        Some(Value::Number(number)) => number.as_i64(),
        Some(Value::String(text)) => {
            let text = text.trim();
            if let Some(rest) = text.to_ascii_lowercase().strip_prefix("http") {
                let digits = rest.trim();
                return digits.parse::<i64>().ok();
            }
            None
        }
        _ => None,
    }
}

fn status_note(status: &str) -> String {
    let lower = status.trim().to_lowercase();
    let note = match lower.as_str() {
        "upstream_unavailable" => "上游不可用",
        "reauthentication_required" => "上游认证失效",
        "request_deadline_exceeded" => "请求超过截止时间",
        "concurrency_limit" => "并发上限",
        "unexpected_upstream_response" => "上游响应不符合预期",
        "upstream_stream_error" => "上游流失败",
        "upstream_read_error" => "上游读取失败",
        "upstream_prelude_overflow" => "上游预读超出上限",
        "upstream_first_byte_timeout" => "上游首字节超时",
        "upstream_explicit_error" => "上游明确报错",
        "upstream_bad_frame" => "上游返回无效帧",
        "upstream_missing_terminal" => "上游流缺少完整终止",
        "upstream_error" => "上游错误",
        "degenerate_repetition" => "上游重复输出",
        _ => "",
    };
    if !note.is_empty() {
        return note.to_string();
    }
    if lower.starts_with("aggregate_") {
        return format!("上游流失败（{}）", status.chars().take(64).collect::<String>());
    }
    format!("账本状态：{}", status.chars().take(64).collect::<String>())
}

fn result_of(event: &Event) -> (&'static str, String, Option<i64>) {
    let Some(status) = event.status.as_ref().and_then(Value::as_str) else {
        return ("unknown", "账本没有记录完成状态".to_string(), None);
    };
    let text = status.trim();
    if text.is_empty() {
        return ("unknown", "账本没有记录完成状态".to_string(), None);
    }
    let lower = text.to_lowercase();
    if lower == "ok" || lower == "success" {
        return ("success", "HTTP 200，正常完成".to_string(), Some(200));
    }
    if UM_RETRY_STATUSES.contains(&lower.as_str()) {
        return ("unknown", "物理重试记录，不是独立请求".to_string(), None);
    }
    if UM_TRUNCATED_STATUSES.contains(&lower.as_str()) {
        return (
            "truncated",
            "HTTP 200，但回复在完成前截断，按失败计".to_string(),
            Some(200),
        );
    }
    if lower == "cancelled" || lower == "canceled" {
        return ("cancelled", "收到明确的取消请求后停止".to_string(), None);
    }
    if lower == "client_disconnected" {
        return (
            "client_disconnected",
            "客户端在回复完成前断开连接；不推断为主动取消".to_string(),
            None,
        );
    }
    if let Some(status) = http_status(Some(&Value::String(text.to_string()))) {
        if (200..300).contains(&status) {
            return ("success", format!("HTTP {status}，正常完成"), Some(status));
        }
        return ("failed", status_note(text), Some(status));
    }
    ("failed", status_note(text), None)
}

fn token_reading(event: &Event, side: &str) -> Reading {
    let value = if side == "input" {
        event.input_tokens
    } else {
        event.output_tokens
    };
    if event.usage_source.as_deref() == Some("none") {
        return Reading::unknown(&format!(
            "上游没有报告 {side} Token（写端记为 0 表示未知）"
        ));
    }
    let Some(value) = value else {
        return Reading::unknown(&format!("旧记录缺少 {side} Token 字段"));
    };
    if event.estimated || event.usage_source.as_deref() == Some("estimated") {
        return Reading::new(
            ReadingKind::Estimate,
            Some(NumberValue::Int(value)),
            Some("本地估算，不是上游报告值".to_string()),
        );
    }
    if event.usage_source.as_deref() == Some("partial") {
        return Reading::at_least_int(value, "上游只报告了一侧，另一侧为本地估算");
    }
    Reading::exact_int(value)
}

fn total_reading(event: &Event) -> Reading {
    sum_readings(&[token_reading(event, "input"), token_reading(event, "output")])
}

fn cache_reading(event: &Event) -> Reading {
    match event.cache_report_status.as_deref() {
        Some("reported") => match event.cache_tokens {
            Some(value) => Reading::exact_int(value),
            None => Reading::unknown("缓存报告状态为 reported 但缓存字段缺失"),
        },
        Some("unreported") => Reading::unreported("上游没有报告缓存"),
        _ => Reading::unknown("旧记录没有缓存报告状态（Q11 unknown）"),
    }
}

fn reasoning_reading(event: &Event) -> Reading {
    let Some(value) = event.reasoning_tokens else {
        return Reading::unknown("旧记录缺少思考 Token 字段；已计入输出，不重复相加");
    };
    if value > 0 {
        return Reading::new(
            ReadingKind::Exact,
            Some(NumberValue::Int(value)),
            Some("已计入输出，不重复相加".to_string()),
        );
    }
    Reading::unreported("上游没有报告思考 Token（写端把缺失折为 0；已计入输出）")
}

fn cache_report(events: &[&Event]) -> (&'static str, String) {
    if events.is_empty() {
        return ("unknown", "今天没有请求，缓存报告状态不适用".to_string());
    }
    let reported = events
        .iter()
        .filter(|event| event.cache_report_status.as_deref() == Some("reported"))
        .count();
    let unreported = events
        .iter()
        .filter(|event| event.cache_report_status.as_deref() == Some("unreported"))
        .count();
    let unknown = events.len() - reported - unreported;
    if reported > 0 && unreported == 0 && unknown == 0 {
        return ("reported", "已含在输入中；每条回复都报告了缓存".to_string());
    }
    if reported > 0 {
        return (
            "partial",
            format!(
                "已含在输入中；部分回复未报告缓存（reported={reported}, unreported={unreported}, unknown={unknown}）"
            ),
        );
    }
    if unreported > 0 && unknown == 0 {
        return ("unreported", "已含在输入中；上游没有报告缓存".to_string());
    }
    if unreported > 0 && unknown > 0 {
        return (
            "partial",
            format!(
                "已含在输入中；部分回复未报告缓存，部分旧记录状态未知（unreported={unreported}, unknown={unknown}）"
            ),
        );
    }
    ("unknown", "已含在输入中；旧记录没有缓存报告状态（Q11 unknown）".to_string())
}

fn cache_tokens_reading(events: &[&Event]) -> Reading {
    if events.is_empty() {
        return Reading::exact_int(0);
    }
    let reported: Vec<&&Event> = events
        .iter()
        .filter(|event| event.cache_report_status.as_deref() == Some("reported"))
        .collect();
    let value: i64 = reported
        .iter()
        .map(|event| event.cache_tokens.unwrap_or(0))
        .sum();
    if reported.len() == events.len() {
        return Reading::exact_int(value);
    }
    if !reported.is_empty() {
        return Reading::at_least_int(value, "部分记录未报告缓存，合计为下界");
    }
    if events
        .iter()
        .any(|event| event.cache_report_status.as_deref() == Some("unreported"))
    {
        return Reading::unreported("上游没有报告缓存");
    }
    Reading::unknown("旧记录没有缓存报告状态（Q11 unknown）")
}

fn requests_reading(logical: &[&Event]) -> (Reading, Reading, Map<String, Value>) {
    let mut counts: BTreeMap<&'static str, i64> = UM_RESULTS
        .iter()
        .map(|result| (*result, 0))
        .collect();
    for event in logical {
        let (result, _note, _status) = result_of(event);
        *counts.entry(result).or_insert(0) += 1;
    }
    let failed = counts.get("failed").copied().unwrap_or(0)
        + counts.get("truncated").copied().unwrap_or(0);
    let failed_reading = if counts.get("unknown").copied().unwrap_or(0) > 0 {
        Reading::at_least_int(
            failed,
            &format!("部分记录没有完成状态，失败数至少为 {failed}"),
        )
    } else {
        Reading::exact_int(failed)
    };
    let counts_json: Map<String, Value> = counts
        .iter()
        .map(|(key, value)| ((*key).to_string(), json!(value)))
        .collect();
    (
        Reading::exact_int(logical.len() as i64),
        failed_reading,
        counts_json,
    )
}

fn speed_payload(logical: &[&Event], empty_note: &str) -> Value {
    let samples: Vec<&&Event> = logical
        .iter()
        .filter(|event| {
            event.output_tokens.is_some() && event.duration_state == DurationState::Ok
        })
        .collect();
    let missing_output = logical
        .iter()
        .filter(|event| event.output_tokens.is_none())
        .count();
    let missing_duration = logical
        .iter()
        .filter(|event| event.duration_state == DurationState::Missing)
        .count();
    let invalid_duration = logical
        .iter()
        .filter(|event| event.duration_state == DurationState::Invalid)
        .count();
    let estimated = samples
        .iter()
        .filter(|event| event.estimated || event.usage_source.as_deref() == Some("estimated"))
        .count();
    let value = if logical.is_empty() {
        Reading::missing(empty_note)
    } else if samples.is_empty() {
        Reading::missing("没有可计时的请求")
    } else {
        let output: i64 = samples
            .iter()
            .map(|event| event.output_tokens.unwrap_or(0))
            .sum();
        let duration: i64 = samples
            .iter()
            .map(|event| event.duration_ms.unwrap_or(0))
            .sum();
        if duration <= 0 {
            Reading::missing("耗时合计非正，无法计算")
        } else {
            let rate = (output as f64 / (duration as f64 / 1000.0) * 1_000_000.0).round() / 1_000_000.0;
            if samples.len() == logical.len() && estimated == 0 {
                Reading::exact_float(rate)
            } else {
                Reading::estimate_float(
                    rate,
                    &format!(
                        "按 {} 个可计时请求的整次平均；{} 个请求缺输出/耗时，{} 个输出为估算",
                        samples.len(),
                        logical.len() - samples.len(),
                        estimated
                    ),
                )
            }
        }
    };
    json!({
        "value": value.to_json(),
        "basis": "whole_request",
        "samples": samples.len(),
        "coverage": {
            "samples": samples.len(),
            "missing_output_tokens": missing_output,
            "missing_duration": missing_duration,
            "invalid_duration": invalid_duration,
            "requests": logical.len(),
            "estimated_samples": estimated,
            "note": "整次平均含首字等待；不是首字耗时或纯生成速度",
        },
    })
}

fn empty_today(status: &str, note: &str, now_ms: f64) -> Value {
    let day = window_day_keys(1, now_ms).first().cloned().unwrap_or_default();
    let start = day_start_ms(&day).unwrap_or(now_ms);
    let boundaries = json!({
        "timezone": "Asia/Shanghai",
        "start": iso(start),
        "end": iso(start + 86_400_000.0),
        "half_open": true,
    });
    let today = if status == "ok" {
        let reading = Reading::exact_int(0);
        json!({
            "day": day,
            "tokens": {"total": reading.to_json(), "input": reading.to_json(), "output": reading.to_json()},
            "cacheRead": {"tokens": reading.to_json(), "report": "unknown",
                            "detail": "今天没有请求，缓存报告状态不适用"},
            "reasoning": reading.to_json(),
            "requests": {"recorded": reading.to_json(), "failed": reading.to_json()},
            "speed": {"value": Reading::missing("今天没有请求").to_json(),
                       "basis": "whole_request", "samples": 0},
            "boundaries": boundaries,
        })
    } else if status == "partial" {
        let reading = Reading::at_least_int(0, "账本不完整，合计为下界");
        json!({
            "day": day,
            "tokens": {"total": reading.to_json(), "input": reading.to_json(), "output": reading.to_json()},
            "cacheRead": {"tokens": reading.to_json(), "report": "unknown",
                            "detail": "账本不完整，缓存报告状态未知"},
            "reasoning": reading.to_json(),
            "requests": {"recorded": reading.to_json(), "failed": reading.to_json()},
            "speed": {"value": Reading::missing("账本不完整，无法计算").to_json(),
                       "basis": "whole_request", "samples": 0},
            "boundaries": boundaries,
        })
    } else {
        let reading = Reading::missing(note);
        json!({
            "day": day,
            "tokens": {"total": reading.to_json(), "input": reading.to_json(), "output": reading.to_json()},
            "cacheRead": {"tokens": reading.to_json(), "report": "unknown", "detail": note},
            "reasoning": reading.to_json(),
            "requests": {"recorded": reading.to_json(), "failed": reading.to_json()},
            "speed": {"value": reading.to_json(), "basis": "whole_request", "samples": null},
            "boundaries": boundaries,
        })
    };
    today
}

fn mark_partial(today: &mut Value) {
    let note = "账本有损坏行/截断，合计为下界";
    for key in ["total", "input", "output"] {
        if let Some(value) = today.get_mut("tokens").and_then(|tokens| tokens.get_mut(key)) {
            *value = lower_bound_from_json(value, note);
        }
    }
    if let Some(value) = today.get_mut("cacheRead").and_then(|cache| cache.get_mut("tokens")) {
        *value = lower_bound_from_json(value, note);
    }
    if let Some(value) = today.get_mut("reasoning") {
        *value = lower_bound_from_json(value, note);
    }
    for key in ["recorded", "failed"] {
        if let Some(value) = today.get_mut("requests").and_then(|requests| requests.get_mut(key)) {
            *value = lower_bound_from_json(value, note);
        }
    }
    if let Some(value) = today
        .get_mut("speed")
        .and_then(|speed| speed.get_mut("value"))
    {
        *value = sample_reading_from_json(value);
    }
}

fn lower_bound_from_json(value: &Value, note: &str) -> Value {
    let reading = reading_from_json(value);
    lower_bound(&reading, note).to_json()
}

fn sample_reading_from_json(value: &Value) -> Value {
    let reading = reading_from_json(value);
    sample_reading(&reading).to_json()
}

fn reading_from_json(value: &Value) -> Reading {
    let Some(object) = value.as_object() else {
        return Reading::unknown("读数缺失或形状无法解析");
    };
    let kind = match object.get("kind").and_then(Value::as_str) {
        Some("exact") => ReadingKind::Exact,
        Some("atLeast") => ReadingKind::AtLeast,
        Some("estimate") => ReadingKind::Estimate,
        Some("unreported") => ReadingKind::Unreported,
        Some("unknown") => ReadingKind::Unknown,
        Some("missing") => ReadingKind::Missing,
        _ => return Reading::unknown("读数的报告状态无法解析"),
    };
    let value = object.get("value").and_then(|value| {
        value
            .as_i64()
            .map(NumberValue::Int)
            .or_else(|| value.as_f64().map(NumberValue::Float))
    });
    let note = object
        .get("note")
        .and_then(Value::as_str)
        .map(str::to_string);
    Reading::new(kind, value, note)
}

pub fn um_today(ledger: &Path, now_ms: f64) -> Value {
    let read = read_ledger(ledger);
    let (status, note) = ledger_status(&read);
    let day = window_day_keys(1, now_ms).first().cloned().unwrap_or_default();
    let start = day_start_ms(&day).unwrap_or(now_ms);
    let end = start + 86_400_000.0;
    let events: Vec<&Event> = read
        .events
        .iter()
        .filter(|event| event.ms.is_some_and(|ms| start <= ms && ms < end))
        .collect();
    let undated = read.events.iter().filter(|event| event.ms.is_none()).count();
    let coverage_payload = coverage(&read);
    if status == "missing" || status == "unavailable" {
        return json!({
            "today": empty_today(status, &note, now_ms),
            "coverage": coverage_payload,
            "timezone": "Asia/Shanghai",
            "checked_epoch": (now_ms / 1000.0).floor() as i64,
        });
    }
    if events.is_empty() && status == "partial" {
        return json!({
            "today": empty_today(status, &note, now_ms),
            "coverage": coverage_payload,
            "timezone": "Asia/Shanghai",
            "checked_epoch": (now_ms / 1000.0).floor() as i64,
        });
    }
    let logical: Vec<&Event> = events.iter().copied().filter(|event| !is_retry(event)).collect();
    let retries: Vec<&Event> = events.iter().copied().filter(|event| is_retry(event)).collect();
    let input_reading = sum_readings(
        &events
            .iter()
            .map(|event| token_reading(event, "input"))
            .collect::<Vec<_>>(),
    );
    let output_reading = sum_readings(
        &events
            .iter()
            .map(|event| token_reading(event, "output"))
            .collect::<Vec<_>>(),
    );
    let total = sum_readings(&[input_reading.clone(), output_reading.clone()]);
    let cache_reading = cache_tokens_reading(&events);
    let (cache_report, cache_detail) = cache_report(&events);
    let reasoning = sum_readings(
        &events
            .iter()
            .map(|event| reasoning_reading(event))
            .collect::<Vec<_>>(),
    );
    let (recorded, failed, result_counts) = requests_reading(&logical);
    let speed = speed_payload(&logical, "今天没有请求");
    let boundaries = json!({
        "timezone": "Asia/Shanghai",
        "start": iso(start),
        "end": iso(end),
        "half_open": true,
    });
    let mut today = json!({
        "day": day,
        "tokens": {"total": total.to_json(), "input": input_reading.to_json(),
                    "output": output_reading.to_json()},
        "cacheRead": {"tokens": cache_reading.to_json(),
                       "report": cache_report, "detail": cache_detail},
        "reasoning": reasoning.to_json(),
        "requests": {"recorded": recorded.to_json(), "failed": failed.to_json()},
        "speed": speed,
        "boundaries": boundaries,
        "results": result_counts,
    });
    if status == "partial" {
        mark_partial(&mut today);
    }
    json!({
        "today": today,
        "coverage": {
            "status": status,
            "note": note,
            "ledger_present": read.ledger_present,
            "events": read.events.len(),
            "scanned": events.len(),
            "undated_records": undated,
            "logical_records": logical.len(),
            "retry_records": retries.len(),
            "truncated": read.truncated,
            "errors": read.errors,
            "boundaries": boundaries,
        },
        "timezone": "Asia/Shanghai",
        "checked_epoch": (now_ms / 1000.0).floor() as i64,
    })
}

fn trend_bucket(events: &[&Event], logical: &[&Event]) -> Value {
    json!({
        "tokens": sum_readings(
            &events.iter().map(|event| total_reading(event)).collect::<Vec<_>>()
        ).to_json(),
        "requests": Reading::exact_int(logical.len() as i64).to_json(),
        "speed": speed_payload(logical, "该时段没有请求")
            .get("value").cloned().unwrap_or(Value::Null),
    })
}

fn merge_buckets(buckets: &[Value]) -> Value {
    let mut tokens = Vec::new();
    let mut requests = Vec::new();
    for bucket in buckets {
        if let Some(value) = bucket.get("tokens") {
            tokens.push(reading_from_json(value));
        }
        if let Some(value) = bucket.get("requests") {
            requests.push(reading_from_json(value));
        }
    }
    json!({
        "tokens": sum_readings(&tokens).to_json(),
        "requests": sum_readings(&requests).to_json(),
        "speed": Reading::missing("按请求加权的整次平均").to_json(),
    })
}

fn mark_trend_partial(trend: &mut Value) {
    let note = "账本有损坏行/截断，合计为下界";
    if let Some(all) = trend.get_mut("all") {
        if let Some(daily) = all.get_mut("daily").and_then(Value::as_array_mut) {
            for bucket in daily {
                mark_trend_bucket(bucket, note);
            }
        }
        if let Some(total) = all.get_mut("total") {
            mark_trend_bucket(total, note);
        }
    }
    if let Some(models) = trend.get_mut("models").and_then(Value::as_array_mut) {
        for row in models {
            if let Some(daily) = row.get_mut("daily").and_then(Value::as_array_mut) {
                for bucket in daily {
                    mark_trend_bucket(bucket, note);
                }
            }
            if let Some(total) = row.get_mut("total") {
                mark_trend_bucket(total, note);
            }
            if let Some(failed) = row.get_mut("failed") {
                *failed = lower_bound_from_json(failed, note);
            }
        }
    }
}

fn mark_trend_bucket(bucket: &mut Value, note: &str) {
    for key in ["tokens", "requests"] {
        if let Some(value) = bucket.get_mut(key) {
            *value = lower_bound_from_json(value, note);
        }
    }
    if let Some(value) = bucket.get_mut("speed") {
        *value = sample_reading_from_json(value);
    }
}

fn empty_trend(
    days: &[String],
    range: &str,
    status: &str,
    note: &str,
    start: f64,
    end: f64,
    now_ms: f64,
    ledger_present: bool,
    errors: &[Value],
) -> Value {
    let missing = Reading::missing(note);
    let bucket = json!({"tokens": missing.to_json(), "requests": missing.to_json(),
                          "speed": missing.to_json()});
    let daily: Vec<Value> = days.iter().map(|_| bucket.clone()).collect();
    json!({
        "trend": {
            "range": range,
            "days": days,
            "all": {"daily": daily, "total": bucket},
            "models": [],
            "speedBasis": "whole_request",
            "boundaries": {"timezone": "Asia/Shanghai", "start": iso(start),
                            "end": iso(end), "half_open": true},
        },
        "coverage": {
            "status": status,
            "note": note,
            "ledger_present": ledger_present,
            "events": 0,
            "scanned": 0,
            "undated_records": 0,
            "logical_records": 0,
            "retry_records": 0,
            "truncated": false,
            "errors": errors,
            "days": days,
        },
        "timezone": "Asia/Shanghai",
        "checked_epoch": (now_ms / 1000.0).floor() as i64,
    })
}

/// Desktop summary over the same ledger and token/retry rules as the web
/// console. Today uses Beijing hours, the other periods use calendar days.
pub fn desktop_trend(ledger: &Path, range: &str, now_ms: f64) -> Result<Value, UmQueryError> {
    if !matches!(range, "today" | "7d" | "30d") || !now_ms.is_finite() {
        return Err(UmQueryError("invalid desktop usage range or time".to_string()));
    }
    let (start, end, _) = window_bounds(range, now_ms)?;
    let hourly = range == "today";
    let step = if hourly { 3_600_000.0 } else { 86_400_000.0 };
    let count = ((end - start) / step) as usize;
    let read = read_ledger(ledger);
    let (status, note) = ledger_status(&read);
    let mut groups = vec![Vec::new(); count];
    let mut scanned = 0;
    for event in &read.events {
        if let Some(at) = event.ms.filter(|at| start <= *at && *at < end) {
            groups[((at - start) / step) as usize].push(event);
            scanned += 1;
        }
    }
    let mut buckets = Vec::with_capacity(count);
    for (index, events) in groups.iter().enumerate() {
        let label = if hourly {
            format!("{index:02}:00")
        } else {
            beijing_day(Some(start + index as f64 * step)).unwrap_or_default()
        };
        let mut bucket = if matches!(status, "missing" | "unavailable") {
            let missing = Reading::missing(&note).to_json();
            json!({ "tokens": missing, "requests": missing })
        } else {
            json!({
                "tokens": sum_readings(&events.iter().map(|event| total_reading(event)).collect::<Vec<_>>()).to_json(),
                "requests": Reading::exact_int(events.iter().filter(|event| !is_retry(event)).count() as i64).to_json(),
            })
        };
        bucket["label"] = json!(label);
        buckets.push(bucket);
    }
    let mut total = merge_buckets(&buckets);
    if status == "partial" {
        for bucket in &mut buckets {
            mark_trend_bucket(bucket, &note);
        }
        mark_trend_bucket(&mut total, &note);
    }
    Ok(json!({
        "buckets": buckets,
        "total": total,
        "coverage": { "status": status, "note": note, "scanned": scanned },
    }))
}

pub fn um_trend(ledger: &Path, range: &str, now_ms: f64) -> Result<Value, UmQueryError> {
    if !UM_TREND_RANGES.contains(&range) {
        return Err(UmQueryError(format!(
            "invalid trend range {range:?}: expected one of {}",
            UM_TREND_RANGES.join(", ")
        )));
    }
    let read = read_ledger(ledger);
    let (status, note) = ledger_status(&read);
    let days = window_day_keys(
        match range {
            "7d" => 7,
            "14d" => 14,
            _ => 30,
        },
        now_ms,
    );
    let start = days
        .first()
        .and_then(|day| day_start_ms(day))
        .unwrap_or(now_ms);
    let end = days
        .last()
        .and_then(|day| day_start_ms(day))
        .unwrap_or(now_ms)
        + 86_400_000.0;
    if status == "missing" || status == "unavailable" {
        return Ok(empty_trend(
            &days,
            range,
            status,
            &note,
            start,
            end,
            now_ms,
            read.ledger_present,
            &read.errors,
        ));
    }
    let events: Vec<&Event> = read
        .events
        .iter()
        .filter(|event| event.ms.is_some_and(|ms| start <= ms && ms < end))
        .collect();
    let undated = read.events.iter().filter(|event| event.ms.is_none()).count();
    let logical: Vec<&Event> = events.iter().copied().filter(|event| !is_retry(event)).collect();
    let retries: Vec<&Event> = events.iter().copied().filter(|event| is_retry(event)).collect();
    let mut by_day: BTreeMap<String, Vec<&Event>> =
        days.iter().map(|day| (day.clone(), Vec::new())).collect();
    for event in &events {
        if let Some(day) = beijing_day(event.ms) {
            if let Some(bucket) = by_day.get_mut(&day) {
                bucket.push(event);
            }
        }
    }
    let mut model_names: BTreeSet<String> = BTreeSet::new();
    for event in &events {
        model_names.insert(event.model.clone());
    }
    let mut day_buckets: Vec<Value> = Vec::new();
    for day in &days {
        let day_events = by_day.get(day).cloned().unwrap_or_default();
        let day_logical: Vec<&Event> = day_events
            .iter()
            .copied()
            .filter(|event| !is_retry(event))
            .collect();
        day_buckets.push(trend_bucket(&day_events, &day_logical));
    }
    let mut model_rows: Vec<(String, Value, f64)> = Vec::new();
    for model in model_names {
        let mut daily = Vec::new();
        let mut model_logical = Vec::new();
        let mut failed_total = 0i64;
        for day in &days {
            let day_events: Vec<&Event> = by_day
                .get(day)
                .map(|events| {
                    events
                        .iter()
                        .copied()
                        .filter(|event| event.model == model)
                        .collect()
                })
                .unwrap_or_default();
            let day_logical: Vec<&Event> = day_events
                .iter()
                .copied()
                .filter(|event| !is_retry(event))
                .collect();
            model_logical.extend(day_logical.iter().copied());
            for event in &day_logical {
                if matches!(result_of(event).0, "failed" | "truncated") {
                    failed_total += 1;
                }
            }
            daily.push(trend_bucket(&day_events, &day_logical));
        }
        let mut total = merge_buckets(&daily);
        if let Some(speed) = speed_payload(&model_logical, "该时段没有请求")
            .get("value")
            .cloned()
        {
            total["speed"] = speed;
        }
        let rank = total
            .get("tokens")
            .and_then(reading_value)
            .unwrap_or(0.0);
        model_rows.push((
            model.clone(),
            json!({
                "model": model,
                "daily": daily,
                "total": total,
                "failed": Reading::exact_int(failed_total).to_json(),
            }),
            rank,
        ));
    }
    model_rows.sort_by(|(left_name, _, left_rank), (right_name, _, right_rank)| {
        right_rank
            .partial_cmp(left_rank)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| left_name.cmp(right_name))
    });
    let all_total = {
        let mut total = merge_buckets(&day_buckets);
        if let Some(speed) = speed_payload(&logical, "该时段没有请求")
            .get("value")
            .cloned()
        {
            total["speed"] = speed;
        }
        total
    };
    let mut trend = json!({
        "range": range,
        "days": days,
        "all": {"daily": day_buckets, "total": all_total},
        "models": model_rows.into_iter().map(|(_, row, _)| row).collect::<Vec<_>>(),
        "speedBasis": "whole_request",
        "boundaries": {"timezone": "Asia/Shanghai", "start": iso(start),
                        "end": iso(end), "half_open": true},
    });
    if status == "partial" {
        mark_trend_partial(&mut trend);
    }
    Ok(json!({
        "trend": trend,
        "coverage": {
            "status": status,
            "note": note,
            "ledger_present": read.ledger_present,
            "events": read.events.len(),
            "scanned": events.len(),
            "undated_records": undated,
            "logical_records": logical.len(),
            "retry_records": retries.len(),
            "truncated": read.truncated,
            "errors": read.errors,
            "days": days,
        },
        "timezone": "Asia/Shanghai",
        "checked_epoch": (now_ms / 1000.0).floor() as i64,
    }))
}

fn reading_value(reading: &Value) -> Option<f64> {
    let object = reading.as_object()?;
    let kind = object.get("kind").and_then(Value::as_str)?;
    if !matches!(kind, "exact" | "atLeast" | "estimate") {
        return None;
    }
    object.get("value").and_then(Value::as_f64)
}

fn valid_request_id(value: Option<&str>) -> Option<String> {
    value
        .filter(|text| {
            text.len() == 32
                && text
                    .bytes()
                    .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        })
        .map(str::to_string)
}

fn um_log_row(category: &str, parsed: &ParsedLogLine, occurrence: usize) -> Value {
    let digest = {
        let mut hasher = Sha256::new();
        hasher.update(
            python_json(&json!({
                "source": "chat2api", "category": category, "ts": parsed.ts,
                "level": parsed.level, "message": parsed.message,
                "request_id": parsed.request_id,
            }))
            .as_bytes(),
        );
        format!("{:x}", hasher.finalize())[..16].to_string()
    };
    json!({
        "key": format!("chat2api:{category}:{digest}:{occurrence}"),
        "at": parsed.ts.map(|ts| (ts * 1000.0) as i64),
        "level": parsed.level,
        "source": if category == "service" { "chat2api".to_string() }
                  else { format!("chat2api/{category}") },
        "text": parsed.message.chars().take(4096).collect::<String>(),
        "requestId": valid_request_id(parsed.request_id.as_deref()),
    })
}

fn row_digest(event: &Event) -> String {
    let payload = json!({
        "time": event.ms,
        "model": event.model,
        "status": event.status,
        "input": event.input_tokens,
        "output": event.output_tokens,
        "cache": event.cache_tokens,
        "cache_report_status": event.cache_report_status,
        "reasoning": event.reasoning_tokens,
        "duration": event.duration_ms,
        "request_id": event.request_id,
        "usage_source": event.usage_source,
        "estimated": event.estimated,
    });
    let mut hasher = Sha256::new();
    hasher.update(python_json(&payload).as_bytes());
    format!("{:x}", hasher.finalize())[..16].to_string()
}

fn python_json(value: &Value) -> String {
    match value {
        Value::Null => "null".to_string(),
        Value::Bool(value) => value.to_string(),
        Value::Number(number) => number.to_string(),
        Value::String(text) => serde_json::to_string(text).unwrap_or_default(),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(python_json).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            format!(
                "{{{}}}",
                keys.iter()
                    .map(|key| format!(
                        "{}: {}",
                        serde_json::to_string(key).unwrap_or_default(),
                        python_json(&map[*key])
                    ))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
    }
}

fn occurrence_keys(events: &[&Event]) -> HashMap<usize, String> {
    let mut counts: HashMap<String, usize> = HashMap::new();
    let mut keys = HashMap::new();
    for (index, event) in events.iter().enumerate() {
        let digest = row_digest(event);
        let occurrence = *counts.get(&digest).unwrap_or(&0);
        counts.insert(digest.clone(), occurrence + 1);
        keys.insert(index, format!("chat2api:{digest}:{occurrence}"));
    }
    keys
}

fn retry_map(events: &[Event]) -> HashMap<String, Vec<&Event>> {
    let mut map: HashMap<String, Vec<&Event>> = HashMap::new();
    for event in events {
        if !is_retry(event) {
            continue;
        }
        if let Some(id) = valid_request_id(event.request_id.as_deref()) {
            map.entry(id).or_default().push(event);
        }
    }
    map
}

fn retry_note(event: &Event) -> String {
    let status = event
        .status
        .as_ref()
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    format!(
        "物理重试记录：{}；本次记录是估算下界",
        status.chars().take(64).collect::<String>()
    )
}

fn request_row(event: &Event, key: String, retries: &[&Event]) -> Value {
    let (result, result_note, http_status) = result_of(event);
    let mut all = vec![event];
    all.extend(retries.iter().copied());
    let input_reading = sum_readings(
        &all.iter().map(|event| token_reading(event, "input")).collect::<Vec<_>>(),
    );
    let output_reading = sum_readings(
        &all.iter().map(|event| token_reading(event, "output")).collect::<Vec<_>>(),
    );
    let cache_reading = sum_readings(
        &all.iter().map(|event| cache_reading(event)).collect::<Vec<_>>(),
    );
    let reasoning_reading = sum_readings(
        &all.iter().map(|event| reasoning_reading(event)).collect::<Vec<_>>(),
    );
    let duration_reading = match event.duration_state {
        DurationState::Ok => Reading::exact_int(event.duration_ms.unwrap_or(0)),
        DurationState::Invalid => Reading::unknown("耗时字段非正或不可解析"),
        DurationState::Missing => Reading::unknown("账本没有记录耗时"),
    };
    let speed_reading = match (event.output_tokens, event.duration_state) {
        (Some(output), DurationState::Ok) => {
            let rate = (output as f64 / (event.duration_ms.unwrap_or(1) as f64 / 1000.0)
                * 1_000_000.0)
                .round()
                / 1_000_000.0;
            if event.estimated {
                Reading::estimate_float(rate, "输出为本地估算")
            } else {
                Reading::exact_float(rate)
            }
        }
        _ => Reading::missing("缺输出 Token 或正耗时，无法计算"),
    };
    let request_id = valid_request_id(event.request_id.as_deref());
    let retry_rows = request_id.as_ref().map(|_| {
        retries
            .iter()
            .map(|event| {
                json!({
                    "at": event.ms.map(|ms| ms.floor() as i64),
                    "note": retry_note(event),
                })
            })
            .collect::<Vec<_>>()
    });
    let error = if matches!(result, "failed" | "truncated" | "client_disconnected" | "cancelled") {
        Some(json!({
            "code": event.status.as_ref().and_then(Value::as_str)
                .unwrap_or("unknown").chars().take(64).collect::<String>(),
            "message": result_note.chars().take(160).collect::<String>(),
        }))
    } else {
        None
    };
    json!({
        "key": key,
        "requestId": request_id,
        "at": event.ms.map(|ms| ms.floor() as i64),
        "model": event.model,
        "result": result,
        "resultNote": result_note,
        "httpStatus": http_status,
        "input": input_reading.to_json(),
        "output": output_reading.to_json(),
        "cacheRead": cache_reading.to_json(),
        "reasoning": reasoning_reading.to_json(),
        "durationMs": duration_reading.to_json(),
        "firstTokenMs": Reading::missing("账本没有记录首字耗时，不推算").to_json(),
        "speed": speed_reading.to_json(),
        "speedBasis": "whole_request",
        "retries": retry_rows,
        "error": error,
        "status": event.status.as_ref().and_then(Value::as_str),
    })
}

fn decode_cursor(cursor: &str) -> Result<(i64, String), UmQueryError> {
    if cursor.is_empty() {
        return Err(UmQueryError("invalid cursor".to_string()));
    }
    let raw = base64_decode(cursor).ok_or_else(|| UmQueryError("invalid cursor".to_string()))?;
    let text = String::from_utf8(raw).map_err(|_| UmQueryError("invalid cursor".to_string()))?;
    let value: Value =
        serde_json::from_str(&text).map_err(|_| UmQueryError("invalid cursor".to_string()))?;
    let Some(object) = value.as_object() else {
        return Err(UmQueryError("invalid cursor".to_string()));
    };
    if object.get("v").and_then(Value::as_i64) != Some(1) {
        return Err(UmQueryError("invalid cursor".to_string()));
    }
    let Some(at) = object.get("at").and_then(Value::as_i64) else {
        return Err(UmQueryError("invalid cursor".to_string()));
    };
    let Some(key) = object.get("key").and_then(Value::as_str) else {
        return Err(UmQueryError("invalid cursor".to_string()));
    };
    if key.is_empty() {
        return Err(UmQueryError("invalid cursor".to_string()));
    }
    Ok((at, key.to_string()))
}

fn encode_cursor(at: i64, key: &str) -> String {
    let payload = json!({"v": 1, "at": at, "key": key});
    base64_encode(python_json(&payload).as_bytes())
}

struct UmLogCursor {
    mode: String,
    at: Option<i64>,
    key: Option<String>,
    reader: Option<String>,
}

fn decode_um_log_cursor(cursor: &str) -> Option<UmLogCursor> {
    if cursor.is_empty() {
        return None;
    }
    let raw = base64_decode(cursor)?;
    let value: Value = serde_json::from_slice(&raw).ok()?;
    let object = value.as_object()?;
    if object.get("v").and_then(Value::as_i64) != Some(1) {
        return None;
    }
    let mode = object.get("mode")?.as_str()?.to_string();
    if mode != "same" && mode != "older" {
        return None;
    }
    let at = match object.get("at") {
        None | Some(Value::Null) => None,
        Some(value) => Some(value.as_i64()?),
    };
    let key = match object.get("key") {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) if !value.is_empty() => Some(value.clone()),
        _ => return None,
    };
    let reader = match object.get("reader") {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) if !value.is_empty() => Some(value.clone()),
        _ => return None,
    };
    if mode == "same" && at.is_none() {
        return None;
    }
    Some(UmLogCursor {
        mode,
        at,
        key,
        reader,
    })
}

fn encode_um_log_cursor(
    mode: &str,
    at: Option<i64>,
    key: Option<&str>,
    reader: Option<&str>,
) -> String {
    let payload = json!({
        "v": 1,
        "mode": mode,
        "at": at,
        "key": key,
        "reader": reader,
    });
    base64_encode(python_json(&payload).as_bytes())
}

pub fn um_requests(
    ledger: &Path,
    window: &str,
    model: Option<&str>,
    result: &str,
    search: &str,
    cursor: Option<&str>,
    limit: i64,
    now_ms: f64,
) -> Result<Value, UmQueryError> {
    if !UM_WINDOWS.contains(&window) {
        return Err(UmQueryError(format!(
            "invalid window {window:?}: expected one of {}",
            UM_WINDOWS.join(", ")
        )));
    }
    if result != "all" && !UM_RESULTS.contains(&result) {
        return Err(UmQueryError(format!(
            "invalid result {result:?}: expected all or one of {}",
            UM_RESULTS.join(", ")
        )));
    }
    let search: String = search.chars().take(UM_MAX_SEARCH_CHARS).collect();
    let limit = limit.clamp(1, UM_MAX_PAGE);
    let cursor_at_key = cursor.map(decode_cursor).transpose()?;
    let read = read_ledger(ledger);
    let (status, note) = ledger_status(&read);
    let (start, end, label) = window_bounds(window, now_ms)?;
    if status == "missing" || status == "unavailable" {
        let page_coverage = json!({
            "status": status, "note": note, "ledger_present": read.ledger_present,
            "events": read.events.len(), "errors": read.errors,
            "truncated": read.truncated,
        });
        return Ok(json!({
            "page": {
                "rows": [],
                "scope": format!("{label}的 UM 账本（chat2api）"),
                "earlier": null,
                "coverage": page_coverage,
            },
            "models": [],
            "coverage": coverage(&read),
            "timezone": "Asia/Shanghai",
            "checked_epoch": (now_ms / 1000.0).floor() as i64,
        }));
    }
    let events: Vec<&Event> = read
        .events
        .iter()
        .filter(|event| event.ms.is_some_and(|ms| start <= ms && ms < end))
        .collect();
    let undated = read.events.iter().filter(|event| event.ms.is_none()).count();
    let retries: Vec<&Event> = events.iter().copied().filter(|event| is_retry(event)).collect();
    let logical: Vec<&Event> = events.iter().copied().filter(|event| !is_retry(event)).collect();
    let retry_map = retry_map(&read.events);
    let keys = occurrence_keys(&logical);
    let logical_ids: BTreeSet<String> = logical
        .iter()
        .filter_map(|event| valid_request_id(event.request_id.as_deref()))
        .collect();
    let unattached = retries
        .iter()
        .filter(|event| {
            valid_request_id(event.request_id.as_deref())
                .is_none_or(|id| !logical_ids.contains(&id))
        })
        .count();
    let mut rows = Vec::new();
    for (index, event) in logical.iter().enumerate() {
        let (row_result, row_note, _http) = result_of(event);
        if model.is_some_and(|model| event.model != model) {
            continue;
        }
        if result != "all" && row_result != result {
            continue;
        }
        let event_request_id = valid_request_id(event.request_id.as_deref());
        let attached = event_request_id
            .as_ref()
            .and_then(|id| retry_map.get(id))
            .cloned()
            .unwrap_or_default();
        if !search.is_empty() {
            let haystack = format!(
                "{} {} {} {}",
                event.model,
                event.status.as_ref().and_then(Value::as_str).unwrap_or(""),
                event_request_id.as_deref().unwrap_or(""),
                row_note
            );
            if !haystack.to_lowercase().contains(&search.to_lowercase()) {
                continue;
            }
        }
        let key = keys.get(&index).cloned().unwrap_or_default();
        rows.push(request_row(event, key, &attached));
    }
    rows.sort_by(|left, right| {
        let left_at = left.get("at").and_then(Value::as_i64).unwrap_or(0);
        let right_at = right.get("at").and_then(Value::as_i64).unwrap_or(0);
        right_at
            .cmp(&left_at)
            .then_with(|| {
                left.get("key")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .cmp(right.get("key").and_then(Value::as_str).unwrap_or(""))
            })
    });
    if let Some((cursor_at, cursor_key)) = cursor_at_key {
        rows.retain(|row| {
            let at = row.get("at").and_then(Value::as_i64).unwrap_or(0);
            let key = row.get("key").and_then(Value::as_str).unwrap_or("");
            (-at, key) > (-cursor_at, cursor_key.as_str())
        });
    }
    let earlier = if rows.len() > limit as usize {
        let last = &rows[limit as usize - 1];
        Some(encode_cursor(
            last.get("at").and_then(Value::as_i64).unwrap_or(0),
            last.get("key").and_then(Value::as_str).unwrap_or(""),
        ))
    } else {
        None
    };
    rows.truncate(limit as usize);
    let mut models: Vec<String> = events.iter().map(|event| event.model.clone()).collect();
    models.sort();
    models.dedup();
    let mut page_coverage = coverage(&read);
    if let Some(object) = page_coverage.as_object_mut() {
        object.insert("scanned".to_string(), json!(events.len()));
        object.insert("undated_records".to_string(), json!(undated));
        object.insert("logical_records".to_string(), json!(logical.len()));
        object.insert("retry_records".to_string(), json!(retries.len()));
        object.insert("unattached_retries".to_string(), json!(unattached));
        object.insert("bounded".to_string(), json!(true));
        object.insert("clamped".to_string(), json!(false));
    }
    Ok(json!({
        "page": {
            "rows": rows,
            "scope": format!(
                "北京时间{label}（{} – {}）的 UM 账本（chat2api），时间倒序；物理重试不单独成行",
                iso(start).unwrap_or_default(), iso(end).unwrap_or_default()
            ),
            "earlier": earlier,
            "coverage": page_coverage,
        },
        "models": models,
        "coverage": page_coverage,
        "timezone": "Asia/Shanghai",
        "checked_epoch": (now_ms / 1000.0).floor() as i64,
    }))
}

pub(crate) fn base64_encode(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::new();
    for chunk in data.chunks(3) {
        let first = chunk[0] as u32;
        let second = chunk.get(1).copied().unwrap_or(0) as u32;
        let third = chunk.get(2).copied().unwrap_or(0) as u32;
        let value = (first << 16) | (second << 8) | third;
        output.push(ALPHABET[((value >> 18) & 63) as usize] as char);
        output.push(ALPHABET[((value >> 12) & 63) as usize] as char);
        output.push(if chunk.len() > 1 {
            ALPHABET[((value >> 6) & 63) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            ALPHABET[(value & 63) as usize] as char
        } else {
            '='
        });
    }
    output
}

pub(crate) fn base64_decode(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if bytes.len() % 4 != 0 {
        return None;
    }
    let mut output = Vec::new();
    for chunk in bytes.chunks(4) {
        let mut values = [0u32; 4];
        for (index, byte) in chunk.iter().enumerate() {
            values[index] = match *byte {
                b'A'..=b'Z' => (*byte - b'A') as u32,
                b'a'..=b'z' => (*byte - b'a' + 26) as u32,
                b'0'..=b'9' => (*byte - b'0' + 52) as u32,
                b'+' => 62,
                b'/' => 63,
                b'=' => 0,
                _ => return None,
            };
        }
        let value = (values[0] << 18) | (values[1] << 12) | (values[2] << 6) | values[3];
        output.push((value >> 16) as u8);
        if chunk[2] != b'=' {
            output.push((value >> 8) as u8);
        }
        if chunk[3] != b'=' {
            output.push(value as u8);
        }
    }
    Some(output)
}

fn log_category_path(
    category: &str,
    service_log: &Path,
    watch_dir: Option<&Path>,
) -> Option<PathBuf> {
    let parent = service_log.parent().unwrap_or_else(|| Path::new("."));
    match category {
        "service" => Some(service_log.to_path_buf()),
        "console" => Some(parent.join("chat2api.console.log")),
        "console_prev" => Some(parent.join("chat2api.console.prev.log")),
        "watch" => watch_dir.map(|dir| dir.join("chat2api-watch.log")),
        _ => None,
    }
}

#[derive(Debug)]
struct ParsedLogLine {
    ts: Option<f64>,
    level: &'static str,
    message: String,
    request_id: Option<String>,
    start: u64,
}

struct LogPrefix {
    tag: Option<String>,
    ts: String,
    level: &'static str,
    message: String,
}

fn word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn skip_ascii_whitespace(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len() && bytes[index].is_ascii_whitespace() {
        index += 1;
    }
    index
}

fn scan_fixed_digits(
    bytes: &[u8],
    mut index: usize,
    min: usize,
    max: usize,
) -> Option<usize> {
    let start = index;
    while index < bytes.len()
        && index - start < max
        && bytes[index].is_ascii_digit()
    {
        index += 1;
    }
    if index - start < min {
        return None;
    }
    Some(index)
}

fn scan_log_timestamp(text: &str, start: usize) -> Option<usize> {
    let bytes = text.as_bytes();
    let mut index = start;
    index = scan_fixed_digits(bytes, index, 4, 4)?;
    if !matches!(bytes.get(index), Some(b'-') | Some(b'/')) {
        return None;
    }
    index += 1;
    index = scan_fixed_digits(bytes, index, 2, 2)?;
    if !matches!(bytes.get(index), Some(b'-') | Some(b'/')) {
        return None;
    }
    index += 1;
    index = scan_fixed_digits(bytes, index, 2, 2)?;
    let after_day = index;
    let weekday_start = skip_ascii_whitespace(bytes, index);
    if text[weekday_start..].starts_with('周') {
        let mut chars = text[weekday_start + '周'.len_utf8()..].chars();
        if let Some(weekday) = chars.next() {
            if "一二三四五六日".contains(weekday) {
                index = weekday_start + '周'.len_utf8() + weekday.len_utf8();
            }
        }
    }
    if index == after_day {
        // No weekday marker: the separator is read from the original position.
    }
    if !matches!(bytes.get(index), Some(b' ') | Some(b'T')) {
        return None;
    }
    index += 1;
    index = scan_fixed_digits(bytes, index, 1, 2)?;
    if bytes.get(index) != Some(&b':') {
        return None;
    }
    index += 1;
    index = scan_fixed_digits(bytes, index, 2, 2)?;
    if bytes.get(index) != Some(&b':') {
        return None;
    }
    index += 1;
    index = scan_fixed_digits(bytes, index, 2, 2)?;
    if matches!(bytes.get(index), Some(b'.') | Some(b',')) {
        index += 1;
        let fraction_start = index;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        if index == fraction_start {
            return None;
        }
    }
    let zone_whitespace_start = index;
    let zone_start = skip_ascii_whitespace(bytes, index);
    if bytes.get(zone_start) == Some(&b'Z') {
        index = zone_start + 1;
    } else if matches!(bytes.get(zone_start), Some(b'+') | Some(b'-')) {
        let mut zone_index = zone_start + 1;
        zone_index = scan_fixed_digits(bytes, zone_index, 2, 2)?;
        if bytes.get(zone_index) == Some(&b':') {
            zone_index += 1;
        }
        zone_index = scan_fixed_digits(bytes, zone_index, 2, 2)?;
        index = zone_index;
    } else {
        index = zone_whitespace_start;
    }
    Some(index)
}

fn match_log_level(text: &str) -> Option<(&'static str, usize)> {
    const LEVELS: [(&str, &str); 11] = [
        ("TRACE", "debug"),
        ("DEBUG", "debug"),
        ("INFO", "info"),
        ("NOTICE", "info"),
        ("WARNING", "warn"),
        ("WARN", "warn"),
        ("ERROR", "error"),
        ("ERR", "error"),
        ("CRITICAL", "error"),
        ("CRIT", "error"),
        ("FATAL", "error"),
    ];
    let bytes = text.as_bytes();
    for (raw, canonical) in LEVELS {
        if bytes.len() < raw.len()
            || !bytes[..raw.len()].eq_ignore_ascii_case(raw.as_bytes())
        {
            continue;
        }
        if bytes
            .get(raw.len())
            .map_or(true, |byte| !word_byte(*byte))
        {
            return Some((canonical, raw.len()));
        }
    }
    None
}

fn parse_log_prefix(text: &str) -> Option<LogPrefix> {
    let bytes = text.as_bytes();
    let mut index = 0usize;
    let mut tag = None;
    if bytes.first() == Some(&b'[') {
        if let Some(close) = text[1..].find(']').map(|offset| offset + 1) {
            let content = &text[1..close];
            if !content.is_empty()
                && content.chars().count() <= 24
                && !content.chars().any(char::is_whitespace)
            {
                let after = close + 1;
                if text[after..]
                    .chars()
                    .next()
                    .is_some_and(char::is_whitespace)
                {
                    tag = Some(format!("[{content}]"));
                    index = skip_ascii_whitespace(bytes, after);
                }
            }
        }
    }
    if bytes.get(index) == Some(&b'[') {
        index += 1;
    }
    let timestamp_start = index;
    let timestamp_end = scan_log_timestamp(text, index)?;
    index = timestamp_end;
    if bytes.get(index) == Some(&b']') {
        index += 1;
    }
    let after_timestamp = index;
    index = skip_ascii_whitespace(bytes, index);
    if index == after_timestamp {
        return None;
    }
    let mut level = "unknown";
    let mut message = text[index..].to_string();
    if let Some((canonical, consumed)) = match_log_level(&text[index..]) {
        level = canonical;
        index += consumed;
        if matches!(bytes.get(index), Some(b',') | Some(b':')) {
            index += 1;
        }
        index = skip_ascii_whitespace(bytes, index);
        message = text[index..].to_string();
    }
    Some(LogPrefix {
        tag,
        ts: text[timestamp_start..timestamp_end].to_string(),
        level,
        message,
    })
}

fn python_value_text(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(value) => {
            if *value {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Value::Number(number) => number.to_string(),
        Value::String(text) => text.clone(),
        Value::Array(_) | Value::Object(_) => value.to_string(),
    }
}

fn parse_json_log_line(text: &str) -> Option<(String, String, Option<String>)> {
    let stripped = text.trim();
    if !(stripped.starts_with('{') && stripped.ends_with('}')) {
        return None;
    }
    let body = serde_json::from_str::<Value>(stripped).ok()?;
    let body = body.as_object()?;
    let mut ts = None;
    for key in ["time", "ts", "at_utc", "at"] {
        match body.get(key) {
            Some(Value::String(value)) if !value.trim().is_empty() => {
                ts = Some(value.trim().to_string());
                break;
            }
            Some(Value::Number(number)) => {
                if let Some(value) = number.as_i64() {
                    ts = Some(value.to_string());
                    break;
                }
            }
            _ => {}
        }
    }
    let ts = ts?;
    let mut bits = Vec::new();
    for (key, label) in [
        ("model", "model"),
        ("status", "status"),
        ("input_tokens", "in"),
        ("output_tokens", "out"),
        ("cached_input_tokens", "cached"),
        ("reasoning_tokens", "reasoning"),
        ("duration_ms", "duration_ms"),
        ("credits", "credits"),
    ] {
        let Some(value) = body.get(key) else {
            continue;
        };
        if value.is_null() || value.as_str() == Some("") {
            continue;
        }
        bits.push(format!("{label}={}", python_value_text(value)));
    }
    let message = if bits.is_empty() {
        stripped.to_string()
    } else {
        bits.join(" ")
    };
    let request_id = body.get("request_id").map(|value| {
        let mut text = redact(&python_value_text(value));
        text.truncate(
            text.char_indices()
                .nth(64)
                .map_or(text.len(), |(index, _)| index),
        );
        text
    });
    Some((ts, message, request_id))
}

fn find_request_assignment(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut search = 0usize;
    let mut found = None;
    while let Some(index) = text[search..].find("request=").map(|offset| search + offset) {
        if index == 0 || !word_byte(bytes[index - 1]) {
            let value_start = index + b"request=".len();
            let value_end = text[value_start..]
                .find(char::is_whitespace)
                .map_or(text.len(), |offset| value_start + offset);
            if value_end > value_start {
                found = Some(
                    redact(&text[value_start..value_end])
                        .chars()
                        .take(64)
                        .collect::<String>(),
                );
            }
        }
        search = index + b"request=".len();
    }
    found
}

fn parse_log_line(line: &str) -> ParsedLogLine {
    let text = line.trim();
    if let Some(prefix) = parse_log_prefix(text) {
        let mut message = prefix.message;
        if let Some(tag) = prefix.tag {
            message = format!("{tag} {message}");
        }
        let safe = redact(&message);
        let request_id = find_request_assignment(&safe);
        return ParsedLogLine {
            ts: crate::local_logs::normalize_ts(&prefix.ts),
            level: prefix.level,
            message: safe,
            request_id,
            start: 0,
        };
    }
    if let Some((ts, message, request_id)) = parse_json_log_line(text) {
        return ParsedLogLine {
            ts: crate::local_logs::normalize_ts(&ts),
            level: "unknown",
            message: redact(&message),
            request_id,
            start: 0,
        };
    }
    ParsedLogLine {
        ts: None,
        level: "unknown",
        message: redact(text),
        request_id: None,
        start: 0,
    }
}

fn redact_quoted_json_keys(output: &mut String, keys: &[&str]) {
    let mut search = 0usize;
    loop {
        let lower = output.as_bytes().to_ascii_lowercase();
        let mut found = None;
        for key in keys {
            let quoted = format!("\"{key}\"");
            let needle = quoted.as_bytes();
            if let Some(index) = lower[search..]
                .windows(needle.len())
                .position(|window| window == needle)
            {
                found = Some((search + index, needle.len()));
                break;
            }
        }
        let Some((index, key_len)) = found else {
            break;
        };
        let mut cursor = index + key_len;
        while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        if lower.get(cursor) != Some(&b':') {
            search = cursor;
            continue;
        }
        cursor += 1;
        while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        if lower.get(cursor) != Some(&b'"') {
            search = cursor;
            continue;
        }
        cursor += 1;
        let Some(end_offset) = output[cursor..].find('"') else {
            break;
        };
        let value_end = cursor + end_offset;
        if output.is_char_boundary(cursor) && output.is_char_boundary(value_end) {
            output.replace_range(cursor..value_end, "<redacted>");
        }
        search = cursor + "<redacted>".len();
    }
}

fn redact(text: &str) -> String {
    // Reuse the full Python `_SECRET_RE` port in `local_logs`, then cover the
    // quoted JSON carriers that port does not list (sign/signature/code).
    let mut output = crate::local_logs::redact(text);
    redact_quoted_json_keys(&mut output, &["sign", "signature", "code"]);
    output
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LogReadError {
    Missing,
    Denied,
    Io,
}

#[derive(Debug, Default)]
struct LogTail {
    rows: Vec<ParsedLogLine>,
    scanned_bytes: u64,
    window_start_offset: u64,
    truncated: bool,
    next_cursor: Option<String>,
    reset_hint: bool,
}

struct ReaderCursor {
    source: String,
    category: String,
    offset: u64,
    key: Option<String>,
}

fn log_read_error(error: &std::io::Error) -> LogReadError {
    match error.kind() {
        std::io::ErrorKind::NotFound => LogReadError::Missing,
        std::io::ErrorKind::PermissionDenied => LogReadError::Denied,
        _ => LogReadError::Io,
    }
}

fn log_read_error_status(error: LogReadError) -> (&'static str, String) {
    match error {
        LogReadError::Missing => ("missing", "file_missing".to_string()),
        LogReadError::Denied => ("denied", "permission_denied".to_string()),
        LogReadError::Io => ("error", "io_error:OSError".to_string()),
    }
}

fn empty_log_coverage(
    status: &str,
    error: Option<&str>,
    source: &str,
    note: Option<&str>,
) -> Value {
    json!({
        "status": status,
        "error": error,
        "source": source,
        "rows_read": 0,
        "rows_returned": 0,
        "undated_rows": 0,
        "truncated": false,
        "bounded": true,
        "note": note.unwrap_or("读取失败/未配置不是合法空列表"),
    })
}

fn empty_logs_payload(
    status: &str,
    error: Option<&str>,
    source: &str,
    label: &str,
    note: Option<&str>,
) -> Value {
    json!({
        "status": status,
        "error": error,
        "lines": [],
        "sources": UM_LOG_CATEGORIES,
        "scope": format!(
            "chat2api 运行日志有界尾读（最多 256 KiB / 400 行）· 北京时间{label} · 不是完整历史"
        ),
        "earlier": null,
        "coverage": empty_log_coverage(status, error, source, note),
    })
}

fn log_file_identity(metadata: &std::fs::Metadata) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        format!(
            "{}:{}:{}",
            metadata.ino(),
            metadata.dev(),
            metadata.len()
        )
    }
    #[cfg(not(unix))]
    {
        let created = metadata
            .created()
            .ok()
            .and_then(|time| time.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|duration| duration.as_nanos() as i64)
            .unwrap_or(0);
        format!("0:{created}:{}", metadata.len())
    }
}

fn cursor_meta(key: Option<&str>) -> Option<(i64, i64, i64)> {
    let key = key?;
    let mut parts: Vec<&str> = key.rsplitn(3, ':').collect();
    if parts.len() != 3 {
        return None;
    }
    parts.reverse();
    Some((
        parts[0].parse().ok()?,
        parts[1].parse().ok()?,
        parts[2].parse().ok()?,
    ))
}

fn decode_reader_cursor(cursor: &str) -> Option<ReaderCursor> {
    let raw = base64_decode(cursor)?;
    let value: Value = serde_json::from_slice(&raw).ok()?;
    let object = value.as_object()?;
    let source = object.get("src")?.as_str()?.to_string();
    let category = object.get("cat")?.as_str()?.to_string();
    let offset = object.get("off")?.as_i64()?;
    if offset < 0 {
        return None;
    }
    let key = match object.get("key") {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) => Some(value.clone()),
        _ => return None,
    };
    Some(ReaderCursor {
        source,
        category,
        offset: offset as u64,
        key,
    })
}

fn encode_reader_cursor(
    source: &str,
    category: &str,
    offset: u64,
    key: Option<&str>,
) -> String {
    let payload = json!({
        "src": source,
        "cat": category,
        "off": offset,
        "key": key,
    });
    base64_encode(python_json(&payload).as_bytes())
}

fn reader_cursor_stale(
    cursor: &ReaderCursor,
    category: &str,
    size: u64,
    file_key: &str,
) -> bool {
    if cursor.source != "chat2api" || cursor.category != category || cursor.offset > size {
        return true;
    }
    if cursor.key.as_deref() != Some(file_key) {
        let minted = cursor_meta(cursor.key.as_deref());
        let current = cursor_meta(Some(file_key));
        return match (minted, current) {
            (Some(minted), Some(current)) => {
                minted.0 != current.0
                    || minted.1 != current.1
                    || current.2 < minted.2
            }
            _ => true,
        };
    }
    false
}

fn read_log_tail(
    path: &Path,
    category: &str,
    reader_cursor: Option<&str>,
) -> Result<LogTail, LogReadError> {
    let metadata = std::fs::metadata(path).map_err(|error| log_read_error(&error))?;
    let size = metadata.len();
    let file_key = log_file_identity(&metadata);
    let (off, older_mode) = match reader_cursor {
        None => (size, false),
        Some(cursor) => match decode_reader_cursor(cursor) {
            Some(decoded) if !reader_cursor_stale(&decoded, category, size, &file_key) => {
                (decoded.offset, true)
            }
            _ => {
                return Ok(LogTail {
                    reset_hint: true,
                    ..LogTail::default()
                })
            }
        },
    };
    let window_start = off.saturating_sub(UM_LOG_MAX_TAIL_BYTES);
    use std::io::{Read as _, Seek as _, SeekFrom};
    let mut file = std::fs::File::open(path).map_err(|error| log_read_error(&error))?;
    file.seek(SeekFrom::Start(window_start))
        .map_err(|error| log_read_error(&error))?;
    let mut data = Vec::new();
    (&mut file)
        .take(off - window_start)
        .read_to_end(&mut data)
        .map_err(|error| log_read_error(&error))?;
    let scanned_bytes = data.len() as u64;
    let mut window_start_offset = window_start;
    if window_start_offset > 0 {
        let mut previous = [0u8; 1];
        file.seek(SeekFrom::Start(window_start_offset - 1))
            .map_err(|error| log_read_error(&error))?;
        file.read_exact(&mut previous)
            .map_err(|error| log_read_error(&error))?;
        if previous[0] != b'\n' {
            let Some(newline) = data.iter().position(|byte| *byte == b'\n') else {
                return Ok(LogTail {
                    scanned_bytes,
                    window_start_offset,
                    truncated: true,
                    ..LogTail::default()
                });
            };
            window_start_offset += newline as u64 + 1;
            data.drain(..=newline);
        }
    }
    let mut parts: Vec<&[u8]> = data.split(|byte| *byte == b'\n').collect();
    let trailing = parts.pop().unwrap_or(&[]);
    let trailing_live = !trailing.is_empty() && (!older_mode || off >= size);
    let mut rows = Vec::new();
    let mut position = window_start_offset;
    for part in parts {
        let text = String::from_utf8_lossy(&part[..part.len().min(8192)]).to_string();
        let mut parsed = parse_log_line(&text);
        parsed.start = position;
        rows.push(parsed);
        position += part.len() as u64 + 1;
    }
    let mut truncated = trailing_live;
    let mut trimmed = false;
    if rows.len() > UM_LOG_MAX_ROWS {
        let dropped = rows.len() - UM_LOG_MAX_ROWS;
        rows.drain(..dropped);
        truncated = true;
        trimmed = true;
    }
    let anchor = if trimmed {
        rows.first().map_or(window_start_offset, |row| row.start)
    } else {
        window_start_offset
    };
    let next_cursor = if anchor > 0 {
        Some(encode_reader_cursor(
            "chat2api",
            category,
            anchor,
            Some(&file_key),
        ))
    } else {
        None
    };
    Ok(LogTail {
        rows,
        scanned_bytes,
        window_start_offset,
        truncated,
        next_cursor,
        reset_hint: false,
    })
}

fn um_log_lines(
    service_log: &Path,
    watch_dir: Option<&Path>,
    window: &str,
    source: &str,
    now_ms: f64,
    levels: &[String],
    cursor: Option<&str>,
    limit: i64,
) -> (String, Option<String>, Vec<Value>, String, Option<String>, Value) {
    if !UM_LOG_CATEGORIES.contains(&source) {
        return (
            "error".to_string(),
            Some("unknown_log_source".to_string()),
            Vec::new(),
            source.to_string(),
            None,
            empty_log_coverage("error", Some("unknown_log_source"), source, None),
        );
    }
    if levels.iter().any(|level| !UM_LOG_LEVELS.contains(&level.as_str())) {
        return (
            "error".to_string(),
            Some("invalid_level".to_string()),
            Vec::new(),
            source.to_string(),
            None,
            empty_log_coverage("error", Some("invalid_level"), source, None),
        );
    }
    let limit = limit.clamp(1, UM_LOG_MAX_PAGE);
    let cursor = match cursor {
        None => None,
        Some(cursor) => match decode_um_log_cursor(cursor) {
            Some(decoded) => Some(decoded),
            None => {
                return (
                    "error".to_string(),
                    Some("invalid_cursor".to_string()),
                    Vec::new(),
                    source.to_string(),
                    None,
                    empty_log_coverage("error", Some("invalid_cursor"), source, None),
                )
            }
        },
    };
    let Some(path) = log_category_path(source, service_log, watch_dir) else {
        return (
            "unconfigured".to_string(),
            Some("not_configured".to_string()),
            Vec::new(),
            source.to_string(),
            None,
            empty_log_coverage("unconfigured", Some("not_configured"), source, None),
        );
    };
    let reader_cursor = cursor.as_ref().and_then(|cursor| cursor.reader.as_deref());
    let tail = match read_log_tail(&path, source, reader_cursor) {
        Ok(tail) => tail,
        Err(error) => {
            let (status, error_code) = log_read_error_status(error);
            let coverage = empty_log_coverage(&status, Some(&error_code), source, None);
            return (
                status.to_string(),
                Some(error_code),
                Vec::new(),
                source.to_string(),
                None,
                coverage,
            );
        }
    };
    let (start, end, _label) = window_bounds(window, now_ms)
        .unwrap_or((now_ms - 86_400_000.0, now_ms + 1.0, "今天".to_string()));
    let mut rows = Vec::new();
    let mut undated = 0;
    for parsed in &tail.rows {
        let Some(at) = parsed.ts else {
            undated += 1;
            continue;
        };
        let at_ms = (at * 1000.0) as i64;
        if !(start <= at_ms as f64 && (at_ms as f64) < end) {
            continue;
        }
        if !levels.is_empty() && !levels.iter().any(|level| level == parsed.level) {
            continue;
        }
        rows.push(um_log_row(source, parsed, rows.len()));
    }
    rows.sort_by(|left, right| {
        let left_at = left.get("at").and_then(Value::as_i64).unwrap_or(0);
        let right_at = right.get("at").and_then(Value::as_i64).unwrap_or(0);
        right_at.cmp(&left_at).then_with(|| {
            left.get("key").and_then(Value::as_str).unwrap_or("")
                .cmp(right.get("key").and_then(Value::as_str).unwrap_or(""))
        })
    });
    if let Some(cursor) = &cursor {
        if cursor.mode == "same" {
            let cursor_at = cursor.at.unwrap_or(0);
            let cursor_key = cursor.key.as_deref().unwrap_or("");
            rows.retain(|row| {
                let at = row.get("at").and_then(Value::as_i64).unwrap_or(0);
                let key = row.get("key").and_then(Value::as_str).unwrap_or("");
                (-at, key) > (-cursor_at, cursor_key)
            });
        }
    }
    let rows_read = tail.rows.len();
    let earlier = if rows.len() > limit as usize {
        let last = &rows[limit as usize - 1];
        Some(encode_um_log_cursor(
            "same",
            Some(last.get("at").and_then(Value::as_i64).unwrap_or(0)),
            last.get("key").and_then(Value::as_str),
            reader_cursor,
        ))
    } else if let Some(next_cursor) = tail.next_cursor.as_deref() {
        Some(encode_um_log_cursor(
            "older",
            None,
            None,
            Some(next_cursor),
        ))
    } else {
        None
    };
    rows.truncate(limit as usize);
    let coverage = json!({
        "status": "ok",
        "error": null,
        "source": source,
        "scanned_bytes": tail.scanned_bytes,
        "window_start_offset": tail.window_start_offset,
        "rows_read": rows_read,
        "rows_returned": rows.len(),
        "undated_rows": undated,
        "truncated": tail.truncated,
        "reset_hint": tail.reset_hint,
        "bounded": true,
        "note": "有界尾读/窗口合并：不是完整历史",
    });
    ("ok".to_string(), None, rows, source.to_string(), earlier, coverage)
}

pub const UM_LOGIN_ROUND_MAX_PAGE: i64 = 400;

/// Read the durable login-round history the local 8000 gateway writes (#115).
/// Missing history is a legal empty read; an unreadable file is an error, never an
/// empty list. The read only parses the file: it never probes the upstream,
/// starts a login or appends a record.
pub fn um_login_rounds(
    path: &Path,
    round: Option<u64>,
    q: Option<&str>,
    limit: i64,
) -> Result<Value, UmQueryError> {
    um_login_rounds_scoped(path, round, q, limit, "本机 8000")
}

pub fn um_login_rounds_scoped(
    path: &Path,
    round: Option<u64>,
    q: Option<&str>,
    limit: i64,
    scope: &str,
) -> Result<Value, UmQueryError> {
    let limit = limit.clamp(1, UM_LOGIN_ROUND_MAX_PAGE);
    let search = q
        .unwrap_or("")
        .trim()
        .to_lowercase()
        .chars()
        .take(UM_MAX_SEARCH_CHARS)
        .collect::<String>();
    let records = read_records(path)
        .map_err(|error| UmQueryError(format!("login_history_unreadable: {error}")))?;
    let mut rows: Vec<Value> = records
        .iter()
        .filter(|record| round.map_or(true, |round| record.round == round))
        .filter(|record| {
            search.is_empty()
                || record
                    .reason
                    .as_deref()
                    .is_some_and(|reason| reason.to_lowercase().contains(&search))
                || record
                    .outcome
                    .as_deref()
                    .is_some_and(|outcome| outcome.to_lowercase().contains(&search))
                || record
                    .trigger_source
                    .as_deref()
                    .is_some_and(|source| source.to_lowercase().contains(&search))
                || record.rejected_before_expiry_status.is_some_and(|status| {
                    format!("旧凭据尚未到期，已被 UM 拒绝 HTTP {status}")
                        .to_lowercase()
                        .contains(&search)
                })
        })
        .map(login_round_row)
        .collect();
    let truncated = rows.len() > limit as usize;
    rows.truncate(limit as usize);
    Ok(json!({
        "status": "ok",
        "error": null,
        "rounds": rows,
        "count": rows.len(),
        "truncated": truncated,
        "scope": format!(
            "{scope} UM 登录轮次历史（最近 7 天 / 4 MiB）· 只读，不触发探测或登录 · 本页 {} 条",
            rows.len()
        ),
        "coverage": {
            "status": "ok",
            "error": null,
            "source": path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("login-history.jsonl"),
            "bounded": true,
            "note": "读取失败/损坏不是合法空列表",
        },
    }))
}

fn login_round_row(record: &LoginRoundRecord) -> Value {
    let bounded = |value: Option<&str>, max: usize| {
        value.map(|raw| {
            raw.chars()
                .filter(|character| !character.is_control())
                .take(max)
                .collect::<String>()
        })
    };
    json!({
        "key": format!("{}:{}", record.run_id, record.round),
        "runId": record.run_id.chars().take(120).collect::<String>(),
        "round": record.round,
        "state": record.state,
        "triggerSource": bounded(record.trigger_source.as_deref(), 40),
        "driver": bounded(record.driver.as_deref(), 20),
        "visible": record.visible,
        "startedAt": (record.started_epoch * 1000.0).round() as i64,
        "endedAt": record.ended_epoch.map(|epoch| (epoch * 1000.0).round() as i64),
        "submitCount": record.submit_count,
        "outcome": bounded(record.outcome.as_deref(), 40),
        "reason": record.reason.as_deref().map(sanitize_reason),
        "detail": record.detail.as_deref().map(sanitize_reason),
        "upstreamStatus": record.upstream_status,
        "evidenceVerdict": bounded(record.evidence_verdict.as_deref(), 40),
        "rejectedBeforeExpiryStatus": record.rejected_before_expiry_status,
    })
}

pub fn um_logs(
    service_log: &Path,
    watch_dir: Option<&Path>,
    window: &str,
    levels: &[String],
    source: &str,
    cursor: Option<&str>,
    limit: i64,
    now_ms: f64,
) -> Value {
    let limit = limit.clamp(1, UM_LOG_MAX_PAGE);
    if !UM_LOG_CATEGORIES.contains(&source) {
        return empty_logs_payload(
            "error",
            Some("unknown_log_source"),
            source,
            "今天",
            None,
        );
    }
    if levels.iter().any(|level| !UM_LOG_LEVELS.contains(&level.as_str())) {
        return empty_logs_payload(
            "error",
            Some("invalid_level"),
            source,
            "今天",
            None,
        );
    }
    let (_start, _end, label) = match window_bounds(window, now_ms) {
        Ok(bounds) => bounds,
        Err(error) => {
            return empty_logs_payload(
                "error",
                Some(&error.0),
                source,
                "今天",
                None,
            );
        }
    };
    let (status, error, rows, _source, earlier, coverage) = um_log_lines(
        service_log,
        watch_dir,
        window,
        source,
        now_ms,
        levels,
        cursor,
        limit,
    );
    if status != "ok" {
        return empty_logs_payload(
            &status,
            error.as_deref(),
            source,
            "今天",
            Some("读取失败/未配置/缺失/拒绝不是合法空列表"),
        );
    }
    json!({
        "status": status,
        "error": error,
        "lines": rows,
        "sources": UM_LOG_CATEGORIES,
        "scope": format!("chat2api {source} 日志有界尾读（最多 256 KiB / 400 行）· 北京时间{label} · 不是完整历史"),
        "earlier": earlier,
        "coverage": coverage,
    })
}

pub fn um_request_logs(
    service_log: &Path,
    watch_dir: Option<&Path>,
    request_id: &str,
    limit: i64,
    _now_ms: f64,
) -> Result<Value, UmQueryError> {
    if request_id.len() != 32
        || !request_id
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(UmQueryError("invalid_request_id".to_string()));
    }
    let limit = limit.clamp(1, UM_LOG_MAX_PAGE);
    let mut lines = Vec::new();
    let mut categories = Map::new();
    let mut rows_read = 0;
    for category in UM_LOG_CATEGORIES {
        let Some(path) = log_category_path(category, service_log, watch_dir) else {
            categories.insert(
                category.to_string(),
                json!({"status": "unconfigured", "error": "not_configured", "rows": null}),
            );
            continue;
        };
        let tail = match read_log_tail(&path, category, None) {
            Ok(tail) => tail,
            Err(error) => {
                let (status, error_code) = log_read_error_status(error);
                categories.insert(
                    category.to_string(),
                    json!({"status": status, "error": error_code, "rows": null}),
                );
                continue;
            }
        };
        for parsed in &tail.rows {
            if valid_request_id(parsed.request_id.as_deref()).as_deref() != Some(request_id) {
                continue;
            }
            rows_read += 1;
            lines.push(um_log_row(category, parsed, lines.len()));
        }
        categories.insert(
            category.to_string(),
            json!({"status": "ok", "error": null, "rows": tail.rows.len()}),
        );
    }
    lines.sort_by(|left, right| {
        let left_at = left.get("at").and_then(Value::as_i64).unwrap_or(0);
        let right_at = right.get("at").and_then(Value::as_i64).unwrap_or(0);
        right_at.cmp(&left_at).then_with(|| {
            left.get("key").and_then(Value::as_str).unwrap_or("")
                .cmp(right.get("key").and_then(Value::as_str).unwrap_or(""))
        })
    });
    lines.truncate(limit as usize);
    let ok = categories
        .values()
        .filter(|entry| entry.get("status").and_then(Value::as_str) == Some("ok"))
        .count();
    let status = if ok == categories.len() {
        "ok"
    } else if ok > 0 {
        "partial"
    } else {
        "unavailable"
    };
    let coverage = json!({
        "status": status,
        "categories": categories,
        "rows_read": rows_read,
        "rows_returned": lines.len(),
        "bounded": true,
        "note": "读取失败/拒绝/缺失的分类不是空列表；见分类状态",
    });
    Ok(json!({
        "requestId": request_id,
        "lines": lines,
        "scope": "chat2api 运行日志有界尾读（service/console/console_prev/watch）· 只匹配可靠 32 位十六进制 request_id · 不是完整历史",
        "coverage": coverage,
    }))
}

fn maintenance_row(operation: &Value) -> Value {
    let created = operation.get("created_at").and_then(Value::as_f64);
    let finished = operation.get("finished_at").and_then(Value::as_f64);
    let state = match operation.get("state").and_then(Value::as_str) {
        Some("queued") => "queued",
        Some("running") => "running",
        Some("succeeded") => "succeeded",
        Some("failed") => "failed",
        Some("cancelled") => "cancelled",
        _ => "interrupted",
    };
    let steps: Vec<Value> = operation
        .get("steps")
        .and_then(Value::as_array)
        .map(|steps| {
            steps
                .iter()
                .filter_map(|step| {
                    let step = step.as_object()?;
                    let at = step.get("at").and_then(Value::as_f64)?;
                    Some(json!({"at": (at * 1000.0) as i64,
                                  "note": step.get("note").and_then(Value::as_str)
                                      .unwrap_or("").chars().take(240).collect::<String>()}))
                })
                .collect()
        })
        .unwrap_or_default();
    let result = if let Some(error) = operation.get("error").and_then(Value::as_object) {
        let code = error.get("code").and_then(Value::as_str).unwrap_or("error");
        let message = error.get("message").and_then(Value::as_str).unwrap_or("");
        format!(
            "{}: {}",
            code.chars().take(64).collect::<String>(),
            message.chars().take(160).collect::<String>()
        )
        .trim_matches([':', ' '].as_ref())
        .to_string()
    } else if let Some(result) = operation.get("result").and_then(Value::as_object) {
        let bits: Vec<String> = ["status", "state", "verify", "label", "key", "action", "reason", "detail", "note"]
            .iter()
            .filter_map(|key| {
                let value = result.get(*key)?;
                if value.is_object() || value.is_array() {
                    return None;
                }
                Some(format!(
                    "{key}={}",
                    match value {
                        Value::String(text) => text.clone(),
                        other => other.to_string(),
                    }
                    .chars()
                    .take(80)
                    .collect::<String>()
                ))
            })
            .collect();
        if bits.is_empty() {
            match state {
                "succeeded" => "已完成（未记录结果）".to_string(),
                "failed" => "失败（未记录错误）".to_string(),
                "queued" => "排队中".to_string(),
                "running" => "执行中".to_string(),
                "cancelled" => "已取消".to_string(),
                _ => "已中断".to_string(),
            }
        } else {
            bits.join(" · ").chars().take(240).collect()
        }
    } else {
        match state {
            "succeeded" => "已完成（未记录结果）".to_string(),
            "failed" => "失败（未记录错误）".to_string(),
            "queued" => "排队中".to_string(),
            "running" => "执行中".to_string(),
            "cancelled" => "已取消".to_string(),
            _ => "已中断".to_string(),
        }
    };
    json!({
        "key": operation.get("id").and_then(Value::as_str).unwrap_or(""),
        "action": operation.get("action").and_then(Value::as_str).unwrap_or(""),
        "state": state,
        "at": created.map(|value| (value * 1000.0) as i64),
        "finishedAt": finished.map(|value| (value * 1000.0) as i64),
        "steps": steps,
        "result": result,
    })
}

fn is_maintenance(operation: &Value) -> bool {
    let action = operation.get("action").and_then(Value::as_str);
    if action.is_some_and(|action| UM_MAINTENANCE_ACTIONS.contains(&action)) {
        return true;
    }
    action.is_some_and(|action| UM_MAINTENANCE_SERVICE_ACTIONS.contains(&action))
        && operation.get("target").and_then(Value::as_str) == Some(UM_MAINTENANCE_TARGET)
}

pub fn um_maintenance(reader: &OperationsReader, limit: i64) -> Value {
    let limit = limit.clamp(1, UM_MAINTENANCE_MAX_READ);
    let payload = reader.list(&format!("?limit={}", UM_MAINTENANCE_MAX_READ));
    let (operations, coverage) = match payload {
        Ok(payload) => (
            payload
                .get("operations")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default(),
            json!({
                "status": "ok",
                "error": null,
                "operations_read": payload.get("operations")
                    .and_then(Value::as_array).map_or(0, Vec::len),
                "rows_returned": 0,
                "bounded": true,
                "note": format!(
                    "只读最近 {UM_MAINTENANCE_MAX_READ} 条原操作记录；更早的维护项未读取，不是空列表"
                ),
            }),
        ),
        Err(_error) => (
            Vec::new(),
            json!({
                "status": "unavailable",
                "error": "console_unavailable",
                "operations_read": 0,
                "rows_returned": 0,
                "bounded": true,
                "note": "操作记录不可用不是合法空列表",
            }),
        ),
    };
    let operations_read = coverage
        .get("operations_read")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let mut rows: Vec<Value> = operations
        .iter()
        .filter(|operation| is_maintenance(operation))
        .map(maintenance_row)
        .collect();
    rows.sort_by(|left, right| {
        let left_at = left.get("at").and_then(Value::as_i64).unwrap_or(0);
        let right_at = right.get("at").and_then(Value::as_i64).unwrap_or(0);
        right_at.cmp(&left_at).then_with(|| {
            left.get("key").and_then(Value::as_str).unwrap_or("")
                .cmp(right.get("key").and_then(Value::as_str).unwrap_or(""))
        })
    });
    rows.truncate(limit as usize);
    let mut coverage = coverage;
    if let Some(object) = coverage.as_object_mut() {
        object.insert("rows_returned".to_string(), json!(rows.len()));
        object.insert("operations_read".to_string(), json!(operations_read));
    }
    json!({
        "rows": rows,
        "scope": format!(
            "原操作记录中的 UM 维护项（account.um_status / account.um_verify / account.login / service.* on chat2api）；只读最近 {UM_MAINTENANCE_MAX_READ} 条，不与请求混表"
        ),
        "earlier": null,
        "coverage": coverage,
    })
}
