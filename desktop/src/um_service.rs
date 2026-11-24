//! The overview UM service card.
//!
//! The card reads the app instance's gateway `/admin/um/services`. The gateway is
//! itself the UM service, so the read face reports the process answering the
//! request; when the gateway is not running the App cannot read the face and shows
//! the gateway start entry instead. A failed read keeps its reason and never
//! invents a PID.

use std::time::Duration;

use serde_json::Value;

use crate::overview::Tone;

pub const SERVICE_KEY: &str = "chat2api";

/// One read of the UM service status, or the reason it could not be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceView {
    pub state: String,
    pub detail: String,
    pub tone: Tone,
    pub checked_at: String,
    /// The gateway itself is not running; the card points at the gateway entry.
    pub gateway_missing: bool,
    pub gateway_detail: String,
    pub can_start_gateway: bool,
}

impl ServiceView {
    /// The read failed; the reason stays visible and no PID is invented.
    pub fn failure(reason: impl Into<String>) -> Self {
        Self {
            state: "读取失败".to_string(),
            detail: reason.into(),
            tone: Tone::Bad,
            checked_at: "核对时间未知".to_string(),
            gateway_missing: false,
            gateway_detail: String::new(),
            can_start_gateway: false,
        }
    }

    /// The app supervisor has not started the gateway; the service cannot be read.
    pub fn gateway_unavailable(detail: &str) -> Self {
        Self {
            state: "不可用".to_string(),
            detail: "本机网关未运行，先启动本机网关".to_string(),
            tone: Tone::Bad,
            checked_at: "核对时间未知".to_string(),
            gateway_missing: true,
            gateway_detail: detail.to_string(),
            can_start_gateway: true,
        }
    }

    /// The gateway is still starting; the service read waits for it.
    pub fn gateway_starting(detail: &str) -> Self {
        Self {
            state: "启动中".to_string(),
            detail: "网关正在启动，请稍候".to_string(),
            tone: Tone::Warn,
            checked_at: "核对时间未知".to_string(),
            gateway_missing: true,
            gateway_detail: detail.to_string(),
            can_start_gateway: false,
        }
    }
}

/// The gateway's control-token HTTP surface used by the card.
pub trait ServiceApi {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String>;
}

pub struct HttpServiceApi {
    agent: ureq::Agent,
}

impl HttpServiceApi {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_millis(2000))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpServiceApi {
    fn default() -> Self {
        Self::new()
    }
}

impl ServiceApi for HttpServiceApi {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String> {
        let response = self
            .agent
            .get(url)
            .set("X-Admin-Control", token)
            .call()
            .map_err(|error| api_error(url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 服务状态不是有效 JSON：{error}"))
    }
}

/// Maps a transport/HTTP failure without relaying the response body.
///
/// Only the gateway's short error code is kept, so a response that echoes a
/// credential cannot reach the card.
fn api_error(url: &str, error: ureq::Error) -> String {
    match error {
        ureq::Error::Status(code, response) => {
            let detail = response
                .into_json::<Value>()
                .ok()
                .and_then(|body| {
                    body.get("error")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .filter(|detail| safe_reason(detail));
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

fn safe_reason(reason: &str) -> bool {
    !reason.is_empty()
        && reason.len() <= 120
        && reason
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-.:".contains(&byte))
}

/// Reads the gateway's service snapshot and folds a failure into a card reason.
pub fn read(api: &dyn ServiceApi, base_url: &str, token: &str) -> ServiceView {
    match read_result(api, base_url, token) {
        Ok(view) => view,
        Err(reason) => ServiceView::failure(reason),
    }
}

fn read_result(api: &dyn ServiceApi, base_url: &str, token: &str) -> Result<ServiceView, String> {
    let url = format!("{}/admin/um/services", base_url.trim_end_matches('/'));
    let payload = api.get_json(&url, token)?;
    parse_services(&payload)
}

/// Parses the gateway's `services` snapshot.
///
/// A missing or malformed snapshot is an error, never a fabricated service row.
pub fn parse_services(payload: &Value) -> Result<ServiceView, String> {
    let services = payload
        .get("services")
        .and_then(Value::as_array)
        .ok_or_else(|| "网关没有返回 services 数组".to_string())?;
    let entry = services
        .iter()
        .find(|entry| entry.get("key").and_then(Value::as_str) == Some(SERVICE_KEY))
        .ok_or_else(|| "网关没有报告 chat2api 服务".to_string())?;
    let raw_state = entry
        .get("state")
        .and_then(Value::as_str)
        .ok_or_else(|| "本机网关状态字段缺失".to_string())?;
    let (state, tone) = state_word(raw_state);
    let port = entry
        .get("port")
        .and_then(Value::as_u64)
        .and_then(|port| u16::try_from(port).ok());
    let pid = entry
        .get("processIds")
        .and_then(Value::as_array)
        .and_then(|ids| ids.first())
        .and_then(Value::as_u64)
        .and_then(|pid| u32::try_from(pid).ok());

    let mut facts = Vec::new();
    facts.push(match port {
        Some(port) => format!("端口 {port}"),
        None => "端口未知".to_string(),
    });
    match pid {
        Some(pid) => facts.push(format!("PID {pid}")),
        None if matches!(raw_state, "Running" | "Starting" | "Stopping") => {
            facts.push("PID 未知".to_string())
        }
        None => {}
    }
    if let Some(detail) = entry
        .get("detail")
        .and_then(Value::as_str)
        .filter(|detail| !detail.is_empty())
    {
        facts.push(detail.to_string());
    }
    if entry.get("healthy").and_then(Value::as_bool) == Some(false) {
        facts.push("健康检查未通过".to_string());
    }

    Ok(ServiceView {
        state,
        detail: facts.join(" · "),
        tone,
        checked_at: checked_at(payload),
        gateway_missing: false,
        gateway_detail: String::new(),
        can_start_gateway: false,
    })
}

fn state_word(state: &str) -> (String, Tone) {
    match state {
        "Running" => ("运行中".to_string(), Tone::Good),
        "Starting" => ("启动中".to_string(), Tone::Warn),
        "Stopping" => ("停止中".to_string(), Tone::Warn),
        "Stopped" => ("未运行".to_string(), Tone::Bad),
        "Residual" => ("发现受管残留进程".to_string(), Tone::Bad),
        "Degraded" => ("降级".to_string(), Tone::Warn),
        "Conflict" => ("端口被占用".to_string(), Tone::Warn),
        _ => ("状态未知".to_string(), Tone::Neutral),
    }
}

fn checked_at(payload: &Value) -> String {
    use chrono::TimeZone;
    let Some(millis) = payload.get("checkedAt").and_then(Value::as_i64) else {
        return "核对时间未知".to_string();
    };
    chrono::Local
        .timestamp_millis_opt(millis)
        .single()
        .map(|time| format!("核对于 {}", time.format("%H:%M:%S")))
        .unwrap_or_else(|| "核对时间未知".to_string())
}

/// The Slint surface of the UM service card.
///
/// The shell implements it on the main window; the headless tests implement it on
/// the same window type so the card's copy and layout stay the tested seam.
pub trait ServiceSurface {
    fn set_service_state(&self, value: &str);
    fn set_service_detail(&self, value: &str);
    fn set_service_tone(&self, tone: Tone);
    fn set_service_checked_at(&self, value: &str);
    fn set_service_gateway_missing(&self, value: bool);
    fn set_service_gateway_detail(&self, value: &str);
    fn set_service_can_start_gateway(&self, value: bool);
}

/// Applies one read result to the UM service card.
pub fn apply_service<S: ServiceSurface>(surface: &S, view: &ServiceView) {
    surface.set_service_state(&view.state);
    surface.set_service_detail(&view.detail);
    surface.set_service_tone(view.tone);
    surface.set_service_checked_at(&view.checked_at);
    surface.set_service_gateway_missing(view.gateway_missing);
    surface.set_service_gateway_detail(&view.gateway_detail);
    surface.set_service_can_start_gateway(view.can_start_gateway);
}
