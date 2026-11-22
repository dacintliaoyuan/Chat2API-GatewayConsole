//! The overview recent-issues card (#151).
//!
//! Request failures, truncations and client disconnects come from the instance
//! ledger's request results for today. Login failures and manual takeovers come from
//! the gateway's `/admin/um/login-rounds` history. Service and catalog/test
//! issues come from the read faces the App already holds; a source that has not
//! been read yet says 不可用 with the reason instead of a fabricated zero.

use std::path::Path;
use std::time::Duration;

use serde_json::Value;
use um_console::local_um::desktop_requests;

use crate::models::{CatalogView, ProbeView};
use crate::overview::Tone;
use crate::um_service::ServiceView;

/// Where 查看证据 jumps: the request page's result filter and time window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IssueQuery {
    /// The request result index: 2 failed, 3 truncated, 4 client disconnected.
    pub result: i32,
    /// The request time-range index; 0 is 今天.
    pub window: i32,
}

/// One row of the recent-issues card.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IssueRow {
    pub title: String,
    pub count: String,
    pub detail: String,
    pub tone: Tone,
    /// The request-page query 查看证据 applies; `None` when the evidence is not
    /// on the request page.
    pub evidence: Option<IssueQuery>,
    /// Why 查看证据 is disabled; empty when `evidence` is `Some`.
    pub evidence_note: String,
}

/// One read of the recent-issues card.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecentIssues {
    pub status: String,
    pub tone: Tone,
    pub rows: Vec<IssueRow>,
    /// The numeric issue total; counts with no numeric reading do not add zero.
    pub total: i64,
    /// Read faces that failed or were not available; the status line keeps them.
    pub failures: Vec<String>,
}

/// The login-round read the overview holds for this refresh.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoginRounds {
    /// The gateway answered with the `/admin/um/login-rounds` payload.
    Rounds(Value),
    /// The gateway was not ready or the read failed; the reason stays visible.
    Unavailable(String),
}

/// The gateway's bounded login-round history, behind a seam so the card is
/// tested without a live gateway.
pub trait LoginRoundsReader: Send + Sync {
    fn read_rounds(&self, base_url: &str, token: &str) -> Result<Value, String>;
}

/// The production login-round reader.
pub struct HttpLoginRoundsReader {
    agent: ureq::Agent,
}

impl HttpLoginRoundsReader {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_millis(2000))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpLoginRoundsReader {
    fn default() -> Self {
        Self::new()
    }
}

impl LoginRoundsReader for HttpLoginRoundsReader {
    fn read_rounds(&self, base_url: &str, token: &str) -> Result<Value, String> {
        let url = format!(
            "{}/admin/um/login-rounds?limit=200",
            base_url.trim_end_matches('/')
        );
        let response = self
            .agent
            .get(&url)
            .set("X-Admin-Control", token)
            .call()
            .map_err(|error| format!("{url}: {error}"))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 登录轮次不是有效 JSON：{error}"))
    }
}

/// Reads one login-round payload, folding a transport failure into the reason.
pub fn read_login_rounds(
    reader: &dyn LoginRoundsReader,
    base_url: &str,
    token: &str,
) -> LoginRounds {
    match reader.read_rounds(base_url, token) {
        Ok(payload) => parse_login_rounds(&payload),
        Err(reason) => LoginRounds::Unavailable(reason),
    }
}

/// Parses the gateway's login-round payload. A missing history path and an
/// unreadable file are not legal empty lists.
pub fn parse_login_rounds(payload: &Value) -> LoginRounds {
    match payload.get("status").and_then(Value::as_str) {
        Some("ok") => LoginRounds::Rounds(payload.clone()),
        Some("unconfigured") => {
            LoginRounds::Unavailable("未配置登录历史路径（login-history.jsonl）".to_string())
        }
        Some("unavailable") => LoginRounds::Unavailable(format!(
            "登录历史不可读：{}",
            payload
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("读取失败")
        )),
        _ => LoginRounds::Unavailable("登录轮次响应格式无效：缺少 status".to_string()),
    }
}

/// One read of the recent-issues card over the sources the App already holds.
pub fn read_recent_issues(
    ledger: &Path,
    now_ms: i64,
    login: &LoginRounds,
    service: Option<&ServiceView>,
    catalog: Option<&CatalogView>,
    probe: Option<&ProbeView>,
) -> RecentIssues {
    let mut rows = Vec::new();
    let mut failures = Vec::new();

    request_issues(ledger, now_ms, &mut rows, &mut failures);
    login_issue(login, &mut rows, &mut failures);
    service_issue(service, &mut rows, &mut failures);
    catalog_test_issue(catalog, probe, &mut rows, &mut failures);

    let total = rows.iter().map(IssueRow::count_number).sum();
    let status = if !failures.is_empty() {
        if rows.is_empty() {
            format!("近期问题读取失败：{}", failures.join("；"))
        } else {
            format!("近期问题 {total} 项 · 读取不完整：{}", failures.join("；"))
        }
    } else if rows.is_empty() {
        "近期无问题".to_string()
    } else {
        format!("近期问题 {total} 项")
    };
    let tone = if rows.iter().any(|row| row.tone == Tone::Bad)
        || rows.is_empty() && !failures.is_empty()
    {
        Tone::Bad
    } else if rows.iter().any(|row| row.tone == Tone::Warn) {
        Tone::Warn
    } else if rows.is_empty() {
        Tone::Good
    } else {
        Tone::Neutral
    };
    RecentIssues {
        status,
        tone,
        rows,
        total,
        failures,
    }
}

impl IssueRow {
    fn count_number(&self) -> i64 {
        self.count
            .strip_suffix(" 次")
            .or_else(|| self.count.strip_suffix(" 项"))
            .or_else(|| self.count.strip_suffix(" 轮"))
            .and_then(|count| count.parse::<i64>().ok())
            .unwrap_or(0)
    }

    fn request(title: &str, count: i64, detail: String, tone: Tone, result: i32) -> Self {
        Self {
            title: title.to_string(),
            count: format!("{count} 次"),
            detail,
            tone,
            evidence: Some(IssueQuery { result, window: 0 }),
            evidence_note: String::new(),
        }
    }

    fn unavailable(title: &str, detail: String, evidence_note: String) -> Self {
        Self {
            title: title.to_string(),
            count: "不可用".to_string(),
            detail,
            tone: Tone::Bad,
            evidence: None,
            evidence_note,
        }
    }
}

fn request_issues(
    ledger: &Path,
    now_ms: i64,
    rows: &mut Vec<IssueRow>,
    failures: &mut Vec<String>,
) {
    let payload = match desktop_requests(ledger, "today", None, "all", "", None, 1, now_ms as f64) {
        Ok(payload) => payload,
        Err(error) => {
            failures.push("请求结果读取失败".to_string());
            rows.push(IssueRow::unavailable(
                "请求结果读取失败",
                format!("读取失败：{error}"),
                "证据不可用：请求结果读取失败".to_string(),
            ));
            return;
        }
    };
    let status = payload
        .pointer("/page/coverage/status")
        .and_then(Value::as_str)
        .unwrap_or("unavailable");
    if status == "missing" {
        // No ledger file is the legal empty state: no requests were recorded.
        return;
    }
    if status == "unavailable" {
        let note = payload
            .pointer("/page/coverage/note")
            .and_then(Value::as_str)
            .unwrap_or("账本读取失败或结构损坏");
        failures.push("请求结果读取失败".to_string());
        rows.push(IssueRow::unavailable(
            "请求结果读取失败",
            format!("读取失败：{note}"),
            "证据不可用：请求结果读取失败".to_string(),
        ));
        return;
    }
    let counts = payload
        .pointer("/summary/requests/counts")
        .and_then(Value::as_object);
    let count = |name: &str| {
        counts
            .and_then(|counts| counts.get(name))
            .and_then(Value::as_i64)
            .unwrap_or(0)
    };
    let partial = if status == "partial" {
        payload
            .pointer("/page/coverage/note")
            .and_then(Value::as_str)
            .map(|note| format!("；账本部分不可读：{note}"))
            .unwrap_or_default()
    } else {
        String::new()
    };
    let failed = count("failed");
    if failed > 0 {
        let unknown = count("unknown");
        let mut detail =
            format!("今天账本记录了 {failed} 次失败（含上游错误）；重试不重复计请求{partial}");
        if unknown > 0 {
            detail.push_str(&format!("；另有 {unknown} 条结果未确认，未计入失败"));
        }
        rows.push(IssueRow::request("请求失败", failed, detail, Tone::Bad, 2));
    }
    let truncated = count("truncated");
    if truncated > 0 {
        rows.push(IssueRow::request(
            "截断",
            truncated,
            format!("今天账本记录了 {truncated} 次截断：HTTP 200 但回复未完成，按失败计{partial}"),
            Tone::Bad,
            3,
        ));
    }
    let disconnected = count("client_disconnected");
    if disconnected > 0 {
        rows.push(IssueRow::request(
            "客户端断开",
            disconnected,
            format!(
                "今天账本记录了 {disconnected} 次客户端在回复完成前断开；不推断为主动取消{partial}"
            ),
            Tone::Warn,
            4,
        ));
    }
}

fn login_issue(login: &LoginRounds, rows: &mut Vec<IssueRow>, failures: &mut Vec<String>) {
    let payload = match login {
        LoginRounds::Rounds(payload) => payload,
        LoginRounds::Unavailable(reason) => {
            failures.push("登录轮次读取失败".to_string());
            rows.push(IssueRow::unavailable(
                "登录轮次读取失败",
                format!("读取失败：{reason}"),
                "证据不可用：登录轮次读取失败".to_string(),
            ));
            return;
        }
    };
    let Some(rounds) = payload.get("rounds").and_then(Value::as_array) else {
        failures.push("登录轮次读取失败".to_string());
        rows.push(IssueRow::unavailable(
            "登录轮次读取失败",
            "读取失败：登录轮次响应缺少 rounds 数组".to_string(),
            "证据不可用：登录轮次读取失败".to_string(),
        ));
        return;
    };
    let mut failed = 0i64;
    let mut manual = 0i64;
    let mut latest: Option<(f64, String)> = None;
    for round in rounds {
        let outcome = round.get("outcome").and_then(Value::as_str);
        let state = round.get("state").and_then(Value::as_str).unwrap_or("");
        let counted = match outcome {
            Some("manual_takeover") => {
                manual += 1;
                true
            }
            Some(
                "rejected"
                | "terminal"
                | "unavailable"
                | "verification_unknown"
                | "transient"
                | "token_uncommitted",
            ) => {
                failed += 1;
                true
            }
            None if state == "incomplete" => {
                failed += 1;
                true
            }
            _ => false,
        };
        if counted {
            let started = round
                .get("startedAt")
                .and_then(Value::as_i64)
                .map(|millis| millis as f64 / 1000.0)
                .unwrap_or(0.0);
            if latest.as_ref().is_none_or(|(known, _)| started > *known) {
                latest = Some((started, login_round_label(outcome, state, round)));
            }
        }
    }
    if failed + manual == 0 {
        return;
    }
    let mut parts = Vec::new();
    if failed > 0 {
        parts.push(format!("失败 {failed} 轮"));
    }
    if manual > 0 {
        parts.push(format!("人工接管 {manual} 轮"));
    }
    if let Some((_, label)) = latest {
        parts.push(format!("最近 {label}"));
    }
    rows.push(IssueRow {
        title: "登录失败或人工接管".to_string(),
        count: format!("{} 轮", failed + manual),
        detail: parts.join(" · "),
        tone: Tone::Warn,
        evidence: None,
        evidence_note: "证据不在请求页：登录轮次没有独立页面".to_string(),
    });
}

fn login_round_label(outcome: Option<&str>, state: &str, round: &Value) -> String {
    let label = match outcome {
        Some("rejected") => "凭据被拒绝",
        Some("terminal") => "登录终止",
        Some("unavailable") => "登录不可用",
        Some("verification_unknown") => "核验结果未知",
        Some("transient") => "临时失败",
        Some("token_uncommitted") => "令牌未提交",
        Some("manual_takeover") => "人工接管",
        None if state == "incomplete" => "轮次中断",
        _ => "登录问题",
    };
    let mut text = label.to_string();
    if let Some(reason) = round.get("reason").and_then(Value::as_str) {
        text.push_str(&format!("（{reason}）"));
    }
    if let Some(at) = round.get("startedAt").and_then(Value::as_i64) {
        text.push_str(&format!(" {}", login_time(at)));
    }
    text
}

fn login_time(millis: i64) -> String {
    use chrono::TimeZone;
    chrono::FixedOffset::east_opt(8 * 3600)
        .expect("valid Beijing offset")
        .timestamp_millis_opt(millis)
        .single()
        .map(|time| time.format("%m-%d %H:%M:%S").to_string())
        .unwrap_or_else(|| "时间未知".to_string())
}

fn service_issue(
    service: Option<&ServiceView>,
    rows: &mut Vec<IssueRow>,
    failures: &mut Vec<String>,
) {
    let Some(service) = service else {
        failures.push("UM 服务状态读取失败".to_string());
        rows.push(IssueRow::unavailable(
            "服务",
            "不可用（本机实例无操作记录来源）：尚未读取 UM 服务状态".to_string(),
            "证据不可用：尚未读取 UM 服务状态".to_string(),
        ));
        return;
    };
    if service.gateway_missing {
        rows.push(IssueRow {
            title: "本实例网关未运行".to_string(),
            count: "1 项".to_string(),
            detail: service.gateway_detail.clone(),
            tone: Tone::Bad,
            evidence: None,
            evidence_note: "证据不在请求页：网关状态在网关卡与维护菜单".to_string(),
        });
        return;
    }
    if service.state == "运行中" {
        return;
    }
    if service.state == "读取失败" {
        failures.push("UM 服务状态读取失败".to_string());
        rows.push(IssueRow::unavailable(
            "UM 服务状态读取失败",
            format!("读取失败：{}", service.detail),
            "证据不可用：UM 服务状态读取失败".to_string(),
        ));
        return;
    }
    rows.push(IssueRow {
        title: format!("UM 服务{}", service.state),
        count: "1 项".to_string(),
        detail: service.detail.clone(),
        tone: service.tone,
        evidence: None,
        evidence_note: "证据不在请求页：服务状态在总览服务卡与维护菜单".to_string(),
    });
}

fn catalog_test_issue(
    catalog: Option<&CatalogView>,
    probe: Option<&ProbeView>,
    rows: &mut Vec<IssueRow>,
    failures: &mut Vec<String>,
) {
    let catalog_failure = catalog
        .filter(|view| view.tone == Tone::Bad)
        .map(|view| view.status.clone());
    let catalog_unread = catalog.is_none();
    let probe_read_failure = probe
        .filter(|view| view.tone == Tone::Bad)
        .map(|view| view.status.clone());
    let probe_unread = probe.is_none();
    let probe_failed: Vec<&crate::models::ProbeRow> = probe
        .map(|view| {
            view.rows
                .iter()
                .filter(|row| row.state == "未通过")
                .collect()
        })
        .unwrap_or_default();

    let mut parts = Vec::new();
    let mut count = 0i64;
    if let Some(status) = &catalog_failure {
        count += 1;
        parts.push(format!("目录刷新失败：{status}"));
    } else if catalog_unread && probe_failed.is_empty() && probe_read_failure.is_none() {
        failures.push("目录/测试读取失败".to_string());
        parts.push("不可用（本机实例无操作记录来源）：尚未读取模型目录".to_string());
    }
    if let Some(status) = &probe_read_failure {
        failures.push("上下文探测读取失败".to_string());
        parts.push(format!("上下文探测读取失败：{status}"));
    } else if !probe_failed.is_empty() {
        count += probe_failed.len() as i64;
        let models = probe_failed
            .iter()
            .map(|row| format!("{}（{}）", row.name, row.summary))
            .collect::<Vec<_>>()
            .join("；");
        parts.push(format!(
            "上下文探测未通过 {} 个模型：{models}",
            probe_failed.len()
        ));
    } else if probe_unread && catalog_failure.is_none() && !catalog_unread {
        failures.push("目录/测试读取失败".to_string());
        parts.push("不可用（本机实例无操作记录来源）：尚未读取上下文探测".to_string());
    }
    if parts.is_empty() {
        return;
    }
    let unavailable = probe_read_failure.is_some() || (catalog_unread && probe_failed.is_empty());
    rows.push(IssueRow {
        title: "目录/测试".to_string(),
        count: if unavailable {
            "不可用".to_string()
        } else {
            format!("{count} 项")
        },
        detail: parts.join("；"),
        tone: Tone::Bad,
        evidence: None,
        evidence_note: "证据不在请求页：目录与测试在设置页".to_string(),
    });
}

/// The window fields the recent-issues card owns.
///
/// The Slint shell implements this for its `MainWindow`; the headless tests
/// implement it on the same window type so [`apply_recent_issues`] is asserted
/// through the visible properties, not through Slint internals.
pub trait RecentIssuesSurface {
    fn set_issue_rows(&self, rows: &[IssueRow]);
    fn set_issue_status(&self, status: &str);
    fn set_issue_tone(&self, tone: Tone);
}

/// Apply one read result to the recent-issues card.
pub fn apply_recent_issues<S: RecentIssuesSurface>(surface: &S, issues: &RecentIssues) {
    surface.set_issue_rows(&issues.rows);
    surface.set_issue_status(&issues.status);
    surface.set_issue_tone(issues.tone);
}
