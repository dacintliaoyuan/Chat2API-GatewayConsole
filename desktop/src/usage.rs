//! Slint view models over the instance ledger and service log.

use std::path::Path;

use chrono::{FixedOffset, TimeZone};
use serde_json::Value;
use um_console::local_um::{desktop_trend, um_requests};

use crate::logs::{parse_log_line, sanitize_text};
use crate::logstream::{DEFAULT_MAX_LINES, DEFAULT_TAIL_BYTES};
use crate::tail::read_tail;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Range {
    Today,
    Day,
    Week,
    Month,
    All,
}

impl Range {
    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Day,
            2 => Self::Week,
            3 => Self::Month,
            4 => Self::All,
            _ => Self::Today,
        }
    }

    fn key(self) -> &'static str {
        match self {
            Self::Today => "today",
            Self::Day => "24h",
            Self::Week => "7d",
            Self::Month => "30d",
            Self::All => "all",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Coverage {
    Complete,
    Partial,
    Empty,
    Unavailable,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Bucket {
    pub label: String,
    pub tokens: String,
    pub requests: String,
    pub token_count: Option<i64>,
    pub request_count: Option<i64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct UsageView {
    pub buckets: Vec<Bucket>,
    pub token_total: String,
    pub request_total: String,
    pub coverage: Coverage,
    pub message: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestRow {
    pub key: String,
    pub request_id: Option<String>,
    pub at: String,
    pub model: String,
    pub result: String,
    pub input: String,
    pub output: String,
    pub duration: String,
    pub first_token: String,
    pub detail: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RequestSummary {
    pub token_total: String,
    pub token_input: String,
    pub token_output: String,
    pub request_total: String,
    pub request_failed: String,
    pub cache_hit_rate: String,
    pub speed: String,
    pub speed_note: String,
    pub first_token: String,
}

impl Default for RequestSummary {
    fn default() -> Self {
        Self {
            token_total: "—".into(),
            token_input: "—".into(),
            token_output: "—".into(),
            request_total: "—".into(),
            request_failed: "—".into(),
            cache_hit_rate: "—".into(),
            speed: "—".into(),
            speed_note: String::new(),
            first_token: "—".into(),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct MetricCard {
    pub label: String,
    pub value: String,
    pub detail: String,
    /// 0 keeps the muted detail ink; 2 marks a real failure count.
    pub detail_tone: i32,
}

impl RequestSummary {
    /// The five metric cards the request page shows. Missing values keep the
    /// summary's `—` instead of being folded into zero.
    pub fn cards(&self) -> Vec<MetricCard> {
        let speed = if self.speed == "—" {
            "—".to_string()
        } else {
            format!("{} Token/秒", self.speed)
        };
        // The failure count is the one metric that reads as bad when it is real.
        let failures = self.request_failed.trim();
        let failed = failures != "0" && failures != "—" && !failures.is_empty();
        vec![
            MetricCard {
                label: "Token".into(),
                value: self.token_total.clone(),
                detail: format!("入 {} · 出 {}", self.token_input, self.token_output),
                detail_tone: 0,
            },
            MetricCard {
                label: "请求".into(),
                value: self.request_total.clone(),
                detail: format!("失败 {}", self.request_failed),
                detail_tone: if failed { 2 } else { 0 },
            },
            MetricCard {
                label: "缓存命中率".into(),
                value: self.cache_hit_rate.clone(),
                detail: "缓存读取 / 输入 Token".into(),
                detail_tone: 0,
            },
            MetricCard {
                label: "输出速度".into(),
                value: speed,
                detail: if self.speed_note.is_empty() {
                    "Token/秒 · 整次平均".into()
                } else {
                    self.speed_note.clone()
                },
                detail_tone: 0,
            },
            MetricCard {
                label: "首字耗时".into(),
                value: self.first_token.clone(),
                detail: "请求开始到首个输出".into(),
                detail_tone: 0,
            },
        ]
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ModelSpeed {
    pub model: String,
    pub requests: String,
    pub output: String,
    pub speed: String,
    pub first_token: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RequestModelPoint {
    pub model: String,
    pub tokens: Option<i64>,
    pub requests: Option<i64>,
    pub speed: Option<f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RequestPoint {
    pub label: String,
    pub tokens: String,
    pub requests: String,
    pub speed: String,
    pub token_count: Option<i64>,
    pub request_count: Option<i64>,
    pub speed_count: Option<f64>,
    pub models: Vec<RequestModelPoint>,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct RequestChart {
    pub points: Vec<RequestPoint>,
    pub models: Vec<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RequestPage {
    pub rows: Vec<RequestRow>,
    pub earlier: Option<String>,
    pub coverage: Coverage,
    pub message: String,
    pub summary: RequestSummary,
    pub model_speeds: Vec<ModelSpeed>,
    pub chart: RequestChart,
}

/// Page cursor stack and last good page for the request list. `pages` always
/// holds the first page as `None`; a later page holds its cursor. Keeping the
/// outer `Option` distinct from the inner cursor is what lets a refresh of the
/// first page still issue a request.
#[derive(Default)]
pub struct RequestNav {
    pub pages: Vec<Option<String>>,
    pub earlier: Option<String>,
    pub rows: Vec<RequestRow>,
    pub summary: RequestSummary,
    pub model_speeds: Vec<ModelSpeed>,
    pub chart: RequestChart,
    pub now: i64,
    pub generation: u64,
}

impl RequestNav {
    /// Starts a new query at the first page and snapshots the current time.
    pub fn query(&mut self, now: i64) -> Option<Option<String>> {
        self.pages.clear();
        self.pages.push(None);
        self.earlier = None;
        self.now = now;
        self.generation = self.generation.wrapping_add(1);
        self.pages.last().cloned()
    }

    /// Moves one page older (`1`) or newer (`-1`). `None` means there is no
    /// such page, so the caller must not issue a request.
    pub fn page(&mut self, direction: i32) -> Option<Option<String>> {
        match direction {
            1 => match self.earlier.take() {
                Some(cursor) => self.pages.push(Some(cursor)),
                None => return None,
            },
            -1 if self.pages.len() > 1 => {
                self.pages.pop();
                self.earlier = None;
            }
            -1 => return None,
            _ => return None,
        }
        self.generation = self.generation.wrapping_add(1);
        self.pages.last().cloned()
    }

    /// Re-reads the current page at the same snapshot time. The first page is
    /// `Some(None)`, a legal request with no cursor; only a missing page is
    /// `None`.
    pub fn refresh(&mut self) -> Option<Option<String>> {
        let cursor = self.pages.last().cloned()?;
        self.generation = self.generation.wrapping_add(1);
        Some(cursor)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestLogs {
    pub lines: Vec<String>,
    pub message: String,
}

fn text(value: &Value, fallback: &str) -> String {
    sanitize_text(value.as_str().unwrap_or(fallback))
}

fn format_at(value: Option<i64>) -> String {
    let beijing = FixedOffset::east_opt(8 * 3600).expect("valid Beijing UTC offset");
    value
        .and_then(|time| beijing.timestamp_millis_opt(time).single())
        .map(|time| time.format("%m-%d %H:%M:%S").to_string())
        .unwrap_or_else(|| "—".to_string())
}

fn amount(reading: &Value) -> Option<i64> {
    reading["value"].as_i64().filter(|value| *value >= 0)
}

fn display(reading: &Value) -> String {
    let Some(value) = amount(reading) else {
        return "—".to_string();
    };
    let prefix = match reading["kind"].as_str() {
        Some("estimate") => "约 ",
        Some("atLeast") => "≥ ",
        _ => "",
    };
    format!("{prefix}{value}")
}

// The chart axis writes thousands with separators; the token and request totals
// use the same formatter so "3,240,000,000" always pairs with
// "500,000,000" instead of a compact "3.24B". Durations stay ungrouped.
fn amount_display(reading: &Value) -> String {
    let Some(value) = amount(reading) else {
        return "—".to_string();
    };
    let prefix = match reading["kind"].as_str() {
        Some("estimate") => "约 ",
        Some("atLeast") => "≥ ",
        _ => "",
    };
    format!("{prefix}{}", grouped(value))
}

/// Large counts use 亿/万 so a nine-digit total does not crowd the cards, the
/// chart axis or the model speed table; small counts stay exact.
fn grouped(value: i64) -> String {
    let magnitude = value.unsigned_abs();
    if magnitude >= 100_000_000 {
        return format!("{} 亿", scaled(value as f64 / 100_000_000.0));
    }
    if magnitude >= 10_000 {
        return format!("{} 万", scaled(value as f64 / 10_000.0));
    }
    let digits = value.to_string();
    let mut out = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, digit) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index) % 3 == 0 {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

fn scaled(value: f64) -> String {
    let text = format!("{value:.2}");
    let text = text.trim_end_matches('0').trim_end_matches('.');
    text.to_string()
}

fn float_amount(reading: &Value) -> Option<f64> {
    reading["value"].as_f64().filter(|value| value.is_finite())
}

fn reading_prefix(reading: &Value) -> &'static str {
    match reading["kind"].as_str() {
        Some("estimate") => "约 ",
        Some("atLeast") => "≥ ",
        _ => "",
    }
}

/// One decimal for rates such as tokens/s; missing readings stay `—`.
fn float_display(reading: &Value) -> String {
    let Some(value) = float_amount(reading) else {
        return "—".to_string();
    };
    format!("{}{value:.1}", reading_prefix(reading))
}

fn percent_display(reading: &Value) -> String {
    let Some(value) = float_amount(reading) else {
        return "—".to_string();
    };
    format!("{}{value:.1}%", reading_prefix(reading))
}

fn first_token_display(reading: &Value) -> String {
    let value = display(reading);
    if value == "—" {
        value
    } else {
        format!("{value} ms")
    }
}

fn coverage(value: &Value, empty: bool, empty_message: &str) -> (Coverage, String) {
    match value["status"].as_str() {
        Some("ok") if empty => (Coverage::Empty, empty_message.to_string()),
        Some("ok") => (Coverage::Complete, String::new()),
        Some("partial") => (
            Coverage::Partial,
            text(&value["note"], "仅显示可读取的记录"),
        ),
        Some("missing") => (
            Coverage::Empty,
            "尚未生成本实例账本。发送模型请求后可查看记录。".into(),
        ),
        _ => (
            Coverage::Unavailable,
            text(&value["note"], "无法读取本实例账本"),
        ),
    }
}

pub fn read_usage(ledger: &Path, range: Range, now_ms: i64) -> UsageView {
    let value = match desktop_trend(ledger, range.key(), now_ms as f64) {
        Ok(value) => value,
        Err(error) => {
            return UsageView {
                buckets: Vec::new(),
                token_total: "—".into(),
                request_total: "—".into(),
                coverage: Coverage::Unavailable,
                message: format!("无法读取选定时间的用量：{error}"),
            };
        }
    };
    let rows = value["buckets"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let (coverage, message) = coverage(
        &value["coverage"],
        value["coverage"]["scanned"].as_u64() == Some(0),
        "所选时间没有已记录的模型请求。",
    );
    let buckets = rows
        .iter()
        .map(|row| Bucket {
            label: text(&row["label"], "—"),
            tokens: amount_display(&row["tokens"]),
            requests: amount_display(&row["requests"]),
            token_count: amount(&row["tokens"]),
            request_count: amount(&row["requests"]),
        })
        .collect();
    UsageView {
        buckets,
        token_total: amount_display(&value["total"]["tokens"]),
        request_total: amount_display(&value["total"]["requests"]),
        coverage,
        message,
    }
}

/// Keeps the last good chart when a refresh cannot read the ledger. The status
/// still says the refresh failed, so stale points are never presented as fresh.
pub fn keep_previous_on_failure(previous: &UsageView, next: UsageView) -> UsageView {
    if next.coverage != Coverage::Unavailable || previous.buckets.is_empty() {
        return next;
    }
    UsageView {
        buckets: previous.buckets.clone(),
        token_total: previous.token_total.clone(),
        request_total: previous.request_total.clone(),
        coverage: Coverage::Unavailable,
        message: format!("刷新失败：{}", next.message),
    }
}

pub fn read_requests(
    ledger: &Path,
    range: Range,
    result_index: i32,
    search: &str,
    cursor: Option<&str>,
    now_ms: i64,
) -> RequestPage {
    let result = match result_index {
        1 => "success",
        2 => "failed",
        3 => "truncated",
        4 => "cancelled",
        5 => "client_disconnected",
        _ => "all",
    };
    let value = match um_requests(
        ledger,
        range.key(),
        None,
        result,
        search,
        cursor,
        60,
        now_ms as f64,
    ) {
        Ok(value) => value,
        Err(error) => {
            return RequestPage {
                rows: Vec::new(),
                earlier: None,
                coverage: Coverage::Unavailable,
                message: format!("无法读取所选请求：{error}"),
                summary: RequestSummary::default(),
                model_speeds: Vec::new(),
                chart: RequestChart::default(),
            };
        }
    };
    let page = &value["page"];
    let rows = page["rows"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let (coverage, message) = coverage(&page["coverage"], rows.is_empty(), "没有符合条件的请求。");
    RequestPage {
        rows: rows.iter().map(request_row).collect(),
        earlier: page["earlier"].as_str().map(str::to_owned),
        coverage,
        message,
        summary: request_summary(&value["summary"]),
        model_speeds: value["modelSpeeds"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default()
            .iter()
            .map(model_speed)
            .collect(),
        chart: request_chart(&value),
    }
}

fn request_summary(value: &Value) -> RequestSummary {
    RequestSummary {
        token_total: amount_display(&value["tokens"]["total"]),
        token_input: amount_display(&value["tokens"]["input"]),
        token_output: amount_display(&value["tokens"]["output"]),
        request_total: amount_display(&value["requests"]["recorded"]),
        request_failed: amount_display(&value["requests"]["failed"]),
        cache_hit_rate: percent_display(&value["cache"]["hitRate"]),
        speed: float_display(&value["speed"]["value"]),
        speed_note: text(&value["speed"]["coverage"]["note"], ""),
        first_token: first_token_display(&value["firstTokenMs"]),
    }
}

fn model_speed(row: &Value) -> ModelSpeed {
    ModelSpeed {
        model: text(&row["model"], "未知模型"),
        requests: amount_display(&row["requests"]),
        output: amount_display(&row["output"]),
        speed: float_display(&row["speed"]),
        first_token: first_token_display(&row["firstTokenMs"]),
    }
}

fn request_chart(value: &Value) -> RequestChart {
    let points = value["buckets"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(|row| RequestPoint {
            label: text(&row["label"], "—"),
            tokens: amount_display(&row["tokens"]),
            requests: amount_display(&row["requests"]),
            speed: float_display(&row["speed"]),
            token_count: amount(&row["tokens"]),
            request_count: amount(&row["requests"]),
            speed_count: float_amount(&row["speed"]),
            models: row["models"]
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or_default()
                .iter()
                .map(|model| RequestModelPoint {
                    model: text(&model["model"], "未知模型"),
                    tokens: amount(&model["tokens"]),
                    requests: amount(&model["requests"]),
                    speed: float_amount(&model["speed"]),
                })
                .collect(),
        })
        .collect();
    RequestChart {
        points,
        models: value["splitModels"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default()
            .iter()
            .map(|model| text(model, ""))
            .collect(),
    }
}

/// Keeps the current request page when a refresh cannot read the ledger. The
/// filters, page cursor and scroll stay with the caller; only the status changes.
pub fn keep_previous_page_on_failure(previous: &RequestPage, next: RequestPage) -> RequestPage {
    if next.coverage != Coverage::Unavailable || previous.rows.is_empty() {
        return next;
    }
    RequestPage {
        rows: previous.rows.clone(),
        earlier: previous.earlier.clone(),
        coverage: Coverage::Unavailable,
        message: format!("刷新失败：{}", next.message),
        summary: previous.summary.clone(),
        model_speeds: previous.model_speeds.clone(),
        chart: previous.chart.clone(),
    }
}

fn request_row(row: &Value) -> RequestRow {
    let duration = display(&row["durationMs"]);
    let duration = if duration == "—" {
        duration
    } else {
        format!("{duration} ms")
    };
    let result = match row["result"].as_str() {
        Some("success") => "成功",
        Some("failed") => "失败",
        Some("truncated") => "回复截断",
        Some("cancelled") => "已取消",
        Some("client_disconnected") => "客户端断开",
        _ => "结果未知",
    };
    let note = text(&row["resultNote"], "未知");
    let retries = row["retries"].as_array().map(Vec::len);
    let detail = match retries {
        Some(count) => format!("{note}\n已关联的物理重试 {count} 次"),
        None => note,
    };
    RequestRow {
        key: text(&row["key"], ""),
        request_id: row["requestId"].as_str().map(str::to_string),
        at: format_at(row["at"].as_i64()),
        model: text(&row["model"], "未知模型"),
        result: result.into(),
        input: display(&row["input"]),
        output: display(&row["output"]),
        duration,
        first_token: first_token_display(&row["firstTokenMs"]),
        detail,
    }
}

pub fn read_request_logs(
    service_log: &Path,
    request_id: Option<&str>,
    _now_ms: i64,
) -> RequestLogs {
    let Some(id) = request_id.filter(|id| {
        id.len() == 32
            && id
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    }) else {
        return RequestLogs {
            lines: Vec::new(),
            message: "这条账本记录没有可关联的请求 ID。".into(),
        };
    };
    let Ok(tail) = read_tail(service_log, DEFAULT_TAIL_BYTES) else {
        return RequestLogs {
            lines: Vec::new(),
            message: "本实例运行日志暂不可读。".into(),
        };
    };
    let complete = tail.rsplit_once('\n').map_or("", |(complete, _)| complete);
    let lines = complete
        .lines()
        .rev()
        .take(DEFAULT_MAX_LINES)
        .filter_map(|line| {
            let value: Value = serde_json::from_str(line).ok()?;
            (value["request_id"].as_str() == Some(id)).then(|| parse_log_line(line))
        })
        .take(20)
        .map(|entry| format!("{}  {}", format_at(entry.at), entry.text))
        .collect::<Vec<_>>();
    let message = if lines.is_empty() {
        "近期运行日志里没有匹配的记录。"
    } else {
        "仅列出近期运行日志中的匹配事件。"
    };
    RequestLogs {
        lines,
        message: message.into(),
    }
}

/// Detail text for one ledger row. `logs` is `None` while the matching service
/// log events are still being read; the finished text only ever includes the
/// sanitized lines already filtered by [`read_request_logs`].
pub fn request_detail(row: &RequestRow, logs: Option<&RequestLogs>) -> String {
    let mut detail = format!(
        "{}  {}\n模型  {}\n请求 ID  {}\n耗时  {}\n首字  {}\n输入  {} Token  ·  输出  {} Token\n\n{}",
        row.at,
        row.result,
        row.model,
        row.request_id.as_deref().unwrap_or("—"),
        row.duration,
        row.first_token,
        row.input,
        row.output,
        row.detail
    );
    match logs {
        Some(logs) => {
            detail.push_str("\n\n运行日志\n");
            detail.push_str(&logs.message);
            if !logs.lines.is_empty() {
                detail.push('\n');
                detail.push_str(&logs.lines.join("\n"));
            }
        }
        None => detail.push_str("\n\n正在读取关联运行日志…"),
    }
    detail
}
