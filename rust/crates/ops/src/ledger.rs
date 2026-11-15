//! Read-only parity for the share usage ledger (`share/metering.py`).
//!
//! The Rust reader opens exactly the caller-provided ledger, never the private
//! `usage-chat2api.jsonl`, and never writes, creates or truncates it. Missing
//! evidence stays `null`; unknown usage is not folded into a measured zero.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::path::Path;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, TimeZone, Utc};
use serde_json::{Map, Number, Value};

pub const DEFAULT_LIMIT: i64 = 200000;
pub const REQUEST_PAGE_DEFAULT: i64 = 50;
pub const REQUEST_PAGE_MAX: i64 = 200;
pub const PRIVATE_LEDGER_BASENAMES: [&str; 1] = ["usage-chat2api.jsonl"];
pub const USAGE_SOURCES: [&str; 4] = ["upstream", "partial", "estimated", "none"];
pub const CACHE_REPORT_STATUSES: [&str; 2] = ["reported", "unreported"];
pub const COMPLETE_STATUS: &str = "ok";
pub const UNATTRIBUTED_FILTER: &str = "unattributed";
pub const SHARE_REQUEST_RESULTS: [&str; 4] = ["success", "failed", "cancelled", "unknown"];

const BEIJING_OFFSET_SECONDS: i32 = 8 * 3600;

/// The clock seam used by the read surfaces. Production uses [`SystemClock`];
/// tests inject a fixed clock so `checkedAt` and relative windows are stable.
pub trait Clock {
    fn now_ms(&self) -> f64;
    fn utc_iso_seconds(&self) -> String;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> f64 {
        Utc::now().timestamp_millis() as f64
    }

    fn utc_iso_seconds(&self) -> String {
        Utc::now().format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    }
}

#[derive(Debug, Clone)]
pub struct UsageOptions {
    pub limit: i64,
    pub window: Option<String>,
    pub key_id: Option<String>,
    pub model: Option<String>,
    pub result: Option<String>,
}

impl Default for UsageOptions {
    fn default() -> Self {
        Self {
            limit: DEFAULT_LIMIT,
            window: None,
            key_id: None,
            model: None,
            result: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct RequestOptions {
    pub limit: i64,
    pub cursor: Option<String>,
    pub window: Option<String>,
    pub key_id: Option<String>,
    pub model: Option<String>,
    pub result: Option<String>,
}

impl Default for RequestOptions {
    fn default() -> Self {
        Self {
            limit: REQUEST_PAGE_DEFAULT,
            cursor: None,
            window: None,
            key_id: None,
            model: None,
            result: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerError {
    message: String,
}

impl LedgerError {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for LedgerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for LedgerError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ReadCoverage {
    ledger_present: bool,
    ledger_readable: bool,
    records_total: usize,
    unparsed_lines: usize,
    truncated: bool,
}

#[derive(Debug, Clone, Default)]
struct TokenUsage {
    complete_requests: i64,
    lower_bound_requests: i64,
    estimated_requests: i64,
    unknown_requests: i64,
    input_tokens: i64,
    output_tokens: i64,
    lower_bound_input_tokens: i64,
    lower_bound_output_tokens: i64,
    estimated_input_tokens: i64,
    estimated_output_tokens: i64,
    lower_bound_input_known: i64,
    lower_bound_output_known: i64,
    estimated_input_known: i64,
    estimated_output_known: i64,
    cached_input_tokens: i64,
    cache_reported_requests: i64,
    cache_unreported_requests: i64,
    cache_unknown_requests: i64,
}

#[derive(Debug, Clone, Default)]
struct Summary {
    requests: i64,
    unknown_requests: i64,
    input_tokens: i64,
    output_tokens: i64,
    coverage: CoverageSummary,
    token_usage: TokenUsage,
}

#[derive(Debug, Clone, Default)]
struct CoverageSummary {
    records: i64,
    unknown_time_requests: i64,
    cache_reported_requests: i64,
    cache_unreported_requests: i64,
    cache_unknown_requests: i64,
}

#[derive(Debug, Clone)]
struct ModelEntry {
    model: String,
    summary: Summary,
}

#[derive(Debug, Clone)]
struct KeyBucket {
    key_id: Option<String>,
    summary: Summary,
    models: BTreeMap<String, Summary>,
    days: BTreeMap<String, Summary>,
    undated: Summary,
}

impl KeyBucket {
    fn new(key_id: Option<String>) -> Self {
        Self {
            key_id,
            summary: Summary::default(),
            models: BTreeMap::new(),
            days: BTreeMap::new(),
            undated: Summary::default(),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Source {
    Upstream,
    Partial,
    Estimated,
    None,
}

impl Source {
    fn as_str(self) -> &'static str {
        match self {
            Source::Upstream => "upstream",
            Source::Partial => "partial",
            Source::Estimated => "estimated",
            Source::None => "none",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CacheReport {
    Reported,
    Unreported,
    Unknown,
}

impl CacheReport {
    fn as_str(self) -> &'static str {
        match self {
            CacheReport::Reported => "reported",
            CacheReport::Unreported => "unreported",
            CacheReport::Unknown => "unknown",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RequestResult {
    Success,
    Failed,
    Cancelled,
    Unknown,
}

impl RequestResult {
    fn as_str(self) -> &'static str {
        match self {
            RequestResult::Success => "success",
            RequestResult::Failed => "failed",
            RequestResult::Cancelled => "cancelled",
            RequestResult::Unknown => "unknown",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Window {
    OneHour,
    Today,
    TwentyFourHours,
    SevenDays,
    ThirtyDays,
    All,
}

impl Window {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "1h" => Some(Window::OneHour),
            "today" => Some(Window::Today),
            "24h" => Some(Window::TwentyFourHours),
            "7d" => Some(Window::SevenDays),
            "30d" => Some(Window::ThirtyDays),
            "all" => Some(Window::All),
            _ => None,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Window::OneHour => "1h",
            Window::Today => "today",
            Window::TwentyFourHours => "24h",
            Window::SevenDays => "7d",
            Window::ThirtyDays => "30d",
            Window::All => "all",
        }
    }
}

/// Read a bounded summary of one share ledger.
pub fn read_share_usage(
    path: &Path,
    options: &UsageOptions,
    clock: &dyn Clock,
) -> Result<Value, LedgerError> {
    if options.limit < 0 {
        return Err(LedgerError::new("limit must be a non-negative integer"));
    }
    refuse_private_ledger(path)?;
    let window = parse_window(options.window.as_deref())?;
    let key_filter = filter_value("key", options.key_id.as_deref(), None, 128)?;
    let model_filter = filter_value("model", options.model.as_deref(), None, 200)?;
    let result_filter = filter_value(
        "result",
        options.result.as_deref(),
        Some(&SHARE_REQUEST_RESULTS),
        200,
    )?;
    let now_ms = clock.now_ms();
    let cutoff = window_cutoff(window, now_ms);
    let (records, read_coverage) = read_records(path, options.limit);
    let scanned_records = records.len();
    let mut filtered = Vec::new();
    let mut excluded_unknown_time = 0i64;
    for record in &records {
        let at_ms = parse_ts(record.get("time"));
        if !record_matches(
            record,
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
        filtered.push(record.clone());
    }

    let mut buckets: BTreeMap<Option<String>, KeyBucket> = BTreeMap::new();
    let mut counters = Counters {
        records: filtered.len(),
        records_scanned: scanned_records,
        excluded_unknown_time_requests: excluded_unknown_time,
        ..Counters::default()
    };
    for record in &filtered {
        let key_id = record
            .get("key_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned);
        if key_id.is_some() {
            counters.attributed += 1;
        } else {
            counters.unattributed += 1;
        }
        let bucket = buckets
            .entry(key_id.clone())
            .or_insert_with(|| KeyBucket::new(key_id.clone()));
        if key_id.is_none() {
            let reason = record
                .get("attribution")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    if record.contains_key("attribution") {
                        "absent".to_string()
                    } else {
                        "legacy".to_string()
                    }
                });
            *counters.attribution_reasons.entry(reason).or_insert(0) += 1;
        }
        if status_or_complete(record) != COMPLETE_STATUS {
            counters.incomplete_requests += 1;
        }
        *counters
            .results
            .entry(request_result(record).as_str())
            .or_insert(0) += 1;
        let model = model_of(record);
        let at_ms = parse_ts(record.get("time"));
        if at_ms.is_none() {
            counters.unknown_time_requests += 1;
        }
        let source = source_of(record);
        let cache_status = cache_report_status(record);
        match cache_status {
            CacheReport::Reported => counters.cache_reported_requests += 1,
            CacheReport::Unreported => counters.cache_unreported_requests += 1,
            CacheReport::Unknown => counters.cache_unknown_requests += 1,
        }
        if source == Source::None {
            counters.unknown_usage_requests += 1;
        }
        let day = beijing_day(at_ms);
        let model_summary = bucket.models.entry(model.clone()).or_default();
        let day_summary = match day {
            Some(day) => bucket.days.entry(day).or_default(),
            None => &mut bucket.undated,
        };
        add_summary(&mut bucket.summary, record, source, at_ms, true);
        add_summary(model_summary, record, source, at_ms, false);
        add_summary(day_summary, record, source, at_ms, false);
    }

    let mut keys: Vec<&KeyBucket> = buckets
        .values()
        .filter(|bucket| bucket.key_id.is_some())
        .collect();
    keys.sort_by(|left, right| {
        right
            .summary
            .requests
            .cmp(&left.summary.requests)
            .then_with(|| key_id_or_empty(left).cmp(key_id_or_empty(right)))
    });
    let keys: Vec<Value> = keys.into_iter().map(finish_key).collect();
    let unattributed = buckets
        .get(&None)
        .cloned()
        .unwrap_or_else(|| KeyBucket::new(None));

    let mut output = Map::new();
    output.insert("requests".to_string(), json_int(filtered.len() as i64));
    output.insert("keys".to_string(), Value::Array(keys));
    output.insert("unattributed".to_string(), finish_key(&unattributed));
    output.insert(
        "checkedAt".to_string(),
        Value::String(clock.utc_iso_seconds()),
    );
    output.insert(
        "filters".to_string(),
        filters(
            window,
            key_filter.as_deref(),
            model_filter.as_deref(),
            result_filter.as_deref(),
        ),
    );
    output.insert("results".to_string(), finish_results(&counters.results));
    output.insert(
        "coverage".to_string(),
        finish_coverage(
            &counters,
            read_coverage,
            options.limit,
            path,
            filtered.len(),
        ),
    );
    Ok(Value::Object(output))
}

/// Read one bounded, newest-first page of individual share requests.
pub fn read_share_requests(
    path: &Path,
    options: &RequestOptions,
    clock: &dyn Clock,
) -> Result<Value, LedgerError> {
    if options.limit < 1 || options.limit > REQUEST_PAGE_MAX {
        return Err(LedgerError::new(format!(
            "limit must be an integer in 1..{REQUEST_PAGE_MAX}"
        )));
    }
    refuse_private_ledger(path)?;
    let window = parse_window(options.window.as_deref())?;
    let key_filter = filter_value("key", options.key_id.as_deref(), None, 128)?;
    let model_filter = filter_value("model", options.model.as_deref(), None, 200)?;
    let result_filter = filter_value(
        "result",
        options.result.as_deref(),
        Some(&SHARE_REQUEST_RESULTS),
        200,
    )?;
    let cursor_line = match options.cursor.as_deref() {
        Some(cursor) => {
            if cursor.is_empty() || !cursor.chars().all(|character| character.is_numeric()) {
                return Err(LedgerError::new("invalid cursor"));
            }
            match cursor.parse::<i64>() {
                Ok(line) if line >= 0 => Some(line),
                _ => return Err(LedgerError::new("invalid cursor")),
            }
        }
        None => None,
    };
    let now_ms = clock.now_ms();
    let cutoff = window_cutoff(window, now_ms);
    let (numbered, read_coverage) = read_records_numbered(path, DEFAULT_LIMIT);
    let mut matched = Vec::new();
    let mut excluded_unknown_time = 0i64;
    for (line_no, record) in numbered.iter().rev() {
        let at_ms = parse_ts(record.get("time"));
        if !record_matches(
            record,
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
        if cursor_line.is_some_and(|cursor| *line_no as i64 >= cursor) {
            continue;
        }
        matched.push((*line_no, record));
    }
    let limit = options.limit as usize;
    let page: Vec<(usize, &Map<String, Value>)> = matched.iter().take(limit).copied().collect();
    let has_more = matched.len() > limit;
    let next_cursor = if has_more && !page.is_empty() {
        Some(page[page.len() - 1].0.to_string())
    } else {
        None
    };
    let page_counters = page_counters(&page);
    let mut coverage = Map::new();
    coverage.insert(
        "ledgerPresent".to_string(),
        Value::Bool(read_coverage.ledger_present),
    );
    coverage.insert(
        "ledgerReadable".to_string(),
        Value::Bool(read_coverage.ledger_readable),
    );
    coverage.insert(
        "recordsTotal".to_string(),
        json_int(read_coverage.records_total as i64),
    );
    coverage.insert(
        "unparsedLines".to_string(),
        json_int(read_coverage.unparsed_lines as i64),
    );
    coverage.insert(
        "truncated".to_string(),
        Value::Bool(read_coverage.truncated),
    );
    coverage.insert("ledger".to_string(), Value::String(ledger_name(path)));
    coverage.insert("readLimit".to_string(), json_int(DEFAULT_LIMIT));
    coverage.insert("limit".to_string(), json_int(options.limit));
    coverage.insert(
        "recordsScanned".to_string(),
        json_int(numbered.len() as i64),
    );
    coverage.insert("recordsMatched".to_string(), json_int(matched.len() as i64));
    coverage.insert("recordsReturned".to_string(), json_int(page.len() as i64));
    coverage.insert(
        "olderRecordsUnread".to_string(),
        Value::Bool(read_coverage.truncated),
    );
    coverage.insert(
        "excludedUnknownTimeRequests".to_string(),
        json_int(excluded_unknown_time),
    );
    coverage.insert(
        "filters".to_string(),
        filters(
            window,
            key_filter.as_deref(),
            model_filter.as_deref(),
            result_filter.as_deref(),
        ),
    );
    for (key, value) in page_counters {
        coverage.insert(key, value);
    }

    let rows: Vec<Value> = page.iter().map(|(_, record)| request_row(record)).collect();
    let mut output = Map::new();
    output.insert("rows".to_string(), Value::Array(rows));
    output.insert(
        "nextCursor".to_string(),
        next_cursor.map(Value::String).unwrap_or(Value::Null),
    );
    output.insert(
        "truncated".to_string(),
        Value::Bool(has_more || read_coverage.truncated),
    );
    output.insert(
        "checkedAt".to_string(),
        Value::String(clock.utc_iso_seconds()),
    );
    output.insert(
        "filters".to_string(),
        filters(
            window,
            key_filter.as_deref(),
            model_filter.as_deref(),
            result_filter.as_deref(),
        ),
    );
    output.insert("coverage".to_string(), Value::Object(coverage));
    Ok(Value::Object(output))
}

#[derive(Debug, Clone, Default)]
struct Counters {
    records: usize,
    records_scanned: usize,
    attributed: i64,
    unattributed: i64,
    incomplete_requests: i64,
    unknown_usage_requests: i64,
    unknown_time_requests: i64,
    attribution_reasons: BTreeMap<String, i64>,
    cache_reported_requests: i64,
    cache_unreported_requests: i64,
    cache_unknown_requests: i64,
    excluded_unknown_time_requests: i64,
    results: BTreeMap<&'static str, i64>,
}

fn parse_window(value: Option<&str>) -> Result<Window, LedgerError> {
    let value = match value {
        None => return Ok(Window::All),
        Some("") => return Ok(Window::All),
        Some(value) => value,
    };
    Window::parse(value).ok_or_else(|| LedgerError::new("invalid window filter"))
}

fn filter_value(
    name: &str,
    value: Option<&str>,
    allowed: Option<&[&str]>,
    max_length: usize,
) -> Result<Option<String>, LedgerError> {
    let value = match value {
        None => return Ok(None),
        Some(value) => value,
    };
    if value.is_empty()
        || value.chars().count() > max_length
        || allowed.is_some_and(|allowed| !allowed.contains(&value))
    {
        return Err(LedgerError::new(format!("invalid {name} filter")));
    }
    Ok(Some(value.to_string()))
}

fn window_cutoff(window: Window, now_ms: f64) -> Option<f64> {
    match window {
        Window::All => None,
        Window::Today => {
            let offset = FixedOffset::east_opt(BEIJING_OFFSET_SECONDS)?;
            let local = offset.timestamp_millis_opt(now_ms as i64).single()?;
            let midnight = local.date_naive().and_hms_opt(0, 0, 0)?;
            Some(
                offset
                    .from_local_datetime(&midnight)
                    .single()?
                    .timestamp_millis() as f64,
            )
        }
        Window::OneHour => Some(now_ms - 3600.0 * 1000.0),
        Window::TwentyFourHours => Some(now_ms - 86_400.0 * 1000.0),
        Window::SevenDays => Some(now_ms - 7.0 * 86_400.0 * 1000.0),
        Window::ThirtyDays => Some(now_ms - 30.0 * 86_400.0 * 1000.0),
    }
}

fn filters(
    window: Window,
    key_id: Option<&str>,
    model: Option<&str>,
    result: Option<&str>,
) -> Value {
    let mut output = Map::new();
    output.insert(
        "window".to_string(),
        Value::String(window.as_str().to_string()),
    );
    output.insert(
        "key".to_string(),
        key_id
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "model".to_string(),
        model
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "result".to_string(),
        result
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    Value::Object(output)
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
        let record_key = record.get("key_id").and_then(Value::as_str);
        if key_id == UNATTRIBUTED_FILTER {
            if record_key.is_some_and(|value| !value.is_empty()) {
                return false;
            }
        } else if record_key != Some(key_id) {
            return false;
        }
    }
    if let Some(model) = model {
        if model_of(record) != model {
            return false;
        }
    }
    if let Some(result) = result {
        if request_result(record).as_str() != result {
            return false;
        }
    }
    true
}

fn parse_ts(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    let text = value.as_str()?.trim();
    if text.is_empty() {
        return None;
    }
    let normalized = if let Some(stripped) = text.strip_suffix('Z') {
        format!("{stripped}+00:00")
    } else {
        text.to_string()
    };
    if let Ok(parsed) = DateTime::parse_from_rfc3339(&normalized) {
        return Some(parsed.timestamp_millis() as f64);
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f%:z",
        "%Y-%m-%d %H:%M:%S%.f%:z",
        "%Y-%m-%dT%H:%M%.f%:z",
        "%Y-%m-%d %H:%M%.f%:z",
        "%Y-%m-%dT%H:%M:%S%.f%z",
        "%Y-%m-%d %H:%M:%S%.f%z",
    ] {
        if let Ok(parsed) = DateTime::parse_from_str(&normalized, format) {
            return Some(parsed.timestamp_millis() as f64);
        }
    }
    for format in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ] {
        if let Ok(parsed) = NaiveDateTime::parse_from_str(&normalized, format) {
            return Some(parsed.and_utc().timestamp_millis() as f64);
        }
    }
    NaiveDate::parse_from_str(&normalized, "%Y-%m-%d")
        .ok()
        .and_then(|date| date.and_hms_opt(0, 0, 0))
        .map(|parsed| parsed.and_utc().timestamp_millis() as f64)
}

fn beijing_day(ms: Option<f64>) -> Option<String> {
    let ms = ms?;
    let offset = FixedOffset::east_opt(BEIJING_OFFSET_SECONDS)?;
    let local = offset.timestamp_millis_opt(ms as i64).single()?;
    Some(local.format("%Y-%m-%d").to_string())
}

fn count(record: &Map<String, Value>, names: &[&str]) -> Option<i64> {
    for name in names {
        let Some(value) = record.get(*name) else {
            continue;
        };
        match value {
            Value::Bool(_) => continue,
            Value::Number(number) => {
                if let Some(value) = number.as_i64() {
                    if value >= 0 {
                        return Some(value);
                    }
                } else if let Some(value) = number.as_f64() {
                    if value.is_finite() && value >= 0.0 {
                        return Some(value as i64);
                    }
                }
            }
            _ => {}
        }
    }
    None
}

fn trusted_count(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Number(number)) => number.as_i64().filter(|value| *value >= 0),
        _ => None,
    }
}

fn source_of(record: &Map<String, Value>) -> Source {
    let source = match record.get("usage_source").and_then(Value::as_str) {
        Some(value) if USAGE_SOURCES.contains(&value) => match value {
            "upstream" => Source::Upstream,
            "partial" => Source::Partial,
            "estimated" => Source::Estimated,
            "none" => Source::None,
            _ => Source::None,
        },
        _ => match record.get("estimated").and_then(Value::as_bool) {
            Some(true) => Source::Estimated,
            Some(false) => Source::Upstream,
            None => return Source::None,
        },
    };
    if source != Source::None
        && count(record, &["input_tokens", "output_tokens", "total_tokens"]).is_none()
    {
        return Source::None;
    }
    source
}

fn cache_report_status(record: &Map<String, Value>) -> CacheReport {
    match record.get("cache_report_status").and_then(Value::as_str) {
        Some("reported") => CacheReport::Reported,
        Some("unreported") => CacheReport::Unreported,
        _ => CacheReport::Unknown,
    }
}

fn request_result(record: &Map<String, Value>) -> RequestResult {
    let status = match record.get("status").and_then(Value::as_str) {
        Some(status) if !status.is_empty() => status,
        _ => return RequestResult::Unknown,
    };
    if status == COMPLETE_STATUS {
        RequestResult::Success
    } else if matches!(status, "client_disconnected" | "cancelled" | "canceled") {
        RequestResult::Cancelled
    } else {
        RequestResult::Failed
    }
}

fn status_or_complete(record: &Map<String, Value>) -> String {
    match record.get("status") {
        Some(Value::String(status)) if !status.is_empty() => status.clone(),
        Some(value) if is_truthy(value) => python_str(value),
        _ => COMPLETE_STATUS.to_string(),
    }
}

fn model_of(record: &Map<String, Value>) -> String {
    match record.get("model") {
        Some(Value::String(model)) if !model.is_empty() => model.clone(),
        Some(value) if is_truthy(value) => python_str(value),
        _ => "unknown".to_string(),
    }
}

fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn python_str(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => number.to_string(),
        Value::Null => "None".to_string(),
        _ => value.to_string(),
    }
}

fn add_summary(
    entry: &mut Summary,
    record: &Map<String, Value>,
    source: Source,
    at_ms: Option<f64>,
    include_unknown_request: bool,
) {
    if source != Source::None || include_unknown_request {
        entry.requests += 1;
    }
    entry.coverage.records += 1;
    entry.coverage.unknown_time_requests += i64::from(at_ms.is_none());
    match cache_report_status(record) {
        CacheReport::Reported => entry.coverage.cache_reported_requests += 1,
        CacheReport::Unreported => entry.coverage.cache_unreported_requests += 1,
        CacheReport::Unknown => entry.coverage.cache_unknown_requests += 1,
    }
    accumulate_token_usage(&mut entry.token_usage, record, source);
    if source == Source::None {
        entry.unknown_requests += 1;
        return;
    }
    if let Some(value) = count(record, &["input_tokens"]) {
        entry.input_tokens += value;
    }
    if let Some(value) = count(record, &["output_tokens"]) {
        entry.output_tokens += value;
    }
}

fn accumulate_token_usage(usage: &mut TokenUsage, record: &Map<String, Value>, source: Source) {
    match cache_report_status(record) {
        CacheReport::Reported => {
            match trusted_count(
                record
                    .get("cached_input_tokens")
                    .or_else(|| record.get("cache_tokens")),
            ) {
                Some(cached) => {
                    usage.cache_reported_requests += 1;
                    usage.cached_input_tokens += cached;
                }
                None => usage.cache_unknown_requests += 1,
            }
        }
        CacheReport::Unreported => usage.cache_unreported_requests += 1,
        CacheReport::Unknown => usage.cache_unknown_requests += 1,
    }
    let input_tokens = trusted_count(record.get("input_tokens"));
    let output_tokens = trusted_count(record.get("output_tokens"));
    if source == Source::None || (input_tokens.is_none() && output_tokens.is_none()) {
        usage.unknown_requests += 1;
        return;
    }
    let complete = input_tokens.is_some() && output_tokens.is_some();
    if source == Source::Upstream && complete && status_or_complete(record) == COMPLETE_STATUS {
        usage.complete_requests += 1;
        usage.input_tokens += input_tokens.unwrap_or(0);
        usage.output_tokens += output_tokens.unwrap_or(0);
        return;
    }
    if source == Source::Estimated {
        usage.estimated_requests += 1;
        if let Some(value) = input_tokens {
            usage.estimated_input_tokens += value;
            usage.estimated_input_known += 1;
        }
        if let Some(value) = output_tokens {
            usage.estimated_output_tokens += value;
            usage.estimated_output_known += 1;
        }
    } else {
        usage.lower_bound_requests += 1;
        if let Some(value) = input_tokens {
            usage.lower_bound_input_tokens += value;
            usage.lower_bound_input_known += 1;
        }
        if let Some(value) = output_tokens {
            usage.lower_bound_output_tokens += value;
            usage.lower_bound_output_known += 1;
        }
    }
}

fn finish_token_usage(usage: &TokenUsage, records: i64) -> Value {
    let mut output = Map::new();
    output.insert(
        "completeRequests".to_string(),
        json_int(usage.complete_requests),
    );
    output.insert(
        "lowerBoundRequests".to_string(),
        json_int(usage.lower_bound_requests),
    );
    output.insert(
        "estimatedRequests".to_string(),
        json_int(usage.estimated_requests),
    );
    output.insert(
        "unknownRequests".to_string(),
        json_int(usage.unknown_requests),
    );
    output.insert(
        "inputTokens".to_string(),
        optional_int(usage.input_tokens, usage.complete_requests > 0),
    );
    output.insert(
        "outputTokens".to_string(),
        optional_int(usage.output_tokens, usage.complete_requests > 0),
    );
    output.insert(
        "totalTokens".to_string(),
        optional_int(
            usage.input_tokens + usage.output_tokens,
            usage.complete_requests > 0,
        ),
    );
    for (
        prefix,
        input_tokens,
        output_tokens,
        input_known,
        output_known,
        total_tokens,
        total_known,
    ) in [
        (
            "lowerBound",
            usage.lower_bound_input_tokens,
            usage.lower_bound_output_tokens,
            usage.lower_bound_input_known,
            usage.lower_bound_output_known,
            usage.lower_bound_input_tokens + usage.lower_bound_output_tokens,
            usage.lower_bound_input_known > 0 || usage.lower_bound_output_known > 0,
        ),
        (
            "estimated",
            usage.estimated_input_tokens,
            usage.estimated_output_tokens,
            usage.estimated_input_known,
            usage.estimated_output_known,
            usage.estimated_input_tokens + usage.estimated_output_tokens,
            usage.estimated_input_known > 0 || usage.estimated_output_known > 0,
        ),
    ] {
        output.insert(
            format!("{prefix}InputTokens"),
            optional_int(input_tokens, input_known > 0),
        );
        output.insert(
            format!("{prefix}OutputTokens"),
            optional_int(output_tokens, output_known > 0),
        );
        output.insert(
            format!("{prefix}Tokens"),
            optional_int(total_tokens, total_known),
        );
    }
    output.insert(
        "complete".to_string(),
        Value::Bool(records > 0 && usage.complete_requests == records),
    );
    output.insert(
        "cachedInputTokens".to_string(),
        optional_int(usage.cached_input_tokens, usage.cache_reported_requests > 0),
    );
    output.insert(
        "cacheReportedRequests".to_string(),
        json_int(usage.cache_reported_requests),
    );
    output.insert(
        "cacheUnreportedRequests".to_string(),
        json_int(usage.cache_unreported_requests),
    );
    output.insert(
        "cacheUnknownRequests".to_string(),
        json_int(usage.cache_unknown_requests),
    );
    Value::Object(output)
}

fn finish_summary(summary: &Summary) -> Value {
    let mut output = Map::new();
    output.insert("requests".to_string(), json_int(summary.requests));
    output.insert("inputTokens".to_string(), json_int(summary.input_tokens));
    output.insert("outputTokens".to_string(), json_int(summary.output_tokens));
    output.insert(
        "unknownRequests".to_string(),
        json_int(summary.unknown_requests),
    );
    let mut coverage = Map::new();
    coverage.insert("records".to_string(), json_int(summary.coverage.records));
    coverage.insert(
        "unknownTimeRequests".to_string(),
        json_int(summary.coverage.unknown_time_requests),
    );
    coverage.insert(
        "cacheReportedRequests".to_string(),
        json_int(summary.coverage.cache_reported_requests),
    );
    coverage.insert(
        "cacheUnreportedRequests".to_string(),
        json_int(summary.coverage.cache_unreported_requests),
    );
    coverage.insert(
        "cacheUnknownRequests".to_string(),
        json_int(summary.coverage.cache_unknown_requests),
    );
    let token_usage = finish_token_usage(&summary.token_usage, summary.coverage.records);
    coverage.insert(
        "completeUsageRequests".to_string(),
        json_int(summary.token_usage.complete_requests),
    );
    coverage.insert(
        "partialUsageRequests".to_string(),
        json_int(summary.token_usage.lower_bound_requests),
    );
    coverage.insert(
        "estimatedUsageRequests".to_string(),
        json_int(summary.token_usage.estimated_requests),
    );
    coverage.insert(
        "unknownUsageRequests".to_string(),
        json_int(summary.token_usage.unknown_requests),
    );
    output.insert("coverage".to_string(), Value::Object(coverage));
    output.insert("tokenUsage".to_string(), token_usage);
    Value::Object(output)
}

fn finish_key(bucket: &KeyBucket) -> Value {
    let mut output = Map::new();
    output.insert(
        "key_id".to_string(),
        bucket
            .key_id
            .clone()
            .map(Value::String)
            .unwrap_or(Value::Null),
    );
    if let Value::Object(summary) = finish_summary(&bucket.summary) {
        for (key, value) in summary {
            output.insert(key, value);
        }
    }
    let mut models: Vec<ModelEntry> = bucket
        .models
        .iter()
        .map(|(model, summary)| ModelEntry {
            model: model.clone(),
            summary: summary.clone(),
        })
        .collect();
    models.sort_by(|left, right| {
        right
            .summary
            .requests
            .cmp(&left.summary.requests)
            .then_with(|| left.model.cmp(&right.model))
    });
    let models: Vec<Value> = models
        .into_iter()
        .map(|entry| {
            let mut output = Map::new();
            output.insert("model".to_string(), Value::String(entry.model));
            if let Value::Object(summary) = finish_summary(&entry.summary) {
                for (key, value) in summary {
                    output.insert(key, value);
                }
            }
            Value::Object(output)
        })
        .collect();
    output.insert("models".to_string(), Value::Array(models));
    let days: Vec<Value> = bucket
        .days
        .iter()
        .map(|(day, summary)| {
            let mut output = Map::new();
            output.insert("day".to_string(), Value::String(day.clone()));
            if let Value::Object(summary) = finish_summary(summary) {
                for (key, value) in summary {
                    output.insert(key, value);
                }
            }
            Value::Object(output)
        })
        .collect();
    output.insert("days".to_string(), Value::Array(days));
    output.insert(
        "undated".to_string(),
        if bucket.undated.coverage.records > 0 {
            let mut output = Map::new();
            output.insert("day".to_string(), Value::Null);
            if let Value::Object(summary) = finish_summary(&bucket.undated) {
                for (key, value) in summary {
                    output.insert(key, value);
                }
            }
            Value::Object(output)
        } else {
            Value::Null
        },
    );
    Value::Object(output)
}

fn finish_results(counts: &BTreeMap<&'static str, i64>) -> Value {
    let mut output = Map::new();
    for key in ["success", "failed", "cancelled", "unknown"] {
        output.insert(
            key.to_string(),
            json_int(counts.get(key).copied().unwrap_or(0)),
        );
    }
    output.insert(
        "total".to_string(),
        json_int(counts.values().copied().sum()),
    );
    let mut coverage = Map::new();
    coverage.insert(
        "unknownResult".to_string(),
        json_int(counts.get("unknown").copied().unwrap_or(0)),
    );
    output.insert("coverage".to_string(), Value::Object(coverage));
    Value::Object(output)
}

fn finish_coverage(
    counters: &Counters,
    read_coverage: ReadCoverage,
    limit: i64,
    path: &Path,
    matched_records: usize,
) -> Value {
    let mut output = Map::new();
    output.insert(
        "ledgerPresent".to_string(),
        Value::Bool(read_coverage.ledger_present),
    );
    output.insert(
        "ledgerReadable".to_string(),
        Value::Bool(read_coverage.ledger_readable),
    );
    output.insert(
        "recordsTotal".to_string(),
        json_int(read_coverage.records_total as i64),
    );
    output.insert(
        "unparsedLines".to_string(),
        json_int(read_coverage.unparsed_lines as i64),
    );
    output.insert(
        "truncated".to_string(),
        Value::Bool(read_coverage.truncated),
    );
    output.insert("ledger".to_string(), Value::String(ledger_name(path)));
    output.insert("limit".to_string(), json_int(limit));
    output.insert(
        "recordsScanned".to_string(),
        json_int(counters.records_scanned as i64),
    );
    output.insert(
        "recordsMatched".to_string(),
        json_int(matched_records as i64),
    );
    output.insert("records".to_string(), json_int(counters.records as i64));
    output.insert("attributed".to_string(), json_int(counters.attributed));
    output.insert("unattributed".to_string(), json_int(counters.unattributed));
    output.insert(
        "incompleteRequests".to_string(),
        json_int(counters.incomplete_requests),
    );
    output.insert(
        "unknownUsageRequests".to_string(),
        json_int(counters.unknown_usage_requests),
    );
    output.insert(
        "unknownTimeRequests".to_string(),
        json_int(counters.unknown_time_requests),
    );
    output.insert(
        "excludedUnknownTimeRequests".to_string(),
        json_int(counters.excluded_unknown_time_requests),
    );
    let mut reasons = Map::new();
    for (key, value) in &counters.attribution_reasons {
        reasons.insert(key.clone(), json_int(*value));
    }
    output.insert("attributionReasons".to_string(), Value::Object(reasons));
    output.insert(
        "cacheReportedRequests".to_string(),
        json_int(counters.cache_reported_requests),
    );
    output.insert(
        "cacheUnreportedRequests".to_string(),
        json_int(counters.cache_unreported_requests),
    );
    output.insert(
        "cacheUnknownRequests".to_string(),
        json_int(counters.cache_unknown_requests),
    );
    Value::Object(output)
}

fn page_counters(page: &[(usize, &Map<String, Value>)]) -> Map<String, Value> {
    let mut attributed = 0i64;
    let mut unattributed = 0i64;
    let mut unknown_usage = 0i64;
    let mut cache_reported = 0i64;
    let mut cache_unreported = 0i64;
    let mut cache_unknown = 0i64;
    let mut reasons: BTreeMap<String, i64> = BTreeMap::new();
    for (_, record) in page {
        let key_id = record.get("key_id").and_then(Value::as_str);
        if key_id.is_some_and(|value| !value.is_empty()) {
            attributed += 1;
        } else {
            unattributed += 1;
            let reason = record
                .get("attribution")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .map(str::to_owned)
                .unwrap_or_else(|| {
                    if record.contains_key("attribution") {
                        "absent".to_string()
                    } else {
                        "legacy".to_string()
                    }
                });
            *reasons.entry(reason).or_insert(0) += 1;
        }
        match cache_report_status(record) {
            CacheReport::Reported => cache_reported += 1,
            CacheReport::Unreported => cache_unreported += 1,
            CacheReport::Unknown => cache_unknown += 1,
        }
        if source_of(record) == Source::None {
            unknown_usage += 1;
        }
    }
    let mut output = Map::new();
    output.insert("attributedRequests".to_string(), json_int(attributed));
    output.insert("unattributedRequests".to_string(), json_int(unattributed));
    output.insert("unknownUsageRequests".to_string(), json_int(unknown_usage));
    output.insert(
        "cacheReportedRequests".to_string(),
        json_int(cache_reported),
    );
    output.insert(
        "cacheUnreportedRequests".to_string(),
        json_int(cache_unreported),
    );
    output.insert("cacheUnknownRequests".to_string(), json_int(cache_unknown));
    let mut reasons_output = Map::new();
    for (key, value) in reasons {
        reasons_output.insert(key, json_int(value));
    }
    output.insert(
        "attributionReasons".to_string(),
        Value::Object(reasons_output),
    );
    output
}

fn request_row(record: &Map<String, Value>) -> Value {
    let at_ms = parse_ts(record.get("time"));
    let model = record
        .get("model")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let source = source_of(record);
    let cache_status = cache_report_status(record);
    let request_id = record
        .get("request_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let key_id = record
        .get("key_id")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let attribution = record
        .get("attribution")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let status = record
        .get("status")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    let (input_tokens, output_tokens, cache_tokens, reasoning_tokens) = if source == Source::None {
        (None, None, None, None)
    } else {
        (
            count(record, &["input_tokens"]),
            count(record, &["output_tokens"]),
            if cache_status == CacheReport::Reported {
                count(record, &["cached_input_tokens", "cache_tokens"])
            } else {
                None
            },
            count(record, &["reasoning_tokens"]),
        )
    };
    let mut output = Map::new();
    output.insert(
        "requestId".to_string(),
        request_id
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "time".to_string(),
        record
            .get("time")
            .and_then(Value::as_str)
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "day".to_string(),
        beijing_day(at_ms).map(Value::String).unwrap_or(Value::Null),
    );
    output.insert(
        "keyId".to_string(),
        key_id
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "model".to_string(),
        model
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "status".to_string(),
        status
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "result".to_string(),
        Value::String(request_result(record).as_str().to_string()),
    );
    output.insert(
        "attribution".to_string(),
        attribution
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    output.insert(
        "usageSource".to_string(),
        Value::String(source.as_str().to_string()),
    );
    output.insert(
        "cacheReportStatus".to_string(),
        Value::String(cache_status.as_str().to_string()),
    );
    output.insert("inputTokens".to_string(), optional_int_value(input_tokens));
    output.insert(
        "outputTokens".to_string(),
        optional_int_value(output_tokens),
    );
    output.insert(
        "durationMs".to_string(),
        optional_int_value(count(record, &["duration_ms"])),
    );
    output.insert("cacheTokens".to_string(), optional_int_value(cache_tokens));
    output.insert(
        "reasoningTokens".to_string(),
        optional_int_value(reasoning_tokens),
    );
    Value::Object(output)
}

fn read_records(path: &Path, limit: i64) -> (Vec<Map<String, Value>>, ReadCoverage) {
    let (numbered, coverage) = read_records_numbered(path, limit);
    (
        numbered.into_iter().map(|(_, record)| record).collect(),
        coverage,
    )
}

type NumberedRecords = Vec<(usize, Map<String, Value>)>;

fn read_records_numbered(path: &Path, limit: i64) -> (NumberedRecords, ReadCoverage) {
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (
                Vec::new(),
                ReadCoverage {
                    ledger_present: false,
                    ledger_readable: false,
                    records_total: 0,
                    unparsed_lines: 0,
                    truncated: false,
                },
            );
        }
        Err(_) => {
            return (
                Vec::new(),
                ReadCoverage {
                    ledger_present: true,
                    ledger_readable: false,
                    records_total: 0,
                    unparsed_lines: 0,
                    truncated: false,
                },
            );
        }
    };
    let mut records = Vec::new();
    let mut total = 0usize;
    let mut unparsed = 0usize;
    for (index, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(line) {
            Ok(Value::Object(record)) => {
                total += 1;
                records.push((index + 1, record));
            }
            _ => unparsed += 1,
        }
    }
    let truncated = (total as i64) > limit;
    let records = if limit > 0 {
        let keep = limit as usize;
        let start = records.len().saturating_sub(keep);
        records.split_off(start)
    } else {
        Vec::new()
    };
    (
        records,
        ReadCoverage {
            ledger_present: true,
            ledger_readable: true,
            records_total: total,
            unparsed_lines: unparsed,
            truncated,
        },
    )
}

fn refuse_private_ledger(path: &Path) -> Result<(), LedgerError> {
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    if PRIVATE_LEDGER_BASENAMES.contains(&name.as_str()) {
        return Err(LedgerError::new(
            "refusing to read the private chat2api ledger; pass the share backend ledger explicitly",
        ));
    }
    Ok(())
}

fn ledger_name(path: &Path) -> String {
    path.file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn key_id_or_empty(bucket: &KeyBucket) -> &str {
    bucket.key_id.as_deref().unwrap_or("")
}

fn optional_int(value: i64, present: bool) -> Value {
    if present {
        json_int(value)
    } else {
        Value::Null
    }
}

fn optional_int_value(value: Option<i64>) -> Value {
    value.map(json_int).unwrap_or(Value::Null)
}

fn json_int(value: i64) -> Value {
    Value::Number(Number::from(value))
}
