//! S5b: the share-usage read face (`share/metering.py`).
//!
//! Read-only: the reader opens the caller-provided ledger for reading only,
//! never creates, truncates or writes it, and never reads the private
//! `usage-chat2api.jsonl`. Unknown values stay `null`; they are never folded
//! into `0`.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use chrono::{DateTime, FixedOffset, NaiveDateTime, TimeZone, Utc};
use serde_json::{json, Map, Value};

use crate::share_read::read_key_labels;

pub const DEFAULT_LIMIT: usize = 200_000;
pub const SHARE_REQUEST_PAGE_DEFAULT: i64 = 50;
pub const SHARE_REQUEST_PAGE_MAX: i64 = 200;
pub const PRIVATE_LEDGER_BASENAME: &str = "usage-chat2api.jsonl";
pub const UNATTRIBUTED_FILTER: &str = "unattributed";
pub const COMPLETE_STATUS: &str = "ok";
pub const USAGE_SOURCES: [&str; 4] = ["upstream", "partial", "estimated", "none"];
pub const CACHE_REPORT_STATUSES: [&str; 2] = ["reported", "unreported"];
pub const SHARE_REQUEST_RESULTS: [&str; 4] = ["success", "failed", "cancelled", "unknown"];
pub const SHARE_USAGE_WINDOW_NAMES: [&str; 6] = ["1h", "today", "24h", "7d", "30d", "all"];

pub fn share_usage_windows() -> [(&'static str, Option<f64>); 6] {
    [
        ("1h", Some(3600.0)),
        ("today", None),
        ("24h", Some(86400.0)),
        ("7d", Some(7.0 * 86400.0)),
        ("30d", Some(30.0 * 86400.0)),
        ("all", None),
    ]
}

fn beijing() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).expect("beijing offset")
}

fn parse_ts(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    let text = value.as_str()?.trim();
    if text.is_empty() {
        return None;
    }
    let normalized = match text.strip_suffix('Z') {
        Some(prefix) => format!("{prefix}+00:00"),
        None => text.to_string(),
    };
    if let Ok(parsed) = DateTime::parse_from_rfc3339(&normalized) {
        return Some(parsed.timestamp_millis() as f64);
    }
    if let Ok(parsed) = NaiveDateTime::parse_from_str(text, "%Y-%m-%d %H:%M:%S%.f") {
        return Some(parsed.and_utc().timestamp_millis() as f64);
    }
    None
}

fn beijing_day(ms: Option<f64>) -> Option<String> {
    let ms = ms?;
    let parsed = beijing().timestamp_millis_opt(ms as i64).single()?;
    Some(parsed.format("%Y-%m-%d").to_string())
}

fn count(record: &Map<String, Value>, names: &[&str]) -> Option<i64> {
    for name in names {
        let Some(value) = record.get(*name) else {
            continue;
        };
        if value.is_boolean() {
            continue;
        }
        if let Some(value) = value.as_i64() {
            if value >= 0 {
                return Some(value);
            }
        }
        if let Some(value) = value.as_f64() {
            if value.is_finite() && value >= 0.0 {
                return Some(value as i64);
            }
        }
    }
    None
}

fn trusted_count(value: Option<&Value>) -> Option<i64> {
    value.and_then(Value::as_i64).filter(|value| *value >= 0)
}

fn source_of(record: &Map<String, Value>) -> &'static str {
    let source = match record.get("usage_source").and_then(Value::as_str) {
        Some("upstream") => "upstream",
        Some("partial") => "partial",
        Some("estimated") => "estimated",
        Some("none") => "none",
        _ => match record.get("estimated").and_then(Value::as_bool) {
            Some(true) => "estimated",
            Some(false) => "upstream",
            None => return "none",
        },
    };
    if source == "none" {
        return source;
    }
    if count(record, &["input_tokens", "output_tokens", "total_tokens"]).is_none() {
        return "none";
    }
    source
}

fn cache_report_status(record: &Map<String, Value>) -> &'static str {
    match record.get("cache_report_status").and_then(Value::as_str) {
        Some("reported") => "reported",
        Some("unreported") => "unreported",
        _ => "unknown",
    }
}

fn request_result(record: &Map<String, Value>) -> &'static str {
    let status = record.get("status").and_then(Value::as_str);
    let Some(status) = status else {
        return "unknown";
    };
    if status.is_empty() {
        return "unknown";
    }
    if status == COMPLETE_STATUS {
        return "success";
    }
    if matches!(status, "client_disconnected" | "cancelled" | "canceled") {
        return "cancelled";
    }
    "failed"
}

fn filter_value(
    name: &str,
    value: Option<&str>,
    allowed: Option<&[&str]>,
    max_length: usize,
) -> Result<Option<String>, ValueError> {
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_empty() || value.len() > max_length {
        return Err(ValueError(format!("invalid {name} filter")));
    }
    if let Some(allowed) = allowed {
        if !allowed.contains(&value) {
            return Err(ValueError(format!("invalid {name} filter")));
        }
    }
    Ok(Some(value.to_string()))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValueError(pub String);

impl std::fmt::Display for ValueError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.0)
    }
}

impl std::error::Error for ValueError {}

fn now_ms() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as f64)
        .unwrap_or_default()
}

fn window_cutoff(window: &str, now_ms: f64) -> Result<Option<f64>, ValueError> {
    let seconds = match window {
        "1h" => Some(3600.0),
        "today" => None,
        "24h" => Some(86400.0),
        "7d" => Some(7.0 * 86400.0),
        "30d" => Some(30.0 * 86400.0),
        "all" => None,
        _ => return Err(ValueError("invalid window filter".to_string())),
    };
    if window == "today" {
        let local = beijing()
            .timestamp_millis_opt(now_ms as i64)
            .single()
            .ok_or_else(|| ValueError("invalid window filter".to_string()))?;
        let midnight = local
            .date_naive()
            .and_hms_opt(0, 0, 0)
            .expect("midnight")
            .and_local_timezone(beijing())
            .single();
        return Ok(midnight.map(|value| value.timestamp_millis() as f64));
    }
    Ok(seconds.map(|seconds| now_ms - seconds * 1000.0))
}

fn record_matches(
    record: &Map<String, Value>,
    at_ms: Option<f64>,
    cutoff: Option<f64>,
    key_id: Option<&str>,
    model: Option<&str>,
    result: Option<&str>,
) -> bool {
    if let Some(cutoff) = cutoff {
        match at_ms {
            Some(at_ms) if at_ms >= cutoff => {}
            _ => return false,
        }
    }
    if let Some(key_id) = key_id {
        let record_key = record.get("key_id");
        if key_id == UNATTRIBUTED_FILTER {
            if record_key
                .and_then(Value::as_str)
                .map(|value| !value.is_empty())
                .unwrap_or(false)
            {
                return false;
            }
        } else if record_key.and_then(Value::as_str) != Some(key_id) {
            return false;
        }
    }
    if let Some(model) = model {
        let record_model = record
            .get("model")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or("unknown");
        if record_model != model {
            return false;
        }
    }
    if let Some(result) = result {
        if request_result(record) != result {
            return false;
        }
    }
    true
}

fn filter_echo(
    window: &str,
    key_id: Option<&str>,
    model: Option<&str>,
    result: Option<&str>,
) -> Value {
    json!({
        "window": window,
        "key": key_id,
        "model": model,
        "result": result,
    })
}

#[derive(Debug, Clone)]
struct ReadCoverage {
    ledger_present: bool,
    ledger_readable: bool,
    records_total: usize,
    unparsed_lines: usize,
    truncated: bool,
}

fn empty_summary() -> Map<String, Value> {
    let mut coverage = Map::new();
    coverage.insert("records".to_string(), json!(0));
    coverage.insert("unknownTimeRequests".to_string(), json!(0));
    coverage.insert("cacheReportedRequests".to_string(), json!(0));
    coverage.insert("cacheUnreportedRequests".to_string(), json!(0));
    coverage.insert("cacheUnknownRequests".to_string(), json!(0));
    let mut token_usage = Map::new();
    for field in [
        "completeRequests",
        "lowerBoundRequests",
        "estimatedRequests",
        "unknownRequests",
        "inputTokens",
        "outputTokens",
        "lowerBoundInputTokens",
        "lowerBoundOutputTokens",
        "estimatedInputTokens",
        "estimatedOutputTokens",
        "lowerBoundInputKnown",
        "lowerBoundOutputKnown",
        "estimatedInputKnown",
        "estimatedOutputKnown",
        "cachedInputTokens",
        "cacheReportedRequests",
        "cacheUnreportedRequests",
        "cacheUnknownRequests",
    ] {
        token_usage.insert(field.to_string(), json!(0));
    }
    let mut summary = Map::new();
    summary.insert("requests".to_string(), json!(0));
    summary.insert("unknownRequests".to_string(), json!(0));
    summary.insert("inputTokens".to_string(), json!(0));
    summary.insert("outputTokens".to_string(), json!(0));
    summary.insert("coverage".to_string(), Value::Object(coverage));
    summary.insert("tokenUsage".to_string(), Value::Object(token_usage));
    summary
}

fn empty_key(key_id: Option<&str>) -> Map<String, Value> {
    let mut key = empty_summary();
    key.insert(
        "key_id".to_string(),
        key_id.map(Value::from).unwrap_or(Value::Null),
    );
    key.insert("models".to_string(), json!({}));
    key.insert("days".to_string(), json!({}));
    let mut undated = empty_summary();
    undated.insert("day".to_string(), Value::Null);
    key.insert("undated".to_string(), Value::Object(undated));
    key
}

fn accumulate_token_usage(
    usage: &mut Map<String, Value>,
    record: &Map<String, Value>,
    source: &str,
) {
    let cache_status = cache_report_status(record);
    match cache_status {
        "reported" => {
            let cached = trusted_count(
                record
                    .get("cached_input_tokens")
                    .or_else(|| record.get("cache_tokens")),
            );
            match cached {
                None => increment(usage, "cacheUnknownRequests", 1),
                Some(cached) => {
                    increment(usage, "cacheReportedRequests", 1);
                    add(usage, "cachedInputTokens", cached);
                }
            }
        }
        "unreported" => increment(usage, "cacheUnreportedRequests", 1),
        _ => increment(usage, "cacheUnknownRequests", 1),
    }
    let input_tok = trusted_count(record.get("input_tokens"));
    let output_tok = trusted_count(record.get("output_tokens"));
    if source == "none" || (input_tok.is_none() && output_tok.is_none()) {
        increment(usage, "unknownRequests", 1);
        return;
    }
    let complete = input_tok.is_some() && output_tok.is_some();
    if source == "upstream"
        && complete
        && record
            .get("status")
            .and_then(Value::as_str)
            .filter(|status| !status.is_empty())
            .unwrap_or(COMPLETE_STATUS)
            == COMPLETE_STATUS
    {
        increment(usage, "completeRequests", 1);
        add(usage, "inputTokens", input_tok.unwrap_or_default());
        add(usage, "outputTokens", output_tok.unwrap_or_default());
        return;
    }
    let prefix = if source == "estimated" {
        "estimated"
    } else {
        "lowerBound"
    };
    increment(usage, &format!("{prefix}Requests"), 1);
    for (side, value) in [("Input", input_tok), ("Output", output_tok)] {
        if let Some(value) = value {
            add(usage, &format!("{prefix}{side}Tokens"), value);
            increment(usage, &format!("{prefix}{side}Known"), 1);
        }
    }
}

fn add_summary(
    entry: &mut Map<String, Value>,
    record: &Map<String, Value>,
    source: &str,
    at_ms: Option<f64>,
    include_unknown_request: bool,
) {
    if source != "none" || include_unknown_request {
        increment(entry, "requests", 1);
    }
    let cache_status = cache_report_status(record);
    {
        let coverage = entry
            .get_mut("coverage")
            .and_then(Value::as_object_mut)
            .expect("coverage");
        increment(coverage, "records", 1);
        if at_ms.is_none() {
            increment(coverage, "unknownTimeRequests", 1);
        }
        if cache_status == "reported" {
            increment(coverage, "cacheReportedRequests", 1);
        } else if cache_status == "unreported" {
            increment(coverage, "cacheUnreportedRequests", 1);
        } else {
            increment(coverage, "cacheUnknownRequests", 1);
        }
    }
    let token_usage = entry
        .get_mut("tokenUsage")
        .and_then(Value::as_object_mut)
        .expect("token usage");
    accumulate_token_usage(token_usage, record, source);
    if source == "none" {
        increment(entry, "unknownRequests", 1);
        return;
    }
    for (field, names) in [
        ("inputTokens", vec!["input_tokens"]),
        ("outputTokens", vec!["output_tokens"]),
    ] {
        if let Some(value) = count(record, &names) {
            add(entry, field, value);
        }
    }
}

fn increment(object: &mut Map<String, Value>, field: &str, by: i64) {
    let current = object
        .get(field)
        .and_then(Value::as_i64)
        .unwrap_or_default();
    object.insert(field.to_string(), json!(current + by));
}

fn add(object: &mut Map<String, Value>, field: &str, by: i64) {
    increment(object, field, by);
}

fn finish_token_usage(usage: &Map<String, Value>, records: i64) -> Value {
    let mut result = Map::new();
    for field in [
        "completeRequests",
        "lowerBoundRequests",
        "estimatedRequests",
        "unknownRequests",
    ] {
        result.insert(
            field.to_string(),
            usage.get(field).cloned().unwrap_or_default(),
        );
    }
    let complete_requests = usage
        .get("completeRequests")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    for field in ["inputTokens", "outputTokens"] {
        let value = usage.get(field).and_then(Value::as_i64).unwrap_or_default();
        result.insert(
            field.to_string(),
            if complete_requests > 0 {
                json!(value)
            } else {
                Value::Null
            },
        );
    }
    let input = usage
        .get("inputTokens")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    let output = usage
        .get("outputTokens")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    result.insert(
        "totalTokens".to_string(),
        if complete_requests > 0 {
            json!(input + output)
        } else {
            Value::Null
        },
    );
    for prefix in ["lowerBound", "estimated"] {
        let mut sides = Vec::new();
        for side in ["Input", "Output"] {
            let field = format!("{prefix}{side}Tokens");
            let known = usage
                .get(&format!("{prefix}{side}Known"))
                .and_then(Value::as_i64)
                .unwrap_or_default();
            let value = usage
                .get(&field)
                .and_then(Value::as_i64)
                .unwrap_or_default();
            result.insert(field, if known > 0 { json!(value) } else { Value::Null });
            sides.push((value, known));
        }
        let total = sides.iter().map(|(value, _)| *value).sum::<i64>();
        let known = sides.iter().any(|(_, known)| *known > 0);
        result.insert(
            format!("{prefix}Tokens"),
            if known { json!(total) } else { Value::Null },
        );
    }
    result.insert(
        "complete".to_string(),
        json!(records > 0 && complete_requests == records),
    );
    let cache_reported = usage
        .get("cacheReportedRequests")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    result.insert(
        "cachedInputTokens".to_string(),
        if cache_reported > 0 {
            usage
                .get("cachedInputTokens")
                .cloned()
                .unwrap_or_else(|| json!(0))
        } else {
            Value::Null
        },
    );
    for field in [
        "cacheReportedRequests",
        "cacheUnreportedRequests",
        "cacheUnknownRequests",
    ] {
        result.insert(
            field.to_string(),
            usage.get(field).cloned().unwrap_or_else(|| json!(0)),
        );
    }
    Value::Object(result)
}

fn finish_summary(entry: &Map<String, Value>) -> Value {
    let mut result = Map::new();
    for field in ["requests", "inputTokens", "outputTokens", "unknownRequests"] {
        result.insert(
            field.to_string(),
            entry.get(field).cloned().unwrap_or_default(),
        );
    }
    let coverage = entry
        .get("coverage")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let records = coverage
        .get("records")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    let token_usage = finish_token_usage(
        entry
            .get("tokenUsage")
            .and_then(Value::as_object)
            .expect("token usage"),
        records,
    );
    let mut coverage = coverage;
    for (source, target) in [
        ("completeRequests", "completeUsageRequests"),
        ("lowerBoundRequests", "partialUsageRequests"),
        ("estimatedRequests", "estimatedUsageRequests"),
        ("unknownRequests", "unknownUsageRequests"),
    ] {
        coverage.insert(
            target.to_string(),
            token_usage.get(source).cloned().unwrap_or_else(|| json!(0)),
        );
    }
    result.insert("coverage".to_string(), Value::Object(coverage));
    result.insert("tokenUsage".to_string(), token_usage);
    Value::Object(result)
}

fn finish_key(bucket: &Map<String, Value>) -> Value {
    let mut models: Vec<&Map<String, Value>> = bucket
        .get("models")
        .and_then(Value::as_object)
        .map(|models| models.values().filter_map(Value::as_object).collect())
        .unwrap_or_default();
    models.sort_by(|left, right| {
        let left_requests = left
            .get("requests")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        let right_requests = right
            .get("requests")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        right_requests.cmp(&left_requests).then_with(|| {
            let left_model = left
                .get("model")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let right_model = right
                .get("model")
                .and_then(Value::as_str)
                .unwrap_or_default();
            left_model.cmp(right_model)
        })
    });
    let mut days: Vec<&String> = bucket
        .get("days")
        .and_then(Value::as_object)
        .map(|days| days.keys().collect())
        .unwrap_or_default();
    days.sort();
    let mut result = Map::new();
    result.insert(
        "key_id".to_string(),
        bucket.get("key_id").cloned().unwrap_or(Value::Null),
    );
    if let Value::Object(summary) = finish_summary(bucket) {
        result.extend(summary);
    }
    result.insert(
        "models".to_string(),
        Value::Array(
            models
                .into_iter()
                .map(|entry| {
                    let mut value = Map::new();
                    value.insert(
                        "model".to_string(),
                        entry.get("model").cloned().unwrap_or(Value::Null),
                    );
                    if let Value::Object(summary) = finish_summary(entry) {
                        value.extend(summary);
                    }
                    Value::Object(value)
                })
                .collect(),
        ),
    );
    result.insert(
        "days".to_string(),
        Value::Array(
            days.into_iter()
                .filter_map(|day| bucket.get("days").and_then(|days| days.get(day.as_str())))
                .filter_map(Value::as_object)
                .map(|entry| {
                    let mut value = Map::new();
                    value.insert(
                        "day".to_string(),
                        entry.get("day").cloned().unwrap_or(Value::Null),
                    );
                    if let Value::Object(summary) = finish_summary(entry) {
                        value.extend(summary);
                    }
                    Value::Object(value)
                })
                .collect(),
        ),
    );
    let undated = bucket.get("undated").and_then(Value::as_object);
    let undated = undated
        .filter(|entry| {
            entry
                .get("coverage")
                .and_then(|coverage| coverage.get("records"))
                .and_then(Value::as_i64)
                .unwrap_or_default()
                > 0
        })
        .map(|entry| {
            let mut value = Map::new();
            value.insert("day".to_string(), Value::Null);
            if let Value::Object(summary) = finish_summary(entry) {
                value.extend(summary);
            }
            Value::Object(value)
        })
        .unwrap_or(Value::Null);
    result.insert("undated".to_string(), undated);
    Value::Object(result)
}

fn finish_results(counts: &Map<String, Value>) -> Value {
    let success = counts
        .get("success")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    let failed = counts
        .get("failed")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    let cancelled = counts
        .get("cancelled")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    let unknown = counts
        .get("unknown")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    json!({
        "success": success,
        "failed": failed,
        "cancelled": cancelled,
        "unknown": unknown,
        "total": success + failed + cancelled + unknown,
        "coverage": {"unknownResult": unknown},
    })
}

fn request_row(record: &Map<String, Value>) -> Value {
    let at_ms = parse_ts(record.get("time"));
    let model = record
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let source = source_of(record);
    let cache_status = cache_report_status(record);
    let request_id = record.get("request_id").and_then(Value::as_str);
    let key_id = record.get("key_id").and_then(Value::as_str);
    let attribution = record.get("attribution").and_then(Value::as_str);
    let status = record.get("status").and_then(Value::as_str);
    let (input_tokens, output_tokens, cache_tokens, reasoning_tokens) = if source == "none" {
        (None, None, None, None)
    } else {
        (
            count(record, &["input_tokens"]),
            count(record, &["output_tokens"]),
            if cache_status == "reported" {
                count(record, &["cached_input_tokens", "cache_tokens"])
            } else {
                None
            },
            count(record, &["reasoning_tokens"]),
        )
    };
    json!({
        "requestId": request_id.filter(|value| !value.is_empty()),
        "time": record.get("time").and_then(Value::as_str),
        "day": beijing_day(at_ms),
        "keyId": key_id.filter(|value| !value.is_empty()),
        "model": model,
        "status": status.filter(|value| !value.is_empty()),
        "result": request_result(record),
        "attribution": attribution.filter(|value| !value.is_empty()),
        "usageSource": source,
        "cacheReportStatus": cache_status,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "durationMs": count(record, &["duration_ms"]),
        "cacheTokens": cache_tokens,
        "reasoningTokens": reasoning_tokens,
    })
}

type NumberedRecords = Vec<(usize, Map<String, Value>)>;

fn read_records_numbered(
    path: &Path,
    limit: usize,
) -> Result<(NumberedRecords, ReadCoverage), ValueError> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default();
    if name == PRIVATE_LEDGER_BASENAME {
        return Err(ValueError(
            "refusing to read the private chat2api ledger; pass the share ".to_string()
                + "backend ledger explicitly",
        ));
    }
    let content = match std::fs::read(path) {
        Ok(content) => content,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok((
                Vec::new(),
                ReadCoverage {
                    ledger_present: false,
                    ledger_readable: false,
                    records_total: 0,
                    unparsed_lines: 0,
                    truncated: false,
                },
            ))
        }
        Err(_) => {
            return Ok((
                Vec::new(),
                ReadCoverage {
                    ledger_present: true,
                    ledger_readable: false,
                    records_total: 0,
                    unparsed_lines: 0,
                    truncated: false,
                },
            ))
        }
    };
    let content = match String::from_utf8(content) {
        Ok(content) => content,
        Err(_) => {
            return Ok((
                Vec::new(),
                ReadCoverage {
                    ledger_present: true,
                    ledger_readable: false,
                    records_total: 0,
                    unparsed_lines: 0,
                    truncated: false,
                },
            ))
        }
    };
    let mut records = Vec::new();
    let mut unparsed = 0;
    for (line_no, line) in content.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(line) {
            Ok(Value::Object(record)) => records.push((line_no + 1, record)),
            _ => unparsed += 1,
        }
    }
    let total = records.len();
    let truncated = total > limit;
    let tail = if limit == 0 {
        Vec::new()
    } else {
        records[total.saturating_sub(limit)..].to_vec()
    };
    Ok((
        tail,
        ReadCoverage {
            ledger_present: true,
            ledger_readable: true,
            records_total: total,
            unparsed_lines: unparsed,
            truncated,
        },
    ))
}

fn coverage_fields(coverage: &ReadCoverage) -> Map<String, Value> {
    let mut fields = Map::new();
    fields.insert("ledgerPresent".to_string(), json!(coverage.ledger_present));
    fields.insert(
        "ledgerReadable".to_string(),
        json!(coverage.ledger_readable),
    );
    fields.insert("recordsTotal".to_string(), json!(coverage.records_total));
    fields.insert("unparsedLines".to_string(), json!(coverage.unparsed_lines));
    fields.insert("truncated".to_string(), json!(coverage.truncated));
    fields
}

fn ledger_basename(path: &Path) -> String {
    path.file_name()
        .and_then(|name| name.to_str())
        .unwrap_or_default()
        .to_string()
}

fn checked_at() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
}

#[derive(Debug, Default, Clone)]
pub struct UsageFilters {
    pub window: Option<String>,
    pub key_id: Option<String>,
    pub model: Option<String>,
    pub result: Option<String>,
}

pub fn read_share_usage(
    ledger_path: &Path,
    filters: &UsageFilters,
    limit: usize,
    now: Option<f64>,
) -> Result<Value, ValueError> {
    let window = filter_value(
        "window",
        Some(filters.window.as_deref().unwrap_or("all")),
        Some(&SHARE_USAGE_WINDOW_NAMES),
        200,
    )?
    .expect("window");
    let key_filter = filter_value("key", filters.key_id.as_deref(), None, 128)?;
    let model_filter = filter_value("model", filters.model.as_deref(), None, 200)?;
    let result_filter = filter_value(
        "result",
        filters.result.as_deref(),
        Some(&SHARE_REQUEST_RESULTS),
        200,
    )?;
    let now_ms = now.map(|now| now * 1000.0).unwrap_or_else(now_ms);
    let cutoff = window_cutoff(&window, now_ms)?;
    let (records, read_coverage) = read_records_numbered(ledger_path, limit)?;
    let scanned_records = records.len();
    let mut excluded_unknown_time = 0;
    let mut filtered = Vec::new();
    for (_line_no, record) in records {
        let at_ms = parse_ts(record.get("time"));
        if !record_matches(
            &record,
            at_ms,
            cutoff,
            key_filter.as_deref(),
            model_filter.as_deref(),
            result_filter.as_deref(),
        ) {
            if cutoff.is_some() && at_ms.is_none() {
                excluded_unknown_time += 1;
            }
            continue;
        }
        filtered.push(record);
    }
    let records = filtered;
    let mut buckets: BTreeMap<Option<String>, Map<String, Value>> = BTreeMap::new();
    let mut counters = Map::new();
    counters.insert("records".to_string(), json!(records.len()));
    counters.insert("recordsScanned".to_string(), json!(scanned_records));
    counters.insert("attributed".to_string(), json!(0));
    counters.insert("unattributed".to_string(), json!(0));
    counters.insert("incompleteRequests".to_string(), json!(0));
    counters.insert("unknownUsageRequests".to_string(), json!(0));
    counters.insert("unknownTimeRequests".to_string(), json!(0));
    counters.insert("attributionReasons".to_string(), json!({}));
    counters.insert("cacheReportedRequests".to_string(), json!(0));
    counters.insert("cacheUnreportedRequests".to_string(), json!(0));
    counters.insert("cacheUnknownRequests".to_string(), json!(0));
    counters.insert(
        "excludedUnknownTimeRequests".to_string(),
        json!(excluded_unknown_time),
    );
    counters.insert(
        "results".to_string(),
        json!({"success": 0, "failed": 0, "cancelled": 0, "unknown": 0}),
    );
    for record in &records {
        let key_id = record
            .get("key_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_string);
        if key_id.is_some() {
            increment(&mut counters, "attributed", 1);
        } else {
            increment(&mut counters, "unattributed", 1);
        }
        let bucket = buckets
            .entry(key_id.clone())
            .or_insert_with(|| empty_key(key_id.as_deref()));
        if key_id.is_none() {
            let reason = record
                .get("attribution")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_string)
                .unwrap_or_else(|| {
                    if record.contains_key("attribution") {
                        "absent".to_string()
                    } else {
                        "legacy".to_string()
                    }
                });
            increment_reasons(&mut counters, &reason, 1);
        }
        if record
            .get("status")
            .and_then(Value::as_str)
            .filter(|status| !status.is_empty())
            .unwrap_or(COMPLETE_STATUS)
            != COMPLETE_STATUS
        {
            increment(&mut counters, "incompleteRequests", 1);
        }
        let result = request_result(record);
        if let Some(results) = counters.get_mut("results").and_then(Value::as_object_mut) {
            increment(results, result, 1);
        }
        let model = record
            .get("model")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .unwrap_or("unknown")
            .to_string();
        let at_ms = parse_ts(record.get("time"));
        if at_ms.is_none() {
            increment(&mut counters, "unknownTimeRequests", 1);
        }
        let source = source_of(record);
        let cache_status = cache_report_status(record);
        match cache_status {
            "reported" => increment(&mut counters, "cacheReportedRequests", 1),
            "unreported" => increment(&mut counters, "cacheUnreportedRequests", 1),
            _ => increment(&mut counters, "cacheUnknownRequests", 1),
        }
        if source == "none" {
            increment(&mut counters, "unknownUsageRequests", 1);
        }
        let day = beijing_day(at_ms);
        {
            let models = bucket
                .entry("models".to_string())
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .expect("models");
            let model_entry = models
                .entry(model.clone())
                .or_insert_with(|| {
                    let mut entry = empty_summary();
                    entry.insert("model".to_string(), json!(model));
                    Value::Object(entry)
                })
                .as_object_mut()
                .expect("model entry");
            add_summary(model_entry, record, source, at_ms, false);
        }
        if let Some(day) = day {
            let days = bucket
                .entry("days".to_string())
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .expect("days");
            let day_entry = days
                .entry(day.clone())
                .or_insert_with(|| {
                    let mut entry = empty_summary();
                    entry.insert("day".to_string(), json!(day));
                    Value::Object(entry)
                })
                .as_object_mut()
                .expect("day entry");
            add_summary(day_entry, record, source, at_ms, false);
        } else {
            let undated = bucket
                .entry("undated".to_string())
                .or_insert_with(|| Value::Object(empty_summary()))
                .as_object_mut()
                .expect("undated");
            add_summary(undated, record, source, at_ms, false);
        }
        add_summary(bucket, record, source, at_ms, true);
    }
    let mut keys: Vec<&Map<String, Value>> = buckets.values().collect();
    keys.sort_by(|left, right| {
        let left_requests = left
            .get("requests")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        let right_requests = right
            .get("requests")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        right_requests.cmp(&left_requests).then_with(|| {
            let left_key = left
                .get("key_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let right_key = right
                .get("key_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            left_key.cmp(right_key)
        })
    });
    let unattributed = buckets
        .get(&None)
        .map(finish_key)
        .unwrap_or_else(|| finish_key(&empty_key(None)));
    let coverage = finish_coverage(&counters, &read_coverage, limit, ledger_path, records.len());
    Ok(json!({
        "requests": records.len(),
        "keys": keys
            .into_iter()
            .filter(|key| key.get("key_id").and_then(Value::as_str).is_some())
            .map(finish_key)
            .collect::<Vec<_>>(),
        "unattributed": unattributed,
        "checkedAt": checked_at(),
        "filters": filter_echo(
            &window,
            key_filter.as_deref(),
            model_filter.as_deref(),
            result_filter.as_deref(),
        ),
        "results": finish_results(
            counters.get("results").and_then(Value::as_object).expect("results")
        ),
        "coverage": coverage,
    }))
}

fn increment_reasons(counters: &mut Map<String, Value>, reason: &str, by: i64) {
    let reasons = counters
        .entry("attributionReasons".to_string())
        .or_insert_with(|| json!({}))
        .as_object_mut()
        .expect("attribution reasons");
    increment(reasons, reason, by);
}

fn finish_coverage(
    counters: &Map<String, Value>,
    read_coverage: &ReadCoverage,
    limit: usize,
    ledger_path: &Path,
    matched_records: usize,
) -> Value {
    let mut result = coverage_fields(read_coverage);
    result.insert("ledger".to_string(), json!(ledger_basename(ledger_path)));
    result.insert("limit".to_string(), json!(limit));
    result.insert(
        "recordsScanned".to_string(),
        counters
            .get("recordsScanned")
            .cloned()
            .unwrap_or_else(|| json!(counters.get("records").cloned().unwrap_or(json!(0)))),
    );
    result.insert("recordsMatched".to_string(), json!(matched_records));
    result.insert(
        "records".to_string(),
        counters.get("records").cloned().unwrap_or_else(|| json!(0)),
    );
    for field in [
        "attributed",
        "unattributed",
        "incompleteRequests",
        "unknownUsageRequests",
        "unknownTimeRequests",
        "excludedUnknownTimeRequests",
        "cacheReportedRequests",
        "cacheUnreportedRequests",
        "cacheUnknownRequests",
    ] {
        result.insert(
            field.to_string(),
            counters.get(field).cloned().unwrap_or_else(|| json!(0)),
        );
    }
    let mut reasons: BTreeMap<String, i64> = BTreeMap::new();
    if let Some(map) = counters
        .get("attributionReasons")
        .and_then(Value::as_object)
    {
        for (key, value) in map {
            if let Some(value) = value.as_i64() {
                reasons.insert(key.clone(), value);
            }
        }
    }
    result.insert("attributionReasons".to_string(), json!(reasons));
    Value::Object(result)
}

pub fn read_share_requests(
    ledger_path: &Path,
    filters: &UsageFilters,
    cursor: Option<&str>,
    limit: i64,
    now: Option<f64>,
) -> Result<Value, ValueError> {
    if !(1..=SHARE_REQUEST_PAGE_MAX).contains(&limit) {
        return Err(ValueError(format!(
            "limit must be an integer in 1..{SHARE_REQUEST_PAGE_MAX}"
        )));
    }
    let window = filter_value(
        "window",
        Some(filters.window.as_deref().unwrap_or("all")),
        Some(&SHARE_USAGE_WINDOW_NAMES),
        200,
    )?
    .expect("window");
    let key_id = filter_value("key", filters.key_id.as_deref(), None, 128)?;
    let model = filter_value("model", filters.model.as_deref(), None, 200)?;
    let result_filter = filter_value(
        "result",
        filters.result.as_deref(),
        Some(&SHARE_REQUEST_RESULTS),
        200,
    )?;
    let cursor_line = match cursor {
        None => None,
        Some(cursor) => {
            if cursor.is_empty() || !cursor.bytes().all(|byte| byte.is_ascii_digit()) {
                return Err(ValueError("invalid cursor".to_string()));
            }
            let line = cursor
                .parse::<usize>()
                .map_err(|_| ValueError("invalid cursor".to_string()))?;
            Some(line)
        }
    };
    let now_ms = now.map(|now| now * 1000.0).unwrap_or_else(now_ms);
    let cutoff = window_cutoff(&window, now_ms)?;
    let (numbered, read_coverage) = read_records_numbered(ledger_path, DEFAULT_LIMIT)?;
    let mut matched = Vec::new();
    let mut excluded_unknown_time = 0;
    for (line_no, record) in numbered.iter().rev() {
        let at_ms = parse_ts(record.get("time"));
        if !record_matches(
            record,
            at_ms,
            cutoff,
            key_id.as_deref(),
            model.as_deref(),
            result_filter.as_deref(),
        ) {
            if cutoff.is_some() && at_ms.is_none() {
                excluded_unknown_time += 1;
            }
            continue;
        }
        if let Some(cursor_line) = cursor_line {
            if *line_no >= cursor_line {
                continue;
            }
        }
        matched.push((*line_no, record));
    }
    let limit_usize = limit as usize;
    let page: Vec<(usize, &Map<String, Value>)> =
        matched.iter().take(limit_usize).cloned().collect();
    let has_more = matched.len() > limit_usize;
    let next_cursor = if has_more && !page.is_empty() {
        Some(page[page.len() - 1].0.to_string())
    } else {
        None
    };
    let mut coverage = coverage_fields(&read_coverage);
    coverage.insert("ledger".to_string(), json!(ledger_basename(ledger_path)));
    coverage.insert("readLimit".to_string(), json!(DEFAULT_LIMIT));
    coverage.insert("limit".to_string(), json!(limit));
    coverage.insert("recordsScanned".to_string(), json!(numbered.len()));
    coverage.insert("recordsMatched".to_string(), json!(matched.len()));
    coverage.insert("recordsReturned".to_string(), json!(page.len()));
    coverage.insert(
        "olderRecordsUnread".to_string(),
        json!(read_coverage.truncated),
    );
    coverage.insert(
        "excludedUnknownTimeRequests".to_string(),
        json!(excluded_unknown_time),
    );
    coverage.insert(
        "filters".to_string(),
        filter_echo(
            &window,
            key_id.as_deref(),
            model.as_deref(),
            result_filter.as_deref(),
        ),
    );
    let page_counters = page_counters(&page);
    for (field, value) in page_counters {
        coverage.insert(field, value);
    }
    let rows: Vec<Value> = page
        .iter()
        .map(|(_line_no, record)| request_row(record))
        .collect();
    Ok(json!({
        "rows": rows,
        "nextCursor": next_cursor,
        "truncated": has_more || read_coverage.truncated,
        "checkedAt": checked_at(),
        "filters": filter_echo(
            &window,
            key_id.as_deref(),
            model.as_deref(),
            result_filter.as_deref(),
        ),
        "coverage": coverage,
    }))
}

fn page_counters(page: &[(usize, &Map<String, Value>)]) -> Map<String, Value> {
    let mut counters = Map::new();
    for field in [
        "attributedRequests",
        "unattributedRequests",
        "unknownUsageRequests",
        "cacheReportedRequests",
        "cacheUnreportedRequests",
        "cacheUnknownRequests",
    ] {
        counters.insert(field.to_string(), json!(0));
    }
    counters.insert("attributionReasons".to_string(), json!({}));
    for (_line_no, record) in page {
        if record
            .get("key_id")
            .and_then(Value::as_str)
            .map(|value| !value.is_empty())
            .unwrap_or(false)
        {
            increment(&mut counters, "attributedRequests", 1);
        } else {
            increment(&mut counters, "unattributedRequests", 1);
            let reason = record
                .get("attribution")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_string)
                .unwrap_or_else(|| {
                    if record.contains_key("attribution") {
                        "absent".to_string()
                    } else {
                        "legacy".to_string()
                    }
                });
            increment_reasons(&mut counters, &reason, 1);
        }
        let source = source_of(record);
        let cache_status = cache_report_status(record);
        match cache_status {
            "reported" => increment(&mut counters, "cacheReportedRequests", 1),
            "unreported" => increment(&mut counters, "cacheUnreportedRequests", 1),
            _ => increment(&mut counters, "cacheUnknownRequests", 1),
        }
        if source == "none" {
            increment(&mut counters, "unknownUsageRequests", 1);
        }
    }
    let mut reasons: BTreeMap<String, i64> = BTreeMap::new();
    if let Some(map) = counters
        .get("attributionReasons")
        .and_then(Value::as_object)
    {
        for (key, value) in map {
            if let Some(value) = value.as_i64() {
                reasons.insert(key.clone(), value);
            }
        }
    }
    counters.insert("attributionReasons".to_string(), json!(reasons));
    counters
}

/// Attach the whitelisted Key names/deleted state used by both usage read
/// faces. A missing/unreadable management store keeps every label `null`.
pub fn attach_key_labels(
    payload: &mut Value,
    rows_key: &str,
    id_key: &str,
    store_path: Option<&PathBuf>,
) {
    let wanted: std::collections::BTreeSet<String> = payload
        .get(rows_key)
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .filter_map(|entry| entry.get(id_key))
                .filter_map(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let labels = if wanted.is_empty() {
        None
    } else {
        store_path.and_then(|path| read_key_labels(path, &wanted).ok())
    };
    if let Some(rows) = payload.get_mut(rows_key).and_then(Value::as_array_mut) {
        for entry in rows {
            let Some(entry) = entry.as_object_mut() else {
                continue;
            };
            let key_id = entry.get(id_key).and_then(Value::as_str);
            let label =
                key_id.and_then(|key_id| labels.as_ref().and_then(|labels| labels.get(key_id)));
            match label {
                None => {
                    entry.insert("keyName".to_string(), Value::Null);
                    entry.insert("deleted".to_string(), Value::Null);
                }
                Some(label) => {
                    entry.insert("keyName".to_string(), json!(label.name));
                    entry.insert("deleted".to_string(), json!(label.deleted));
                }
            }
        }
    }
    if let Some(coverage) = payload.get_mut("coverage").and_then(Value::as_object_mut) {
        coverage.insert(
            "keyNames".to_string(),
            labels
                .as_ref()
                .map(|labels| json!(labels.len()))
                .unwrap_or(Value::Null),
        );
    }
}

/// Test seam: one usage summary from an in-memory ledger.
pub fn usage_summary_for_tests(
    ledger_path: &Path,
    filters: &UsageFilters,
    limit: usize,
    now: Option<f64>,
) -> Result<Value, ValueError> {
    read_share_usage(ledger_path, filters, limit, now)
}

/// Test seam: one request page from an in-memory ledger.
pub fn usage_requests_for_tests(
    ledger_path: &Path,
    filters: &UsageFilters,
    cursor: Option<&str>,
    limit: i64,
    now: Option<f64>,
) -> Result<Value, ValueError> {
    read_share_requests(ledger_path, filters, cursor, limit, now)
}
