//! Unified gateway error envelope.
//!
//! Chat/Responses/models/version and `/api/*` serialize as the OpenAI shape
//! `{"error": {"message", "type", "code", "param", "detail"}}`; `/v1/messages`
//! serializes as the Anthropic shape
//! `{"type": "error", "error": {"type", "message", "code", "param", "detail"}}`.
//! `type` is the error category, `code` is the stable machine code, `param` is the
//! offending field or `null`, and `detail` carries structured diagnostics.
//!
//! The messages and codes are ported from `chat2api/chat2api.py`; the console body
//! boundary keeps the `usagesrv.py` codes for the local control-token routes. The
//! 8001 console `{"error": "machine_code"}` shape is a different surface and is
//! not produced here.

use serde_json::{json, Map, Value};

use crate::http::Response;

/// The protocol shape used to serialize one local gateway error.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ErrorProtocol {
    /// OpenAI `{"error": {...}}`, used by Chat, Responses, models, version and
    /// the local `/api/*` aliases.
    #[default]
    OpenAi,
    /// Anthropic `{"type": "error", "error": {...}}`, used by `/v1/messages`.
    Anthropic,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayError {
    pub status: u16,
    pub message: String,
    /// The error category (`invalid_request_error`, `authentication_error`,
    /// `permission_error`, `not_found_error`, `rate_limit_error`, `api_error`,
    /// `overloaded_error`, `upstream_error`).
    pub error_type: String,
    /// The stable machine code.
    pub code: String,
    /// The offending request field, when one is known.
    pub param: Option<Box<String>>,
    pub detail: Option<Box<Value>>,
    /// Boxed so the error stays under clippy's large-error threshold.
    pub reason: Option<Box<Value>>,
    pub upstream_status: Option<u16>,
}

impl GatewayError {
    pub fn new(status: u16, message: impl Into<String>, code: impl Into<String>) -> Self {
        let code = code.into();
        let error_type = category_for(status, &code).to_string();
        Self {
            status,
            message: message.into(),
            error_type,
            code,
            param: None,
            detail: None,
            reason: None,
            upstream_status: None,
        }
    }

    pub fn with_detail(mut self, detail: Value) -> Self {
        self.detail = Some(Box::new(detail));
        self
    }

    pub fn with_code(mut self, code: impl Into<String>) -> Self {
        self.code = code.into();
        self.error_type = category_for(self.status, &self.code).to_string();
        self
    }

    pub fn with_param(mut self, param: impl Into<String>) -> Self {
        self.param = Some(Box::new(param.into()));
        self
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Self {
        self.reason = Some(Box::new(Value::String(reason.into())));
        self
    }

    pub fn with_upstream_status(mut self, status: u16) -> Self {
        self.upstream_status = Some(status);
        self
    }

    fn error_object(&self) -> Map<String, Value> {
        let mut error = Map::new();
        error.insert("message".to_string(), Value::String(self.message.clone()));
        error.insert("type".to_string(), Value::String(self.error_type.clone()));
        error.insert("code".to_string(), Value::String(self.code.clone()));
        error.insert(
            "param".to_string(),
            self.param
                .as_ref()
                .map(|param| Value::String(param.as_ref().clone()))
                .unwrap_or(Value::Null),
        );
        error.insert(
            "detail".to_string(),
            self.detail
                .as_ref()
                .map(|detail| detail.as_ref().clone())
                .unwrap_or(Value::Null),
        );
        if let Some(reason) = &self.reason {
            error.insert("reason".to_string(), reason.as_ref().clone());
        }
        if let Some(status) = self.upstream_status {
            error.insert("upstream_status".to_string(), json!(status));
        }
        error
    }

    /// The OpenAI-shaped body.
    pub fn body(&self) -> Value {
        json!({"error": Value::Object(self.error_object())})
    }

    /// The Anthropic-shaped body; the machine code and diagnostics stay inside
    /// the `error` object so Anthropic clients still read `error.type` and
    /// `error.message`.
    pub fn anthropic_body(&self) -> Value {
        json!({
            "type": "error",
            "error": Value::Object(self.error_object()),
        })
    }

    pub fn response(&self) -> Response {
        Response::json(self.status, &self.body()).closing()
    }

    pub fn anthropic_response(&self) -> Response {
        Response::json(self.status, &self.anthropic_body()).closing()
    }

    pub fn response_for(&self, protocol: ErrorProtocol) -> Response {
        match protocol {
            ErrorProtocol::OpenAi => self.response(),
            ErrorProtocol::Anthropic => self.anthropic_response(),
        }
    }

    // ----- 8003 share gateway errors (chat2api/chat2api.py) -----

    pub fn invalid_request(message: impl Into<String>) -> Self {
        Self::new(400, message, "invalid_request")
    }

    pub fn invalid_json() -> Self {
        Self::new(400, "Request body is not valid JSON", "invalid_json")
    }

    pub fn invalid_body() -> Self {
        Self::new(400, "Request body must be a JSON object", "invalid_body")
    }

    pub fn payload_too_large() -> Self {
        Self::new(413, "Request body too large", "payload_too_large")
    }

    pub fn unsupported_chat_request() -> Self {
        Self::new(400, "unsupported_chat_request", "unsupported_chat_request")
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
            "unsupported_responses_request",
        )
    }

    pub fn route_not_shared() -> Self {
        Self::new(404, "route_not_shared", "route_not_shared")
    }

    pub fn unknown_path(path: &str) -> Self {
        Self::new(404, format!("Unknown path: {path}"), "unknown_path")
    }

    pub fn invalid_api_key() -> Self {
        Self::new(401, "invalid_api_key", "invalid_api_key")
    }

    pub fn local_auth_error() -> Self {
        Self::new(401, "Local API key missing or invalid", "local_auth_error")
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
        Self::new(
            403,
            "Local management endpoint denied",
            "control_denied_endpoint",
        )
    }

    pub fn control_denied_token() -> Self {
        Self::new(
            403,
            "Local management token missing or invalid",
            "control_denied_token",
        )
    }

    /// A share configuration read/parse failure is neither an invalid Key nor a
    /// concurrency refusal; it fails closed with a readable reason.
    pub fn share_config_unavailable(detail: impl Into<String>) -> Self {
        Self::new(
            503,
            "Share configuration is unavailable",
            "share_config_unavailable",
        )
        .with_detail(Value::String(detail.into()))
    }

    pub fn sharing_limit_reached(reason: Option<&str>, detail: Option<&str>) -> Self {
        let mut error = Self::new(429, "sharing_limit_reached", "sharing_limit_reached");
        error.reason = Some(Box::new(
            reason
                .map(|reason| Value::String(reason.to_string()))
                .unwrap_or(Value::Null),
        ));
        if let Some(detail) = detail {
            error.detail = Some(Box::new(Value::String(detail.to_string())));
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
            error.detail = Some(Box::new(Value::String(detail.to_string())));
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
            "models_unavailable",
        )
    }

    /// Python `_compat_proxy` maps 400/422 to the upstream status without a
    /// transient-retry signal; the body carries structured diagnostics only in
    /// the server log.
    pub fn upstream_rejected(status: u16) -> Self {
        Self::new(
            status,
            "Upstream rejected the request; the server log has structured diagnostics",
            "upstream_request_rejected",
        )
    }

    /// Python `_compat_proxy` maps every other non-200 to a 502 with a bounded
    /// upstream text detail.
    pub fn upstream_request_failed(status: u16, text: &str) -> Self {
        Self::new(502, "Upstream request failed", "chat2api_error")
            .with_detail(Value::String(format!("HTTP {status}: {text}")))
    }

    pub fn not_implemented(feature: &str) -> Self {
        Self::new(501, "not_implemented", "not_implemented")
            .with_detail(Value::String(feature.to_string()))
    }

    // ----- machine codes introduced by the protocol-aware error contract -----

    /// The upstream rejected the request for permission reasons.
    pub fn upstream_forbidden() -> Self {
        Self::new(403, "Upstream denied the request", "upstream_forbidden")
    }

    /// The upstream quota is exhausted; retrying cannot help.
    pub fn upstream_quota_exhausted() -> Self {
        Self::new(
            402,
            "Upstream quota is exhausted",
            "upstream_quota_exhausted",
        )
    }

    /// Reauthentication was attempted and the upstream still rejects the
    /// credential.
    pub fn reauthentication_failed() -> Self {
        Self::new(
            403,
            "Open WebUI authentication could not be restored",
            "reauthentication_failed",
        )
    }

    /// The API path exists but the method is not implemented.
    pub fn method_not_allowed() -> Self {
        Self::new(405, "Method not allowed", "method_not_allowed")
    }

    /// A compressed request body is refused because the encoding is unsupported.
    pub fn unsupported_content_encoding() -> Self {
        Self::new(
            415,
            "Unsupported Content-Encoding",
            "unsupported_content_encoding",
        )
    }

    /// The request body did not arrive within its read budget.
    pub fn request_timeout() -> Self {
        Self::new(408, "Request body read timed out", "request_timeout")
    }

    /// The gateway's own upstream wait budget expired.
    pub fn upstream_timeout() -> Self {
        Self::new(504, "Upstream request timed out", "upstream_timeout")
    }

    // ----- console (8002) request-boundary codes (usagesrv.py) -----

    pub fn console_body(status: u16, code: &str) -> Self {
        Self::new(status, code, code)
    }

    pub fn duplicate_header(name: &str) -> Self {
        Self::new(400, format!("duplicate header: {name}"), "invalid_request").with_param(name)
    }

    pub fn unknown_fields(fields: &[String]) -> Self {
        let list = fields
            .iter()
            .map(|field| format!("'{field}'"))
            .collect::<Vec<_>>()
            .join(", ");
        let mut error = Self::new(
            400,
            format!("unknown field(s): [{list}]"),
            "invalid_request",
        );
        if let Some(field) = fields.first() {
            error = error.with_param(field);
        }
        error
    }
}

/// Map one status/code pair onto the protocol-neutral error category.
fn category_for(status: u16, code: &str) -> &'static str {
    match code {
        "reauthentication_required" | "reauthentication_failed" => "authentication_error",
        "upstream_forbidden" => "permission_error",
        "upstream_quota_exhausted" => "rate_limit_error",
        "share_config_unavailable" | "share_revoke_unassembled" | "not_implemented" => "api_error",
        "request_deadline_exceeded" | "upstream_attempt_budget_exhausted" => "upstream_error",
        _ if code.starts_with("upstream_") => "upstream_error",
        _ => match status {
            400 | 405 | 408 | 413 | 415 | 422 => "invalid_request_error",
            401 => "authentication_error",
            402 | 429 => "rate_limit_error",
            403 => "permission_error",
            404 => "not_found_error",
            529 => "overloaded_error",
            502..=504 => "upstream_error",
            _ => "api_error",
        },
    }
}
