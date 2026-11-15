//! Unified gateway error body.
//!
//! Every error is serialized as `{"error": {"message", "type", "detail"}}` plus
//! the optional Python extras `code`, `reason` and `upstream_status`. The
//! messages and codes are ported from `chat2api/chat2api.py`; the console body
//! boundary keeps the `usagesrv.py` codes for the local control-token routes.

use serde_json::{json, Map, Value};

use crate::http::Response;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayError {
    pub status: u16,
    pub message: String,
    pub error_type: String,
    pub detail: Option<Value>,
    pub code: Option<String>,
    pub reason: Option<Value>,
    pub upstream_status: Option<u16>,
}

impl GatewayError {
    pub fn new(status: u16, message: impl Into<String>, error_type: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
            error_type: error_type.into(),
            detail: None,
            code: None,
            reason: None,
            upstream_status: None,
        }
    }

    pub fn with_detail(mut self, detail: Value) -> Self {
        self.detail = Some(detail);
        self
    }

    pub fn with_code(mut self, code: impl Into<String>) -> Self {
        self.code = Some(code.into());
        self
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Self {
        self.reason = Some(Value::String(reason.into()));
        self
    }

    pub fn with_upstream_status(mut self, status: u16) -> Self {
        self.upstream_status = Some(status);
        self
    }

    pub fn body(&self) -> Value {
        let mut error = Map::new();
        error.insert("message".to_string(), Value::String(self.message.clone()));
        error.insert("type".to_string(), Value::String(self.error_type.clone()));
        error.insert(
            "detail".to_string(),
            self.detail.clone().unwrap_or(Value::Null),
        );
        if let Some(code) = &self.code {
            error.insert("code".to_string(), Value::String(code.clone()));
        }
        if let Some(reason) = &self.reason {
            error.insert("reason".to_string(), reason.clone());
        }
        if let Some(status) = self.upstream_status {
            error.insert("upstream_status".to_string(), json!(status));
        }
        json!({"error": Value::Object(error)})
    }

    pub fn response(&self) -> Response {
        Response::json(self.status, &self.body()).closing()
    }

    // ----- 8003 share gateway errors (chat2api/chat2api.py) -----

    pub fn invalid_request(message: impl Into<String>) -> Self {
        Self::new(400, message, "invalid_request")
    }

    pub fn invalid_json() -> Self {
        Self::new(400, "Request body is not valid JSON", "invalid_json")
    }

    pub fn invalid_body() -> Self {
        Self::new(
            400,
            "Request body must be a JSON object",
            "invalid_body",
        )
    }

    pub fn payload_too_large() -> Self {
        Self::new(413, "Request body too large", "payload_too_large")
    }

    pub fn unsupported_chat_request() -> Self {
        Self::new(
            400,
            "unsupported_chat_request",
            "unsupported_chat_request",
        )
    }

    pub fn unsupported_messages_request() -> Self {
        Self::new(
            400,
            "unsupported_messages_request",
            "unsupported_messages_request",
        )
    }

    pub fn unsupported_responses_request() -> Self {
        Self::new(
            400,
            "unsupported_responses_request",
            "chat2api_error",
        )
    }

    pub fn route_not_shared() -> Self {
        Self::new(404, "route_not_shared", "route_not_shared")
    }

    pub fn unknown_path(path: &str) -> Self {
        Self::new(404, format!("Unknown path: {path}"), "chat2api_error")
    }

    pub fn invalid_api_key() -> Self {
        Self::new(401, "invalid_api_key", "share_gateway_error")
            .with_code("invalid_api_key")
    }

    pub fn local_auth_error() -> Self {
        Self::new(
            401,
            "Local API key missing or invalid",
            "local_auth_error",
        )
    }

    pub fn origin_denied() -> Self {
        Self::new(
            403,
            "Browser origins are not allowed by default; list the origin in \
             CHAT2API_ALLOWED_ORIGINS to permit it",
            "origin_denied",
        )
    }

    pub fn control_denied_endpoint() -> Self {
        Self::new(403, "Local management endpoint denied", "chat2api_error")
    }

    pub fn control_denied_token() -> Self {
        Self::new(
            403,
            "Local management token missing or invalid",
            "chat2api_error",
        )
    }

    pub fn sharing_limit_reached(reason: Option<&str>, detail: Option<&str>) -> Self {
        let mut error = Self::new(
            429,
            "sharing_limit_reached",
            "share_gateway_error",
        )
        .with_code("sharing_limit_reached");
        error.reason = Some(
            reason
                .map(|reason| Value::String(reason.to_string()))
                .unwrap_or(Value::Null),
        );
        if let Some(detail) = detail {
            error.detail = Some(Value::String(detail.to_string()));
        }
        error
    }

    pub fn reauthentication_required(detail: Option<&str>) -> Self {
        let mut error = Self::new(
            503,
            "Open WebUI authentication requires an interactive sign-in",
            "reauthentication_required",
        );
        if let Some(detail) = detail {
            error.detail = Some(Value::String(detail.to_string()));
        }
        error
    }

    pub fn upstream_unavailable(status: Option<u16>) -> Self {
        let mut error = Self::new(502, "upstream_unavailable", "upstream_unavailable");
        if let Some(status) = status {
            error = error.with_upstream_status(status);
        }
        error
    }

    pub fn models_unavailable() -> Self {
        Self::new(
            502,
            "Failed to fetch model list (auth may have expired)",
            "chat2api_error",
        )
    }

    pub fn not_implemented(feature: &str) -> Self {
        Self::new(
            501,
            "not_implemented",
            "not_implemented",
        )
        .with_detail(Value::String(feature.to_string()))
    }

    // ----- console (8002) request-boundary codes (usagesrv.py) -----

    pub fn console_body(status: u16, code: &str) -> Self {
        let error_type = match code {
            "invalid_json" => "invalid_json",
            "request_too_large" => "request_too_large",
            _ => "invalid_request",
        };
        Self::new(status, code, error_type)
    }

    pub fn duplicate_header(name: &str) -> Self {
        Self::new(400, format!("duplicate header: {name}"), "invalid_request")
    }

    pub fn unknown_fields(fields: &[String]) -> Self {
        let list = fields
            .iter()
            .map(|field| format!("'{field}'"))
            .collect::<Vec<_>>()
            .join(", ");
        Self::new(400, format!("unknown field(s): [{list}]"), "invalid_request")
    }
}
