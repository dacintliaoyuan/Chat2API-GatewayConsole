//! Local `/api/summary` payload (`usagesrv.py::build_summary`).
//!
//! The local role aggregates the private chat2api ledger and the frozen DSH
//! ledger day snapshot. It never reads the share ledger; share usage has its own
//! read face. A missing or unreadable ledger is Python's legal empty aggregate
//! (`_read_jsonl` returns no records), never a fabricated source row.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use chrono::{DateTime, FixedOffset, NaiveDateTime, TimeZone};
use serde_json::{json, Map, Value};

pub const SUMMARY_CACHE_SECONDS: f64 = 300.0;
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
    paths: LocalSummaryPaths,
    value: std::sync::Mutex<Option<(Value, f64, Instant)>>,
}

impl LocalSummaryCache {
    pub fn new(paths: LocalSummaryPaths) -> Self {
        Self {
            paths,
            value: std::sync::Mutex::new(None),
        }
    }

    pub fn payload(&self) -> Value {
        let now = Instant::now();
        let now_epoch = epoch_seconds();
        if let Ok(cache) = self.value.lock() {
            if let Some((value, at_epoch, at)) = cache.as_ref() {
                if now.duration_since(*at).as_secs_f64() < SUMMARY_CACHE_SECONDS {
                    let mut stamped = value.clone();
                    if let Some(object) = stamped.as_object_mut() {
                        object.insert("_generated_epoch".to_string(), json!(at_epoch));
                        object.insert(
                            "_cached_age_seconds".to_string(),
                            json!(round_one_decimal(now_epoch - at_epoch)),
                        );
                    }
                    return stamped;
                }
            }
        }
        let built = build_local_summary(&self.paths);
        let at = Instant::now();
        if let Ok(mut cache) = self.value.lock() {
            *cache = Some((built.clone(), now_epoch, at));
        }
        let mut stamped = built;
        if let Some(object) = stamped.as_object_mut() {
            object.insert("_generated_epoch".to_string(), json!(now_epoch));
            object.insert("_cached_age_seconds".to_string(), json!(0.0));
        }
        stamped
    }
}

pub fn build_local_summary(paths: &LocalSummaryPaths) -> Value {
    let chat2api = bucket(&read_jsonl(&paths.usage_log), "estimate");
    let dsh = read_dsh_ledger(&paths.dsh_snapshot);
    json!({
        "generatedAt": generated_at(),
        "sources": {
            "chat2apiLog": display_path(&paths.usage_log),
            "dshLedger": display_path(&paths.dsh_snapshot),
        },
        "services": {"chat2api": chat2api, "dsh": dsh},
        "dsh": {"url": DSH_URL, "note": DSH_NOTE},
    })
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
            .and_then(parse_ts);
        record.insert("_ms".to_string(), ms.map_or(Value::Null, |value| json!(value)));
        records.push(record);
    }
    records.sort_by(|left, right| {
        let left = left.get("_ms").and_then(Value::as_f64).unwrap_or(0.0);
        let right = right.get("_ms").and_then(Value::as_f64).unwrap_or(0.0);
        left.partial_cmp(&right).unwrap_or(std::cmp::Ordering::Equal)
    });
    records
}

fn parse_ts(text: &str) -> Option<f64> {
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

fn day_start_ms(day: &str) -> Option<f64> {
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

fn int_value(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(number)) => number.as_i64().unwrap_or(0),
        Some(Value::String(text)) => text.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
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

fn bucket(records: &[Map<String, Value>], kind: &str) -> Value {
    let mut models: BTreeMap<String, Totals> = BTreeMap::new();
    let mut days: BTreeMap<String, Value> = BTreeMap::new();
    let mut total = Totals::default();
    for record in records {
        let ms = record.get("_ms").and_then(Value::as_f64);
        let model = record
            .get("model")
            .map_or_else(|| "unknown".to_string(), stringify);
        let input = int_value(record.get("input_tokens"));
        let output = int_value(record.get("output_tokens"));
        let cache = int_value(
            record
                .get("cached_input_tokens")
                .or_else(|| record.get("cache_tokens")),
        );
        let reasoning = int_value(record.get("reasoning_tokens"));
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
    json!({
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
    })
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

fn read_dsh_ledger(path: &Path) -> Value {
    let snapshot = std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .unwrap_or_else(|| json!({}));
    let days = snapshot
        .get("days")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
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
        let calls = int_value(entry.get("calls"));
        total.requests += calls;
        total.input += int_value(entry.get("input"));
        total.output += int_value(entry.get("output"));
        total.cache += int_value(entry.get("cacheRead"));
        total.reasoning += int_value(entry.get("reasoning"));
        day_totals.insert(
            day.clone(),
            json!({
                "day": day,
                "requests": calls,
                "input": int_value(entry.get("input")),
                "output": int_value(entry.get("output")),
                "cache": int_value(entry.get("cacheRead")),
                "reasoning": int_value(entry.get("reasoning")),
            }),
        );
        if let Some(by_model) = entry.get("byProviderModel").and_then(Value::as_object) {
            for (model, row) in by_model {
                let Some(row) = row.as_object() else {
                    continue;
                };
                let entry = models.entry(model.clone()).or_insert_with(|| Totals {
                    requests: 0,
                    ..Totals::default()
                });
                entry.requests += int_value(row.get("calls"));
                entry.input += int_value(row.get("input"));
                entry.output += int_value(row.get("output"));
                entry.cache += int_value(row.get("cacheRead"));
                entry.reasoning += int_value(row.get("reasoning"));
            }
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
    json!({
        "kind": "dsh-ledger",
        "requests": total.requests,
        "tokens": {"input": total.input, "output": total.output,
                    "cache": total.cache, "reasoning": total.reasoning},
        "models": model_rows,
        "days": day_totals.into_values().collect::<Vec<_>>(),
    })
}
