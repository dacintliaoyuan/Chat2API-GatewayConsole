//! Slint view models over the instance ledger and service log.

use std::collections::HashSet;
use std::path::Path;

use chrono::{FixedOffset, TimeZone};
use serde_json::Value;
use um_console::local_um::{desktop_requests, desktop_trend};

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

    /// The visible range name used in the export copy.
    pub fn label(self) -> &'static str {
        match self {
            Self::Today => "今天",
            Self::Day => "24 小时",
            Self::Week => "7 天",
            Self::Month => "30 天",
            Self::All => "全部",
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
    /// How many ledger rows the read scanned. Zero means there is no usage to
    /// export even when the range has calendar buckets.
    pub scanned: u64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RequestRow {
    pub key: String,
    pub request_id: Option<String>,
    pub at: String,
    pub model: String,
    pub result: String,
    /// Input tokens as the ledger reported them; a missing reading keeps its
    /// reason instead of folding to zero or a bare dash.
    pub input: ReadingView,
    /// Output tokens as the ledger reported them; a missing reading keeps its
    /// reason instead of folding to zero or a bare dash.
    pub output: ReadingView,
    pub duration: String,
    pub first_token: String,
    pub detail: String,
    /// Numeric HTTP status such as `200`; `None` when the response ended
    /// before a status or the ledger did not record one.
    pub http_status: Option<String>,
    /// The error code the gateway wrote (for example `upstream_unavailable`).
    /// Kept separate from the human-readable message.
    pub error_code: Option<String>,
    /// The human-readable error message. Never filled from the code.
    pub error_message: Option<String>,
    pub cache_read: ReadingView,
    pub reasoning: ReadingView,
    /// Why the first-token reading is unavailable; `None` when it is present.
    pub first_token_note: Option<String>,
    pub speed: ReadingView,
    pub speed_basis: String,
    /// `None` when the ledger has no request ID and physical retries cannot be
    /// attached; `Some(empty)` when it recorded the request but no retries.
    pub retries: Option<Vec<RetryStep>>,
}

/// One physical retry step attached to a logical request.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RetryStep {
    pub at: String,
    pub note: String,
}

/// A ledger reading as the detail panel shows it: the formatted amount, or the
/// ledger's reason why no amount exists. Missing values stay distinct from zero.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ReadingView {
    pub value: Option<String>,
    pub note: Option<String>,
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
        // The value already carries Token/秒; a note that repeats it would print
        // the unit twice, so only the qualifier stays in the detail line.
        let speed_note = self.speed_note.as_str();
        let speed_detail = if speed_note.is_empty() {
            "整次平均，含首字等待"
        } else {
            speed_note
                .strip_prefix("Token/秒 · ")
                .unwrap_or(speed_note)
        };
        vec![
            MetricCard {
                label: "Token".into(),
                value: self.token_total.clone(),
                detail: format!("入 {} · 出 {}", self.token_input, self.token_output),
                detail_tone: 3,
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
                detail_tone: 3,
            },
            MetricCard {
                label: "输出速度".into(),
                value: speed,
                detail: speed_detail.into(),
                detail_tone: 3,
            },
            MetricCard {
                label: "首字耗时".into(),
                value: self.first_token.clone(),
                detail: "请求开始到首个输出".into(),
                detail_tone: 3,
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
    /// Models seen anywhere in the selected time window, sorted and deduplicated.
    /// The model dropdown uses these options; the result and search filters do
    /// not shrink them.
    pub models: Vec<String>,
}

/// What a 加载更早 click should do.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EarlierLoad {
    /// No older page is left; the page shows 没有更早记录.
    NoMore,
    /// A 加载更早 read is already in flight; the click is ignored.
    Busy,
    /// Read the page at this cursor and append it to the displayed rows.
    Cursor(String),
}

/// What appending an older page changed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EarlierOutcome {
    /// `added` older rows now follow the displayed ones.
    Appended { added: usize },
    /// No older rows remain; the displayed rows stay on screen.
    NoMore,
    /// The ledger could not be read; the displayed rows stay on screen.
    Failed(String),
}

/// Page cursor stack and last good page for the request list. `pages` always
/// holds the first page as `None`; a later page holds its cursor. Keeping the
/// outer `Option` distinct from the inner cursor is what lets a refresh of the
/// first page still issue a request.
#[derive(Default)]
pub struct RequestNav {
    pub pages: Vec<Option<String>>,
    pub earlier: Option<String>,
    /// The cursor for the next 加载更早 page. It starts at the page anchor's
    /// cursor and advances independently of the 较新/较早 page stack, so loading
    /// history never moves the current page.
    pub append_cursor: Option<String>,
    /// True while a 加载更早 read is in flight; a second click is ignored.
    pub appending: bool,
    pub rows: Vec<RequestRow>,
    pub summary: RequestSummary,
    pub model_speeds: Vec<ModelSpeed>,
    pub chart: RequestChart,
    pub models: Vec<String>,
    pub now: i64,
    pub generation: u64,
}

impl RequestNav {
    /// Starts a new query at the first page and snapshots the current time.
    pub fn query(&mut self, now: i64) -> Option<Option<String>> {
        self.pages.clear();
        self.pages.push(None);
        self.earlier = None;
        self.append_cursor = None;
        self.appending = false;
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
        self.append_cursor = None;
        self.appending = false;
        self.generation = self.generation.wrapping_add(1);
        self.pages.last().cloned()
    }

    /// Refreshes the time window while keeping the page anchor. When Beijing
    /// "today" changes, the previous day's anchors no longer apply.
    pub fn refresh(&mut self, now: i64, range: Range) -> Option<Option<String>> {
        let cursor = self.pages.last().cloned()?;
        let beijing_day = |ms: i64| ms.saturating_add(28_800_000).div_euclid(86_400_000);
        if range == Range::Today && beijing_day(self.now) != beijing_day(now) {
            return self.query(now);
        }
        self.appending = false;
        self.now = now;
        self.generation = self.generation.wrapping_add(1);
        Some(cursor)
    }

    /// Records a page read at the current anchor. `restart_earlier` resets the
    /// 加载更早 cursor from this page; a refresh that kept appended rows passes
    /// `false` so the next 加载更早 continues after them.
    pub fn apply_page(&mut self, page: &RequestPage, restart_earlier: bool) {
        self.rows = page.rows.clone();
        self.earlier = page.earlier.clone();
        if restart_earlier {
            self.append_cursor = page.earlier.clone();
        }
        self.appending = false;
        self.summary = page.summary.clone();
        self.model_speeds = page.model_speeds.clone();
        self.chart = page.chart.clone();
        self.models = page.models.clone();
    }

    /// Starts appending the next older page. The page stack and its anchor stay
    /// put; only the 加载更早 cursor advances.
    pub fn begin_earlier(&mut self) -> EarlierLoad {
        if self.appending {
            return EarlierLoad::Busy;
        }
        let Some(cursor) = self.append_cursor.clone() else {
            return EarlierLoad::NoMore;
        };
        self.appending = true;
        self.generation = self.generation.wrapping_add(1);
        EarlierLoad::Cursor(cursor)
    }

    /// Appends an older page after the displayed rows. Duplicate keys from a
    /// moved ledger are skipped; the rows already shown stay in place.
    pub fn finish_earlier(&mut self, page: RequestPage) -> EarlierOutcome {
        self.appending = false;
        if page.coverage == Coverage::Unavailable {
            return EarlierOutcome::Failed(page.message);
        }
        let mut seen: HashSet<String> = self.rows.iter().map(|row| row.key.clone()).collect();
        let before = self.rows.len();
        for row in page.rows {
            if seen.insert(row.key.clone()) {
                self.rows.push(row);
            }
        }
        let added = self.rows.len() - before;
        self.append_cursor = if added == 0 { None } else { page.earlier };
        if added == 0 {
            EarlierOutcome::NoMore
        } else {
            EarlierOutcome::Appended { added }
        }
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

fn reading_note(reading: &Value) -> Option<String> {
    reading["note"]
        .as_str()
        .map(sanitize_text)
        .filter(|note| !note.is_empty())
}

fn reading_view(reading: &Value) -> ReadingView {
    ReadingView {
        value: amount(reading).map(|value| format!("{}{value}", reading_prefix(reading))),
        note: reading_note(reading),
    }
}

fn rate_view(reading: &Value) -> ReadingView {
    ReadingView {
        value: float_amount(reading).map(|value| format!("{}{value:.1}", reading_prefix(reading))),
        note: reading_note(reading),
    }
}

fn speed_basis_label(basis: &str) -> String {
    match basis {
        "whole_request" => "整次平均，含等待".into(),
        "generation" => "生成阶段实测".into(),
        _ => "口径未知".into(),
    }
}

/// A detail line for a reading. Missing values say 不可用 and keep the ledger's
/// reason; a present value carries its unit and the "already counted in" suffix.
fn reading_line(label: &str, reading: &ReadingView, unit: &str, suffix: &str) -> String {
    match &reading.value {
        Some(value) => format!("{label}  {value}{unit}{suffix}"),
        None => match &reading.note {
            Some(note) => format!("{label}  不可用（{note}）"),
            None => format!("{label}  不可用"),
        },
    }
}

fn http_status_line(row: &RequestRow) -> String {
    match &row.http_status {
        Some(status) => format!("HTTP 状态  {status}"),
        None => "HTTP 状态  不可用（连接在响应前结束或没有记录）".to_string(),
    }
}

fn error_lines(row: &RequestRow) -> String {
    match (&row.error_code, &row.error_message) {
        (Some(code), Some(message)) => format!("错误代码  {code}\n错误信息  {message}"),
        (Some(code), None) => {
            format!("错误代码  {code}\n错误信息  不可用（账本没有记录错误信息）")
        }
        (None, Some(message)) => {
            format!("错误代码  不可用（账本没有记录错误代码）\n错误信息  {message}")
        }
        (None, None) if row.result == "成功" => "无".to_string(),
        (None, None) => {
            "错误代码  不可用（账本没有记录错误代码）\n错误信息  不可用（账本没有记录错误信息）"
                .to_string()
        }
    }
}

fn first_token_line(row: &RequestRow) -> String {
    if row.first_token != "—" {
        return format!("首字耗时  {}", row.first_token);
    }
    match &row.first_token_note {
        Some(note) => format!("首字耗时  不可用（{note}）"),
        None => "首字耗时  不可用".to_string(),
    }
}

fn retry_lines(row: &RequestRow) -> String {
    match &row.retries {
        None => "重试步骤  不可用（缺少 request_id，无法关联物理重试）".to_string(),
        Some(steps) if steps.is_empty() => "重试步骤  没有记录到重试".to_string(),
        Some(steps) => {
            let mut lines = vec!["重试步骤".to_string()];
            lines.extend(
                steps
                    .iter()
                    .map(|step| format!("- {}  {}", step.at, step.note)),
            );
            lines.join("\n")
        }
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
                scanned: 0,
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
        scanned: value["coverage"]["scanned"].as_u64().unwrap_or(0),
    }
}

/// Writes the selected range's usage as CSV to the user-chosen path. A legal
/// empty range and an unreadable ledger are refused with their own reason, so the
/// caller never reports a successful export with no rows.
pub fn export_usage_csv(
    ledger: &Path,
    range: Range,
    now_ms: i64,
    path: &Path,
) -> Result<String, String> {
    let view = read_usage(ledger, range, now_ms);
    if view.coverage == Coverage::Unavailable {
        return Err(view.message);
    }
    if view.coverage == Coverage::Empty || view.scanned == 0 || view.buckets.is_empty() {
        return Err(format!("{}没有可导出的用量", range.label()));
    }
    let mut csv = String::from("时间,Token,请求数\n");
    for bucket in &view.buckets {
        csv.push_str(&format!(
            "{},{},{}\n",
            csv_field(&bucket.label),
            bucket
                .token_count
                .map_or_else(String::new, |value| value.to_string()),
            bucket
                .request_count
                .map_or_else(String::new, |value| value.to_string()),
        ));
    }
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        std::fs::create_dir_all(parent)
            .map_err(|error| format!("无法创建导出目录 {}：{error}", parent.display()))?;
    }
    std::fs::write(path, csv).map_err(|error| format!("无法写入 {}：{error}", path.display()))?;
    Ok(format!(
        "已导出{}的 {} 个时段到 {}",
        range.label(),
        view.buckets.len(),
        path.display()
    ))
}

/// Writes the displayed request rows as CSV to the given path. An empty list is
/// refused instead of writing a header-only file.
pub fn export_requests_csv(rows: &[RequestRow], path: &Path) -> Result<String, String> {
    if rows.is_empty() {
        return Err("没有可导出的请求".to_string());
    }
    let mut csv = String::from("时间,模型,结果,首字耗时,请求 ID\n");
    for row in rows {
        csv.push_str(&format!(
            "{},{},{},{},{}\n",
            csv_field(&row.at),
            csv_field(&row.model),
            csv_field(&row.result),
            csv_field(&row.first_token),
            csv_field(row.request_id.as_deref().unwrap_or("")),
        ));
    }
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        std::fs::create_dir_all(parent)
            .map_err(|error| format!("无法创建导出目录 {}：{error}", parent.display()))?;
    }
    std::fs::write(path, csv).map_err(|error| format!("无法写入 {}：{error}", path.display()))?;
    Ok(format!("已导出 {} 条请求到 {}", rows.len(), path.display()))
}

fn csv_field(text: &str) -> String {
    if text.chars().any(|ch| matches!(ch, ',' | '"' | '\n' | '\r')) {
        format!("\"{}\"", text.replace('"', "\"\""))
    } else {
        text.to_string()
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
        scanned: previous.scanned,
    }
}

pub fn read_requests(
    ledger: &Path,
    range: Range,
    model: Option<&str>,
    result_index: i32,
    search: &str,
    cursor: Option<&str>,
    now_ms: i64,
) -> RequestPage {
    let result = match result_index {
        1 => "success",
        2 => "failed",
        3 => "truncated",
        4 => "client_disconnected",
        5 => "cancelled",
        6 => "unknown",
        _ => "all",
    };
    let model = model.filter(|model| !model.is_empty());
    let value = match desktop_requests(
        ledger,
        range.key(),
        model,
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
                models: Vec::new(),
            };
        }
    };
    let page = &value["page"];
    let rows = page["rows"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    // An empty page says which empty it is: a filter that matched nothing or a
    // time window with no requests at all.
    let filtered = model.is_some() || result_index != 0 || !search.is_empty();
    let empty_message = if filtered {
        "没有符合条件的请求。"
    } else {
        "所选时间没有请求记录。"
    };
    let (coverage, message) = coverage(&page["coverage"], rows.is_empty(), empty_message);
    let mut models = value["models"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default()
        .iter()
        .map(|model| text(model, ""))
        .collect::<Vec<_>>();
    // Keep the active model visible even when the selected window has no row
    // for it; the empty result then explains the filter instead of showing a
    // dropdown that silently disagrees with the rows.
    if let Some(model) = model {
        if !models.iter().any(|candidate| candidate == model) {
            models.push(model.to_string());
            models.sort();
        }
    }
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
        models,
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
        models: previous.models.clone(),
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
        Some("truncated") => "截断",
        Some("client_disconnected") => "客户端断开",
        Some("cancelled") => "已取消",
        _ => "未知",
    };
    let note = text(&row["resultNote"], "未知");
    let retries = row["retries"].as_array().map(Vec::len);
    let detail = match retries {
        Some(count) => format!("{note}\n已关联的物理重试 {count} 次"),
        None => note,
    };
    let first_token = first_token_display(&row["firstTokenMs"]);
    let first_token_note = if first_token == "—" {
        reading_note(&row["firstTokenMs"])
    } else {
        None
    };
    RequestRow {
        key: text(&row["key"], ""),
        request_id: row["requestId"].as_str().map(str::to_string),
        at: format_at(row["at"].as_i64()),
        model: text(&row["model"], "未知模型"),
        result: result.into(),
        input: reading_view(&row["input"]),
        output: reading_view(&row["output"]),
        duration,
        first_token,
        detail,
        http_status: row["httpStatus"].as_i64().map(|status| status.to_string()),
        error_code: row["error"]["code"]
            .as_str()
            .map(sanitize_text)
            .filter(|code| !code.is_empty()),
        error_message: row["error"]["message"]
            .as_str()
            .map(sanitize_text)
            .filter(|message| !message.is_empty()),
        cache_read: reading_view(&row["cacheRead"]),
        reasoning: reading_view(&row["reasoning"]),
        first_token_note,
        speed: rate_view(&row["speed"]),
        speed_basis: speed_basis_label(row["speedBasis"].as_str().unwrap_or("")),
        retries: row["retries"].as_array().map(|steps| {
            steps
                .iter()
                .map(|step| RetryStep {
                    at: format_at(step["at"].as_i64()),
                    note: text(&step["note"], ""),
                })
                .collect()
        }),
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

/// One request's detail panel: the sectioned body plus the raw log disclosure.
/// `logs` is `None` while the matching service events are still being read; the
/// log text only ever includes the sanitized lines already filtered by
/// [`read_request_logs`]. The request ID stays out of the body: the panel header
/// already shows and copies it, so the body never repeats it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RequestDetail {
    pub body: String,
    pub log_label: String,
    pub log_note: String,
    pub log_text: String,
}

pub fn request_detail(row: &RequestRow, logs: Option<&RequestLogs>) -> RequestDetail {
    let body = format!(
        "结果\n  时间  {}\n  结果  {}\n  模型  {}\n  {}\n  结果说明  {}\n{}\n\n耗时\n  {}\n  {}\n  {}\n  {}\n\nToken\n  {}\n  {}\n  {}\n  {}\n\n错误\n{}",
        row.at,
        row.result,
        row.model,
        http_status_line(row),
        row.detail,
        indent(&retry_lines(row)),
        row.duration,
        first_token_line(row),
        reading_line("速度", &row.speed, " Token/秒", ""),
        format!("速度口径  {}", row.speed_basis),
        reading_line("输入", &row.input, " Token", ""),
        reading_line("输出", &row.output, " Token", ""),
        reading_line("缓存读取", &row.cache_read, " Token", "（已含在输入中）"),
        reading_line("思考 Token", &row.reasoning, "", "（已计入输出）"),
        indent(&error_lines(row)),
    );
    let (log_label, log_note, log_text) = match logs {
        Some(logs) => (
            format!("原始日志（{} 条）", logs.lines.len()),
            logs.message.clone(),
            logs.lines.join("\n"),
        ),
        None => (
            "原始日志".to_string(),
            "正在读取关联运行日志…".to_string(),
            String::new(),
        ),
    };
    RequestDetail {
        body,
        log_label,
        log_note,
        log_text,
    }
}

fn indent(text: &str) -> String {
    text.lines()
        .map(|line| format!("  {line}"))
        .collect::<Vec<_>>()
        .join("\n")
}
