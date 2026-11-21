//! Share usage ledger.
//!
//! The row shape is the Python `chat2api.record_usage` shape
//! (`chat2api/chat2api.py:3316-3401`): one JSON object per line with the
//! frozen field names, trusted upstream counts winning over local estimates and
//! missing observations staying `null`/`0` exactly where Python keeps them. A
//! write failure disables accounting for this process and never blocks the proxy.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Instant;

use chrono::{Local, SecondsFormat};
use serde_json::{json, Value};

/// Desktop's private ledger can correlate its local rows to the HTTP event log.
/// Keep the independently minted share request ID when there is one.
pub(crate) fn request_id<'a>(
    shared_id: Option<&'a str>,
    http_id: &'a str,
    desktop_instance: bool,
) -> Option<&'a str> {
    shared_id.or(desktop_instance.then_some(http_id))
}

/// The four coverage labels of `usage_source`.
pub const USAGE_SOURCE_UPSTREAM: &str = "upstream";
pub const USAGE_SOURCE_PARTIAL: &str = "partial";
pub const USAGE_SOURCE_ESTIMATED: &str = "estimated";
pub const USAGE_SOURCE_NONE: &str = "none";

/// The trusted token fields of one OpenAI-shaped usage block.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct NormalizedUsage {
    pub prompt_tokens: Option<u64>,
    pub completion_tokens: Option<u64>,
    pub total_tokens: Option<u64>,
    pub cached_tokens: Option<u64>,
    pub reasoning_tokens: Option<u64>,
}

impl NormalizedUsage {
    pub fn is_empty(&self) -> bool {
        self.prompt_tokens.is_none()
            && self.completion_tokens.is_none()
            && self.total_tokens.is_none()
            && self.cached_tokens.is_none()
            && self.reasoning_tokens.is_none()
    }
}

/// A trusted count: a non-bool, non-negative JSON integer; anything else is
/// absent (never folded into zero).
pub fn reported_tokens(value: Option<&Value>) -> Option<u64> {
    match value {
        Some(Value::Number(number)) => number.as_u64(),
        _ => None,
    }
}

fn usage_side_tokens(
    usage: &serde_json::Map<String, Value>,
    canonical: &str,
    alias: &str,
) -> Option<u64> {
    reported_tokens(usage.get(canonical)).or_else(|| reported_tokens(usage.get(alias)))
}

/// Normalize one upstream usage block without inventing counts.
pub fn normalize_usage(usage: Option<&Value>) -> NormalizedUsage {
    let Some(Value::Object(usage)) = usage else {
        return NormalizedUsage::default();
    };
    NormalizedUsage {
        prompt_tokens: usage_side_tokens(usage, "prompt_tokens", "input_tokens"),
        completion_tokens: usage_side_tokens(usage, "completion_tokens", "output_tokens"),
        total_tokens: reported_tokens(usage.get("total_tokens")),
        cached_tokens: usage
            .get("prompt_tokens_details")
            .or_else(|| usage.get("input_tokens_details"))
            .and_then(Value::as_object)
            .and_then(|details| reported_tokens(details.get("cached_tokens"))),
        reasoning_tokens: usage
            .get("completion_tokens_details")
            .or_else(|| usage.get("output_tokens_details"))
            .and_then(Value::as_object)
            .and_then(|details| reported_tokens(details.get("reasoning_tokens"))),
    }
}

/// Merge cumulative usage snapshots: the latest legal value per field wins.
pub fn merge_usage_snapshots(base: Option<&Value>, update: &Value) -> Option<Value> {
    let merged = merge_usage_fields(normalize_usage(base), normalize_usage(Some(update)));
    if merged.is_empty() {
        None
    } else {
        Some(usage_fields_to_json(&merged))
    }
}

fn merge_usage_fields(base: NormalizedUsage, update: NormalizedUsage) -> NormalizedUsage {
    NormalizedUsage {
        prompt_tokens: update.prompt_tokens.or(base.prompt_tokens),
        completion_tokens: update.completion_tokens.or(base.completion_tokens),
        total_tokens: update.total_tokens.or(base.total_tokens),
        cached_tokens: update.cached_tokens.or(base.cached_tokens),
        reasoning_tokens: update.reasoning_tokens.or(base.reasoning_tokens),
    }
}

/// Build the ledger's OpenAI-shaped usage block from normalized fields.
pub fn usage_fields_to_json(fields: &NormalizedUsage) -> Value {
    let mut value = serde_json::Map::new();
    if let Some(prompt) = fields.prompt_tokens {
        value.insert("prompt_tokens".to_string(), json!(prompt));
    }
    if let Some(completion) = fields.completion_tokens {
        value.insert("completion_tokens".to_string(), json!(completion));
    }
    if let Some(total) = fields.total_tokens {
        value.insert("total_tokens".to_string(), json!(total));
    }
    if let Some(cached) = fields.cached_tokens {
        value.insert(
            "prompt_tokens_details".to_string(),
            json!({"cached_tokens": cached}),
        );
    }
    if let Some(reasoning) = fields.reasoning_tokens {
        value.insert(
            "completion_tokens_details".to_string(),
            json!({"reasoning_tokens": reasoning}),
        );
    }
    Value::Object(value)
}

/// Merge two cumulative Anthropic usage snapshots: the larger value per field
/// wins, matching `_AnthropicStreamUsage.observe` (`chat2api/chat2api.py:3477-3539`).
pub fn merge_usage_max(base: NormalizedUsage, update: NormalizedUsage) -> NormalizedUsage {
    NormalizedUsage {
        prompt_tokens: max_option(base.prompt_tokens, update.prompt_tokens),
        completion_tokens: max_option(base.completion_tokens, update.completion_tokens),
        total_tokens: max_option(base.total_tokens, update.total_tokens),
        cached_tokens: max_option(base.cached_tokens, update.cached_tokens),
        reasoning_tokens: max_option(base.reasoning_tokens, update.reasoning_tokens),
    }
}

fn max_option(left: Option<u64>, right: Option<u64>) -> Option<u64> {
    match (left, right) {
        (Some(left), Some(right)) => Some(left.max(right)),
        (left, right) => left.or(right),
    }
}

/// Translate one Anthropic `usage` block into the ledger's normalized fields.
///
/// Anthropic counts input tokens WITHOUT cache reads and cache writes, while the
/// ledger's `prompt_tokens` includes the cached subset. Cache reads become
/// `prompt_tokens_details.cached_tokens`; cache writes stay part of the
/// non-cached input because they were fresh input processing, not a cache hit.
/// Only reported fields appear; nothing is invented (`chat2api/chat2api.py:3420-3447`).
pub fn anthropic_usage_fields(
    usage: Option<&Value>,
    input_side: bool,
    output_side: bool,
) -> NormalizedUsage {
    let Some(Value::Object(usage)) = usage else {
        return NormalizedUsage::default();
    };
    let mut fields = NormalizedUsage::default();
    if input_side {
        let input_tokens = reported_tokens(usage.get("input_tokens"));
        let cache_read = reported_tokens(usage.get("cache_read_input_tokens"));
        let cache_write = reported_tokens(usage.get("cache_creation_input_tokens"));
        if input_tokens.is_some() || cache_read.is_some() || cache_write.is_some() {
            fields.prompt_tokens = Some(
                input_tokens.unwrap_or(0) + cache_read.unwrap_or(0) + cache_write.unwrap_or(0),
            );
        }
        if cache_read.is_some() {
            fields.cached_tokens = cache_read;
        }
    }
    if output_side {
        fields.completion_tokens = reported_tokens(usage.get("output_tokens"));
    }
    fields
}

/// Complete usage of a non-stream Anthropic message; `None` when absent.
///
/// A 200 body that is not an Anthropic message (or carries no usage block)
/// yields `None`, so the row keeps the local estimate with explicit coverage
/// instead of a fabricated zero (`chat2api/chat2api.py:3449-3458`).
pub fn anthropic_message_usage(payload: &Value) -> Option<NormalizedUsage> {
    if payload.get("type").and_then(Value::as_str) != Some("message") {
        return None;
    }
    let fields = anthropic_usage_fields(payload.get("usage"), true, true);
    if fields.is_empty() {
        None
    } else {
        Some(fields)
    }
}

/// Local output-token estimate from a non-stream Anthropic message body.
///
/// Only text blocks are counted; an unreadable body yields 0, which keeps the
/// row's coverage explicit (`chat2api/chat2api.py:3461-3475`).
pub fn anthropic_output_estimate(payload: &Value) -> u64 {
    let Some(content) = payload.get("content").and_then(Value::as_array) else {
        return 0;
    };
    let text = content
        .iter()
        .filter(|block| block.get("type").and_then(Value::as_str) == Some("text"))
        .filter_map(|block| block.get("text").and_then(Value::as_str))
        .collect::<String>();
    if text.is_empty() {
        0
    } else {
        estimate_tokens(&text)
    }
}

/// Rough token estimate used by Python `_est_tokens`: CJK chars count as one
/// token each, the rest as four characters per token.
pub fn estimate_tokens(text: &str) -> u64 {
    let cjk = text
        .chars()
        .filter(|character| ('\u{4e00}'..='\u{9fff}').contains(character))
        .count() as u64;
    let total = text.chars().count() as u64;
    (cjk + (total - cjk) / 4).max(1)
}

/// Estimate the input side from `messages` the way Python `_message_tokens` does.
pub fn message_tokens(messages: Option<&Value>) -> u64 {
    let Some(Value::Array(messages)) = messages else {
        return 0;
    };
    messages
        .iter()
        .map(|message| {
            let content = message.get("content");
            let text = match content {
                Some(Value::Array(parts)) => parts
                    .iter()
                    .filter_map(|part| part.get("text").and_then(Value::as_str))
                    .collect::<Vec<_>>()
                    .join(" "),
                Some(Value::String(text)) => text.clone(),
                _ => String::new(),
            };
            estimate_tokens(&text) + 4
        })
        .sum()
}

/// One ledger row.
pub struct UsageRecord<'a> {
    pub model: &'a str,
    pub stream: bool,
    pub started_at: Instant,
    pub usage: Option<&'a Value>,
    pub input_estimate: u64,
    pub output_estimate: u64,
    pub status: &'a str,
    pub protocol: Option<&'a str>,
    pub key_id: Option<&'a str>,
    pub request_id: Option<&'a str>,
    pub attribution: &'a str,
    /// Time from request start to the first streamed byte the client can use.
    /// `None` for non-stream requests and streams that never committed.
    pub first_token_ms: Option<u64>,
}

/// The append-only share ledger.
pub struct Ledger {
    path: Option<PathBuf>,
    disabled: AtomicBool,
    lock: Mutex<()>,
}

impl Ledger {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self {
            path,
            disabled: AtomicBool::new(false),
            lock: Mutex::new(()),
        }
    }

    pub fn disabled(&self) -> bool {
        self.disabled.load(Ordering::SeqCst)
    }

    /// Append one row; a failure disables accounting and never blocks.
    pub fn record(&self, record: UsageRecord<'_>) {
        if self.disabled.load(Ordering::SeqCst) {
            return;
        }
        let Some(path) = &self.path else {
            return;
        };
        let fields = normalize_usage(record.usage);
        let input_tokens = fields.prompt_tokens.unwrap_or(record.input_estimate);
        let output_tokens = fields.completion_tokens.unwrap_or(record.output_estimate);
        let total_tokens = fields.total_tokens.unwrap_or(input_tokens + output_tokens);
        let usage_source = if fields.prompt_tokens.is_some() && fields.completion_tokens.is_some() {
            USAGE_SOURCE_UPSTREAM
        } else if !fields.is_empty() {
            USAGE_SOURCE_PARTIAL
        } else if record.input_estimate > 0 || record.output_estimate > 0 {
            USAGE_SOURCE_ESTIMATED
        } else {
            USAGE_SOURCE_NONE
        };
        let mut row = json!({
            "time": Local::now().to_rfc3339_opts(SecondsFormat::Secs, false),
            "service": "chat2api",
            "model": record.model,
            "stream": record.stream,
            "status": record.status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": fields.cached_tokens.unwrap_or(0),
            "cache_report_status": if fields.cached_tokens.is_some() {
                "reported"
            } else {
                "unreported"
            },
            "reasoning_tokens": fields.reasoning_tokens.unwrap_or(0),
            "total_tokens": total_tokens,
            "estimated": fields.is_empty(),
            "duration_ms": record.started_at.elapsed().as_millis() as u64,
            "usage_source": usage_source,
            "protocol": record.protocol,
            "key_id": record.key_id,
            "request_id": record.request_id,
            "attribution": record.attribution,
        });
        if let Some(first_token_ms) = record.first_token_ms {
            row["first_token_ms"] = json!(first_token_ms);
        }
        let mut line = serde_json::to_string(&row).unwrap_or_default();
        line.push('\n');
        let result = (|| -> std::io::Result<()> {
            if let Some(parent) = path.parent() {
                if !parent.as_os_str().is_empty() {
                    std::fs::create_dir_all(parent)?;
                }
            }
            let _guard = self.lock.lock().expect("ledger lock poisoned");
            let mut file = OpenOptions::new().create(true).append(true).open(path)?;
            file.write_all(line.as_bytes())
        })();
        if result.is_err() {
            self.disabled.store(true, Ordering::SeqCst);
            eprintln!("[chat2api] usage log write failed, accounting disabled");
        }
    }
}
