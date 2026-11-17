//! Local `/api/summary` payload (`usagesrv.py::build_summary`).
//!
//! The local role aggregates the private chat2api ledger and the frozen DSH
//! ledger day snapshot. It never reads the share ledger; share usage has its own
//! read face. A missing or unreadable ledger is Python's legal empty aggregate
//! (`_read_jsonl` returns no records), never a fabricated source row.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, FixedOffset, NaiveDateTime, TimeZone};
use serde_json::{json, Map, Value};

pub const SUMMARY_CACHE_SECONDS: f64 = 300.0;
pub const MAX_SUMMARY_RECORDS: usize = 200_000;
pub const DSH_MERGE_DAY: &str = "2026-09-02";
pub const DSH_URL: &str = "http://127.0.0.1:3080";
pub const DSH_NOTE: &str = "用量为 2026-09-02 账本日快照的原始用量；快照生成方法见 data/dsh-ledger-20260902.json 内 _说明";
pub const USAGE_TOKEN_FIELDS: [&str; 6] = [
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_tokens",
    "reasoning_tokens",
    "total_tokens",
];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalSummaryPaths {
    pub usage_log: PathBuf,
    pub dsh_snapshot: PathBuf,
}

#[derive(Debug)]
pub struct LocalSummaryCache {
    inner: Arc<SummaryCacheInner>,
}

#[derive(Debug)]
struct SummaryCacheInner {
    paths: LocalSummaryPaths,
    cache_seconds: f64,
    state: Mutex<SummaryCacheState>,
}

#[derive(Debug, Default)]
struct SummaryCacheState {
    value: Option<CachedSummary>,
    refreshing: bool,
    refresh_error: Option<String>,
}

#[derive(Debug, Clone)]
struct CachedSummary {
    value: Value,
    at_epoch: f64,
    at: Instant,
}

impl LocalSummaryCache {
    pub fn new(paths: LocalSummaryPaths) -> Self {
        Self::with_cache_seconds(paths, SUMMARY_CACHE_SECONDS)
    }

    /// Test seam: the production cache window is 300 seconds.
    pub fn with_cache_seconds(paths: LocalSummaryPaths, cache_seconds: f64) -> Self {
        Self {
            inner: Arc::new(SummaryCacheInner {
                paths,
                cache_seconds,
                state: Mutex::new(SummaryCacheState::default()),
            }),
        }
    }

    pub fn payload(&self) -> Value {
        let now = Instant::now();
        let cached = {
            let mut state = self.lock();
            let cached = state
                .value
                .as_ref()
                .map(|cached| (cached.value.clone(), cached.at_epoch, cached.at));
            cached.map(|(value, at_epoch, at)| {
                let age = now.duration_since(at).as_secs_f64();
                let mut stamped = value;
                stamp_summary(&mut stamped, at_epoch, age);
                if let Some(error) = &state.refresh_error {
                    if let Some(object) = stamped.as_object_mut() {
                        object.insert("_refresh_error".to_string(), json!(error));
                        object.insert("_stale".to_string(), json!(true));
                    }
                }
                let start_refresh = age >= self.inner.cache_seconds && !state.refreshing;
                if start_refresh {
                    state.refreshing = true;
                }
                (stamped, start_refresh)
            })
        };
        if let Some((stamped, start_refresh)) = cached {
            if start_refresh {
                self.spawn_refresh();
            }
            return stamped;
        }
        self.cold_start()
    }

    fn cold_start(&self) -> Value {
        let Ok(built) = try_build_local_summary(&self.inner.paths) else {
            return json!({"error": "summary unavailable"});
        };
        let at_epoch = epoch_seconds();
        let at = Instant::now();
        let mut state = self.lock();
        if let Some(cached) = state.value.as_ref() {
            let mut stamped = cached.value.clone();
            let age = at.duration_since(cached.at).as_secs_f64();
            stamp_summary(&mut stamped, cached.at_epoch, age);
            return stamped;
        }
        state.value = Some(CachedSummary {
            value: built.clone(),
            at_epoch,
            at,
        });
        state.refresh_error = None;
        let mut stamped = built;
        stamp_summary(&mut stamped, at_epoch, 0.0);
        stamped
    }

    fn spawn_refresh(&self) {
        let inner = Arc::clone(&self.inner);
        std::thread::spawn(move || {
            let built = try_build_local_summary(&inner.paths);
            let mut state = inner
                .state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            match built {
                Ok(value) => {
                    state.value = Some(CachedSummary {
                        value,
                        at_epoch: epoch_seconds(),
                        at: Instant::now(),
                    });
                    state.refresh_error = None;
                }
                Err(_) => {
                    state.refresh_error = Some("summary unavailable".to_string());
                }
            }
            state.refreshing = false;
        });
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, SummaryCacheState> {
        self.inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

fn stamp_summary(value: &mut Value, at_epoch: f64, age: f64) {
    if let Some(object) = value.as_object_mut() {
        object.insert("_generated_epoch".to_string(), json!(at_epoch));
        object.insert(
            "_cached_age_seconds".to_string(),
            json!(round_one_decimal(age)),
        );
    }
}

/// One corrupt ledger/snapshot value; the public summary reports only Python's
/// `{"error": "summary unavailable"}` and never a fabricated zero aggregate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SummaryBuildError {
    detail: String,
}

impl SummaryBuildError {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl std::fmt::Display for SummaryBuildError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.detail)
    }
}

impl std::error::Error for SummaryBuildError {}

pub fn build_local_summary(paths: &LocalSummaryPaths) -> Value {
    match try_build_local_summary(paths) {
        Ok(value) => value,
        Err(_) => json!({"error": "summary unavailable"}),
    }
}

pub fn try_build_local_summary(paths: &LocalSummaryPaths) -> Result<Value, SummaryBuildError> {
    let chat2api = bucket(&read_jsonl(&paths.usage_log), "estimate")?;
    let dsh = read_dsh_ledger(&paths.dsh_snapshot)?;
    Ok(json!({
        "generatedAt": generated_at(),
        "sources": {
            "chat2apiLog": display_path(&paths.usage_log),
            "dshLedger": display_path(&paths.dsh_snapshot),
        },
        "services": {"chat2api": chat2api, "dsh": dsh},
        "dsh": {"url": DSH_URL, "note": DSH_NOTE},
    }))
}

fn generated_at() -> String {
    let offset = FixedOffset::east_opt(8 * 3600).expect("+08:00");
    offset
        .timestamp_opt(chrono::Utc::now().timestamp(), 0)
        .single()
        .expect("local timestamp")
        .format("%Y-%m-%dT%H:%M:%S%:z")
        .to_string()
}

fn epoch_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_secs_f64()
}

fn round_one_decimal(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn display_path(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| "(未配置)".to_string())
}

fn read_jsonl(path: &Path) -> Vec<Map<String, Value>> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    let mut records = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let Ok(Value::Object(mut record)) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let ms = record
            .get("time")
            .and_then(Value::as_str)
            .and_then(parse_ts_ms);
        record.insert("_ms".to_string(), ms.map_or(Value::Null, |value| json!(value)));
        records.push(record);
    }
    records.sort_by(|left, right| {
        let left = left.get("_ms").and_then(Value::as_f64).unwrap_or(0.0);
        let right = right.get("_ms").and_then(Value::as_f64).unwrap_or(0.0);
        left.partial_cmp(&right).unwrap_or(std::cmp::Ordering::Equal)
    });
    if records.len() > MAX_SUMMARY_RECORDS {
        records.drain(..records.len() - MAX_SUMMARY_RECORDS);
    }
    records
}

pub(crate) fn parse_ts_ms(text: &str) -> Option<f64> {
    let text = text.trim();
    if text.is_empty() {
        return None;
    }
    let normalized = text.strip_suffix('Z').map_or_else(
        || text.to_string(),
        |prefix| format!("{prefix}+00:00"),
    );
    if let Ok(parsed) = DateTime::parse_from_rfc3339(&normalized) {
        return Some(parsed.timestamp_millis() as f64);
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%:z",
    ] {
        if let Ok(parsed) = DateTime::parse_from_str(&normalized, format) {
            return Some(parsed.timestamp_millis() as f64);
        }
    }
    for format in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%d %H:%M:%S%.f"] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(&normalized, format) {
            return Some(parsed.and_utc().timestamp_millis() as f64);
        }
    }
    None
}

fn beijing_day(ms: Option<f64>) -> Option<String> {
    let ms = ms?;
    let offset = FixedOffset::east_opt(8 * 3600).expect("+08:00");
    let seconds = (ms / 1000.0).floor() as i64;
    let stamp = offset.timestamp_opt(seconds, 0).single()?;
    Some(stamp.format("%Y-%m-%d").to_string())
}

pub(crate) fn day_start_ms(day: &str) -> Option<f64> {
    let offset = FixedOffset::east_opt(8 * 3600).expect("+08:00");
    let naive = NaiveDateTime::parse_from_str(&format!("{day}T00:00:00"), "%Y-%m-%dT%H:%M:%S")
        .ok()?;
    offset
        .from_local_datetime(&naive)
        .single()
        .map(|stamp| stamp.timestamp_millis() as f64)
}

fn usage_known(record: &Map<String, Value>) -> bool {
    USAGE_TOKEN_FIELDS.iter().any(|name| {
        let Some(value) = record.get(*name) else {
            return false;
        };
        if value.is_boolean() {
            return false;
        }
        match value {
            Value::Number(number) => number.as_f64().is_some_and(|value| !value.is_nan()),
            Value::String(text) => text.trim().parse::<f64>().is_ok(),
            _ => false,
        }
    })
}

fn int_value(value: Option<&Value>) -> Result<i64, SummaryBuildError> {
    let Some(value) = value else {
        return Ok(0);
    };
    match value {
        Value::Null => Ok(0),
        Value::Bool(flag) => Ok(i64::from(*flag)),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                return Ok(value);
            }
            if let Some(value) = number.as_u64() {
                return i64::try_from(value).map_err(|_| {
                    SummaryBuildError::new(format!("token value out of range: {value}"))
                });
            }
            let Some(value) = number.as_f64() else {
                return Err(SummaryBuildError::new("unreadable token number"));
            };
            if !value.is_finite() {
                return Err(SummaryBuildError::new("token value is not finite"));
            }
            let truncated = value.trunc();
            if truncated < i64::MIN as f64 || truncated > i64::MAX as f64 {
                return Err(SummaryBuildError::new(format!(
                    "token value out of range: {value}"
                )));
            }
            if truncated != value {
                return Err(SummaryBuildError::new(format!(
                    "token value is not an integer: {value}"
                )));
            }
            Ok(truncated as i64)
        }
        Value::String(text) => {
            if text.is_empty() {
                return Ok(0);
            }
            parse_python_int(text).ok_or_else(|| {
                SummaryBuildError::new(format!("token value is not an integer: {text:?}"))
            })
        }
        Value::Array(_) | Value::Object(_) => Err(SummaryBuildError::new(format!(
            "token value is not a number: {value}"
        ))),
    }
}

fn parse_python_int(text: &str) -> Option<i64> {
    let text = text.trim();
    if text.is_empty() {
        return None;
    }
    let (sign, digits) = match text.as_bytes().first() {
        Some(b'+') => (1i64, &text[1..]),
        Some(b'-') => (-1i64, &text[1..]),
        _ => (1i64, text),
    };
    if digits.is_empty() {
        return None;
    }
    let mut value: i64 = 0;
    let mut previous_underscore = false;
    for (index, character) in digits.char_indices() {
        if character == '_' {
            if index == 0 || previous_underscore || index + character.len_utf8() == digits.len() {
                return None;
            }
            previous_underscore = true;
            continue;
        }
        let digit = character.to_digit(10)?;
        value = value.checked_mul(10)?.checked_add(i64::from(digit))?;
        previous_underscore = false;
    }
    if previous_underscore {
        return None;
    }
    Some(sign * value)
}

fn cache_report_status(record: &Map<String, Value>) -> &'static str {
    match record.get("cache_report_status").and_then(Value::as_str) {
        Some("reported") => "reported",
        Some("unreported") => "unreported",
        _ => "unknown",
    }
}

#[derive(Default)]
struct Totals {
    requests: i64,
    input: i64,
    output: i64,
    cache: i64,
    reasoning: i64,
    unknown_usage: i64,
    cache_reported: i64,
    cache_unreported: i64,
    cache_unknown: i64,
}

fn bucket(records: &[Map<String, Value>], kind: &str) -> Result<Value, SummaryBuildError> {
    let mut models: BTreeMap<String, Totals> = BTreeMap::new();
    let mut days: BTreeMap<String, Value> = BTreeMap::new();
    let mut total = Totals::default();
    for record in records {
        let ms = record.get("_ms").and_then(Value::as_f64);
        let model = record
            .get("model")
            .map_or_else(|| "unknown".to_string(), stringify);
        let input = int_value(record.get("input_tokens"))?;
        let output = int_value(record.get("output_tokens"))?;
        let cache = int_value(
            record
                .get("cached_input_tokens")
                .or_else(|| record.get("cache_tokens")),
        )?;
        let reasoning = int_value(record.get("reasoning_tokens"))?;
        let status = cache_report_status(record);
        let known = usage_known(record);

        total.requests += 1;
        match status {
            "reported" => total.cache_reported += 1,
            "unreported" => total.cache_unreported += 1,
            _ => total.cache_unknown += 1,
        }
        if known {
            total.input += input;
            total.output += output;
            if status != "unreported" {
                total.cache += cache;
            }
            total.reasoning += reasoning;
        } else {
            total.unknown_usage += 1;
        }

        let entry = models.entry(model.clone()).or_insert_with(|| Totals {
            requests: 0,
            ..Totals::default()
        });
        entry.requests += 1;
        match status {
            "reported" => entry.cache_reported += 1,
            "unreported" => entry.cache_unreported += 1,
            _ => entry.cache_unknown += 1,
        }
        if known {
            entry.input += input;
            entry.output += output;
            if status != "unreported" {
                entry.cache += cache;
            }
            entry.reasoning += reasoning;
        } else {
            entry.unknown_usage += 1;
        }

        if let Some(day) = beijing_day(ms) {
            let row = days.entry(day.clone()).or_insert_with(|| {
                json!({"day": day, "requests": 0, "input": 0, "output": 0, "cache": 0, "reasoning": 0})
            });
            increment(row, "requests", 1);
            increment(row, "input", input);
            increment(row, "output", output);
            increment(row, "cache", cache);
            increment(row, "reasoning", reasoning);
        }
    }
    let mut model_rows: Vec<(String, Totals)> = models.into_iter().collect();
    model_rows.sort_by(|(left_name, left), (right_name, right)| {
        right
            .requests
            .cmp(&left.requests)
            .then_with(|| left_name.cmp(right_name))
    });
    let model_rows: Vec<Value> = model_rows
        .into_iter()
        .map(|(model, totals)| {
            json!({
                "model": model,
                "requests": totals.requests,
                "input": totals.input,
                "output": totals.output,
                "cache": totals.cache,
                "reasoning": totals.reasoning,
                "unknown_usage": totals.unknown_usage,
                "cache_reported": totals.cache_reported,
                "cache_unreported": totals.cache_unreported,
                "cache_unknown": totals.cache_unknown,
            })
        })
        .collect();
    Ok(json!({
        "kind": kind,
        "requests": total.requests,
        "tokens": {"input": total.input, "output": total.output,
                    "cache": total.cache, "reasoning": total.reasoning},
        "cacheReportedRequests": total.cache_reported,
        "cacheUnreportedRequests": total.cache_unreported,
        "cacheUnknownRequests": total.cache_unknown,
        "unknownUsageRequests": total.unknown_usage,
        "models": model_rows,
        "days": days.into_values().collect::<Vec<_>>(),
    }))
}

fn increment(object: &mut Value, field: &str, by: i64) {
    if let Some(value) = object.get_mut(field) {
        let current = value.as_i64().unwrap_or(0);
        *value = json!(current + by);
    }
}

fn stringify(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "unknown".to_string(),
        other => other.to_string(),
    }
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn python_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Python's `snapshot.get(key) or {}` shape: a missing/null/falsy value is the
/// legal empty map; a truthy non-object value is a corrupt snapshot, never an
/// empty aggregate.
fn snapshot_object(
    value: Option<&Value>,
    name: &str,
) -> Result<Map<String, Value>, SummaryBuildError> {
    match value {
        None | Some(Value::Null) => Ok(Map::new()),
        Some(Value::Object(map)) => Ok(map.clone()),
        Some(other) if !python_truthy(other) => Ok(Map::new()),
        Some(other) => Err(SummaryBuildError::new(format!(
            "snapshot {name} is not a JSON object: {}",
            python_type_name(other)
        ))),
    }
}

fn read_dsh_ledger(path: &Path) -> Result<Value, SummaryBuildError> {
    let snapshot = std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .unwrap_or_else(|| json!({}));
    let root = snapshot_object(Some(&snapshot), "root")?;
    let days = snapshot_object(root.get("days"), "days")?;
    let mut models: BTreeMap<String, Totals> = BTreeMap::new();
    let mut day_totals: BTreeMap<String, Value> = BTreeMap::new();
    let mut total = Totals::default();
    for (day, entry) in days {
        if day != DSH_MERGE_DAY {
            continue;
        }
        let Some(entry) = entry.as_object() else {
            continue;
        };
        let calls = int_value(entry.get("calls"))?;
        let input = int_value(entry.get("input"))?;
        let output = int_value(entry.get("output"))?;
        let cache = int_value(entry.get("cacheRead"))?;
        let reasoning = int_value(entry.get("reasoning"))?;
        total.requests += calls;
        total.input += input;
        total.output += output;
        total.cache += cache;
        total.reasoning += reasoning;
        day_totals.insert(
            day.clone(),
            json!({
                "day": day,
                "requests": calls,
                "input": input,
                "output": output,
                "cache": cache,
                "reasoning": reasoning,
            }),
        );
        let by_model = snapshot_object(entry.get("byProviderModel"), "byProviderModel")?;
        for (model, row) in by_model {
            let Some(row) = row.as_object() else {
                continue;
            };
            let entry = models.entry(model.clone()).or_insert_with(|| Totals {
                requests: 0,
                ..Totals::default()
            });
            entry.requests += int_value(row.get("calls"))?;
            entry.input += int_value(row.get("input"))?;
            entry.output += int_value(row.get("output"))?;
            entry.cache += int_value(row.get("cacheRead"))?;
            entry.reasoning += int_value(row.get("reasoning"))?;
        }
    }
    let mut model_rows: Vec<(String, Totals)> = models.into_iter().collect();
    model_rows.sort_by(|(left_name, left), (right_name, right)| {
        right
            .requests
            .cmp(&left.requests)
            .then_with(|| left_name.cmp(right_name))
    });
    let model_rows: Vec<Value> = model_rows
        .into_iter()
        .map(|(model, totals)| {
            json!({
                "model": model,
                "requests": totals.requests,
                "input": totals.input,
                "output": totals.output,
                "cache": totals.cache,
                "reasoning": totals.reasoning,
            })
        })
        .collect();
    Ok(json!({
        "kind": "dsh-ledger",
        "requests": total.requests,
        "tokens": {"input": total.input, "output": total.output,
                    "cache": total.cache, "reasoning": total.reasoning},
        "models": model_rows,
        "days": day_totals.into_values().collect::<Vec<_>>(),
    }))
}
