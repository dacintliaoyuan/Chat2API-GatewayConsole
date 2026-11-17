//! Local usage statistics read faces (`usagesrv.py` + `console/stats.py`).
//!
//! The local role reads the private chat2api ledger and the frozen DSH day
//! snapshot. Missing ledgers stay visible in `coverage` and never produce
//! fabricated rows; unknown values stay `null` instead of being folded to zero.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use chrono::{FixedOffset, NaiveDate, TimeZone};
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
    }
}

fn read_snapshot_events(path: &Path) -> EventRead {
    // The metadata call only classifies the path: NotFound is a missing snapshot,
    // any other error is an explicit read error (never folded into missing).
    if let Err(error) = std::fs::metadata(path) {
        if error.kind() == std::io::ErrorKind::NotFound {
            return EventRead::missing();
        }
        return EventRead {
            read_error: Some(error.to_string()),
            ..EventRead::missing()
        };
    }
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

fn hour_label(ms: i64) -> String {
    let seconds = ms.div_euclid(1000);
    let Some(stamp) = beijing().timestamp_opt(seconds, 0).single() else {
        return String::new();
    };
    stamp.format("%Y-%m-%dT%H:00:00+08:00").to_string()
}

fn round_digits(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}")
        .parse::<f64>()
        .unwrap_or(value)
}

#[derive(Default)]
struct RateBucket {
    output_tokens: i64,
    duration_ms: i64,
    requests: i64,
}

pub fn usage_rates(paths: &StatsPaths, hours: i64, now_ms: f64) -> Value {
    let (window_hours, clamped) = clamp_int(hours, 24, 1, MAX_RATE_RANGE_HOURS);
    let hour_starts = {
        let current = (now_ms / 3_600_000.0).floor() as i64 * 3_600_000;
        (0..window_hours)
            .rev()
            .map(|offset| current - offset * 3_600_000)
            .collect::<Vec<i64>>()
    };
    let window_start = hour_starts.first().copied().unwrap_or(0);
    let window_end = window_start + window_hours * 3_600_000;
    let mut buckets: BTreeMap<(i64, String, String), RateBucket> = BTreeMap::new();
    let mut skipped = json!({
        "missing_output_tokens": 0,
        "missing_duration": 0,
        "invalid_duration": 0,
        "invalid_time": 0,
    });
    let scans = scan_sources(paths);
    let mut coverage_sources = Map::new();
    let mut recorded_requests = Map::new();
    for scan in &scans {
        coverage_sources.insert(scan.source.clone(), scan.coverage.clone());
        if !scan.trusted
            || scan.coverage["ledger_present"].as_bool() != Some(true)
        {
            recorded_requests.insert(scan.source.clone(), Value::Null);
            continue;
        }
        let mut recorded = 0i64;
        for event in &scan.events {
            let Some(ms) = event.ms else {
                skipped["invalid_time"] = json!(skipped["invalid_time"].as_i64().unwrap_or(0) + 1);
                continue;
            };
            if !(window_start as f64 <= ms && ms < window_end as f64) {
                continue;
            }
            recorded += 1;
            if event.output_tokens.is_none() {
                skipped["missing_output_tokens"] =
                    json!(skipped["missing_output_tokens"].as_i64().unwrap_or(0) + 1);
            }
            match event.duration_state {
                DurationState::Missing => {
                    skipped["missing_duration"] =
                        json!(skipped["missing_duration"].as_i64().unwrap_or(0) + 1);
                }
                DurationState::Invalid => {
                    skipped["invalid_duration"] =
                        json!(skipped["invalid_duration"].as_i64().unwrap_or(0) + 1);
                }
                DurationState::Ok => {}
            }
            let (Some(output_tokens), DurationState::Ok, Some(duration_ms)) =
                (event.output_tokens, event.duration_state, event.duration_ms)
            else {
                continue;
            };
            let hour = (ms / 3_600_000.0).floor() as i64 * 3_600_000;
            let bucket = buckets
                .entry((hour, scan.source.clone(), event.model.clone()))
                .or_default();
            bucket.output_tokens += output_tokens;
            bucket.duration_ms += duration_ms;
            bucket.requests += 1;
        }
        recorded_requests.insert(scan.source.clone(), json!(recorded));
    }
    let mut rows = Vec::new();
    for ((hour, source, model), bucket) in &buckets {
        rows.push(json!({
            "hour": hour_label(*hour),
            "source": source,
            "model": model,
            "output_tokens": bucket.output_tokens,
            "duration_ms": bucket.duration_ms,
            "requests": bucket.requests,
            "rate": if bucket.duration_ms > 0 {
                Some(round_digits(
                    bucket.output_tokens as f64 / (bucket.duration_ms as f64 / 1000.0),
                    6,
                ))
            } else {
                None
            },
        }));
    }
    let mut truncated = clamped;
    let mut dropped_rows = 0usize;
    if rows.len() > MAX_ROWS_PER_ENDPOINT {
        dropped_rows = rows.len() - MAX_ROWS_PER_ENDPOINT;
        rows.drain(..dropped_rows);
        truncated = true;
    }
    json!({
        "hours": rows,
        "skipped": skipped,
        "coverage": {
            "hours": window_hours,
            "rows": rows.len(),
            "truncated": truncated,
            "clamped": clamped,
            "dropped_rows": dropped_rows,
            "sources": coverage_sources,
        },
        "recorded_requests": recorded_requests,
        "timezone": TIMEZONE_NAME,
        "checked_epoch": (now_ms / 1000.0).floor() as i64,
    })
}

#[derive(Debug, Clone)]
pub struct StatsError(pub String);

impl std::fmt::Display for StatsError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for StatsError {}

#[derive(Default, Clone)]
struct TokenEvidence {
    records: i64,
    complete_records: i64,
    partial_records: i64,
    estimated_records: i64,
    unknown_records: i64,
    complete_input: i64,
    complete_output: i64,
    partial_input: i64,
    partial_output: i64,
    estimated_input: i64,
    estimated_output: i64,
    partial_input_known: i64,
    partial_output_known: i64,
    estimated_input_known: i64,
    estimated_output_known: i64,
}

#[derive(Default, Clone)]
struct StatEntry {
    requests: i64,
    input: i64,
    output: i64,
    cache: i64,
    reasoning: i64,
    token_evidence: TokenEvidence,
}

#[derive(Default)]
struct StatAgg {
    days: BTreeMap<String, BTreeMap<String, StatEntry>>,
    undated: usize,
    error: Option<Value>,
}

#[derive(Debug)]
struct RecordRead {
    records: Vec<Map<String, Value>>,
    missing: bool,
    read_error: Option<String>,
    bad_lines: usize,
}

#[derive(Debug)]
struct SnapshotRead {
    snapshot: Option<Value>,
    missing: bool,
    read_error: Option<String>,
    corrupt: Option<String>,
}

#[derive(Default, Clone)]
struct Totals {
    requests: i64,
    input_tokens: i64,
    output_tokens: i64,
    cache_tokens: i64,
    reasoning_tokens: i64,
}

impl Totals {
    fn to_json(&self) -> Value {
        json!({
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_tokens": self.cache_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        })
    }

    fn add_json(&mut self, other: &Value) {
        self.requests += other["requests"].as_i64().unwrap_or(0);
        self.input_tokens += other["input_tokens"].as_i64().unwrap_or(0);
        self.output_tokens += other["output_tokens"].as_i64().unwrap_or(0);
        self.cache_tokens += other["cache_tokens"].as_i64().unwrap_or(0);
        self.reasoning_tokens += other["reasoning_tokens"].as_i64().unwrap_or(0);
    }
}

fn python_int_or_zero(value: Option<&Value>) -> Result<i64, String> {
    match value {
        None | Some(Value::Null) => Ok(0),
        Some(Value::Bool(value)) => Ok(i64::from(*value)),
        Some(Value::Number(number)) => {
            if let Some(value) = number.as_i64() {
                return Ok(value);
            }
            match number.as_f64() {
                Some(value) if value.is_finite() => Ok(value.trunc() as i64),
                _ => Err(format!("invalid literal for int() with base 10: {number}")),
            }
        }
        Some(Value::String(text)) => {
            if text.is_empty() {
                return Ok(0);
            }
            let trimmed = text.trim();
            if trimmed.is_empty() {
                return Err(format!("invalid literal for int() with base 10: '{text}'"));
            }
            let cleaned = trimmed.replace('_', "");
            cleaned
                .parse::<i64>()
                .map_err(|_| format!("invalid literal for int() with base 10: '{text}'"))
        }
        Some(other) => Err(format!(
            "int() argument must be a string, a bytes-like object or a real number, not '{}'",
            python_type_name(other)
        )),
    }
}

fn python_str_or_unknown(value: Option<&Value>) -> String {
    match value {
        Some(value) if !falsy(value) => python_str(value),
        _ => "unknown".to_string(),
    }
}

fn accumulate_token_evidence(
    evidence: &mut TokenEvidence,
    record: &Map<String, Value>,
    snapshot: bool,
) {
    evidence.records += 1;
    let input = trusted_token_count(record.get(if snapshot {
        "input"
    } else {
        "input_tokens"
    }));
    let output = trusted_token_count(record.get(if snapshot {
        "output"
    } else {
        "output_tokens"
    }));
    let source = record.get("usage_source").and_then(Value::as_str);
    if source == Some("none") || (input.is_none() && output.is_none()) {
        evidence.unknown_records += 1;
        return;
    }
    let complete = input.is_some() && output.is_some();
    let estimated = source == Some("estimated")
        || (source.is_none() && record.get("estimated") == Some(&Value::Bool(true)));
    if estimated {
        evidence.estimated_records += 1;
    } else if complete
        && source != Some("partial")
        && !non_ok_status(record.get("status"))
    {
        evidence.complete_records += 1;
        evidence.complete_input += input.unwrap_or_default();
        evidence.complete_output += output.unwrap_or_default();
        return;
    } else {
        evidence.partial_records += 1;
    }
    let prefix = if estimated { "estimated" } else { "partial" };
    for (side, value) in [("input", input), ("output", output)] {
        if let Some(value) = value {
            match (prefix, side) {
                ("partial", "input") => evidence.partial_input += value,
                ("partial", "output") => evidence.partial_output += value,
                ("estimated", "input") => evidence.estimated_input += value,
                ("estimated", "output") => evidence.estimated_output += value,
                _ => {}
            }
            match (prefix, side) {
                ("partial", "input") => evidence.partial_input_known += 1,
                ("partial", "output") => evidence.partial_output_known += 1,
                ("estimated", "input") => evidence.estimated_input_known += 1,
                ("estimated", "output") => evidence.estimated_output_known += 1,
                _ => {}
            }
        }
    }
}

fn emit_token_evidence(evidence: &TokenEvidence, kind: &str) -> Map<String, Value> {
    let mut output = Map::new();
    let complete = evidence.records > 0
        && evidence.complete_records == evidence.records;
    output.insert(
        "token_coverage".to_string(),
        json!({
            "records": evidence.records,
            "complete_records": evidence.complete_records,
            "partial_records": evidence.partial_records,
            "estimated_records": evidence.estimated_records,
            "unknown_records": evidence.unknown_records,
            "complete": complete,
            "unit": if kind == "dsh-ledger" { "model_day_aggregate" } else { "request" },
        }),
    );
    output.insert(
        "complete_input_tokens".to_string(),
        if evidence.complete_records > 0 {
            json!(evidence.complete_input)
        } else {
            Value::Null
        },
    );
    output.insert(
        "complete_output_tokens".to_string(),
        if evidence.complete_records > 0 {
            json!(evidence.complete_output)
        } else {
            Value::Null
        },
    );
    output.insert(
        "token_total".to_string(),
        if evidence.complete_records > 0 {
            json!(evidence.complete_input + evidence.complete_output)
        } else {
            Value::Null
        },
    );
    for prefix in ["partial", "estimated"] {
        let (input_value, output_value, input_known, output_known) = match prefix {
            "partial" => (
                evidence.partial_input,
                evidence.partial_output,
                evidence.partial_input_known,
                evidence.partial_output_known,
            ),
            _ => (
                evidence.estimated_input,
                evidence.estimated_output,
                evidence.estimated_input_known,
                evidence.estimated_output_known,
            ),
        };
        output.insert(
            format!("{prefix}_input_tokens"),
            if input_known > 0 { json!(input_value) } else { Value::Null },
        );
        output.insert(
            format!("{prefix}_output_tokens"),
            if output_known > 0 { json!(output_value) } else { Value::Null },
        );
        output.insert(
            format!("{prefix}_token_total"),
            if input_known > 0 || output_known > 0 {
                json!(input_value + output_value)
            } else {
                Value::Null
            },
        );
    }
    output
}

fn read_ledger_jsonl(path: &Path) -> RecordRead {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return RecordRead {
                records: Vec::new(),
                missing: true,
                read_error: None,
                bad_lines: 0,
            };
        }
        Err(error) => {
            return RecordRead {
                records: Vec::new(),
                missing: false,
                read_error: Some(error.to_string()),
                bad_lines: 0,
            };
        }
    };
    let mut records = Vec::new();
    let mut bad_lines = 0;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let record = match serde_json::from_str::<Value>(line) {
            Ok(Value::Object(record)) => record,
            Ok(_) => continue,
            Err(_) => {
                bad_lines += 1;
                continue;
            }
        };
        let mut record = record;
        let ms = record
            .get("time")
            .and_then(Value::as_str)
            .and_then(parse_ts_ms);
        record.insert(
            "_ms".to_string(),
            ms.map_or(Value::Null, |value| json!(value)),
        );
        records.push(record);
    }
    records.sort_by(|left, right| {
        let left = left.get("_ms").and_then(Value::as_f64).unwrap_or(0.0);
        let right = right.get("_ms").and_then(Value::as_f64).unwrap_or(0.0);
        left.partial_cmp(&right).unwrap_or(std::cmp::Ordering::Equal)
    });
    if records.len() > LEDGER_RECORD_LIMIT {
        records.drain(..records.len() - LEDGER_RECORD_LIMIT);
    }
    RecordRead {
        records,
        missing: false,
        read_error: None,
        bad_lines,
    }
}

fn read_dsh_snapshot(path: &Path) -> SnapshotRead {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return SnapshotRead {
                snapshot: None,
                missing: true,
                read_error: None,
                corrupt: None,
            };
        }
        Err(error) => {
            return SnapshotRead {
                snapshot: None,
                missing: false,
                read_error: Some(error.to_string()),
                corrupt: None,
            };
        }
    };
    let snapshot = match serde_json::from_str::<Value>(&text) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            return SnapshotRead {
                snapshot: None,
                missing: false,
                read_error: None,
                corrupt: Some(format!("JSONDecodeError: {error}")),
            };
        }
    };
    if !snapshot.is_object() {
        return SnapshotRead {
            snapshot: None,
            missing: false,
            read_error: None,
            corrupt: Some("snapshot root is not a JSON object".to_string()),
        };
    }
    SnapshotRead {
        snapshot: Some(snapshot),
        missing: false,
        read_error: None,
        corrupt: None,
    }
}

fn collect_jsonl_records(
    records: &[Map<String, Value>],
    models_filter: Option<&BTreeSet<String>>,
    lo_iso: &str,
    hi_iso: &str,
) -> StatAgg {
    let mut days: BTreeMap<String, BTreeMap<String, StatEntry>> = BTreeMap::new();
    let mut undated = 0usize;
    let mut failed = 0usize;
    let mut last_error = None;
    for record in records {
        let mut token_seen = false;
        let mut token_group: Option<(String, String)> = None;
        let result = (|| -> Result<(), String> {
            let ms = record
                .get("_ms")
                .and_then(Value::as_f64)
                .or_else(|| {
                    record
                        .get("time")
                        .and_then(Value::as_str)
                        .and_then(parse_ts_ms)
                });
            let Some(day) = beijing_day(ms) else {
                undated += 1;
                return Ok(());
            };
            if day.as_str() < lo_iso || day.as_str() > hi_iso {
                return Ok(());
            }
            let model = python_str_or_unknown(record.get("model"));
            if models_filter.is_some_and(|filter| !filter.contains(&model)) {
                return Ok(());
            }
            token_group = Some((day.clone(), model.clone()));
            let input = python_int_or_zero(record.get("input_tokens"))?;
            let output = python_int_or_zero(record.get("output_tokens"))?;
            let cache = python_int_or_zero(
                record
                    .get("cached_input_tokens")
                    .or_else(|| record.get("cache_tokens")),
            )?;
            let reasoning = python_int_or_zero(record.get("reasoning_tokens"))?;
            let entry = days
                .entry(day)
                .or_default()
                .entry(model)
                .or_default();
            accumulate_token_evidence(&mut entry.token_evidence, record, false);
            token_seen = true;
            entry.requests += 1;
            entry.input += input;
            entry.output += output;
            entry.cache += cache;
            entry.reasoning += reasoning;
            Ok(())
        })();
        if let Err(error) = result {
            if let Some((day, model)) = &token_group {
                if !token_seen {
                    let entry = days
                        .entry(day.clone())
                        .or_default()
                        .entry(model.clone())
                        .or_default();
                    accumulate_token_evidence(&mut entry.token_evidence, record, false);
                }
            }
            failed += 1;
            last_error = Some(format!("ValueError: {error}"));
        }
    }
    StatAgg {
        days,
        undated,
        error: (failed > 0).then(|| {
            json!({
                "type": "record_error",
                "failed_records": failed,
                "detail": last_error,
            })
        }),
    }
}

fn collect_dsh_records(
    snapshot: &Map<String, Value>,
    models_filter: Option<&BTreeSet<String>>,
    lo_iso: &str,
    hi_iso: &str,
) -> StatAgg {
    let mut days: BTreeMap<String, BTreeMap<String, StatEntry>> = BTreeMap::new();
    let days_root = match snapshot.get("days") {
        None => Map::new(),
        Some(Value::Object(days)) => days.clone(),
        Some(other) => {
            return StatAgg {
                days,
                undated: 0,
                error: Some(json!({
                    "type": "corrupt_snapshot",
                    "detail": format!(
                        "snapshot days is not a JSON object: {}",
                        python_type_name(other)
                    ),
                })),
            };
        }
    };
    let merge_day = "2026-09-02";
    if merge_day < lo_iso || merge_day > hi_iso {
        return StatAgg::default();
    }
    let Some(day_entry) = days_root.get(merge_day).and_then(Value::as_object) else {
        return StatAgg::default();
    };
    let by_provider_model = match day_entry.get("byProviderModel") {
        None => Map::new(),
        Some(Value::Object(rows)) => rows.clone(),
        Some(other) => {
            return StatAgg {
                days,
                undated: 0,
                error: Some(json!({
                    "type": "corrupt_snapshot",
                    "detail": format!(
                        "byProviderModel is not a JSON object: {}",
                        python_type_name(other)
                    ),
                })),
            };
        }
    };
    let mut failed = 0usize;
    let mut last_error = None;
    for (model, row) in by_provider_model {
        let Some(row) = row.as_object() else {
            continue;
        };
        if models_filter.is_some_and(|filter| !filter.contains(&model)) {
            continue;
        }
        let entry = days
            .entry(merge_day.to_string())
            .or_default()
            .entry(model)
            .or_default();
        accumulate_token_evidence(&mut entry.token_evidence, row, true);
        let result = (|| -> Result<(i64, i64, i64, i64, i64), String> {
            Ok((
                python_int_or_zero(row.get("calls"))?,
                python_int_or_zero(row.get("input"))?,
                python_int_or_zero(row.get("output"))?,
                python_int_or_zero(row.get("cacheRead"))?,
                python_int_or_zero(row.get("reasoning"))?,
            ))
        })();
        match result {
            Ok((calls, input, output, cache, reasoning)) => {
                entry.requests += calls;
                entry.input += input;
                entry.output += output;
                entry.cache += cache;
                entry.reasoning += reasoning;
            }
            Err(error) => {
                failed += 1;
                last_error = Some(format!("ValueError: {error}"));
            }
        }
    }
    StatAgg {
        days,
        undated: 0,
        error: (failed > 0).then(|| {
            json!({
                "type": "record_error",
                "failed_rows": failed,
                "detail": last_error,
            })
        }),
    }
}

fn emit_models(kind: &str, entries: &BTreeMap<String, StatEntry>) -> Vec<Value> {
    let mut rows: Vec<(String, Value)> = entries
        .iter()
        .map(|(model, entry)| {
            let tokens = emit_token_evidence(&entry.token_evidence, kind);
            let mut row = json!({
                "model": model,
                "requests": entry.requests,
                "input_tokens": entry.input,
                "output_tokens": entry.output,
            });
            let object = row.as_object_mut().expect("model row");
            for (key, value) in tokens {
                object.insert(key, value);
            }
            object.insert("cache_tokens".to_string(), json!(entry.cache));
            object.insert("reasoning_tokens".to_string(), json!(entry.reasoning));
            (model.clone(), row)
        })
        .collect();
    rows.sort_by(|(left_name, left), (right_name, right)| {
        right["requests"]
            .as_i64()
            .unwrap_or(0)
            .cmp(&left["requests"].as_i64().unwrap_or(0))
            .then_with(|| left_name.cmp(right_name))
    });
    rows.into_iter().map(|(_, row)| row).collect()
}

fn emit_source_totals(models_out: &[Value]) -> Value {
    let mut totals = Totals::default();
    for model in models_out {
        totals.add_json(model);
    }
    totals.to_json()
}

fn load_source(
    source: &str,
    path: &Path,
    models_filter: Option<&BTreeSet<String>>,
    lo_iso: &str,
    hi_iso: &str,
) -> (StatAgg, Value) {
    let kind = if source == "dsh" {
        "dsh-ledger"
    } else {
        "estimate"
    };
    let mut coverage = json!({
        "ledger_present": false,
        "days_with_records": 0,
        "kind": kind,
        "ledger_files": [display_path(path)],
    });
    let agg = if source == "dsh" {
        let read = read_dsh_snapshot(path);
        if read.missing {
            StatAgg::default()
        } else {
            coverage["ledger_present"] = json!(true);
            if let Some(error) = &read.read_error {
                coverage["errors"] = json!([{"type": "read_error", "detail": error}]);
            }
            if let Some(corrupt) = &read.corrupt {
                let mut errors = coverage["errors"].as_array().cloned().unwrap_or_default();
                errors.push(json!({"type": "corrupt_snapshot", "detail": corrupt}));
                coverage["errors"] = json!(errors);
            }
            collect_dsh_records(
                read.snapshot
                    .as_ref()
                    .and_then(Value::as_object)
                    .unwrap_or(&Map::new()),
                models_filter,
                lo_iso,
                hi_iso,
            )
        }
    } else {
        let read = read_ledger_jsonl(path);
        if read.missing {
            StatAgg::default()
        } else {
            coverage["ledger_present"] = json!(true);
            let mut errors = Vec::new();
            if let Some(error) = &read.read_error {
                errors.push(json!({"type": "read_error", "detail": error}));
            }
            if read.bad_lines > 0 {
                errors.push(json!({"type": "malformed_lines", "bad_lines": read.bad_lines}));
            }
            if !errors.is_empty() {
                coverage["errors"] = json!(errors);
            }
            collect_jsonl_records(&read.records, models_filter, lo_iso, hi_iso)
        }
    };
    if let Some(error) = &agg.error {
        let mut errors = coverage["errors"].as_array().cloned().unwrap_or_default();
        errors.push(error.clone());
        coverage["errors"] = json!(errors);
    }
    if agg.undated > 0 {
        coverage["undated_records"] = json!(agg.undated);
    }
    coverage["days_with_records"] = json!(agg.days.len());
    (agg, coverage)
}

fn parse_date(value: &str) -> Result<NaiveDate, StatsError> {
    let text = value.trim();
    let bytes = text.as_bytes();
    let shaped = bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| index == 4 || index == 7 || byte.is_ascii_digit());
    if shaped {
        if let Ok(date) = NaiveDate::parse_from_str(text, "%Y-%m-%d") {
            return Ok(date);
        }
    }
    Err(StatsError(format!(
        "invalid date '{value}': expected YYYY-MM-DD (Asia/Shanghai calendar day)"
    )))
}

fn parse_range(date_from: &str, date_to: &str) -> Result<(NaiveDate, NaiveDate, i64), StatsError> {
    let lo = parse_date(date_from)?;
    let hi = parse_date(date_to)?;
    if lo > hi {
        return Err(StatsError(format!(
            "invalid date range: date_from {} > date_to {}",
            lo.format("%Y-%m-%d"),
            hi.format("%Y-%m-%d")
        )));
    }
    let range_days = (hi - lo).num_days() + 1;
    if range_days > MAX_RANGE_DAYS {
        return Err(StatsError(format!(
            "date range too large: {} days exceeds the {}-day query limit",
            range_days, MAX_RANGE_DAYS
        )));
    }
    Ok((lo, hi, range_days))
}

fn day_token_rows(blocks: &[Value]) -> Vec<Value> {
    let mut rows = Vec::new();
    for block in blocks {
        let Some(models) = block["models"].as_array() else {
            continue;
        };
        for row in models {
            if row["token_total"].is_null() {
                continue;
            }
            rows.push(json!({
                "source": block["source"],
                "model": row["model"],
                "input_tokens": row["complete_input_tokens"],
                "output_tokens": row["complete_output_tokens"],
                "token_total": row["token_total"],
                "partial_token_total": row["partial_token_total"],
                "estimated_token_total": row["estimated_token_total"],
                "token_coverage": row["token_coverage"],
            }));
        }
    }
    rows.sort_by(|left, right| {
        right["token_total"]
            .as_f64()
            .unwrap_or(0.0)
            .partial_cmp(&left["token_total"].as_f64().unwrap_or(0.0))
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| {
                left["source"]
                    .as_str()
                    .unwrap_or_default()
                    .cmp(right["source"].as_str().unwrap_or_default())
            })
            .then_with(|| {
                left["model"]
                    .as_str()
                    .unwrap_or_default()
                    .cmp(right["model"].as_str().unwrap_or_default())
            })
    });
    rows
}

pub fn bounded_stats(
    paths: &StatsPaths,
    date_from: &str,
    date_to: &str,
    sources: Option<&[String]>,
    models: Option<&[String]>,
) -> Result<Value, StatsError> {
    let (lo, hi, range_days) = parse_range(date_from, date_to)?;
    let models_filter = models.map(|models| {
        models
            .iter()
            .map(|model| model.to_string())
            .collect::<BTreeSet<String>>()
    });
    let selected: BTreeSet<String> = match sources {
        Some(sources) => sources.iter().cloned().collect(),
        None => ["chat2api".to_string(), "dsh".to_string()]
            .into_iter()
            .collect(),
    };
    let lo_iso = lo.format("%Y-%m-%d").to_string();
    let hi_iso = hi.format("%Y-%m-%d").to_string();
    let mut per_source: BTreeMap<String, StatAgg> = BTreeMap::new();
    let mut coverage_sources = Map::new();
    for source in &selected {
        if source != "chat2api" && source != "dsh" {
            coverage_sources.insert(
                source.clone(),
                json!({
                    "ledger_present": false,
                    "days_with_records": 0,
                    "kind": Value::Null,
                    "ledger_files": [],
                    "errors": [{
                        "type": "unknown_source",
                        "detail": "requested source has no usable ledger",
                    }],
                }),
            );
            per_source.insert(source.clone(), StatAgg::default());
            continue;
        }
        let path = if source == "dsh" {
            &paths.dsh_snapshot
        } else {
            &paths.usage_log
        };
        let (agg, coverage) =
            load_source(source, path, models_filter.as_ref(), &lo_iso, &hi_iso);
        per_source.insert(source.clone(), agg);
        coverage_sources.insert(source.clone(), coverage);
    }
    let mut day_keys = BTreeSet::new();
    for agg in per_source.values() {
        for day in agg.days.keys() {
            day_keys.insert(day.clone());
        }
    }
    let mut totals = Totals::default();
    let mut days_out = Vec::new();
    for day in &day_keys {
        let mut blocks = Vec::new();
        let mut day_total = Totals::default();
        for source in &selected {
            let Some(agg) = per_source.get(source) else {
                continue;
            };
            let Some(entries) = agg.days.get(day) else {
                continue;
            };
            let kind = if source == "dsh" {
                "dsh-ledger"
            } else {
                "estimate"
            };
            let models_out = emit_models(kind, entries);
            let source_totals = emit_source_totals(&models_out);
            day_total.add_json(&source_totals);
            blocks.push(json!({
                "source": source,
                "kind": kind,
                "models": models_out,
                "source_totals": source_totals,
            }));
        }
        let mut token_coverage = json!({
            "records": 0,
            "complete_records": 0,
            "partial_records": 0,
            "estimated_records": 0,
            "unknown_records": 0,
        });
        for block in &blocks {
            let Some(models) = block["models"].as_array() else {
                continue;
            };
            for row in models {
                for key in [
                    "records",
                    "complete_records",
                    "partial_records",
                    "estimated_records",
                    "unknown_records",
                ] {
                    token_coverage[key] = json!(
                        token_coverage[key].as_i64().unwrap_or(0)
                            + row["token_coverage"][key].as_i64().unwrap_or(0)
                    );
                }
            }
        }
        token_coverage["complete"] = json!(
            token_coverage["records"].as_i64().unwrap_or(0) > 0
                && token_coverage["complete_records"].as_i64().unwrap_or(0)
                    == token_coverage["records"].as_i64().unwrap_or(0)
        );
        days_out.push(json!({
            "date": day,
            "sources": blocks,
            "model_token_ranking": day_token_rows(&blocks),
            "token_coverage": token_coverage,
            "day_total": day_total.to_json(),
        }));
        totals.add_json(&day_total.to_json());
    }
    let mut missing_days = Vec::new();
    let mut cursor = lo;
    while cursor <= hi {
        let key = cursor.format("%Y-%m-%d").to_string();
        if !day_keys.contains(&key) {
            missing_days.push(key);
        }
        let Some(next) = cursor.succ_opt() else {
            break;
        };
        cursor = next;
    }
    Ok(json!({
        "days": days_out,
        "totals": totals.to_json(),
        "coverage": {
            "sources": coverage_sources,
            "missing_days": missing_days,
        },
        "meta": {
            "timezone": TIMEZONE_NAME,
            "range_days": range_days,
        },
    }))
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
