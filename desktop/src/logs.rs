//! Bounded run-log view over the app instance's own service log.
//!
//! The gateway writes JSONL events to `%LOCALAPPDATA%\Chat2API\instance\gateway.log`.
//! The live incremental reader and its backfill live in [`crate::logstream`]; this
//! module parses one line and keeps the page status. A missing or unreadable file
//! keeps its own failure reason and is never shown as a legal empty list.

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
const MAX_LOG_LINES: usize = 400;

pub fn parse_log_line(line: &str) -> LogEntry {
    let line = line.trim();
    let Ok(body) = serde_json::from_str::<Value>(line) else {
        return LogEntry {
            at: None,
            level: "raw".to_string(),
            text: sanitize_text(line),
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
        Some(level) => sanitize_text(level),
        None if event == Some("request_summary") => "error".to_string(),
        None => "info".to_string(),
    };
    let text = if event == Some("request_summary") {
        sanitize_text(&format!(
            "request_summary {} {} final={} tries={}",
            body.get("request_id")
                .and_then(Value::as_str)
                .unwrap_or("?"),
            body.get("protocol").and_then(Value::as_str).unwrap_or("?"),
            body.get("final").and_then(Value::as_str).unwrap_or("?"),
            body.get("tries").and_then(Value::as_u64).unwrap_or(0),
        ))
    } else {
        sanitize_text(line)
    };
    LogEntry { at, level, text }
}

/// Escapes control characters and redacts credential-looking values before the
/// text reaches a Slint label or a log line. The raw service log is never
/// modified; this only sanitizes the displayed text.
pub fn sanitize_text(text: &str) -> String {
    escape_control_chars(&redact_secrets(text))
}

fn escape_control_chars(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '\t' => out.push(' '),
            ch if ch.is_control() => {
                out.push_str(&format!("\\u{{{:04x}}}", ch as u32));
            }
            ch => out.push(ch),
        }
    }
    out
}

const SECRET_KEYS: &[&str] = &[
    "authorization",
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "api-key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
];

fn redact_secrets(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0usize;
    while cursor < bytes.len() {
        if let Some((value_start, value_end)) = secret_key_value(bytes, cursor) {
            out.push_str(&text[cursor..value_start]);
            out.push_str("[redacted]");
            cursor = value_end;
            continue;
        }
        if starts_with_ignore_case(bytes, cursor, b"bearer") && is_word_boundary(bytes, cursor, 6) {
            out.push_str(&text[cursor..cursor + 6]);
            let value_start = skip_spaces(bytes, cursor + 6);
            out.push_str(&text[cursor + 6..value_start]);
            let value_end = value_end(bytes, value_start);
            if value_end > value_start {
                out.push_str("[redacted]");
            }
            cursor = value_end;
            continue;
        }
        if starts_with_ignore_case(bytes, cursor, b"sk-") && is_prefix_boundary(bytes, cursor) {
            out.push_str(&text[cursor..cursor + 3]);
            let value_end = value_end(bytes, cursor + 3);
            if value_end > cursor + 3 {
                out.push_str("[redacted]");
            }
            cursor = value_end;
            continue;
        }
        let ch = text[cursor..].chars().next().expect("valid utf-8 boundary");
        out.push(ch);
        cursor += ch.len_utf8();
    }
    out
}

fn secret_key_value(bytes: &[u8], cursor: usize) -> Option<(usize, usize)> {
    for key in SECRET_KEYS {
        let key_bytes = key.as_bytes();
        if !starts_with_ignore_case(bytes, cursor, key_bytes)
            || !is_word_boundary(bytes, cursor, key_bytes.len())
        {
            continue;
        }
        let mut separator = skip_spaces(bytes, cursor + key_bytes.len());
        if separator < bytes.len() && (bytes[separator] == b'"' || bytes[separator] == b'\'') {
            separator = skip_spaces(bytes, separator + 1);
        }
        if separator >= bytes.len() || (bytes[separator] != b':' && bytes[separator] != b'=') {
            continue;
        }
        let value_start = skip_spaces(bytes, separator + 1);
        let value_end = if *key == "authorization" {
            authorization_value_end(bytes, value_start)
        } else if value_start < bytes.len()
            && (bytes[value_start] == b'"' || bytes[value_start] == b'\'')
        {
            quoted_value_end(bytes, value_start)
        } else {
            value_end(bytes, value_start)
        };
        if value_end > value_start {
            return Some((value_start, value_end));
        }
    }
    None
}

fn authorization_value_end(bytes: &[u8], start: usize) -> usize {
    let mut end = start;
    while end < bytes.len()
        && !matches!(bytes[end], b',' | b';' | b'&' | b'}' | b']' | b'\n' | b'\r')
    {
        end += 1;
    }
    end
}

fn quoted_value_end(bytes: &[u8], start: usize) -> usize {
    let quote = bytes[start];
    let mut end = start + 1;
    while end < bytes.len() && bytes[end] != quote {
        end += 1;
    }
    end
}

fn value_end(bytes: &[u8], start: usize) -> usize {
    let mut end = start;
    while end < bytes.len() && !is_value_delimiter(bytes[end]) {
        end += 1;
    }
    end
}

fn skip_spaces(bytes: &[u8], start: usize) -> usize {
    let mut cursor = start;
    while cursor < bytes.len() && (bytes[cursor] == b' ' || bytes[cursor] == b'\t') {
        cursor += 1;
    }
    cursor
}

fn is_value_delimiter(byte: u8) -> bool {
    byte.is_ascii_whitespace()
        || matches!(
            byte,
            b',' | b';' | b'&' | b'}' | b']' | b')' | b'"' | b'\'' | b'|'
        )
}

fn starts_with_ignore_case(bytes: &[u8], cursor: usize, needle: &[u8]) -> bool {
    bytes
        .get(cursor..cursor + needle.len())
        .is_some_and(|candidate| candidate.eq_ignore_ascii_case(needle))
}

fn is_word_boundary(bytes: &[u8], cursor: usize, length: usize) -> bool {
    let before = cursor == 0 || !is_word_byte(bytes[cursor - 1]);
    let after = cursor + length >= bytes.len() || !is_word_byte(bytes[cursor + length]);
    before && after
}

/// A prefix such as `sk-` ends in punctuation, so only the character before it
/// must be a boundary; the character after it belongs to the value.
fn is_prefix_boundary(bytes: &[u8], cursor: usize) -> bool {
    cursor == 0 || !is_word_byte(bytes[cursor - 1])
}

fn is_word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

pub fn read_log_file(path: &Path) -> LogsView {
    match crate::logstream::LogStream::open_with_limits(path, MAX_LOG_LINES, MAX_LOG_BYTES) {
        Ok(stream) => LogsView::Loaded(stream.entries()),
        Err(reason) => LogsView::Failed(reason),
    }
}

pub fn logs_status(view: &LogsView) -> (String, Tone) {
    match view {
        LogsView::Loaded(rows) if rows.is_empty() => {
            ("暂无日志记录（本实例）".to_string(), Tone::Warn)
        }
        LogsView::Loaded(rows) => (
            format!("最近 {} 条 · 本实例运行日志 · 实时增量", rows.len()),
            Tone::Good,
        ),
        LogsView::Failed(reason) => (format!("日志读取失败：{reason}"), Tone::Bad),
    }
}
