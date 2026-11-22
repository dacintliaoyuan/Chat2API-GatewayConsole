//! Read-only status overview over the app instance's own gateway.
//!
//! `Probe` is the seam: production uses [`HttpProbe`], tests inject a fake.
//! Every failed or missing field keeps its own reason; a failure never becomes
//! "running", a missing model count never becomes 0. The supervision card comes
//! from the process supervisor, not from the old 8001 console.

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
                    label: "本实例网关",
                    value: "运行中".to_string(),
                    detail: format!("{gateway_base}{models}"),
                    tone: Tone::Good,
                }
            } else {
                failures.push(format!("本实例网关 status={status}"));
                Card {
                    label: "本实例网关",
                    value: "异常".to_string(),
                    detail: format!("{gateway_base} · status={status}{models}"),
                    tone: Tone::Bad,
                }
            }
        }
        Err(error) => {
            failures.push(format!("本实例网关 {error}"));
            Card {
                label: "本实例网关",
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
                    detail: "本实例网关未返回认证状态".to_string(),
                    tone: Tone::Warn,
                },
                "未知".to_string(),
                "本实例网关未返回认证状态".to_string(),
                Tone::Warn,
            ),
        },
        Err(_) => (
            Card {
                label: "UM 认证",
                value: "未知".to_string(),
                detail: "本实例网关未返回认证状态".to_string(),
                tone: Tone::Warn,
            },
            "未知".to_string(),
            "本实例网关未返回认证状态".to_string(),
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
                    detail: "本实例网关未返回上游状态".to_string(),
                    tone: Tone::Warn,
                },
            }
        }
        Err(_) => Card {
            label: "上游",
            value: "未知".to_string(),
            detail: "本实例网关未返回上游状态".to_string(),
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
        "本实例网关 · 状态正常".to_string()
    } else {
        format!("刷新失败：{}", overview.failures.join("；"))
    }
}
