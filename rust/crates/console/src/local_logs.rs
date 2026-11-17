//! Local gateway run-log read faces (`console/log_reader.py`).
//!
//! The source registry is server-side only: the client sends source/category
//! ids and never a path. `log-sources` returns the registry with paths
//! reduced to file names; `log-entries`/`log-overview` read bounded windows
//! from the registered targets and keep missing/unconfigured/failed targets
//! distinct from an empty log.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use chrono::{FixedOffset, Local, NaiveDate, NaiveTime, TimeZone};

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;

pub const MAX_TAIL_BYTES: u64 = 262_144;
pub const MAX_ROWS: usize = 400;
pub const MAX_LINE_BYTES: usize = 8192;
pub const MAX_QUERY_CHARS: usize = 120;
pub const SCAN_SECONDS: f64 = 3.0;
pub const MAX_ROTATION_INDEX: u32 = 5;
pub const MAX_OVERVIEW_ROWS: usize = 1200;
pub const SHARE_RUN_ROOT: &str = r"C:\ProgramData\UMShareRun";

const CONSOLE_NOTE: &str = "8001 宿主不落盘行日志（start_usage_service.bat 无重定向，usagesrv 的输出只到隐藏窗口）；控制台自身的操作记录在「操作记录」页签（data/console-state.sqlite3），此处不伪造日志文件。";

#[derive(Debug, Clone)]
pub struct LogPaths {
    pub service_log: PathBuf,
    pub ledger: PathBuf,
    pub watch_dir: Option<PathBuf>,
    pub share_backend_dir: Option<PathBuf>,
}

impl LogPaths {
    pub fn from_config(config: &ConsoleConfig) -> Self {
        let share_backend_dir = match &config.share_backend_dir {
            Some(dir) if dir.is_absolute() => Some(dir.clone()),
            Some(_) => None,
            None => local_app_data()
                .map(|root| root.join("SelfUseAI").join("ai-gateway").join("share-backend")),
        };
        let watch_dir = match &config.watch_dir {
            Some(dir) if dir.is_absolute() => Some(dir.clone()),
            Some(_) => None,
            None => local_app_data()
                .map(|root| root.join("SelfUseAI").join("ai-gateway").join("watch")),
        };
        Self {
            service_log: config.service_log.clone(),
            ledger: config.usage_log.clone(),
            watch_dir,
            share_backend_dir,
        }
    }
}

fn local_app_data() -> Option<PathBuf> {
    std::env::var_os("LOCALAPPDATA")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

#[derive(Debug, Clone)]
pub struct LogCategory {
    pub category: &'static str,
    pub display: &'static str,
    pub path: Option<PathBuf>,
    pub rotating: bool,
    pub rotation_index: Option<u32>,
    pub level_filter: bool,
    pub whole_document: bool,
}

#[derive(Debug, Clone)]
pub struct LogSource {
    pub source: &'static str,
    pub display: &'static str,
    pub note: Option<&'static str>,
    pub categories: Vec<LogCategory>,
}

fn category(
    category: &'static str,
    display: &'static str,
    path: Option<PathBuf>,
    rotating: bool,
    rotation_index: Option<u32>,
    level_filter: bool,
    whole_document: bool,
) -> LogCategory {
    LogCategory {
        category,
        display,
        path,
        rotating,
        rotation_index,
        level_filter,
        whole_document,
    }
}

pub fn build_registry(paths: &LogPaths) -> Vec<LogSource> {
    let chat2api_dir = paths
        .service_log
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .to_path_buf();
    let share = paths.share_backend_dir.as_ref();
    let share_run = Path::new(SHARE_RUN_ROOT);
    vec![
        LogSource {
            source: "chat2api",
            display: "chat2api 主链（8000）",
            note: None,
            categories: vec![
                category(
                    "service",
                    "运行日志 chat2api.log",
                    Some(paths.service_log.clone()),
                    true,
                    Some(MAX_ROTATION_INDEX),
                    true,
                    false,
                ),
                category(
                    "ledger",
                    "调用账本 usage-chat2api.jsonl",
                    Some(paths.ledger.clone()),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "console",
                    "隐藏启动输出 chat2api.console.log（含启动/退出记录）",
                    Some(chat2api_dir.join("chat2api.console.log")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "console_prev",
                    "上一次启动输出 chat2api.console.prev.log",
                    Some(chat2api_dir.join("chat2api.console.prev.log")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "watch",
                    "守护日志 chat2api-watch.log",
                    paths
                        .watch_dir
                        .as_ref()
                        .map(|dir| dir.join("chat2api-watch.log")),
                    true,
                    Some(1),
                    false,
                    false,
                ),
            ],
        },
        LogSource {
            source: "console",
            display: "用量统计服务 8001（本控制台宿主）",
            note: Some(CONSOLE_NOTE),
            categories: Vec::new(),
        },
        LogSource {
            source: "share",
            display: "分享链 8100（合并进程，非 8000 主链）",
            note: None,
            categories: vec![
                category(
                    "service",
                    "运行日志 share-backend.log",
                    share.map(|root| root.join("share-backend.log")),
                    true,
                    Some(MAX_ROTATION_INDEX),
                    true,
                    false,
                ),
                category(
                    "ledger",
                    "调用账本 usage-share-backend.jsonl",
                    share.map(|root| root.join("usage-share-backend.jsonl")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "gateway",
                    "8100 任务输出 raw.txt",
                    Some(share_run.join("run").join("live").join("raw.txt")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "faults",
                    "8100 故障记录 share-faults.jsonl",
                    Some(share_run.join("run").join("live").join("share-faults.jsonl")),
                    false,
                    None,
                    false,
                    false,
                ),
                category(
                    "broker",
                    "broker 启停记录 lifecycle.json",
                    Some(share_run.join("broker").join("lifecycle.json")),
                    false,
                    None,
                    false,
                    true,
                ),
            ],
        },
    ]
}

fn file_name(path: &Path) -> Option<String> {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
}

fn category_json(category: &LogCategory) -> Value {
    let mut object = json!({
        "category": category.category,
        "display": category.display,
        "rotating": category.rotating,
        "name": category.path.as_deref().and_then(file_name),
    });
    let map = object.as_object_mut().expect("category object");
    if let Some(index) = category.rotation_index {
        map.insert("rotation_index".to_string(), json!(index));
    }
    if category.level_filter {
        map.insert("level_filter".to_string(), json!(true));
    }
    if category.whole_document {
        map.insert("whole_document".to_string(), json!(true));
    }
    object
}

pub fn registry_payload(paths: &LogPaths) -> Value {
    let sources: Vec<Value> = build_registry(paths)
        .iter()
        .map(|source| {
            let mut object = json!({
                "source": source.source,
                "display": source.display,
                "categories": source.categories.iter().map(category_json).collect::<Vec<_>>(),
            });
            if let Some(note) = source.note {
                object["note"] = json!(note);
            }
            object
        })
        .collect();
    json!({"ok": true, "sources": sources})
}

#[derive(Debug, Clone)]
struct Cursor {
    source: String,
    category: String,
    offset: i64,
    key: Option<String>,
}

#[derive(Debug, Clone)]
struct TsLineMatch {
    tag: Option<String>,
    ts: String,
    level: Option<&'static str>,
    message: String,
}

#[derive(Debug, Clone)]
struct ParsedTimestamp {
    text: String,
    naive: chrono::NaiveDateTime,
    offset: Option<FixedOffset>,
    end: usize,
}

#[derive(Debug, Clone)]
struct ParsedLogRow {
    ts: Option<String>,
    level: Option<&'static str>,
    message: String,
    parse_ok: bool,
    cut: bool,
    event: Value,
    start: usize,
}

fn base64_encode(data: &[u8]) -> String {
    crate::local_um::base64_encode(data)
}

fn base64_decode(text: &str) -> Option<Vec<u8>> {
    crate::local_um::base64_decode(text)
}

fn decode_cursor(cursor: &str) -> Option<Cursor> {
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
        Some(Value::String(key)) => Some(key.clone()),
        _ => return None,
    };
    Some(Cursor {
        source,
        category,
        offset,
        key,
    })
}

fn encode_cursor(source: &str, category: &str, offset: i64, key: &str) -> String {
    let payload = json!({
        "src": source,
        "cat": category,
        "off": offset,
        "key": key,
    });
    base64_encode(payload.to_string().as_bytes())
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

fn file_identity(metadata: &std::fs::Metadata) -> Option<String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        return Some(format!(
            "{}:{}:{}",
            metadata.ino(),
            metadata.dev(),
            metadata.size()
        ));
    }
    #[cfg(windows)]
    {
        return Some(format!("0:0:{}", metadata.len()));
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = metadata;
        None
    }
}

fn normalize_level(raw: &str) -> String {
    let level = raw.trim().to_ascii_uppercase();
    match level.as_str() {
        "WARN" => "WARNING".to_string(),
        "ERR" => "ERROR".to_string(),
        _ => level,
    }
}

fn event_null() -> Value {
    json!({
        "timestamp": Value::Null,
        "model": Value::Null,
        "request_id": Value::Null,
        "phase": Value::Null,
        "outcome": Value::Null,
        "error_code": Value::Null,
        "duration_ms": Value::Null,
        "token_rate": Value::Null,
        "token_rate_kind": Value::Null,
    })
}

fn event_view(body: &Map<String, Value>, ts: Option<&str>) -> Value {
    let status = body.get("status");
    let mut outcome = Value::Null;
    if let Some(text) = status.and_then(Value::as_str).filter(|text| !text.is_empty()) {
        let lower = text.to_ascii_lowercase();
        if lower == "ok" || lower.starts_with("http2") {
            outcome = json!("ok");
        } else if lower.starts_with("http4")
            || lower.starts_with("http5")
            || matches!(lower.as_str(), "error" | "client_disconnected" | "failed")
        {
            outcome = json!("error");
        }
    }
    let duration = body
        .get("duration_ms")
        .and_then(Value::as_i64)
        .filter(|value| *value > 0);
    let output_tokens = body.get("output_tokens").and_then(Value::as_i64);
    let token_rate = match (output_tokens, duration) {
        (Some(output), Some(duration)) => {
            Some((output as f64 / (duration as f64 / 1000.0) * 100.0).round() / 100.0)
        }
        _ => None,
    };
    let model = body.get("model").filter(|value| !value.is_null()).map(|value| {
        let mut text = redact(&python_str(value));
        text.truncate(64);
        text
    });
    let request_id = body
        .get("request_id")
        .filter(|value| !value.is_null())
        .map(|value| {
            let mut text = redact(&python_str(value));
            text.truncate(64);
            text
        });
    json!({
        "timestamp": ts,
        "model": model,
        "request_id": request_id,
        "phase": body.get("phase").and_then(Value::as_str),
        "outcome": outcome,
        "error_code": if outcome == json!("error") {
            status.map(|value| {
                let mut text = python_str(value);
                text.truncate(32);
                text
            })
        } else {
            None
        },
        "duration_ms": duration,
        "token_rate": token_rate,
        "token_rate_kind": token_rate.map(|_| "mean_overall"),
    })
}

fn word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn find_ci(bytes: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    if needle.is_empty() || from >= bytes.len() {
        return None;
    }
    bytes[from..]
        .windows(needle.len())
        .position(|window| window.eq_ignore_ascii_case(needle))
        .map(|position| from + position)
}

fn replace_value(output: &mut String, start: usize, end: usize, replacement: &str) {
    if start <= end && output.is_char_boundary(start) && output.is_char_boundary(end) {
        output.replace_range(start..end, replacement);
    }
}

fn redact_header_values(output: &mut String, names: &[&str], cookie_rest_of_line: bool) {
    let mut search = 0usize;
    loop {
        let lower = output.as_bytes().to_ascii_lowercase();
        let mut found = None;
        for name in names {
            if let Some(index) = find_ci(&lower, name.as_bytes(), search) {
                if index > 0 && word_byte(lower[index - 1]) {
                    continue;
                }
                let after = index + name.len();
                let mut cursor = after;
                while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
                    cursor += 1;
                }
                if lower.get(cursor) != Some(&b':') {
                    continue;
                }
                cursor += 1;
                while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
                    cursor += 1;
                }
                found = Some(cursor);
                break;
            }
        }
        let Some(value_start) = found else {
            break;
        };
        let value_end = if cookie_rest_of_line {
            output[value_start..]
                .find('\n')
                .map_or(output.len(), |index| value_start + index)
        } else {
            output[value_start..]
                .find(|ch: char| ch.is_whitespace())
                .map_or(output.len(), |index| value_start + index)
        };
        replace_value(output, value_start, value_end, "<redacted>");
        search = value_start + "<redacted>".len();
    }
}

fn redact_authorization(output: &mut String) {
    let mut search = 0usize;
    loop {
        let lower = output.as_bytes().to_ascii_lowercase();
        let Some(index) = find_ci(&lower, b"authorization", search) else {
            break;
        };
        if index > 0 && word_byte(lower[index - 1]) {
            search = index + "authorization".len();
            continue;
        }
        let mut cursor = index + "authorization".len();
        while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        if lower.get(cursor) != Some(&b':') {
            search = index + "authorization".len();
            continue;
        }
        cursor += 1;
        while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        let mut scheme_end = cursor;
        while scheme_end < lower.len() && !lower[scheme_end].is_ascii_whitespace() {
            scheme_end += 1;
        }
        let scheme = &lower[cursor..scheme_end];
        if scheme == b"bearer" || scheme == b"basic" {
            let mut value_start = scheme_end;
            while value_start < lower.len() && lower[value_start].is_ascii_whitespace() {
                value_start += 1;
            }
            let value_end = output[value_start..]
                .find(|ch: char| ch.is_whitespace())
                .map_or(output.len(), |offset| value_start + offset);
            replace_value(output, value_start, value_end, "<redacted>");
            search = value_start + "<redacted>".len();
        } else {
            search = scheme_end;
        }
    }
}

fn redact_key_values(output: &mut String) {
    const KEYS: [&str; 10] = [
        "api_key",
        "api-key",
        "apikey",
        "password",
        "passwd",
        "pwd",
        "token",
        "secret",
        "credential",
        "authorization",
    ];
    let mut search = 0usize;
    loop {
        let lower = output.as_bytes().to_ascii_lowercase();
        let mut found = None;
        for key in KEYS {
            if let Some(index) = find_ci(&lower, key.as_bytes(), search) {
                if index > 0 && word_byte(lower[index - 1]) {
                    continue;
                }
                found = Some(index + key.len());
                break;
            }
        }
        let Some(mut cursor) = found else {
            break;
        };
        while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        if !matches!(lower.get(cursor), Some(b'=') | Some(b':')) {
            search = cursor;
            continue;
        }
        cursor += 1;
        while cursor < lower.len() && lower[cursor].is_ascii_whitespace() {
            cursor += 1;
        }
        let value_end = output[cursor..]
            .find(|ch: char| ch.is_whitespace())
            .map_or(output.len(), |offset| cursor + offset);
        replace_value(output, cursor, value_end, "<redacted>");
        search = cursor + "<redacted>".len();
    }
}

fn redact_quoted_json(output: &mut String) {
    const KEYS: [&str; 14] = [
        "api_key",
        "api-key",
        "apikey",
        "password",
        "passwd",
        "pwd",
        "token",
        "secret",
        "credential",
        "cookie",
        "authorization",
        "access_token",
        "refresh_token",
        "session_token",
    ];
    let mut search = 0usize;
    loop {
        let lower = output.as_bytes().to_ascii_lowercase();
        let mut found = None;
        for key in KEYS {
            let quoted = format!("\"{key}\"");
            if let Some(index) = find_ci(&lower, quoted.as_bytes(), search) {
                found = Some((index, quoted.len()));
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
        replace_value(output, cursor, value_end, "<redacted>");
        search = cursor + "<redacted>".len();
    }
}

fn redact_sk_keys(output: &mut String) {
    let mut search = 0usize;
    loop {
        let lower = output.as_bytes().to_ascii_lowercase();
        let Some(index) = find_ci(&lower, b"sk-", search) else {
            break;
        };
        if index > 0 && word_byte(lower[index - 1]) {
            search = index + 3;
            continue;
        }
        let mut end = index + 3;
        while end < lower.len() && (lower[end].is_ascii_alphanumeric() || lower[end] == b'_' || lower[end] == b'-') {
            end += 1;
        }
        if end - index < 11 || (end < lower.len() && word_byte(lower[end])) {
            search = index + 3;
            continue;
        }
        replace_value(output, index, end, "<redacted-key>");
        search = index + "<redacted-key>".len();
    }
}

fn redact_query_params(output: &mut String) {
    const KEYS: [&str; 12] = [
        "password",
        "passwd",
        "pwd",
        "token",
        "secret",
        "api_key",
        "api-key",
        "apikey",
        "access_token",
        "key",
        "code",
        "credential",
    ];
    let mut search = 0usize;
    loop {
        let lower = output.as_bytes().to_ascii_lowercase();
        let mut found = None;
        for index in search..lower.len() {
            if !matches!(lower[index], b'?' | b'&') {
                continue;
            }
            let key_start = index + 1;
            let Some(equal) = lower[key_start..].iter().position(|byte| *byte == b'=') else {
                continue;
            };
            let key = &lower[key_start..key_start + equal];
            if !KEYS.iter().any(|candidate| candidate.as_bytes() == key) {
                continue;
            }
            found = Some(key_start + equal + 1);
            break;
        }
        let Some(value_start) = found else {
            break;
        };
        let value_end = output[value_start..]
            .find(|ch: char| ch == '&' || ch.is_whitespace() || ch == '"' || ch == '\'')
            .map_or(output.len(), |offset| value_start + offset);
        replace_value(output, value_start, value_end, "<redacted>");
        search = value_start + "<redacted>".len();
    }
}

pub fn redact(text: &str) -> String {
    let mut output = text.to_string();
    redact_header_values(&mut output, &["cookie"], true);
    redact_header_values(
        &mut output,
        &[
            "x-admin-control",
            "x-csrf-token",
            "x-api-key",
            "x-auth-token",
            "x-session-token",
        ],
        false,
    );
    redact_authorization(&mut output);
    redact_key_values(&mut output);
    redact_quoted_json(&mut output);
    redact_sk_keys(&mut output);
    redact_query_params(&mut output);
    output
}

fn parse_digits(bytes: &[u8], start: usize, min: usize, max: usize) -> Option<usize> {
    let mut end = start;
    while end < bytes.len() && bytes[end].is_ascii_digit() {
        end += 1;
    }
    let count = end - start;
    if count < min || count > max {
        None
    } else {
        Some(end)
    }
}

fn skip_whitespace(bytes: &[u8], mut index: usize) -> usize {
    while index < bytes.len() && bytes[index].is_ascii_whitespace() {
        index += 1;
    }
    index
}

fn parse_timestamp_at(text: &str, start: usize) -> Option<ParsedTimestamp> {
    let bytes = text.as_bytes();
    let mut index = start;
    let year_start = index;
    let year = parse_digits(bytes, index, 4, 4)?;
    index = year;
    if !matches!(bytes.get(index), Some(b'-') | Some(b'/')) {
        return None;
    }
    let separator = bytes[index];
    index += 1;
    let month_start = index;
    let month = parse_digits(bytes, index, 2, 2)?;
    index = month;
    if bytes.get(index) != Some(&separator) {
        return None;
    }
    index += 1;
    let day_start = index;
    let day = parse_digits(bytes, index, 2, 2)?;
    index = day;
    let after_day = index;
    let mut weekday_end = index;
    let weekday_ws = skip_whitespace(bytes, weekday_end);
    if text[weekday_ws..].starts_with('周') {
        let mut chars = text[weekday_ws + '周'.len_utf8()..].chars();
        if let Some(weekday) = chars.next() {
            if "一二三四五六日".contains(weekday) {
                weekday_end = weekday_ws + '周'.len_utf8() + weekday.len_utf8();
            }
        }
    }
    index = if weekday_end > after_day {
        weekday_end
    } else {
        after_day
    };
    if !matches!(bytes.get(index), Some(b' ') | Some(b'T')) {
        return None;
    }
    index += 1;
    let hour_start = index;
    let hour = parse_digits(bytes, index, 1, 2)?;
    index = hour;
    if bytes.get(index) != Some(&b':') {
        return None;
    }
    index += 1;
    let minute_start = index;
    let minute = parse_digits(bytes, index, 2, 2)?;
    index = minute;
    if bytes.get(index) != Some(&b':') {
        return None;
    }
    index += 1;
    let second_start = index;
    let second = parse_digits(bytes, index, 2, 2)?;
    index = second;
    let mut fraction = String::new();
    if matches!(bytes.get(index), Some(b'.') | Some(b',')) {
        index += 1;
        let fraction_start = index;
        while index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        if index == fraction_start {
            return None;
        }
        fraction = text[fraction_start..index].to_string();
    }
    let zone_ws_start = index;
    let zone_start = skip_whitespace(bytes, index);
    let mut offset = None;
    if bytes.get(zone_start) == Some(&b'Z') {
        offset = Some(FixedOffset::east_opt(0)?);
        index = zone_start + 1;
    } else if matches!(bytes.get(zone_start), Some(b'+') | Some(b'-')) {
        let sign = if bytes[zone_start] == b'+' { 1 } else { -1 };
        let zone_hour_start = zone_start + 1;
        let zone_hour = parse_digits(bytes, zone_hour_start, 2, 2)?;
        let mut zone_index = zone_hour;
        if bytes.get(zone_index) == Some(&b':') {
            zone_index += 1;
        }
        let zone_minute_start = zone_index;
        let zone_minute = parse_digits(bytes, zone_index, 2, 2)?;
        let hours = text[zone_hour_start..zone_hour].parse::<i32>().ok()?;
        let minutes = text[zone_minute_start..zone_minute].parse::<i32>().ok()?;
        offset = FixedOffset::east_opt(sign * (hours * 3600 + minutes * 60));
        index = zone_minute;
    } else {
        index = zone_ws_start;
    }
    let year = text[year_start..year].parse::<i32>().ok()?;
    let month = text[month_start..month].parse::<u32>().ok()?;
    let day = text[day_start..day].parse::<u32>().ok()?;
    let hour = text[hour_start..hour].parse::<u32>().ok()?;
    let minute = text[minute_start..minute].parse::<u32>().ok()?;
    let second = text[second_start..second].parse::<u32>().ok()?;
    let microsecond = if fraction.is_empty() {
        0
    } else {
        format!("{fraction:0<6}")[..6].parse::<u32>().ok()?
    };
    let date = NaiveDate::from_ymd_opt(year, month, day)?;
    let time = NaiveTime::from_hms_micro_opt(hour, minute, second, microsecond)?;
    Some(ParsedTimestamp {
        text: text[start..index].to_string(),
        naive: chrono::NaiveDateTime::new(date, time),
        offset,
        end: index,
    })
}

pub fn normalize_ts(text: &str) -> Option<f64> {
    let text = text.trim();
    if text.len() == 10 && text.bytes().all(|byte| byte.is_ascii_digit()) {
        return text.parse::<f64>().ok();
    }
    let parsed = parse_timestamp_at(text, 0)?;
    if parsed.end != text.len() {
        return None;
    }
    let micros = match parsed.offset {
        Some(offset) => offset
            .from_local_datetime(&parsed.naive)
            .single()?
            .timestamp_micros(),
        None => Local
            .from_local_datetime(&parsed.naive)
            .earliest()?
            .timestamp_micros(),
    };
    Some(micros as f64 / 1_000_000.0)
}

fn match_level(text: &str) -> Option<(&'static str, usize)> {
    const LEVELS: [(&str, &str); 11] = [
        ("TRACE", "TRACE"),
        ("DEBUG", "DEBUG"),
        ("INFO", "INFO"),
        ("NOTICE", "NOTICE"),
        ("WARNING", "WARNING"),
        ("WARN", "WARNING"),
        ("ERROR", "ERROR"),
        ("ERR", "ERROR"),
        ("CRITICAL", "CRITICAL"),
        ("CRIT", "CRITICAL"),
        ("FATAL", "FATAL"),
    ];
    for (raw, canonical) in LEVELS {
        if text.len() < raw.len() || !text[..raw.len()].eq_ignore_ascii_case(raw) {
            continue;
        }
        let after = text.as_bytes().get(raw.len());
        let boundary = after.map_or(true, |byte| !word_byte(*byte));
        if boundary {
            return Some((canonical, raw.len()));
        }
    }
    None
}

fn match_ts_line(text: &str) -> Option<TsLineMatch> {
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
                if text[after..].chars().next().is_some_and(char::is_whitespace) {
                    tag = Some(format!("[{content}]"));
                    index = skip_whitespace(bytes, after);
                }
            }
        }
    }
    if bytes.get(index) == Some(&b'[') {
        index += 1;
    }
    let parsed = parse_timestamp_at(text, index)?;
    index = parsed.end;
    if bytes.get(index) == Some(&b']') {
        index += 1;
    }
    let after_ts = index;
    index = skip_whitespace(bytes, index);
    if index == after_ts {
        return None;
    }
    let mut level = None;
    if let Some((canonical, consumed)) = match_level(&text[index..]) {
        level = Some(canonical);
        index += consumed;
        if matches!(bytes.get(index), Some(b',') | Some(b':')) {
            index += 1;
        }
        index = skip_whitespace(bytes, index);
    }
    Some(TsLineMatch {
        tag,
        ts: parsed.text,
        level,
        message: text[index..].to_string(),
    })
}

fn python_str(value: &Value) -> String {
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

fn ledger_json_line(text: &str) -> Option<Value> {
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
        bits.push(format!("{label}={}", python_str(value)));
    }
    let message = if bits.is_empty() {
        stripped.to_string()
    } else {
        bits.join(" ")
    };
    Some(json!({
        "ts": ts,
        "message": message,
        "event": event_view(body, Some(&ts)),
    }))
}

fn find_assignment<'a>(text: &'a str, key: &str) -> Option<&'a str> {
    let bytes = text.as_bytes();
    let key_bytes = key.as_bytes();
    let mut search = 0usize;
    while let Some(index) = find_ci(bytes, key_bytes, search) {
        let after = index + key_bytes.len();
        if index == 0 || !word_byte(bytes[index - 1]) {
            if bytes.get(after) == Some(&b'=') {
                let value_start = after + 1;
                let value_end = text[value_start..]
                    .find(|ch: char| ch.is_whitespace())
                    .map_or(text.len(), |offset| value_start + offset);
                if value_end > value_start {
                    return Some(&text[value_start..value_end]);
                }
            }
        }
        search = after;
    }
    None
}

fn find_digits_assignment<'a>(text: &'a str, key: &str, exact: usize) -> Option<&'a str> {
    let bytes = text.as_bytes();
    let key_bytes = key.as_bytes();
    let mut search = 0usize;
    while let Some(index) = find_ci(bytes, key_bytes, search) {
        let after = index + key_bytes.len();
        if index == 0 || !word_byte(bytes[index - 1]) {
            if bytes.get(after) == Some(&b'=') {
                let value_start = after + 1;
                let mut value_end = value_start;
                while value_end < bytes.len() && bytes[value_end].is_ascii_digit() {
                    value_end += 1;
                }
                if value_end - value_start >= exact {
                    return Some(&text[value_start..value_start + exact]);
                }
            }
        }
        search = after;
    }
    None
}

fn ts_line_event(ts: &str, message: &str) -> Value {
    let request_id = find_assignment(message, "request").map(|value| {
        let mut value = redact(value);
        value.truncate(64);
        value
    });
    let model = find_assignment(message, "model").map(|value| {
        let mut value = redact(value);
        value.truncate(64);
        value
    });
    let duration = find_digits_assignment(message, "duration_ms", 1)
        .and_then(|value| value.parse::<i64>().ok())
        .filter(|value| *value > 0);
    let final_outcome = find_assignment(message, "final");
    let status = find_digits_assignment(message, "status", 3);
    let mut outcome = None;
    let mut error_code = None;
    if let Some(value) = final_outcome {
        let upper = value.to_ascii_uppercase();
        if upper == "OK" || upper == "HTTP200" {
            outcome = Some("ok");
        } else if upper.starts_with("HTTP4") || upper.starts_with("HTTP5") {
            outcome = Some("error");
            error_code = Some(upper.chars().take(32).collect::<String>());
        }
    }
    if outcome.is_none() {
        if let Some(status) = status {
            if status.starts_with('4') || status.starts_with('5') {
                outcome = Some("error");
                error_code = Some(format!("HTTP{status}"));
            }
        }
    }
    json!({
        "timestamp": ts,
        "model": model,
        "request_id": request_id,
        "phase": Value::Null,
        "outcome": outcome,
        "error_code": error_code,
        "duration_ms": duration,
        "token_rate": Value::Null,
        "token_rate_kind": Value::Null,
    })
}

fn outcome(status: &str, error: Option<&str>) -> Value {
    let mut output = json!({
        "status": status,
        "rows": [],
        "scanned_bytes": 0,
        "window_start_offset": 0,
        "truncated": false,
        "next_cursor": null,
        "reset_hint": false,
        "file_key": null,
    });
    if let Some(error) = error {
        output["error"] = json!(error);
    }
    output
}

fn valid_request_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b':' | b'-'))
}

fn find_category<'a>(
    registry: &'a [LogSource],
    source: &str,
    category: &str,
) -> Option<&'a LogCategory> {
    registry
        .iter()
        .find(|candidate| candidate.source == source)
        .and_then(|candidate| {
            candidate
                .categories
                .iter()
                .find(|candidate| candidate.category == category)
        })
}

fn read_entries(
    registry: &[LogSource],
    source: &str,
    category: &str,
    cursor: Option<&str>,
    q: Option<&str>,
    level: Option<&str>,
    since: Option<&str>,
    until: Option<&str>,
    rotation: Option<i64>,
    request_id: Option<&str>,
) -> Value {
    if let Some(request_id) = request_id {
        if !valid_request_id(request_id) {
            return outcome("error", Some("invalid_request_id"));
        }
    }
    let Some(category_def) = find_category(registry, source, category) else {
        return outcome("error", Some("unknown_log_source"));
    };
    let Some(raw_path) = &category_def.path else {
        return outcome("unconfigured", Some("not_configured"));
    };
    let mut path = raw_path.clone();
    let rotation_cap = category_def.rotation_index.unwrap_or(0);
    if let Some(rotation) = rotation {
        if !category_def.rotating || rotation < 1 || rotation > i64::from(rotation_cap) {
            return outcome("error", Some("invalid_rotation"));
        }
        let rotated = PathBuf::from(format!("{}.{}", path.display(), rotation));
        if rotated.metadata().map(|meta| meta.len() > 0).unwrap_or(false) {
            path = rotated;
        }
    }
    let mut handle = match std::fs::File::open(&path) {
        Ok(handle) => handle,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return outcome("missing", Some("file_missing"));
        }
        Err(error) if error.kind() == std::io::ErrorKind::PermissionDenied => {
            return outcome("denied", Some("permission_denied"));
        }
        Err(error) => {
            return outcome(
                "error",
                Some(&format!("io_error:{}", io_error_name(&error))),
            );
        }
    };
    use std::io::{Read as _, Seek as _, SeekFrom};
    let metadata = match handle.metadata() {
        Ok(metadata) => metadata,
        Err(error) => {
            return outcome(
                "error",
                Some(&format!("io_error:{}", io_error_name(&error))),
            );
        }
    };
    let size = metadata.len();
    let file_key = file_identity(&metadata);
    if category_def.whole_document {
        if cursor.is_some() {
            let mut result = outcome("ok", None);
            result["reset_hint"] = json!(true);
            result["file_key"] = json!(file_key);
            return result;
        }
        if handle.seek(SeekFrom::Start(0)).is_err() {
            return outcome("error", Some("io_error:OSError"));
        }
        let mut data = Vec::new();
        if handle.read_to_end(&mut data).is_err() {
            return outcome("error", Some("io_error:OSError"));
        }
        data.truncate(MAX_TAIL_BYTES as usize);
        let text = String::from_utf8_lossy(&data).trim().to_string();
        let mut rows = Vec::new();
        if !text.is_empty() {
            let record = ledger_json_line(&text).unwrap_or_else(|| {
                json!({"ts": Value::Null, "message": text.clone(), "event": event_null()})
            });
            rows.push(json!({
                "ts": record["ts"],
                "level": Value::Null,
                "message": redact(record["message"].as_str().unwrap_or_default()),
                "parse_ok": false,
                "cut": false,
                "event": record["event"],
            }));
        }
        return json!({
            "status": "ok",
            "rows": rows,
            "scanned_bytes": data.len(),
            "window_start_offset": 0,
            "truncated": size > data.len() as u64,
            "next_cursor": null,
            "reset_hint": false,
            "file_key": file_key,
        });
    }
    let mut parsed_cursor = None;
    if let Some(cursor) = cursor {
        parsed_cursor = decode_cursor(cursor);
        let stale = parsed_cursor.as_ref().map_or(true, |cursor| {
            cursor.source != source
                || cursor.category != category
                || cursor.offset > size as i64
        });
        let stale = stale
            || parsed_cursor.as_ref().is_some_and(|cursor| {
                cursor.key.as_deref() != file_key.as_deref()
                    && {
                        let minted = cursor_meta(cursor.key.as_deref());
                        let current = cursor_meta(file_key.as_deref());
                        minted.is_none()
                            || current.is_none()
                            || minted.as_ref().zip(current.as_ref()).map_or(true, |(minted, current)| {
                                minted.0 != current.0
                                    || minted.1 != current.1
                                    || current.2 < minted.2
                            })
                    }
            });
        if stale {
            let mut result = outcome("ok", None);
            result["reset_hint"] = json!(true);
            result["file_key"] = json!(file_key);
            return result;
        }
    }
    let older_mode = parsed_cursor.is_some();
    let off = parsed_cursor
        .as_ref()
        .map_or(size as i64, |cursor| cursor.offset);
    let win_start = off.saturating_sub(MAX_TAIL_BYTES as i64).max(0);
    if handle.seek(SeekFrom::Start(win_start as u64)).is_err() {
        return outcome("error", Some("io_error:OSError"));
    }
    let mut data = Vec::new();
    if (&mut handle)
        .take((off - win_start) as u64)
        .read_to_end(&mut data)
        .is_err()
    {
        return outcome("error", Some("io_error:OSError"));
    }
    let scanned = data.len();
    let mut win_start = win_start;
    if win_start > 0 {
        let mut previous = [0u8; 1];
        if handle.seek(SeekFrom::Start(win_start as u64 - 1)).is_err()
            || handle.read_exact(&mut previous).is_err()
        {
            return outcome("error", Some("io_error:OSError"));
        }
        if previous[0] != b'\n' {
            let Some(newline) = data.iter().position(|byte| *byte == b'\n') else {
                return json!({
                    "status": "ok",
                    "rows": [],
                    "scanned_bytes": scanned,
                    "window_start_offset": win_start,
                    "truncated": true,
                    "next_cursor": null,
                    "reset_hint": false,
                    "file_key": file_key,
                });
            };
            win_start += newline as i64 + 1;
            data.drain(..=newline);
        }
    }
    let mut parts: Vec<&[u8]> = data.split(|byte| *byte == b'\n').collect();
    let trailing = parts.pop().unwrap_or(&[]);
    let trailing_live = !trailing.is_empty() && (!older_mode || off >= size as i64);
    let deadline = Instant::now() + Duration::from_secs_f64(SCAN_SECONDS);
    let mut parsed: Vec<ParsedLogRow> = Vec::new();
    let mut position = win_start;
    let mut timed_out = false;
    for part in parts {
        if Instant::now() > deadline {
            timed_out = true;
            break;
        }
        let cut = part.len() > MAX_LINE_BYTES;
        let text = String::from_utf8_lossy(&part[..part.len().min(MAX_LINE_BYTES)]).to_string();
        if let Some(matched) = match_ts_line(&text) {
            let mut body = matched.message;
            if let Some(tag) = &matched.tag {
                body = format!("{tag} {body}");
            }
            let safe = redact(&body);
            parsed.push(ParsedLogRow {
                ts: Some(matched.ts.clone()),
                level: matched.level,
                message: safe.clone(),
                parse_ok: matched.level.is_some(),
                cut,
                event: ts_line_event(&matched.ts, &safe),
                start: position as usize,
            });
        } else if let Some(record) = ledger_json_line(&text) {
            parsed.push(ParsedLogRow {
                ts: record["ts"].as_str().map(str::to_string),
                level: None,
                message: redact(record["message"].as_str().unwrap_or_default()),
                parse_ok: false,
                cut,
                event: record["event"].clone(),
                start: position as usize,
            });
        } else if let Some(last) = parsed.last_mut() {
            last.message.push('\n');
            last.message.push_str(&redact(&text));
            if cut {
                last.cut = true;
            }
        } else {
            parsed.push(ParsedLogRow {
                ts: None,
                level: None,
                message: redact(&text),
                parse_ok: false,
                cut,
                event: event_null(),
                start: position as usize,
            });
        }
        position += part.len() as i64 + 1;
    }
    let q_needle = q.unwrap_or_default().chars().take(MAX_QUERY_CHARS).collect::<String>().to_lowercase();
    let level_needle = normalize_level(
        &level
            .unwrap_or_default()
            .chars()
            .take(MAX_QUERY_CHARS)
            .collect::<String>(),
    );
    let since_needle = since
        .unwrap_or_default()
        .chars()
        .take(MAX_QUERY_CHARS)
        .collect::<String>();
    let until_needle = until
        .unwrap_or_default()
        .chars()
        .take(MAX_QUERY_CHARS)
        .collect::<String>();
    let mut kept: Vec<ParsedLogRow> = Vec::new();
    for row in parsed {
        if let Some(request_id) = request_id {
            if row.event["request_id"].as_str() != Some(request_id) {
                continue;
            }
        }
        if !q_needle.is_empty() && !row.message.to_lowercase().contains(&q_needle) {
            continue;
        }
        if !level_needle.is_empty()
            && (!row.parse_ok || row.level.map(str::to_string).as_deref() != Some(level_needle.as_str()))
        {
            continue;
        }
        if !since_needle.is_empty()
            && row.ts.as_deref().map_or(true, |ts| ts < since_needle.as_str())
        {
            continue;
        }
        if !until_needle.is_empty()
            && row.ts.as_deref().map_or(true, |ts| {
                !ts.starts_with(until_needle.as_str()) && ts > until_needle.as_str()
            })
        {
            continue;
        }
        kept.push(row);
    }
    let mut truncated = timed_out || trailing_live;
    let mut trimmed = false;
    if kept.len() > MAX_ROWS {
        let dropped = kept.len() - MAX_ROWS;
        kept.drain(..dropped);
        truncated = true;
        trimmed = true;
    }
    let anchor = if trimmed && !kept.is_empty() {
        kept[0].start as i64
    } else {
        win_start
    };
    let next_cursor = if anchor > 0 {
        file_key
            .as_deref()
            .map(|key| encode_cursor(source, category, anchor, key))
    } else {
        None
    };
    let mut result = json!({
        "status": "ok",
        "rows": kept.iter().map(|row| {
            json!({
                "ts": row.ts,
                "level": row.level,
                "message": row.message,
                "parse_ok": row.parse_ok,
                "cut": row.cut,
                "event": row.event,
            })
        }).collect::<Vec<_>>(),
        "scanned_bytes": scanned,
        "window_start_offset": win_start,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "reset_hint": false,
        "file_key": file_key,
    });
    if let Some(request_id) = request_id {
        result["request_id"] = json!(request_id);
        result["request_scope"] = json!("read_window");
    }
    result
}

pub fn log_entries_payload(
    registry: &[LogSource],
    source: &str,
    category: &str,
    cursor: Option<&str>,
    q: Option<&str>,
    level: Option<&str>,
    since: Option<&str>,
    until: Option<&str>,
    rotation: Option<i64>,
    request_id: Option<&str>,
) -> Value {
    let mut result = read_entries(
        registry, source, category, cursor, q, level, since, until, rotation, request_id,
    );
    if let Some(object) = result.as_object_mut() {
        object.insert("ok".to_string(), json!(true));
    }
    result
}

fn io_error_name(error: &std::io::Error) -> &'static str {
    match error.kind() {
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        _ => "OSError",
    }
}
