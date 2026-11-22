//! The settings page's UM login-round table (#159).
//!
//! The app reads the instance gateway's `/admin/um/login-rounds` face, which is a
//! bounded read of the login history the gateway already writes. The table is
//! read-only: it never probes the upstream, starts a login or appends a round.
//!
//! A legal empty history is 「暂无登录轮次」. An unconfigured or unreadable
//! history keeps its own reason and never becomes an empty table. Closed-set values
//! the app does not know read 「未知」 and keep the raw value in the row detail;
//! they are never folded into a success or a failure.

use std::time::Duration;

use chrono::{FixedOffset, TimeZone};
use serde_json::Value;

use crate::gateway::ControlToken;
use crate::overview::Tone;

const BEIJING_OFFSET_SECONDS: i32 = 8 * 3600;
const MAX_QUERY_CHARS: usize = 120;
const MAX_SCOPE_CHARS: usize = 240;
const MAX_REASON_CHARS: usize = 200;
const MAX_DETAIL_CHARS: usize = 400;
const MAX_TEXT_CHARS: usize = 120;

/// The two filters of the login-round table.
///
/// `round` is the exact round number; `q` matches the reason, outcome, trigger
/// source and the rejected-before-expiry evidence, like the gateway read face.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LoginRoundsQuery {
    pub round: String,
    pub q: String,
}

impl LoginRoundsQuery {
    pub fn is_filtered(&self) -> bool {
        !self.round.trim().is_empty() || !self.q.trim().is_empty()
    }
}

/// One login round as the gateway reports it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoginRound {
    pub key: String,
    pub run_id: String,
    pub round: u64,
    pub state: String,
    pub trigger_source: Option<String>,
    pub driver: Option<String>,
    /// `None` when the gateway did not report the presentation mode; it must
    /// not be folded into 「后台」.
    pub visible: Option<bool>,
    pub started_at: i64,
    pub ended_at: Option<i64>,
    pub submit_count: u64,
    pub outcome: Option<String>,
    pub reason: Option<String>,
    /// The matched page selector behind a rejection, when the gateway recorded it.
    pub detail: Option<String>,
    pub upstream_status: Option<u16>,
    pub evidence_verdict: Option<String>,
    pub rejected_before_expiry_status: Option<u16>,
}

/// One row the table draws. Every field is already the displayed word; `detail`
/// is the full record behind the row's 详情.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoginRoundRow {
    pub key: String,
    pub at: String,
    pub round: String,
    pub state: String,
    pub state_tone: Tone,
    pub mode: String,
    pub trigger: String,
    pub submit: String,
    pub outcome: String,
    pub reason: String,
    pub upstream: String,
    pub detail: String,
}

/// A loaded login-round page.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoginRoundsPage {
    pub rows: Vec<LoginRoundRow>,
    pub status: String,
    pub tone: Tone,
    pub scope: String,
    /// The empty-state copy; empty while the table has rows.
    pub empty_text: String,
    pub truncated: bool,
    pub table_visible: bool,
}

/// One login-round read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoginRoundsView {
    /// The gateway answered with a readable history (possibly empty).
    Loaded(LoginRoundsPage),
    /// The read face itself is unavailable (unconfigured or unreadable path).
    Unavailable { reason: String },
    /// The read failed before a usable answer arrived.
    Failed(String),
}

impl LoginRoundsView {
    pub fn loading() -> Self {
        Self::Loaded(LoginRoundsPage {
            rows: Vec::new(),
            status: "正在读取登录轮次…".to_string(),
            tone: Tone::Neutral,
            scope: String::new(),
            empty_text: String::new(),
            truncated: false,
            table_visible: false,
        })
    }

    pub fn failed(reason: impl Into<String>) -> Self {
        Self::Failed(reason.into())
    }

    pub fn unavailable(reason: impl Into<String>) -> Self {
        Self::Unavailable {
            reason: reason.into(),
        }
    }

    pub fn rows(&self) -> &[LoginRoundRow] {
        match self {
            Self::Loaded(page) => &page.rows,
            Self::Unavailable { .. } | Self::Failed(_) => &[],
        }
    }

    pub fn table_visible(&self) -> bool {
        matches!(self, Self::Loaded(page) if page.table_visible)
    }
}

/// The window fields the settings login-round card owns.
///
/// The shell implements this on the main window; the headless tests use the same
/// window type, so the card's copy and layout stay the tested seam.
pub trait LoginRoundsSurface {
    fn set_rounds(&self, rows: &[LoginRoundRow]);
    fn set_status(&self, status: &str);
    fn set_tone(&self, tone: Tone);
    fn set_scope(&self, scope: &str);
    fn set_empty_text(&self, text: &str);
    fn set_table_visible(&self, visible: bool);
    fn set_truncated(&self, truncated: bool);
    fn set_loading(&self, loading: bool);
    fn selected_round(&self) -> String;
    fn set_selected_round(&self, key: &str);
    fn set_detail(&self, title: &str, body: &str);
}

/// Applies one read result to the settings login-round card.
///
/// A row's detail closes when the new page no longer contains the selected
/// round; a failed or unavailable read closes it as well.
pub fn apply_login_rounds<S: LoginRoundsSurface>(surface: &S, view: &LoginRoundsView) {
    let (status, tone, scope, empty_text, rows, table_visible, truncated) = match view {
        LoginRoundsView::Loaded(page) => (
            page.status.clone(),
            page.tone,
            page.scope.clone(),
            page.empty_text.clone(),
            page.rows.as_slice(),
            page.table_visible,
            page.truncated,
        ),
        LoginRoundsView::Unavailable { reason } => {
            let status = format!("登录轮次不可用：{reason}");
            (
                status.clone(),
                Tone::Bad,
                String::new(),
                status,
                &[][..],
                false,
                false,
            )
        }
        LoginRoundsView::Failed(reason) => {
            let status = format!("登录轮次读取失败：{reason}");
            (
                status.clone(),
                Tone::Bad,
                String::new(),
                status,
                &[][..],
                false,
                false,
            )
        }
    };
    surface.set_rounds(rows);
    surface.set_status(&status);
    surface.set_tone(tone);
    surface.set_scope(&scope);
    surface.set_empty_text(&empty_text);
    surface.set_table_visible(table_visible);
    surface.set_truncated(truncated);
    surface.set_loading(false);
    let selected = surface.selected_round();
    if !selected.is_empty() && !rows.iter().any(|row| row.key == selected) {
        surface.set_selected_round("");
        surface.set_detail("", "");
    }
}

/// Opens one round's 详情, or closes it when the same round is activated again.
/// An empty key closes the detail, which is what the card's close action sends.
pub fn open_round<S: LoginRoundsSurface>(surface: &S, view: Option<&LoginRoundsView>, key: &str) {
    if key.is_empty() || surface.selected_round() == key {
        surface.set_selected_round("");
        surface.set_detail("", "");
        return;
    }
    let Some(row) = view.and_then(|view| view.rows().iter().find(|row| row.key == key)) else {
        return;
    };
    surface.set_selected_round(&row.key);
    surface.set_detail(
        &format!("登录轮次 {} · {}", row.round, row.state),
        &row.detail,
    );
}

/// Parses the gateway's `/admin/um/login-rounds` answer.
///
/// A missing `rounds` array is not a legal empty history. The gateway's
/// `unconfigured` and `unavailable` statuses keep their reason; an unknown status
/// is a failed read, never an empty table.
pub fn parse_login_rounds(
    payload: &Value,
    query: &LoginRoundsQuery,
) -> Result<LoginRoundsView, String> {
    let object = payload
        .as_object()
        .ok_or_else(|| "登录轮次响应不是 JSON 对象".to_string())?;
    let status = object
        .get("status")
        .and_then(Value::as_str)
        .filter(|status| !status.is_empty())
        .ok_or_else(|| "登录轮次响应缺少 status".to_string())?;
    match status {
        "ok" => {
            let rounds = object
                .get("rounds")
                .and_then(Value::as_array)
                .ok_or_else(|| "登录轮次响应缺少 rounds 数组".to_string())?;
            let mut parsed = Vec::with_capacity(rounds.len());
            for value in rounds {
                parsed.push(parse_round(value)?);
            }
            let rows: Vec<LoginRoundRow> = parsed
                .iter()
                .filter(|round| matches_query(round, query))
                .map(row_from_round)
                .collect();
            let truncated = object
                .get("truncated")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let scope = object
                .get("scope")
                .and_then(Value::as_str)
                .map(|scope| sanitize(scope, MAX_SCOPE_CHARS))
                .filter(|scope| !scope.is_empty())
                .unwrap_or_else(|| {
                    "本机实例 UM 登录轮次历史（最近 7 天 / 4 MiB）· 只读，不触发探测或登录"
                        .to_string()
                });
            let empty_text = if rows.is_empty() {
                if query.is_filtered() {
                    "没有符合条件的登录轮次".to_string()
                } else {
                    "暂无登录轮次".to_string()
                }
            } else {
                String::new()
            };
            let mut status_text = if rows.is_empty() {
                empty_text.clone()
            } else {
                format!("UM 登录轮次 · 本页 {} 轮", rows.len())
            };
            if truncated {
                status_text.push_str(" · 已达单页上限，请按轮次或原因缩小范围");
            }
            Ok(LoginRoundsView::Loaded(LoginRoundsPage {
                table_visible: !rows.is_empty(),
                rows,
                status: status_text,
                tone: if empty_text.is_empty() {
                    Tone::Good
                } else {
                    Tone::Neutral
                },
                scope,
                empty_text,
                truncated,
            }))
        }
        "unconfigured" => {
            let error = object
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("login_history_unconfigured");
            let reason = if error == "login_history_unconfigured" {
                "本实例未配置登录历史路径（login_history_unconfigured）".to_string()
            } else {
                format!("本实例未配置登录历史路径（{error}）")
            };
            Ok(LoginRoundsView::unavailable(reason))
        }
        "unavailable" => {
            let error = object
                .get("error")
                .and_then(Value::as_str)
                .map(|error| sanitize(error, MAX_REASON_CHARS))
                .filter(|error| !error.is_empty())
                .unwrap_or_else(|| "登录历史读取失败".to_string());
            Ok(LoginRoundsView::unavailable(format!(
                "登录历史读取失败：{error}"
            )))
        }
        other => Err(format!("登录轮次响应状态未知：{other}")),
    }
}

fn parse_round(value: &Value) -> Result<LoginRound, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "登录轮次行不是 JSON 对象".to_string())?;
    let run_id = object
        .get("runId")
        .and_then(Value::as_str)
        .filter(|run_id| !run_id.is_empty())
        .map(|run_id| sanitize(run_id, MAX_TEXT_CHARS))
        .ok_or_else(|| "登录轮次行缺少 runId".to_string())?;
    let round = object
        .get("round")
        .and_then(Value::as_u64)
        .ok_or_else(|| "登录轮次行缺少 round".to_string())?;
    let state = object
        .get("state")
        .and_then(Value::as_str)
        .filter(|state| !state.is_empty())
        .map(|state| sanitize(state, MAX_TEXT_CHARS))
        .ok_or_else(|| "登录轮次行缺少 state".to_string())?;
    let started_at = object
        .get("startedAt")
        .and_then(number_i64)
        .ok_or_else(|| "登录轮次行缺少 startedAt".to_string())?;
    let key = object
        .get("key")
        .and_then(Value::as_str)
        .map(|key| sanitize(key, MAX_TEXT_CHARS))
        .filter(|key| !key.is_empty())
        .unwrap_or_else(|| format!("{run_id}:{round}"));
    Ok(LoginRound {
        key,
        run_id,
        round,
        state,
        trigger_source: text(object.get("triggerSource"), MAX_TEXT_CHARS),
        driver: text(object.get("driver"), MAX_TEXT_CHARS),
        visible: object.get("visible").and_then(Value::as_bool),
        started_at,
        ended_at: object.get("endedAt").and_then(number_i64),
        submit_count: object
            .get("submitCount")
            .and_then(Value::as_u64)
            .unwrap_or(0),
        outcome: text(object.get("outcome"), MAX_TEXT_CHARS),
        reason: text(object.get("reason"), MAX_REASON_CHARS),
        detail: text(object.get("detail"), MAX_DETAIL_CHARS),
        upstream_status: object
            .get("upstreamStatus")
            .and_then(Value::as_u64)
            .and_then(|status| u16::try_from(status).ok()),
        evidence_verdict: text(object.get("evidenceVerdict"), MAX_TEXT_CHARS),
        rejected_before_expiry_status: object
            .get("rejectedBeforeExpiryStatus")
            .and_then(Value::as_u64)
            .and_then(|status| u16::try_from(status).ok()),
    })
}

fn matches_query(round: &LoginRound, query: &LoginRoundsQuery) -> bool {
    let round_filter = query.round.trim();
    if !round_filter.is_empty() {
        let Ok(round_filter) = round_filter.parse::<u64>() else {
            return false;
        };
        if round.round != round_filter {
            return false;
        }
    }
    let search: String = query
        .q
        .trim()
        .to_lowercase()
        .chars()
        .take(MAX_QUERY_CHARS)
        .collect();
    if search.is_empty() {
        return true;
    }
    let mut fields = Vec::new();
    if let Some(reason) = &round.reason {
        fields.push(reason.to_lowercase());
    }
    if let Some(outcome) = &round.outcome {
        fields.push(outcome.to_lowercase());
    }
    if let Some(source) = &round.trigger_source {
        fields.push(source.to_lowercase());
    }
    if let Some(status) = round.rejected_before_expiry_status {
        fields.push(format!("旧凭据尚未到期，已被 um 拒绝 http {status}"));
    }
    fields.iter().any(|field| field.contains(&search))
}

fn row_from_round(round: &LoginRound) -> LoginRoundRow {
    let (state, state_tone) = state_label(&round.state);
    let (trigger, trigger_raw) = trigger_label(round.trigger_source.as_deref());
    let (outcome, outcome_raw) = outcome_label(round.outcome.as_deref());
    let (evidence, evidence_raw) = evidence_label(round.evidence_verdict.as_deref());
    let at = format_beijing(round.started_at);
    let mode = match round.visible {
        Some(true) => "可见窗口".to_string(),
        Some(false) => "后台".to_string(),
        None => "未知".to_string(),
    };
    let mut reason_parts = Vec::new();
    if let Some(reason) = &round.reason {
        reason_parts.push(reason.clone());
    }
    if let Some(status) = round.rejected_before_expiry_status {
        reason_parts.push(format!("旧凭据尚未到期，已被 UM 拒绝（HTTP {status}）"));
    }
    let reason = if reason_parts.is_empty() {
        "—".to_string()
    } else {
        reason_parts.join(" · ")
    };
    let upstream = match round.upstream_status {
        Some(status) => format!("HTTP {status}"),
        None => "无 HTTP 状态".to_string(),
    };
    let upstream = match evidence {
        Some(evidence) => format!("{upstream} · {evidence}"),
        None => upstream,
    };
    let mut detail = Vec::new();
    detail.push(format!("开始：{at}（北京时间）"));
    detail.push(format!("轮次：{}", round.round));
    detail.push(format!(
        "状态：{state}{}",
        raw_suffix(state_raw(&round.state).as_deref())
    ));
    detail.push(format!("模式：{mode}"));
    detail.push(format!(
        "触发来源：{trigger}{}",
        raw_suffix(trigger_raw.as_deref())
    ));
    detail.push(format!("提交：{}", round.submit_count));
    detail.push(format!(
        "结果：{outcome}{}",
        raw_suffix(outcome_raw.as_deref())
    ));
    detail.push(format!("原因：{reason}"));
    detail.push(format!(
        "上游 / 证据：{upstream}{}",
        raw_suffix(evidence_raw.as_deref())
    ));
    detail.push(format!("运行：{}", round.run_id));
    detail.push(format!(
        "驱动：{}",
        round.driver.as_deref().unwrap_or("未记录")
    ));
    detail.push(format!(
        "结束：{}",
        round
            .ended_at
            .map(format_beijing)
            .unwrap_or_else(|| "未结束".to_string())
    ));
    if let Some(evidence_detail) = &round.detail {
        detail.push(format!("拒绝证据：{evidence_detail}"));
    }
    LoginRoundRow {
        key: round.key.clone(),
        at,
        round: round.round.to_string(),
        state,
        state_tone,
        mode,
        trigger,
        submit: round.submit_count.to_string(),
        outcome,
        reason,
        upstream,
        detail: detail.join("\n"),
    }
}

fn state_label(state: &str) -> (String, Tone) {
    match state {
        "running" => ("进行中".to_string(), Tone::Neutral),
        "completed" => ("已完成".to_string(), Tone::Good),
        "incomplete" => ("未完成".to_string(), Tone::Warn),
        _ => ("未知".to_string(), Tone::Neutral),
    }
}

fn state_raw(state: &str) -> Option<String> {
    match state {
        "running" | "completed" | "incomplete" => None,
        other => Some(other.to_string()),
    }
}

fn trigger_label(source: Option<&str>) -> (String, Option<String>) {
    match source {
        None => ("未记录".to_string(), None),
        Some("business_request") => ("业务请求".to_string(), None),
        Some("auth_watch") => ("后台巡检".to_string(), None),
        Some("explicit") => ("控制台显式".to_string(), None),
        Some(other) => ("未知".to_string(), Some(other.to_string())),
    }
}

fn outcome_label(outcome: Option<&str>) -> (String, Option<String>) {
    match outcome {
        None => ("尚未结束".to_string(), None),
        Some("restored") => ("已恢复".to_string(), None),
        Some("verification_unknown") => ("核验未知".to_string(), None),
        Some("rejected") => ("上游拒绝".to_string(), None),
        Some("manual_takeover") => ("需人工接管".to_string(), None),
        Some("unavailable") => ("上游不可用".to_string(), None),
        Some("terminal") => ("终止".to_string(), None),
        Some("cancelled") => ("已取消".to_string(), None),
        Some("transient") => ("暂时失败".to_string(), None),
        Some("busy") => ("占用中".to_string(), None),
        Some("token_uncommitted") => ("令牌未提交".to_string(), None),
        Some(other) => ("未知".to_string(), Some(other.to_string())),
    }
}

fn evidence_label(verdict: Option<&str>) -> (Option<String>, Option<String>) {
    match verdict {
        None => (None, None),
        Some("valid") => (Some("有效".to_string()), None),
        Some("unknown") => (Some("未知".to_string()), None),
        Some("rejected") => (Some("拒绝".to_string()), None),
        Some(other) => (Some("未知".to_string()), Some(other.to_string())),
    }
}

fn raw_suffix(raw: Option<&str>) -> String {
    raw.map(|raw| format!("（原始值：{}）", sanitize(raw, MAX_TEXT_CHARS)))
        .unwrap_or_default()
}

fn format_beijing(millis: i64) -> String {
    FixedOffset::east_opt(BEIJING_OFFSET_SECONDS)
        .and_then(|offset| offset.timestamp_millis_opt(millis).single())
        .map(|time| time.format("%Y-%m-%d %H:%M:%S").to_string())
        .unwrap_or_else(|| "时间未知".to_string())
}

fn number_i64(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_f64().map(|number| number.round() as i64))
}

fn text(value: Option<&Value>, max: usize) -> Option<String> {
    value
        .and_then(Value::as_str)
        .map(|text| sanitize(text, max))
        .filter(|text| !text.is_empty())
}

fn sanitize(text: &str, max: usize) -> String {
    crate::logs::sanitize_text(text)
        .chars()
        .take(max)
        .collect::<String>()
}

/// The gateway's `/admin/um/login-rounds` read, behind a seam so the table is
/// tested without a live gateway.
pub trait LoginRoundsApi: Send + Sync {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String>;
}

pub struct HttpLoginRounds {
    agent: ureq::Agent,
}

impl HttpLoginRounds {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_secs(5))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpLoginRounds {
    fn default() -> Self {
        Self::new()
    }
}

impl LoginRoundsApi for HttpLoginRounds {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String> {
        let response = self
            .agent
            .get(url)
            .set("X-Admin-Control", token)
            .call()
            .map_err(|error| api_error(url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 登录轮次不是有效 JSON：{error}"))
    }
}

fn api_error(url: &str, error: ureq::Error) -> String {
    match error {
        ureq::Error::Status(code, response) => {
            let detail = response
                .into_json::<Value>()
                .ok()
                .and_then(|body| {
                    body.get("error")
                        .and_then(|error| {
                            error
                                .get("code")
                                .and_then(Value::as_str)
                                .or_else(|| error.get("message").and_then(Value::as_str))
                                .or_else(|| error.as_str())
                        })
                        .map(str::to_string)
                })
                .filter(|detail| safe_reason(detail));
            match detail {
                Some(detail) => format!("{url}: 本实例网关返回 HTTP {code}（{detail}）"),
                None => format!("{url}: 本实例网关返回 HTTP {code}"),
            }
        }
        ureq::Error::Transport(error) => match error.kind() {
            ureq::ErrorKind::ConnectionFailed => {
                format!("{url}: 无法连接本实例网关（连接被拒绝）")
            }
            _ => format!("{url}: 无法连接本实例网关"),
        },
    }
}

fn safe_reason(reason: &str) -> bool {
    !reason.is_empty()
        && reason.len() <= 120
        && reason
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-.:".contains(&byte))
}

/// Builds the read URL. A non-numeric round is rejected before the request so the
/// gateway never has to answer a 400 for a typo; `q` is percent-encoded.
pub fn query_url(base_url: &str, query: &LoginRoundsQuery) -> Result<String, String> {
    let mut url = format!("{}/admin/um/login-rounds", base_url.trim_end_matches('/'));
    let mut pairs = Vec::new();
    let round = query.round.trim();
    if !round.is_empty() {
        let round = round
            .parse::<u64>()
            .map_err(|_| "轮次必须是数字".to_string())?;
        pairs.push(format!("round={round}"));
    }
    let search = query.q.trim();
    if !search.is_empty() {
        let search: String = search.chars().take(MAX_QUERY_CHARS).collect();
        pairs.push(format!("q={}", percent_encode(&search)));
    }
    pairs.push("limit=200".to_string());
    url.push('?');
    url.push_str(&pairs.join("&"));
    Ok(url)
}

fn percent_encode(text: &str) -> String {
    let mut encoded = String::with_capacity(text.len());
    for byte in text.as_bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~') {
            encoded.push(*byte as char);
        } else {
            encoded.push_str(&format!("%{byte:02X}"));
        }
    }
    encoded
}

/// Reads the login-round table and folds every failure into a visible reason.
pub fn read(
    api: &dyn LoginRoundsApi,
    base_url: &str,
    token: &ControlToken,
    query: &LoginRoundsQuery,
) -> LoginRoundsView {
    let url = match query_url(base_url, query) {
        Ok(url) => url,
        Err(reason) => return LoginRoundsView::failed(reason),
    };
    match api.get_json(&url, token.expose()) {
        Ok(payload) => match parse_login_rounds(&payload, query) {
            Ok(view) => view,
            Err(reason) => LoginRoundsView::failed(format!("{url}: {reason}")),
        },
        Err(error) => LoginRoundsView::failed(error),
    }
}
