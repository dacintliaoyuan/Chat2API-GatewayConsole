//! Console (8002) error bodies.
//!
//! `usagesrv.py` answers every console JSON error as `{"error": "..."}` plus
//! `Cache-Control: no-store`; the cloud capability refusal adds `capability`,
//! `role` and `detail`. Errors that close the connection keep the Python
//! `close_connection = True` behavior.

use serde_json::{json, Value};

use um_gateway::http::Response;

#[derive(Debug, Clone, PartialEq)]
pub struct ConsoleError {
    pub status: u16,
    pub body: Value,
    pub close: bool,
}

impl ConsoleError {
    pub fn new(status: u16, body: Value) -> Self {
        Self {
            status,
            body,
            close: false,
        }
    }

    pub fn closing(mut self) -> Self {
        self.close = true;
        self
    }

    pub fn response(&self) -> Response {
        let mut response =
            Response::json(self.status, &self.body).with_header("Cache-Control", "no-store");
        response.close = self.close;
        response
    }

    pub fn unknown_path() -> Self {
        Self::new(404, json!({"error": "unknown path"}))
    }

    pub fn unknown_operation() -> Self {
        Self::new(404, json!({"error": "unknown operation"}))
    }

    pub fn local_only() -> Self {
        Self::new(403, json!({"error": "local-only service"})).closing()
    }

    pub fn duplicate_header(name: &str) -> Self {
        Self::new(400, json!({"error": format!("duplicate header: {name}")})).closing()
    }

    pub fn body(status: u16, code: &str) -> Self {
        Self::new(status, json!({"error": code})).closing()
    }

    /// A body-field rejection after the body was fully read: Python keeps the
    /// connection open (`_reject_unknown_fields` does not set close_connection).
    pub fn body_open(status: u16, code: &str) -> Self {
        Self::new(status, json!({"error": code}))
    }

    pub fn capability_not_assembled(prefix: &str) -> Self {
        Self::new(
            503,
            json!({
                "error": "capability_not_assembled",
                "capability": prefix,
                "role": "cloud",
                "detail": "云端角色只装配 UM 分享：该读面未装配，不探测/不读取非 UM 资源",
            }),
        )
    }

    pub fn console_unavailable() -> Self {
        Self::new(503, json!({"error": "console subsystem unavailable"}))
    }

    pub fn runtime_unavailable() -> Self {
        Self::new(502, json!({"error": "runtime status unavailable"}))
    }

    pub fn summary_unavailable() -> Self {
        Self::new(500, json!({"error": "summary unavailable"}))
    }

    pub fn not_implemented(detail: &str) -> Self {
        Self::new(501, json!({"error": "not_implemented", "detail": detail}))
    }
}

impl From<um_gateway::GatewayError> for ConsoleError {
    fn from(error: um_gateway::GatewayError) -> Self {
        Self::body(error.status, &error.message).closing()
    }
}
