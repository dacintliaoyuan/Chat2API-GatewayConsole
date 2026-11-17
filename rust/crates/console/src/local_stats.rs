//! Local usage statistics read faces (`usagesrv.py` + `console/stats.py`).
//!
//! The local role reads the private chat2api ledger and the frozen DSH day
//! snapshot. Missing ledgers stay visible in `coverage` and never produce
//! fabricated rows; unknown values stay `null` instead of being folded to zero.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use chrono::{FixedOffset, TimeZone};
use serde_json::{json, Map, Value};

use crate::local_summary::parse_ts_ms;

pub const TIMEZONE_NAME: &str = "Asia/Shanghai";
pub const MAX_RANGE_DAYS: i64 = 92;
pub const MAX_TOKEN_RANGE_DAYS: i64 = 31;
pub const MAX_RATE_RANGE_HOURS: i64 = 168;
pub const MAX_ROWS_PER_ENDPOINT: usize = 5000;
pub const MAX_READ_BYTES: u64 = 32 * 1024 * 1024;
pub const LEDGER_RECORD_LIMIT: usize = 200_000;

const CHAT2API_TIME_SOURCE: &str = "账本 time 字段（ISO 8601，带偏移或 Z）";
const DSH_TIME_SOURCE: &str = "快照日（日级聚合，事件时间取该日北京 00:00）";
const MISSING_LEDGER_NOTE: &str = "账本文件缺席：本来源不出行，不冒充零用量";

#[derive(Debug, Clone)]
pub struct StatsPaths {
    pub usage_log: PathBuf,
    pub dsh_snapshot: PathBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum DurationState {
    Ok,
    Missing,
    Invalid,
}

#[derive(Debug, Clone)]
struct Event {
    ms: Option<f64>,
    model: String,
    input_tokens: Option<i64>,
    output_tokens: Option<i64>,
    duration_ms: Option<i64>,
    duration_state: DurationState,
    status: Option<Value>,
    cache_tokens: Option<i64>,
    reasoning_tokens: Option<i64>,
}

#[derive(Debug, Clone)]
struct EventRead {
    events: Vec<Event>,
    present: bool,
    read_error: Option<String>,
    corrupt: Option<String>,
    bad_lines: usize,
    dropped_events: usize,
    bytes_truncated: bool,
    missing: bool,
}

impl EventRead {
    fn missing() -> Self {
        Self {
            events: Vec::new(),
            present: false,
            read_error: None,
            corrupt: None,
            bad_lines: 0,
            dropped_events: 0,
            bytes_truncated: false,
            missing: true,
        }
    }
}

#[derive(Debug, Clone)]
struct SourceScan {
    source: String,
    events: Vec<Event>,
    coverage: Value,
    trusted: bool,
}

#[derive(Default)]
struct Bucket {
    requests: i64,
    complete_requests: i64,
    partial_requests: i64,
    complete_input: i64,
    complete_output: i64,
    partial_input: i64,
    partial_output: i64,
    partial_input_known: i64,
    partial_output_known: i64,
}

fn beijing() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).expect("+08:00")
}

fn display_path(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| "(未配置)".to_string())
}

fn beijing_day(ms: Option<f64>) -> Option<String> {
    let ms = ms?;
    let seconds = (ms / 1000.0).floor() as i64;
    let stamp = beijing().timestamp_opt(seconds, 0).single()?;
    Some(stamp.format("%Y-%m-%d").to_string())
}

fn window_day_keys(days: i64, now_ms: f64) -> Vec<String> {
    let seconds = (now_ms / 1000.0).floor() as i64;
    let Some(today) = beijing().timestamp_opt(seconds, 0).single() else {
        return Vec::new();
    };
    let today = today.date_naive();
    let mut keys = Vec::new();
    for offset in (0..days).rev() {
        keys.push(
            (today - chrono::Duration::days(offset))
                .format("%Y-%m-%d")
                .to_string(),
        );
    }
    keys
}

fn clamp_int(value: i64, _default: i64, low: i64, high: i64) -> (i64, bool) {
    if value < low {
        (low, value != low)
    } else if value > high {
        (high, true)
    } else {
        (value, false)
    }
}

fn int_or_none(value: Option<&Value>) -> Option<i64> {
    match value {
        None | Some(Value::Null) => None,
        Some(Value::Bool(_)) => None,
        Some(Value::Number(number)) => number.as_i64().or_else(|| {
            number
                .as_f64()
                .filter(|value| value.is_finite() && value.fract() == 0.0)
                .map(|value| value as i64)
        }),
        Some(Value::String(text)) => text.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn trusted_token_count(value: Option<&Value>) -> Option<i64> {
    int_or_none(value).filter(|value| *value >= 0)
}

fn first_value<'a>(record: &'a Map<String, Value>, fields: &[&str]) -> Option<&'a Value> {
    fields
        .iter()
        .find_map(|name| record.get(*name).filter(|value| !value.is_null()))
}

fn first_int(record: &Map<String, Value>, fields: &[&str]) -> Option<i64> {
    fields
        .iter()
        .find_map(|name| record.get(*name).and_then(|value| int_or_none(Some(value))))
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

fn falsy(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::Bool(value) => !*value,
        Value::Number(number) => number.as_f64() == Some(0.0),
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Object(map) => map.is_empty(),
    }
}

fn non_ok_status(status: Option<&Value>) -> bool {
    match status {
        None | Some(Value::Null) | Some(Value::Bool(_)) => false,
        Some(Value::Number(number)) => number
            .as_f64()
            .is_some_and(|value| !(200.0..300.0).contains(&value)),
        Some(Value::String(text)) => {
            let text = text.trim().to_ascii_lowercase();
            if text.is_empty() || text == "ok" || text == "success" {
                return false;
            }
            match text.parse::<i64>() {
                Ok(value) => !(200..300).contains(&value),
                Err(_) => true,
            }
        }
        Some(_) => false,
    }
}

fn duration_state(record: &Map<String, Value>, fields: &[&str]) -> (Option<i64>, DurationState) {
    if fields.is_empty() {
        return (None, DurationState::Missing);
    }
    let Some(raw) = first_value(record, fields) else {
        return (None, DurationState::Missing);
    };
    match int_or_none(Some(raw)) {
        Some(value) if value > 0 => (Some(value), DurationState::Ok),
        _ => (None, DurationState::Invalid),
    }
}

fn cache_report_status(record: &Map<String, Value>) -> Option<&'static str> {
    match record.get("cache_report_status").and_then(Value::as_str) {
        Some("reported") => Some("reported"),
        Some("unreported") => Some("unreported"),
        _ => None,
    }
}

fn event_from_record(record: &Map<String, Value>) -> Event {
    let ms = first_value(record, &["time"])
        .and_then(Value::as_str)
        .and_then(parse_ts_ms);
    let duration_ms = duration_state(record, &["duration_ms"]);
    let _usage_source = record
        .get("usage_source")
        .and_then(Value::as_str)
        .filter(|value| ["upstream", "partial", "estimated", "none"].contains(value));
    let _request_id = record
        .get("request_id")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty());
    let model = record
        .get("model")
        .filter(|value| !falsy(value))
        .map_or_else(|| "unknown".to_string(), python_str);
    Event {
        ms,
        model,
        input_tokens: first_int(record, &["input_tokens"]),
        output_tokens: first_int(record, &["output_tokens"]),
        duration_ms: duration_ms.0,
        duration_state: duration_ms.1,
        status: record.get("status").cloned(),
        cache_tokens: first_int(record, &["cached_input_tokens", "cache_tokens"]),
        reasoning_tokens: first_int(record, &["reasoning_tokens"]),
    }
}

fn trim_ascii(bytes: &[u8]) -> &[u8] {
    let mut start = 0;
    let mut end = bytes.len();
    while start < end && bytes[start].is_ascii_whitespace() {
        start += 1;
    }
    while end > start && bytes[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    &bytes[start..end]
}

fn read_jsonl_events(path: &Path) -> EventRead {
    let metadata = match std::fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return EventRead::missing();
        }
        Err(error) => {
            return EventRead {
                read_error: Some(error.to_string()),
                ..EventRead::missing()
            };
        }
    };
    let size = metadata.len();
    let clamped = size > MAX_READ_BYTES;
    let read_start = if clamped { size - MAX_READ_BYTES } else { 0 };
    let mut file = match std::fs::File::open(path) {
        Ok(file) => file,
        Err(error) => {
            return EventRead {
                present: true,
                read_error: Some(error.to_string()),
                ..EventRead::missing()
            };
        }
    };
    use std::io::{Read as _, Seek as _, SeekFrom};
    if file.seek(SeekFrom::Start(read_start)).is_err() {
        return EventRead {
            present: true,
            read_error: Some("seek failed".to_string()),
            ..EventRead::missing()
        };
    }
    let mut data = Vec::new();
    if file.read_to_end(&mut data).is_err() {
        return EventRead {
            present: true,
            read_error: Some("read failed".to_string()),
            ..EventRead::missing()
        };
    }
    if clamped {
        if let Some(index) = data.iter().position(|byte| *byte == b'\n') {
            data.drain(..=index);
        } else {
            data.clear();
        }
    }
    let consumed_end = data
        .iter()
        .rposition(|byte| *byte == b'\n')
        .map(|index| index + 1)
        .unwrap_or(0);
    let mut events = Vec::new();
    let mut bad_lines = 0;
    for line in data[..consumed_end].split(|byte| *byte == b'\n') {
        let line = trim_ascii(line);
        if line.is_empty() {
            continue;
        }
        let record = match serde_json::from_slice::<Value>(line) {
            Ok(Value::Object(record)) => record,
            _ => {
                bad_lines += 1;
                continue;
            }
        };
        events.push(event_from_record(&record));
    }
    let mut dropped_events = 0;
    if events.len() > LEDGER_RECORD_LIMIT {
        dropped_events = events.len() - LEDGER_RECORD_LIMIT;
        events.drain(..dropped_events);
    }
    EventRead {
        events,
        present: true,
        read_error: None,
        corrupt: None,
        bad_lines,
        dropped_events,
        bytes_truncated: clamped,
        missing: false,
    }
}

fn read_snapshot_events(path: &Path) -> EventRead {
    let metadata = match std::fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return EventRead::missing();
        }
        Err(error) => {
            return EventRead {
                read_error: Some(error.to_string()),
                ..EventRead::missing()
            };
        }
    };
    let _ = metadata;
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) => {
            return EventRead {
                present: true,
                read_error: Some(error.to_string()),
                ..EventRead::missing()
            };
        }
    };
    let snapshot = match serde_json::from_str::<Value>(&text) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            return EventRead {
                present: true,
                corrupt: Some(format!("JSONDecodeError: {error}")),
                ..EventRead::missing()
            };
        }
    };
    let Some(root) = snapshot.as_object() else {
        return EventRead {
            present: true,
            corrupt: Some("snapshot root is not a JSON object".to_string()),
            ..EventRead::missing()
        };
    };
    let mut events = Vec::new();
    let mut bad_lines = 0;
    let mut corrupt = None;
    let days = match root.get("days") {
        None => Map::new(),
        Some(Value::Object(days)) => days.clone(),
        Some(other) => {
            corrupt = Some(format!(
                "snapshot days is not a JSON object: {}",
                python_type_name(other)
            ));
            Map::new()
        }
    };
    let day_entry = days.get("2026-09-02").and_then(Value::as_object);
    if let Some(day_entry) = day_entry {
        let by_provider_model = match day_entry.get("byProviderModel") {
            None => Map::new(),
            Some(Value::Object(rows)) => rows.clone(),
            Some(other) => {
                corrupt = Some(format!(
                    "byProviderModel is not a JSON object: {}",
                    python_type_name(other)
                ));
                Map::new()
            }
        };
        let ms = parse_ts_ms("2026-09-02T00:00:00+08:00");
        for (model, row) in by_provider_model {
            let Some(row) = row.as_object() else {
                bad_lines += 1;
                continue;
            };
            events.push(Event {
                ms,
                model,
                input_tokens: int_or_none(row.get("input")),
                output_tokens: int_or_none(row.get("output")),
                duration_ms: None,
                duration_state: DurationState::Missing,
                status: None,
                cache_tokens: int_or_none(row.get("cacheRead")),
                reasoning_tokens: int_or_none(row.get("reasoning")),
            });
        }
    }
    EventRead {
        events,
        present: true,
        read_error: None,
        corrupt,
        bad_lines,
        dropped_events: 0,
        bytes_truncated: false,
        missing: false,
    }
}

fn python_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn source_coverage(
    path: &Path,
    events: &[Event],
    read: &EventRead,
    time_source: &str,
    unknown_fields: &[&str],
) -> Value {
    let mut coverage = json!({
        "ledger": display_path(path),
        "ledger_present": read.present,
        "enabled": true,
        "events": events.len(),
        "time_source": time_source,
        "unknown_fields": unknown_fields,
    });
    if !read.present {
        coverage["note"] = json!(MISSING_LEDGER_NOTE);
        return coverage;
    }
    let mut errors = Vec::new();
    if let Some(error) = &read.read_error {
        errors.push(json!({"type": "read_error", "detail": error}));
    }
    if let Some(corrupt) = &read.corrupt {
        errors.push(json!({"type": "corrupt_ledger", "detail": corrupt}));
    }
    if read.bad_lines > 0 {
        errors.push(json!({"type": "malformed_lines", "bad_lines": read.bad_lines}));
    }
    if !errors.is_empty() {
        coverage["errors"] = json!(errors);
    }
    if read.dropped_events > 0 {
        coverage["dropped_events"] = json!(read.dropped_events);
        coverage["truncated"] = json!(true);
    }
    if read.bytes_truncated {
        coverage["bytes_truncated"] = json!(true);
        coverage["truncated"] = json!(true);
    }
    coverage["undated_records"] = json!(events.iter().filter(|event| event.ms.is_none()).count());
    coverage["non_ok_records"] = json!(events
        .iter()
        .filter(|event| non_ok_status(event.status.as_ref()))
        .count());
    coverage
}

fn scan_sources(paths: &StatsPaths) -> Vec<SourceScan> {
    let chat2api = read_jsonl_events(&paths.usage_log);
    let chat2api_coverage = source_coverage(
        &paths.usage_log,
        &chat2api.events,
        &chat2api,
        CHAT2API_TIME_SOURCE,
        &[],
    );
    let dsh = read_snapshot_events(&paths.dsh_snapshot);
    let dsh_coverage = source_coverage(
        &paths.dsh_snapshot,
        &dsh.events,
        &dsh,
        DSH_TIME_SOURCE,
        &["duration_ms"],
    );
    vec![
        SourceScan {
            source: "chat2api".to_string(),
            trusted: events_trusted(&chat2api_coverage),
            events: chat2api.events,
            coverage: chat2api_coverage,
        },
        SourceScan {
            source: "dsh".to_string(),
            trusted: events_trusted(&dsh_coverage),
            events: dsh.events,
            coverage: dsh_coverage,
        },
    ]
}

fn events_trusted(coverage: &Value) -> bool {
    coverage
        .get("errors")
        .and_then(Value::as_array)
        .map_or(true, |errors| {
            !errors.iter().any(|error| {
                matches!(
                    error.get("type").and_then(Value::as_str),
                    Some("read_error" | "corrupt_ledger")
                )
            })
        })
}

fn accumulate_event(bucket: &mut Bucket, event: &Event) {
    bucket.requests += 1;
    let has_input = event.input_tokens.is_some();
    let has_output = event.output_tokens.is_some();
    if has_input && has_output {
        bucket.complete_requests += 1;
        bucket.complete_input += event.input_tokens.unwrap_or_default();
        bucket.complete_output += event.output_tokens.unwrap_or_default();
        return;
    }
    bucket.partial_requests += 1;
    if let Some(value) = event.input_tokens {
        bucket.partial_input += value;
        bucket.partial_input_known += 1;
    }
    if let Some(value) = event.output_tokens {
        bucket.partial_output += value;
        bucket.partial_output_known += 1;
    }
}

fn missing_reason(bucket: &Bucket) -> &'static str {
    let input_complete = bucket.partial_input_known == bucket.partial_requests;
    let output_complete = bucket.partial_output_known == bucket.partial_requests;
    if !input_complete && !output_complete {
        "no_tokens"
    } else if !input_complete {
        "output_only"
    } else {
        "no_input"
    }
}

fn token_row(day: &str, source: &str, model: &str, bucket: &Bucket) -> Value {
    json!({
        "date": day,
        "source": source,
        "model": model,
        "input_tokens": (bucket.complete_requests > 0).then_some(bucket.complete_input),
        "output_tokens": (bucket.complete_requests > 0).then_some(bucket.complete_output),
        "complete": bucket.partial_requests == 0,
        "complete_requests": bucket.complete_requests,
        "partial_requests": bucket.partial_requests,
        "partial_input_tokens": (bucket.partial_input_known > 0).then_some(bucket.partial_input),
        "partial_output_tokens": (bucket.partial_output_known > 0).then_some(bucket.partial_output),
    })
}

fn missing_row(
    day: Option<&str>,
    source: &str,
    model: &str,
    reason: &str,
    bucket: Option<&Bucket>,
) -> Value {
    let input_tokens = bucket
        .filter(|bucket| bucket.partial_input_known > 0)
        .map(|bucket| bucket.partial_input);
    let output_tokens = bucket
        .filter(|bucket| bucket.partial_output_known > 0)
        .map(|bucket| bucket.partial_output);
    json!({
        "date": day,
        "source": source,
        "model": model,
        "reason": reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    })
}

pub fn usage_tokens(paths: &StatsPaths, days: i64, now_ms: f64) -> Value {
    let (window_days, clamped) = clamp_int(days, 14, 1, MAX_TOKEN_RANGE_DAYS);
    let day_keys = window_day_keys(window_days, now_ms);
    let window: BTreeSet<String> = day_keys.iter().cloned().collect();
    let mut buckets: BTreeMap<(String, String, String), Bucket> = BTreeMap::new();
    let mut undated: BTreeSet<(String, String)> = BTreeSet::new();
    let mut scanned = 0i64;
    let scans = scan_sources(paths);
    let mut coverage_sources = Map::new();
    for scan in &scans {
        coverage_sources.insert(scan.source.clone(), scan.coverage.clone());
        if !scan.trusted {
            continue;
        }
        for event in &scan.events {
            let Some(day) = beijing_day(event.ms) else {
                undated.insert((scan.source.clone(), event.model.clone()));
                continue;
            };
            if !window.contains(&day) {
                continue;
            }
            scanned += 1;
            accumulate_event(
                buckets
                    .entry((day, scan.source.clone(), event.model.clone()))
                    .or_default(),
                event,
            );
        }
    }
    let mut rows = Vec::new();
    let mut missing = Vec::new();
    for ((day, source, model), bucket) in &buckets {
        rows.push(token_row(day, source, model, bucket));
        if bucket.partial_requests > 0 {
            missing.push(missing_row(
                Some(day),
                source,
                model,
                missing_reason(bucket),
                Some(bucket),
            ));
        }
    }
    for (source, model) in &undated {
        missing.push(missing_row(None, source, model, "invalid_time", None));
    }
    rows.sort_by(|left, right| {
        (
            left["date"].as_str().unwrap_or_default(),
            left["source"].as_str().unwrap_or_default(),
            left["model"].as_str().unwrap_or_default(),
        )
            .cmp(&(
                right["date"].as_str().unwrap_or_default(),
                right["source"].as_str().unwrap_or_default(),
                right["model"].as_str().unwrap_or_default(),
            ))
    });
    missing.sort_by(|left, right| {
        (
            left["date"].as_str().unwrap_or_default(),
            left["source"].as_str().unwrap_or_default(),
            left["model"].as_str().unwrap_or_default(),
            left["reason"].as_str().unwrap_or_default(),
        )
            .cmp(&(
                right["date"].as_str().unwrap_or_default(),
                right["source"].as_str().unwrap_or_default(),
                right["model"].as_str().unwrap_or_default(),
                right["reason"].as_str().unwrap_or_default(),
            ))
    });
    let mut truncated = clamped;
    let mut dropped_rows = 0usize;
    if rows.len() > MAX_ROWS_PER_ENDPOINT {
        dropped_rows = rows.len() - MAX_ROWS_PER_ENDPOINT;
        rows.drain(..dropped_rows);
        truncated = true;
    }
    let mut dropped_missing_rows = 0usize;
    if missing.len() > MAX_ROWS_PER_ENDPOINT {
        dropped_missing_rows = missing.len() - MAX_ROWS_PER_ENDPOINT;
        missing.drain(..dropped_missing_rows);
        truncated = true;
    }
    let complete_rows = rows
        .iter()
        .filter(|row| row["complete"].as_bool() == Some(true))
        .count();
    json!({
        "days": rows,
        "missing": missing,
        "coverage": {
            "days": window_days,
            "complete_rows": complete_rows,
            "partial_rows": rows.len() - complete_rows,
            "scanned": scanned,
            "truncated": truncated,
            "clamped": clamped,
            "dropped_rows": dropped_rows,
            "dropped_missing_rows": dropped_missing_rows,
            "sources": coverage_sources,
        },
        "timezone": TIMEZONE_NAME,
        "checked_epoch": (now_ms / 1000.0).floor() as i64,
    })
}
