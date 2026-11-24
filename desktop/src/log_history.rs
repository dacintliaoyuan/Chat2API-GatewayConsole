//! Server-side run-log history for the log page (#158).
//!
//! The live layer keeps tailing the instance's own `gateway.log`; this module
//! reads the gateway's `/admin/um/logs` face for the history layer. The gateway
//! answers a bounded tail read, so `earlier` is the byte cursor for the next older
//! page, not a promise that the whole file history is reachable.
//!
//! The five time windows, the multi-select levels and the source closed set are all
//! applied by the gateway. The keyword stays a client-side filter so an older page
//! can still be loaded when the current page has no matching row.

use chrono::{DateTime, Local};
use serde_json::Value;

use crate::gateway::ControlToken;
use crate::logs::sanitize_text;
use crate::overview::Tone;

/// The page size the App requests from the gateway (1..200 is the gateway cap).
pub const HISTORY_LIMIT: i64 = 100;

/// The five history windows. `All` has no lower bound.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum HistoryRange {
    #[default]
    Today,
    Day,
    Week,
    Month,
    All,
}

impl HistoryRange {
    pub fn index(self) -> i32 {
        match self {
            Self::Today => 0,
            Self::Day => 1,
            Self::Week => 2,
            Self::Month => 3,
            Self::All => 4,
        }
    }

    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Day,
            2 => Self::Week,
            3 => Self::Month,
            4 => Self::All,
            _ => Self::Today,
        }
    }

    pub fn key(self) -> &'static str {
        match self {
            Self::Today => "today",
            Self::Day => "24h",
            Self::Week => "7d",
            Self::Month => "30d",
            Self::All => "all",
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Today => "今天",
            Self::Day => "24 小时",
            Self::Week => "7 天",
            Self::Month => "30 天",
            Self::All => "全部",
        }
    }

    /// The lower bound in epoch milliseconds. `Today` starts at the local
    /// midnight; the other bounded windows are rolling from `now`.
    pub fn since(self, now: DateTime<Local>) -> Option<i64> {
        match self {
            Self::Today => {
                let midnight = now
                    .date_naive()
                    .and_hms_opt(0, 0, 0)
                    .and_then(|time| time.and_local_timezone(Local).earliest())
                    .map(|time| time.timestamp_millis());
                Some(
                    midnight
                        .unwrap_or_else(|| (now - chrono::Duration::days(1)).timestamp_millis()),
                )
            }
            Self::Day => Some((now - chrono::Duration::hours(24)).timestamp_millis()),
            Self::Week => Some((now - chrono::Duration::days(7)).timestamp_millis()),
            Self::Month => Some((now - chrono::Duration::days(30)).timestamp_millis()),
            Self::All => None,
        }
    }

    /// The legacy `desktop.json` value. The old four indices keep their meaning;
    /// the two new variants are appended so an older saved document still maps.
    pub fn log_range(self) -> crate::config::LogRange {
        match self {
            Self::Today => crate::config::LogRange::Today,
            Self::Day => crate::config::LogRange::Day,
            Self::Week => crate::config::LogRange::Week,
            Self::Month => crate::config::LogRange::Month,
            Self::All => crate::config::LogRange::All,
        }
    }

    pub fn from_log_range(range: crate::config::LogRange) -> Self {
        match range {
            crate::config::LogRange::Today => Self::Today,
            crate::config::LogRange::Day => Self::Day,
            crate::config::LogRange::Week => Self::Week,
            crate::config::LogRange::Month => Self::Month,
            crate::config::LogRange::All => Self::All,
            crate::config::LogRange::Live => Self::Today,
        }
    }
}

/// The history level filter. An empty selection means every level, including the
/// ungraded `unknown` and `raw` rows; selecting levels narrows the gateway query.
/// The saved setting and the query filter are the same bitmask.
pub use crate::config::LogLevels as LevelSelection;

/// One `/admin/um/logs` request. The keyword is applied by the App after the
/// page arrives, because the gateway read face has no keyword parameter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoryQuery {
    pub range: HistoryRange,
    pub levels: LevelSelection,
    pub source: String,
    pub keyword: String,
    pub cursor: Option<String>,
    pub limit: i64,
}

impl HistoryQuery {
    pub fn query_string(&self, now: DateTime<Local>) -> String {
        let mut parts = Vec::new();
        if let Some(since) = self.range.since(now) {
            parts.push(format!("since={since}"));
        }
        for level in self.levels.keys() {
            parts.push(format!("level={level}"));
        }
        if !self.source.is_empty() {
            parts.push(format!("source={}", percent_encode(&self.source)));
        }
        if let Some(cursor) = self.cursor.as_deref().filter(|cursor| !cursor.is_empty()) {
            parts.push(format!("cursor={}", percent_encode(cursor)));
        }
        parts.push(format!("limit={}", self.limit.clamp(1, 200)));
        parts.join("&")
    }
}

/// The gateway's log read, behind a seam so the history state machine is tested
/// without a live gateway.
pub trait HistoryClient: Send + Sync {
    fn read_history(
        &self,
        base_url: &str,
        token: &ControlToken,
        query: &HistoryQuery,
    ) -> Result<Value, String>;
}

/// The production history client. Every history read is a fresh gateway read.
pub struct HttpHistoryClient {
    agent: ureq::Agent,
}

impl HttpHistoryClient {
    pub fn new() -> Self {
        Self::with_timeout(std::time::Duration::from_secs(5))
    }

    pub fn with_timeout(timeout: std::time::Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpHistoryClient {
    fn default() -> Self {
        Self::new()
    }
}

impl HistoryClient for HttpHistoryClient {
    fn read_history(
        &self,
        base_url: &str,
        token: &ControlToken,
        query: &HistoryQuery,
    ) -> Result<Value, String> {
        let url = format!(
            "{}/admin/um/logs?{}",
            base_url.trim_end_matches('/'),
            query.query_string(Local::now())
        );
        let response = self
            .agent
            .get(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map_err(|error| history_error(&url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 历史日志不是有效 JSON：{error}"))
    }
}

fn history_error(url: &str, error: ureq::Error) -> String {
    match error {
        ureq::Error::Status(code, response) => {
            let detail = response
                .into_json::<Value>()
                .ok()
                .and_then(|body| {
                    body.get("error")
                        .and_then(|error| error.get("message"))
                        .or_else(|| body.get("error"))
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .filter(|detail| !detail.is_empty() && detail.len() <= 120);
            match detail {
                Some(detail) => format!("{url}: 本机网关返回 HTTP {code}（{detail}）"),
                None => format!("{url}: 本机网关返回 HTTP {code}"),
            }
        }
        ureq::Error::Transport(error) => match error.kind() {
            ureq::ErrorKind::ConnectionFailed => {
                format!("{url}: 无法连接本机网关（连接被拒绝）")
            }
            _ => format!("{url}: 无法连接本机网关"),
        },
    }
}

/// One visible history row. `level` and `source` are already the labels the page
/// draws; unknown closed-set values read 「未知」 and never leak as raw text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoryRow {
    pub key: String,
    pub at: Option<i64>,
    pub level: String,
    pub source: String,
    pub text: String,
    pub detail: String,
}

/// One history page plus the visible status and the cursor for the next older page.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoryView {
    pub status: String,
    pub tone: Tone,
    pub rows: Vec<HistoryRow>,
    pub sources: Vec<String>,
    pub earlier: Option<String>,
    pub empty: bool,
    pub note: String,
}

pub fn level_label(level: &str) -> &'static str {
    match level.to_ascii_lowercase().as_str() {
        "error" => "错误",
        "warn" | "warning" => "警告",
        "info" => "信息",
        "debug" => "调试",
        "unknown" => "未分级",
        "raw" => "原始",
        _ => "未知",
    }
}

pub fn source_label(source: &str, sources: &[String]) -> String {
    if source.is_empty() || !sources.iter().any(|known| known == source) {
        return "未知".to_string();
    }
    sanitize_text(source)
}

/// Parses the gateway's bounded history payload. A missing `lines` or `sources`
/// array is a malformed read, not a legal empty history; the gateway's own
/// `missing`/`error`/`unconfigured` status keeps its reason instead of showing
/// the empty state.
pub fn parse_history_payload(payload: &Value) -> Result<HistoryView, String> {
    let status = payload
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let sources = payload
        .get("sources")
        .and_then(Value::as_array)
        .map(|items| {
            let mut sources: Vec<String> = Vec::new();
            for item in items {
                let Some(source) = item.as_str().filter(|source| !source.is_empty()) else {
                    continue;
                };
                if !sources.iter().any(|known| known == source) {
                    sources.push(source.to_string());
                }
            }
            sources
        })
        .unwrap_or_default();
    if status != "ok" {
        let code = payload
            .get("error")
            .and_then(Value::as_str)
            .filter(|code| !code.is_empty())
            .unwrap_or(status.as_str());
        let status = match status.as_str() {
            "missing" => format!("日志文件缺失（{code}）"),
            "unconfigured" => format!("未配置服务日志路径（{code}）"),
            "error" => format!("日志读取失败（{code}）"),
            other => format!("历史日志读取失败（状态 {other}，{code}）"),
        };
        return Ok(HistoryView {
            status,
            tone: Tone::Bad,
            rows: Vec::new(),
            sources,
            earlier: None,
            empty: false,
            note: String::new(),
        });
    }

    let Some(lines) = payload.get("lines").and_then(Value::as_array) else {
        return Err("历史日志格式无效：缺少 lines 数组".to_string());
    };
    if payload.get("sources").and_then(Value::as_array).is_none() {
        return Err("历史日志格式无效：缺少 sources 数组".to_string());
    }
    let earlier = match payload.get("earlier") {
        None | Some(Value::Null) => None,
        Some(Value::String(cursor)) if cursor.is_empty() => None,
        Some(Value::String(cursor)) => Some(cursor.clone()),
        Some(Value::Number(cursor)) => Some(cursor.to_string()),
        Some(_) => return Err("历史日志格式无效：earlier 不是字符串".to_string()),
    };
    let mut rows = Vec::with_capacity(lines.len());
    for line in lines {
        let Some(object) = line.as_object() else {
            return Err("历史日志格式无效：日志行不是对象".to_string());
        };
        let key = object
            .get("key")
            .and_then(|key| match key {
                Value::String(key) => Some(key.clone()),
                Value::Number(key) => Some(key.to_string()),
                _ => None,
            })
            .filter(|key| !key.is_empty())
            .ok_or_else(|| "历史日志格式无效：日志行缺少 key".to_string())?;
        let at = object.get("at").and_then(|at| match at {
            Value::Number(at) => at.as_i64(),
            Value::String(at) => chrono::DateTime::parse_from_rfc3339(at)
                .ok()
                .map(|at| at.timestamp_millis()),
            _ => None,
        });
        let raw_level = object
            .get("level")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        let raw_source = object.get("source").and_then(Value::as_str).unwrap_or("");
        let Some(text) = object.get("text").and_then(Value::as_str) else {
            return Err("历史日志格式无效：日志行缺少 text".to_string());
        };
        let text = sanitize_text(text);
        let detail = match object
            .get("requestId")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
        {
            Some(id) => format!("{text}\n\n请求 ID：{}", sanitize_text(id)),
            None => text.clone(),
        };
        rows.push(HistoryRow {
            key,
            at,
            level: level_label(raw_level).to_string(),
            source: source_label(raw_source, &sources),
            text,
            detail,
        });
    }
    let empty = rows.is_empty();
    let status = if empty {
        "读取范围内没有符合条件的日志".to_string()
    } else {
        format!("本页 {} 条", rows.len())
    };
    Ok(HistoryView {
        status,
        tone: if empty { Tone::Neutral } else { Tone::Good },
        rows,
        sources,
        earlier: earlier.clone(),
        empty,
        note: if earlier.is_none() {
            "没有更早记录".to_string()
        } else {
            String::new()
        },
    })
}

/// Applies the keyword filter the gateway read face does not accept. A blank
/// keyword keeps every row; a matching page can be empty while `earlier` still
/// points at older pages.
pub fn filter_history_rows(rows: &[HistoryRow], keyword: &str) -> Vec<HistoryRow> {
    let needle = keyword.trim().to_lowercase();
    if needle.is_empty() {
        return rows.to_vec();
    }
    rows.iter()
        .filter(|row| {
            row.text.to_lowercase().contains(&needle) || row.detail.to_lowercase().contains(&needle)
        })
        .cloned()
        .collect()
}

/// Appends an older page below the rows already shown. The gateway cursor is a
/// byte boundary, so a row repeated across pages is skipped instead of duplicated.
pub fn append_history_rows(previous: &[HistoryRow], page: &HistoryView) -> Vec<HistoryRow> {
    let mut rows = previous.to_vec();
    for row in &page.rows {
        if !rows.iter().any(|known| known.key == row.key) {
            rows.push(row.clone());
        }
    }
    rows
}

/// A failed read keeps the rows already shown and replaces only the status line
/// with the read reason. A first read without rows keeps the reason and no rows.
pub fn history_failure(reason: String, previous: Option<&HistoryView>) -> HistoryView {
    let (rows, sources, earlier) = match previous {
        Some(previous) => (
            previous.rows.clone(),
            previous.sources.clone(),
            previous.earlier.clone(),
        ),
        None => (Vec::new(), Vec::new(), None),
    };
    HistoryView {
        status: format!("历史日志读取失败：{reason}"),
        tone: Tone::Bad,
        rows,
        sources,
        earlier,
        empty: false,
        note: String::new(),
    }
}

fn percent_encode(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            encoded.push(byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}
