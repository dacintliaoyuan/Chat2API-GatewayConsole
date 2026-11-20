//! Read-only status overview over the local 8000 gateway and 8001 console.
//!
//! `Probe` is the seam: production uses [`HttpProbe`], tests inject a fake.
//! Every failed or missing field keeps its own reason; a failure never becomes
//! "running", a missing model count never becomes 0.

use std::time::Duration;

use serde_json::Value;

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
    pub console: Card,
    pub auth: Card,
    pub upstream: Card,
    pub account_label: String,
    pub account_detail: String,
    pub account_tone: Tone,
    pub failures: Vec<String>,
}

pub fn read_overview(probe: &dyn Probe, gateway_base: &str, console_base: &str) -> Overview {
    let gateway_url = format!("{}/health", gateway_base.trim_end_matches('/'));
    let console_url = format!("{}/health", console_base.trim_end_matches('/'));
    let gateway_body = probe.get_json(&gateway_url);
    let console_body = probe.get_json(&console_url);
    let mut failures = Vec::new();

    let gateway = match &gateway_body {
        Ok(body) => {
            let status = body.get("status").and_then(Value::as_str).unwrap_or("unknown");
            let models = match body.get("models").and_then(Value::as_u64) {
                Some(count) => format!(" · 模型 {count} 个"),
                None => " · 模型数未知".to_string(),
            };
            if status == "ok" {
                Card {
                    label: "8000 网关",
                    value: "运行中".to_string(),
                    detail: format!("{gateway_base}{models}"),
                    tone: Tone::Good,
                }
            } else {
                failures.push(format!("8000 status={status}"));
                Card {
                    label: "8000 网关",
                    value: "异常".to_string(),
                    detail: format!("{gateway_base} · status={status}{models}"),
                    tone: Tone::Bad,
                }
            }
        }
        Err(error) => {
            failures.push(format!("8000 {error}"));
            Card {
                label: "8000 网关",
                value: "不可用".to_string(),
                detail: error.clone(),
                tone: Tone::Bad,
            }
        }
    };

    let console = match &console_body {
        Ok(body) => {
            let status = body.get("status").and_then(Value::as_str).unwrap_or("unknown");
            let service = body
                .get("service")
                .and_then(Value::as_str)
                .unwrap_or("未知服务");
            let port = body
                .get("port")
                .and_then(Value::as_u64)
                .map(|port| format!(" · {port}"))
                .unwrap_or_default();
            if status == "ok" {
                Card {
                    label: "8001 控制台",
                    value: "运行中".to_string(),
                    detail: format!("{console_base} · {service}{port}"),
                    tone: Tone::Good,
                }
            } else {
                failures.push(format!("8001 status={status}"));
                Card {
                    label: "8001 控制台",
                    value: "异常".to_string(),
                    detail: format!("{console_base} · status={status}"),
                    tone: Tone::Bad,
                }
            }
        }
        Err(error) => {
            failures.push(format!("8001 {error}"));
            Card {
                label: "8001 控制台",
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
                    detail: "8000 未返回认证状态".to_string(),
                    tone: Tone::Warn,
                },
                "未知".to_string(),
                "8000 未返回认证状态".to_string(),
                Tone::Warn,
            ),
        },
        Err(_) => (
            Card {
                label: "UM 认证",
                value: "未知".to_string(),
                detail: "8000 未返回认证状态".to_string(),
                tone: Tone::Warn,
            },
            "未知".to_string(),
            "8000 未返回认证状态".to_string(),
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
                        reason.map(|reason| format!(" · {reason}")).unwrap_or_default()
                    ),
                    tone: Tone::Bad,
                },
                None => Card {
                    label: "上游",
                    value: "未知".to_string(),
                    detail: "8000 未返回上游状态".to_string(),
                    tone: Tone::Warn,
                },
            }
        }
        Err(_) => Card {
            label: "上游",
            value: "未知".to_string(),
            detail: "8000 未返回上游状态".to_string(),
            tone: Tone::Warn,
        },
    };

    Overview {
        gateway,
        console,
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
        "只读 8000/8001 · 状态正常".to_string()
    } else {
        format!("只读 8000/8001 · {}", overview.failures.join("；"))
    }
}
