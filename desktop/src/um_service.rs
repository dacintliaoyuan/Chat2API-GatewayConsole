//! The overview UM service card.
//!
//! The card reads the app instance's gateway `/admin/um/services` and starts the
//! service through `/admin/um/services/start`. A failed read keeps its reason and
//! never invents a PID; a failed start stays stopped and carries the reason.
//! When the app supervisor has not started the gateway, the card says so and points
//! at the existing gateway start entry instead of pretending the service ran.

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
    /// True only for a stopped service; the card then offers 「启动 UM 服务」.
    pub can_start: bool,
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
            can_start: false,
            gateway_missing: false,
            gateway_detail: String::new(),
            can_start_gateway: false,
        }
    }

    /// The app supervisor has not started the gateway; the service cannot be read.
    pub fn gateway_unavailable(detail: &str) -> Self {
        Self {
            state: "不可用".to_string(),
            detail: "网关未运行，先启动网关".to_string(),
            tone: Tone::Bad,
            checked_at: "核对时间未知".to_string(),
            can_start: false,
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
            can_start: false,
            gateway_missing: true,
            gateway_detail: detail.to_string(),
            can_start_gateway: false,
        }
    }
}

/// The gateway's control-token HTTP surface used by the card.
pub trait ServiceApi {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String>;
    fn post_json(&self, url: &str, token: &str, body: &str) -> Result<Value, String>;
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

    fn post_json(&self, url: &str, token: &str, body: &str) -> Result<Value, String> {
        let response = self
            .agent
            .post(url)
            .set("X-Admin-Control", token)
            .set("Content-Type", "application/json")
            .send_string(body)
            .map_err(|error| api_error(url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 启动结果不是有效 JSON：{error}"))
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

/// One start attempt and its follow-up status read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StartResult {
    pub service: ServiceView,
    pub status: String,
    pub tone: Tone,
    /// True when the start landed and the follow-up status read succeeded.
    pub refreshed: bool,
}

/// Starts the UM service, then re-reads the status.
///
/// A failed start keeps the previous service state and carries the reason; a start
/// that cannot be followed by a status read is never folded into running.
pub fn start_and_refresh(
    api: &dyn ServiceApi,
    base_url: &str,
    token: &str,
    previous: &ServiceView,
) -> StartResult {
    let url = format!("{}/admin/um/services/start", base_url.trim_end_matches('/'));
    if let Err(error) = api.post_json(&url, token, "{}") {
        return StartResult {
            service: previous.clone(),
            status: format!("启动失败：{error}"),
            tone: Tone::Bad,
            refreshed: false,
        };
    }
    match read_result(api, base_url, token) {
        Ok(service) => {
            let running = service.state == "运行中";
            StartResult {
                status: if running {
                    "已启动".to_string()
                } else {
                    format!("启动后状态：{}", service.state)
                },
                tone: if running { Tone::Good } else { Tone::Warn },
                service,
                refreshed: true,
            }
        }
        Err(error) => StartResult {
            service: previous.clone(),
            status: format!("启动成功，但状态刷新失败：{error}"),
            tone: Tone::Warn,
            refreshed: false,
        },
    }
}

/// Applies one start result to the UM service card.
pub fn apply_start_result<S: ServiceSurface>(surface: &S, result: &StartResult) {
    apply_service(surface, &result.service);
    surface.set_service_start_status(&result.status);
    surface.set_service_start_tone(result.tone);
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
        .ok_or_else(|| "UM 服务状态字段缺失".to_string())?;
    let (state, tone, can_start) = state_word(raw_state);
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
        can_start,
        gateway_missing: false,
        gateway_detail: String::new(),
        can_start_gateway: false,
    })
}

fn state_word(state: &str) -> (String, Tone, bool) {
    match state {
        "Running" => ("运行中".to_string(), Tone::Good, false),
        "Starting" => ("启动中".to_string(), Tone::Warn, false),
        "Stopping" => ("停止中".to_string(), Tone::Warn, false),
        "Stopped" => ("未运行".to_string(), Tone::Bad, true),
        "Residual" => ("发现受管残留进程".to_string(), Tone::Bad, true),
        "Degraded" => ("降级".to_string(), Tone::Warn, false),
        "Conflict" => ("端口被占用".to_string(), Tone::Warn, false),
        _ => ("状态未知".to_string(), Tone::Neutral, false),
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
    fn set_service_can_start(&self, value: bool);
    fn set_service_gateway_missing(&self, value: bool);
    fn set_service_gateway_detail(&self, value: &str);
    fn set_service_can_start_gateway(&self, value: bool);
    fn set_service_start_status(&self, value: &str);
    fn set_service_start_tone(&self, tone: Tone);
}

/// Applies one read result to the UM service card.
pub fn apply_service<S: ServiceSurface>(surface: &S, view: &ServiceView) {
    surface.set_service_state(&view.state);
    surface.set_service_detail(&view.detail);
    surface.set_service_tone(view.tone);
    surface.set_service_checked_at(&view.checked_at);
    surface.set_service_can_start(view.can_start);
    surface.set_service_gateway_missing(view.gateway_missing);
    surface.set_service_gateway_detail(&view.gateway_detail);
    surface.set_service_can_start_gateway(view.can_start_gateway);
}
