//! Read-only status overview over the app instance's own gateway.
//!
//! `Probe` is the seam: production uses [`HttpProbe`], tests inject a fake.
//! Every failed or missing field keeps its own reason; a failure never becomes
//! "running", a missing model count never becomes 0. The supervision card comes
//! from the process supervisor, not from the old 8001 console.

use std::path::Path;
use std::time::Duration;

use serde_json::Value;

/// Gates the background overview reader. The Slint auto-refresh timer owns the
/// cadence and bumps the refresh epoch; the reader never polls on its own, stays
/// quiet while the window is hidden or minimized, and reads once immediately when
/// the window becomes visible again.
#[derive(Debug, Default)]
pub struct OverviewPoller {
    seen_epoch: Option<u64>,
    paused: bool,
}

impl OverviewPoller {
    pub fn new() -> Self {
        Self::default()
    }

    /// Returns true for the initial read, for a new refresh epoch, and for the
    /// first pass after a pause. A paused poller never reads, so an auto-refresh
    /// tick queued before the window was hidden cannot land on the cards.
    pub fn should_read(&mut self, paused: bool, epoch: u64) -> bool {
        let resumed = self.paused && !paused;
        self.paused = paused;
        if paused {
            return false;
        }
        if self.seen_epoch != Some(epoch) || resumed {
            self.seen_epoch = Some(epoch);
            return true;
        }
        false
    }
}

pub trait Probe {
    fn get_json(&self, url: &str) -> Result<Value, String>;
}

pub struct HttpProbe {
    agent: ureq::Agent,
}

impl HttpProbe {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_millis(2000))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpProbe {
    fn default() -> Self {
        Self::new()
    }
}

impl Probe for HttpProbe {
    fn get_json(&self, url: &str) -> Result<Value, String> {
        self.agent
            .get(url)
            .call()
            .map_err(|error| format!("{url}: {error}"))?
            .into_json::<Value>()
            .map_err(|error| format!("{url}: {error}"))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tone {
    Good,
    Warn,
    Bad,
    Neutral,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Card {
    pub label: &'static str,
    pub value: String,
    pub detail: String,
    pub tone: Tone,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Overview {
    pub gateway: Card,
    pub supervision: Card,
    pub auth: Card,
    pub upstream: Card,
    pub account_label: String,
    pub account_detail: String,
    pub account_tone: Tone,
    pub failures: Vec<String>,
}

pub fn read_overview(probe: &dyn Probe, gateway_base: &str, supervision: Card) -> Overview {
    let gateway_url = format!("{}/health", gateway_base.trim_end_matches('/'));
    let gateway_body = probe.get_json(&gateway_url);
    let mut failures = Vec::new();

    let gateway = match &gateway_body {
        Ok(body) => {
            let status = body
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let models = match body.get("models").and_then(Value::as_u64) {
                Some(count) => format!(" · 模型 {count} 个"),
                None => " · 模型数未知".to_string(),
            };
            if status == "ok" {
                Card {
                    label: "本机网关",
                    value: "运行中".to_string(),
                    detail: format!("{gateway_base}{models}"),
                    tone: Tone::Good,
                }
            } else {
                failures.push(format!("本机网关 status={status}"));
                Card {
                    label: "本机网关",
                    value: "异常".to_string(),
                    detail: format!("{gateway_base} · status={status}{models}"),
                    tone: Tone::Bad,
                }
            }
        }
        Err(error) => {
            failures.push(format!("本机网关 {error}"));
            Card {
                label: "本机网关",
                value: "不可用".to_string(),
                detail: error.clone(),
                tone: Tone::Bad,
            }
        }
    };

    let (auth, account_label, account_detail, account_tone) = match &gateway_body {
        Ok(body) => match body
            .get("authentication")
            .and_then(|authentication| authentication.get("state"))
            .and_then(Value::as_str)
        {
            Some("valid") => {
                let detail = body["authentication"]["label"]
                    .as_str()
                    .unwrap_or("UM 凭据有效")
                    .to_string();
                (
                    Card {
                        label: "UM 认证",
                        value: "已登录".to_string(),
                        detail: detail.clone(),
                        tone: Tone::Good,
                    },
                    "已登录".to_string(),
                    detail,
                    Tone::Good,
                )
            }
            Some("expired") => (
                Card {
                    label: "UM 认证",
                    value: "登录已过期".to_string(),
                    detail: "需要重新登录".to_string(),
                    tone: Tone::Warn,
                },
                "登录已过期".to_string(),
                "需要重新登录".to_string(),
                Tone::Warn,
            ),
            Some("missing") | Some("unconfigured") => (
                Card {
                    label: "UM 认证",
                    value: "尚未登录".to_string(),
                    detail: "未配置 UM 账号，可从「配置账号」登录".to_string(),
                    tone: Tone::Warn,
                },
                "尚未登录".to_string(),
                "未配置 UM 账号，可从「配置账号」登录".to_string(),
                Tone::Warn,
            ),
            Some(other) => (
                Card {
                    label: "UM 认证",
                    value: other.to_string(),
                    detail: format!("认证状态 {other}"),
                    tone: Tone::Bad,
                },
                other.to_string(),
                format!("认证状态 {other}"),
                Tone::Bad,
            ),
            None => (
                Card {
                    label: "UM 认证",
                    value: "未知".to_string(),
                    detail: "本机网关未返回认证状态".to_string(),
                    tone: Tone::Warn,
                },
                "未知".to_string(),
                "本机网关未返回认证状态".to_string(),
                Tone::Warn,
            ),
        },
        Err(_) => (
            Card {
                label: "UM 认证",
                value: "未知".to_string(),
                detail: "本机网关未返回认证状态".to_string(),
                tone: Tone::Warn,
            },
            "未知".to_string(),
            "本机网关未返回认证状态".to_string(),
            Tone::Warn,
        ),
    };

    let upstream = match &gateway_body {
        Ok(body) => {
            let reachability = &body["upstream_reachability"];
            let state = reachability.get("state").and_then(Value::as_str);
            let reason = reachability.get("reason").and_then(Value::as_str);
            let name = body["upstream"]["name"].as_str().unwrap_or("UM 上游");
            let status = reachability
                .get("upstream_status")
                .and_then(Value::as_u64)
                .map(|status| format!(" · HTTP {status}"))
                .unwrap_or_default();
            match state {
                Some("ok") => Card {
                    label: "上游",
                    value: "可达".to_string(),
                    detail: format!("{name}{status}"),
                    tone: Tone::Good,
                },
                Some("unknown") => Card {
                    label: "上游",
                    value: "未知".to_string(),
                    detail: reason.unwrap_or("探测未定").to_string(),
                    tone: Tone::Warn,
                },
                Some(other) => Card {
                    label: "上游",
                    value: other.to_string(),
                    detail: format!(
                        "{name}{status}{}",
                        reason
                            .map(|reason| format!(" · {reason}"))
                            .unwrap_or_default()
                    ),
                    tone: Tone::Bad,
                },
                None => Card {
                    label: "上游",
                    value: "未知".to_string(),
                    detail: "本机网关未返回上游状态".to_string(),
                    tone: Tone::Warn,
                },
            }
        }
        Err(_) => Card {
            label: "上游",
            value: "未知".to_string(),
            detail: "本机网关未返回上游状态".to_string(),
            tone: Tone::Warn,
        },
    };

    Overview {
        gateway,
        supervision,
        auth,
        upstream,
        account_label,
        account_detail,
        account_tone,
        failures,
    }
}

pub fn status_line(overview: &Overview) -> String {
    if overview.failures.is_empty() {
        "本机网关 · 状态正常".to_string()
    } else {
        format!("刷新失败：{}", overview.failures.join("；"))
    }
}

/// One row of the upstream fault history card.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FaultRow {
    pub title: String,
    pub detail: String,
    /// The recovery evidence; empty for an active fault.
    pub evidence: String,
}

/// The upstream fault history the app instance's gateway persisted.
///
/// A missing record is the legal empty state. An unreadable or malformed record
/// keeps its own reason and is never folded into 「当前无上游故障」.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FaultHistory {
    pub status: String,
    pub tone: Tone,
    pub check: String,
    pub active: Option<FaultRow>,
    pub history: Vec<FaultRow>,
}

impl FaultHistory {
    fn empty() -> Self {
        Self {
            status: "当前无上游故障".to_string(),
            tone: Tone::Good,
            check: "尚无上游核验".to_string(),
            active: None,
            history: Vec::new(),
        }
    }

    fn failure(reason: String) -> Self {
        Self {
            status: format!("读取上游故障记录失败：{reason}"),
            tone: Tone::Bad,
            check: String::new(),
            active: None,
            history: Vec::new(),
        }
    }
}

/// Read the fault record the app instance's gateway wrote.
pub fn read_fault_history(path: &Path) -> FaultHistory {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return FaultHistory::empty(),
        Err(error) => return FaultHistory::failure(format!("{error}")),
    };
    let value: Value = match serde_json::from_str(&text) {
        Ok(value) => value,
        Err(error) => return FaultHistory::failure(format!("不是有效 JSON：{error}")),
    };
    let Some(object) = value.as_object() else {
        return FaultHistory::failure("记录格式无效：顶层不是对象".to_string());
    };
    let active = match object.get("active") {
        None | Some(Value::Null) => None,
        Some(value) => match parse_fault_row(value) {
            Some(row) => Some(row),
            None => return FaultHistory::failure("记录格式无效：活动故障字段不完整".to_string()),
        },
    };
    let history = match object.get("history") {
        None => Vec::new(),
        Some(Value::Array(rows)) => {
            let mut parsed = Vec::with_capacity(rows.len());
            for row in rows {
                match parse_recovery_row(row) {
                    Some(row) => parsed.push(row),
                    None => {
                        return FaultHistory::failure(
                            "记录格式无效：历史故障字段不完整".to_string(),
                        )
                    }
                }
            }
            parsed
        }
        Some(_) => return FaultHistory::failure("记录格式无效：history 不是数组".to_string()),
    };
    let check = format_check(object);
    FaultHistory {
        status: if active.is_some() {
            "当前有上游故障".to_string()
        } else {
            "当前无上游故障".to_string()
        },
        tone: if active.is_some() {
            Tone::Bad
        } else {
            Tone::Good
        },
        check,
        active,
        history,
    }
}

fn parse_fault_row(value: &Value) -> Option<FaultRow> {
    let object = value.as_object()?;
    let classification = object.get("classification")?.as_str()?;
    Some(FaultRow {
        title: format!("当前异常 · {}", fault_classification(classification)),
        detail: fault_detail(object, None)?,
        evidence: String::new(),
    })
}

fn parse_recovery_row(value: &Value) -> Option<FaultRow> {
    let object = value.as_object()?;
    let classification = object.get("classification")?.as_str()?;
    let recovered = object.get("recovered_epoch")?.as_f64()?;
    let recovery_source = object.get("recovery_source")?.as_str()?;
    let mut evidence = format!("恢复证据 {}", fault_source(recovery_source));
    if let Some(status) = object.get("recovery_status").and_then(Value::as_u64) {
        evidence.push_str(&format!(" HTTP {status}"));
    }
    if let Some(detail) = object.get("recovery_detail").and_then(Value::as_str) {
        evidence.push_str(&format!(" · {detail}"));
    }
    evidence.push_str(&format!(" · 恢复于 {}", fault_time(recovered)));
    Some(FaultRow {
        title: format!("已恢复 · {}", fault_classification(classification)),
        detail: fault_detail(object, Some(recovered))?,
        evidence,
    })
}

fn fault_detail(object: &serde_json::Map<String, Value>, recovered: Option<f64>) -> Option<String> {
    let classification = object.get("classification")?.as_str()?;
    let failures = object.get("failures")?.as_u64()?;
    let started = object.get("started_epoch")?.as_f64()?;
    let last = object.get("last_epoch")?.as_f64()?;
    let source = object.get("source")?.as_str()?;
    let status = object.get("upstream_status").and_then(Value::as_u64);
    let summary = object.get("summary").and_then(Value::as_str);
    let mut detail = fault_classification(classification);
    if let Some(status) = status {
        detail.push_str(&format!(" HTTP {status}"));
    }
    if let Some(summary) = summary {
        detail.push_str(&format!(" · {summary}"));
    }
    detail.push_str(&format!(
        " · 失败 {failures} 次 · 开始 {} · 最近 {}",
        fault_time(started),
        fault_time(last)
    ));
    match recovered {
        Some(recovered) => detail.push_str(&format!(" · 恢复于 {}", fault_time(recovered))),
        None => detail.push_str(" · 尚未恢复"),
    }
    detail.push_str(&format!(" · 来源 {}", fault_source(source)));
    Some(detail)
}

fn format_check(object: &serde_json::Map<String, Value>) -> String {
    let mut check = match object.get("last_check") {
        None | Some(Value::Null) => "尚无上游核验".to_string(),
        Some(Value::Object(last)) => {
            let epoch = last.get("epoch").and_then(Value::as_f64);
            let result = last.get("result").and_then(Value::as_str);
            match (epoch, result) {
                (Some(epoch), Some(result)) => {
                    let mut text =
                        format!("最近核验 {} · {}", fault_time(epoch), fault_result(result));
                    if let Some(status) = last.get("upstream_status").and_then(Value::as_u64) {
                        text.push_str(&format!(" HTTP {status}"));
                    }
                    if let Some(source) = last.get("source").and_then(Value::as_str) {
                        text.push_str(&format!(" · {}", fault_source(source)));
                    }
                    text
                }
                _ => "最近核验记录格式无效".to_string(),
            }
        }
        Some(_) => "最近核验记录格式无效".to_string(),
    };
    match object.get("next_check_epoch") {
        None | Some(Value::Null) => {}
        Some(value) => match value.as_f64() {
            Some(next) => check.push_str(&format!(" · 下次复查 {}", fault_time(next))),
            None => check.push_str(" · 下次复查时间格式无效"),
        },
    }
    check
}

fn fault_time(epoch: f64) -> String {
    use chrono::TimeZone;
    chrono::Local
        .timestamp_opt(epoch as i64, 0)
        .single()
        .map(|time| time.format("%m-%d %H:%M:%S").to_string())
        .unwrap_or_else(|| "时间未知".to_string())
}

fn fault_classification(code: &str) -> String {
    match code {
        "upstream_5xx" => "上游 5xx".to_string(),
        "timeout" => "超时".to_string(),
        "network_error" => "网络错误".to_string(),
        "stream_error" => "流中断".to_string(),
        other => other.to_string(),
    }
}

fn fault_source(code: &str) -> String {
    match code {
        "background_probe" => "后台探测".to_string(),
        "active_probe" => "主动探测".to_string(),
        "business_request" => "业务请求".to_string(),
        other => other.to_string(),
    }
}

fn fault_result(code: &str) -> String {
    match code {
        "valid" => "有效".to_string(),
        "upstream_5xx" => "上游 5xx".to_string(),
        "timeout" => "超时".to_string(),
        "network_error" => "网络错误".to_string(),
        "non_json_response" => "非 JSON 响应".to_string(),
        other => other.to_string(),
    }
}

/// The Slint surface of the upstream fault history card.
///
/// The shell implements it on the main window; the headless tests implement it on
/// the same window type so the card's copy and layout stay the tested seam.
pub trait FaultSurface {
    fn set_fault_status(&self, status: &str);
    fn set_fault_tone(&self, tone: Tone);
    fn set_fault_check(&self, check: &str);
    fn set_fault_active(&self, active: Option<&FaultRow>);
    fn set_fault_history(&self, rows: &[FaultRow]);
}

/// Apply one read result to the fault history card.
pub fn apply_fault_history<S: FaultSurface>(surface: &S, history: &FaultHistory) {
    surface.set_fault_status(&history.status);
    surface.set_fault_tone(history.tone);
    surface.set_fault_check(&history.check);
    surface.set_fault_active(history.active.as_ref());
    surface.set_fault_history(&history.history);
}
