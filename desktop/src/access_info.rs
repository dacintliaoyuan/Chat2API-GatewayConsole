//! The settings page's access info (#152).
//!
//! The App reads the instance gateway's `/admin/connection` face and shows the UM
//! interface address, the interface type and the gateway's hints in the existing
//! model API address row. The address is only shown when the gateway confirmed
//! it, or when the gateway is not running and the address is derived from the
//! current port (clearly marked as a configured address, never as connected). A
//! failed read keeps its reason and never invents an address.

use std::time::Duration;

use serde_json::Value;

use crate::gateway::ControlToken;
use crate::overview::Tone;

/// One access-info read the settings page shows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccessInfoView {
    /// The base URL shown and copied, without the OpenAI path suffix.
    pub address: String,
    /// The gateway's OpenAI-compatible base URL (`.../v1`).
    pub full_base_url: String,
    /// The visible interface type, or the reason it is missing.
    pub interface_type: String,
    /// The gateway's hints; a missing list becomes one reason line.
    pub hints: Vec<String>,
    /// The read state copy: success, failure reason or the not-running note.
    pub status: String,
    pub tone: Tone,
    /// True only when `/admin/connection` answered; a derived address for a
    /// gateway that is not running is never reported as connected.
    pub connected: bool,
    /// True when `address` may be shown and copied.
    pub address_available: bool,
    /// The explicit note for a derived address; empty after a successful read.
    pub address_note: String,
}

/// Parses the gateway's `/admin/connection` payload.
///
/// A missing or empty `base_url` is not a legal empty answer: the error keeps
/// its reason so the page never shows a made-up address. A missing interface type
/// or hint list is displayed as its own reason instead of an empty row.
pub fn parse_connection(payload: &Value) -> Result<AccessInfoView, String> {
    let base_url = payload
        .get("base_url")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|url| !url.is_empty())
        .ok_or_else(|| "接入信息格式无效：缺少 base_url".to_string())?;
    let protocol_path = text(payload.get("protocol_path"));
    let address = payload
        .get("port")
        .and_then(Value::as_u64)
        .and_then(|port| u16::try_from(port).ok())
        .map(|port| format!("http://127.0.0.1:{port}"))
        .unwrap_or_else(|| strip_protocol_path(base_url, protocol_path.as_deref()));
    Ok(AccessInfoView {
        address,
        full_base_url: base_url.to_string(),
        interface_type: interface_type(payload),
        hints: hints(payload),
        status: "接入信息已从本实例网关读取".to_string(),
        tone: Tone::Good,
        connected: true,
        address_available: true,
        address_note: String::new(),
    })
}

/// The window fields the settings page's access-info section owns.
///
/// The Slint shell implements this for its `MainWindow`; the headless tests
/// implement it on the same window type so [`apply_access_info`] is asserted
/// through the visible properties, not through Slint internals.
pub trait AccessInfoSurface {
    fn set_access_address(&self, address: &str);
    fn set_access_full_base_url(&self, base_url: &str);
    fn set_access_interface_type(&self, interface_type: &str);
    fn set_access_hints(&self, hints: &[String]);
    fn set_access_status(&self, status: &str);
    fn set_access_tone(&self, tone: Tone);
    fn set_access_address_note(&self, note: &str);
    fn set_access_address_available(&self, available: bool);
}

/// Apply one read result to the settings page's single access-info row.
pub fn apply_access_info<S: AccessInfoSurface>(surface: &S, view: &AccessInfoView) {
    surface.set_access_address(&view.address);
    surface.set_access_full_base_url(&view.full_base_url);
    surface.set_access_interface_type(&view.interface_type);
    surface.set_access_hints(&view.hints);
    surface.set_access_status(&view.status);
    surface.set_access_tone(view.tone);
    surface.set_access_address_note(&view.address_note);
    surface.set_access_address_available(view.address_available);
}

/// A failed read keeps its reason and never shows a made-up address.
///
/// When the gateway is not running there is no read face to call, so the address
/// is derived from the current port and explicitly marked as the configured
/// address; it is never reported as connected.
pub fn access_failure(reason: String, gateway_running: bool, port: u16) -> AccessInfoView {
    if gateway_running {
        return AccessInfoView {
            address: String::new(),
            full_base_url: String::new(),
            interface_type: "不可用（读取接入信息失败）".to_string(),
            hints: Vec::new(),
            status: format!("读取接入信息失败：{reason}"),
            tone: Tone::Bad,
            connected: false,
            address_available: false,
            address_note: String::new(),
        };
    }
    let address = format!("http://127.0.0.1:{port}");
    AccessInfoView {
        address: address.clone(),
        full_base_url: format!("{address}/v1"),
        interface_type: "不可用（网关未运行）".to_string(),
        hints: Vec::new(),
        status: "网关未运行，接入信息未读取".to_string(),
        tone: Tone::Neutral,
        connected: false,
        address_available: true,
        address_note: "网关未运行，地址为当前配置".to_string(),
    }
}

fn interface_type(payload: &Value) -> String {
    let protocol = text(payload.get("protocol")).or_else(|| text(payload.get("interface_type")));
    let path = text(payload.get("protocol_path"));
    match protocol {
        Some(protocol) => {
            let protocol = if protocol == "openai-compatible" {
                "OpenAI 兼容".to_string()
            } else {
                protocol
            };
            match path {
                Some(path) => format!("{protocol}（{path}）"),
                None => protocol,
            }
        }
        None => "未提供（网关没有给出接口类型）".to_string(),
    }
}

fn hints(payload: &Value) -> Vec<String> {
    let hints: Vec<String> = payload
        .get("hints")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::trim)
                .filter(|hint| !hint.is_empty())
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    if hints.is_empty() {
        vec!["未提供（网关没有给出接入提示）".to_string()]
    } else {
        hints
    }
}

fn text(value: Option<&Value>) -> Option<String> {
    value
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(str::to_string)
}

fn strip_protocol_path(base_url: &str, protocol_path: Option<&str>) -> String {
    let base_url = base_url.trim_end_matches('/');
    match protocol_path {
        Some(path) if !path.is_empty() => base_url
            .strip_suffix(path)
            .unwrap_or(base_url)
            .trim_end_matches('/')
            .to_string(),
        _ => base_url.to_string(),
    }
}

/// Read one access-info snapshot from the gateway.
pub fn read_access_info(
    reader: &dyn AccessInfoReader,
    base_url: &str,
    token: &ControlToken,
) -> Result<AccessInfoView, String> {
    reader
        .read_connection(base_url, token)
        .and_then(|payload| parse_connection(&payload))
}

/// The gateway's `/admin/connection` read, behind a seam so the settings page is
/// tested without a live gateway.
pub trait AccessInfoReader: Send + Sync {
    fn read_connection(&self, base_url: &str, token: &ControlToken) -> Result<Value, String>;
}

/// The production reader.
pub struct HttpAccessInfoReader {
    agent: ureq::Agent,
}

impl HttpAccessInfoReader {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_secs(5))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpAccessInfoReader {
    fn default() -> Self {
        Self::new()
    }
}

impl AccessInfoReader for HttpAccessInfoReader {
    fn read_connection(&self, base_url: &str, token: &ControlToken) -> Result<Value, String> {
        let url = format!("{}/admin/connection", base_url.trim_end_matches('/'));
        let response = self
            .agent
            .get(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map_err(|error| connection_error(&url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 接入信息不是有效 JSON：{error}"))
    }
}

fn connection_error(url: &str, error: ureq::Error) -> String {
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
                .filter(|detail| !detail.is_empty() && detail.len() <= 120);
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
