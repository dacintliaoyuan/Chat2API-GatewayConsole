//! AC2: the management-channel dispatcher and its fail-closed mappings.
//!
//! The dispatcher owns the shared validation/replay pipeline; the fixed action
//! SEMANTICS are implemented by the S3b slice behind [`ManagementHandler`].
//! A handler that does not implement a verb answers `capability_missing` - never
//! a fabricated success.

use std::io;

use serde_json::{Map, Value};

use crate::error::{BrokerResult, ProtocolError, ResultCode};
use crate::replay::ReplayRegistry;
use crate::request::{validate_management_request, ValidatedRequest};

/// One management action implementation (S3b plugs in here).
pub trait ManagementHandler: Send + Sync {
    fn handle(&self, request: &ValidatedRequest) -> BrokerResult;
}

/// A deployment without management capabilities: every verb is refused
/// explicitly instead of being faked.
pub struct NoManagementCapabilities;

impl ManagementHandler for NoManagementCapabilities {
    fn handle(&self, _request: &ValidatedRequest) -> BrokerResult {
        BrokerResult::err(
            ResultCode::CapabilityMissing,
            "management action not implemented",
        )
    }
}

/// One encoded reply: the structured result plus the associated request id.
#[derive(Debug, Clone, PartialEq)]
pub struct BrokerReply {
    request_id: Option<String>,
    result: BrokerResult,
    transport_error: bool,
}

impl BrokerReply {
    pub fn handler(request_id: String, result: BrokerResult) -> Self {
        Self {
            request_id: Some(request_id),
            result,
            transport_error: false,
        }
    }

    pub fn transport_error(
        request_id: Option<String>,
        code: ResultCode,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            request_id,
            result: BrokerResult::err(code, detail),
            transport_error: true,
        }
    }

    pub fn request_id(&self) -> Option<&str> {
        self.request_id.as_deref()
    }

    pub fn result(&self) -> &BrokerResult {
        &self.result
    }

    pub fn to_value(&self) -> Value {
        let mut frame = self.result.to_value();
        if let Some(map) = frame.as_object_mut() {
            if self.transport_error {
                map.insert("v".to_string(), Value::from(2));
            }
            map.insert(
                "request_id".to_string(),
                self.request_id
                    .clone()
                    .map(Value::String)
                    .unwrap_or(Value::Null),
            );
        }
        frame
    }
}

pub struct ManagementDispatcher {
    replay: ReplayRegistry,
    handler: Box<dyn ManagementHandler>,
}

impl ManagementDispatcher {
    pub fn new(handler: Box<dyn ManagementHandler>) -> Self {
        Self::with_replay(ReplayRegistry::default(), handler)
    }

    pub fn without_capabilities() -> Self {
        Self::new(Box::new(NoManagementCapabilities))
    }

    pub fn with_replay(replay: ReplayRegistry, handler: Box<dyn ManagementHandler>) -> Self {
        Self { replay, handler }
    }

    /// Validate, consume the request id, then dispatch. A forged MAC, stale
    /// timestamp, replay, unknown action or overlimit frame never reaches the
    /// handler.
    pub fn handle_frame(&self, body: &Value, secret: &str, now: i64) -> BrokerReply {
        let request_id = body
            .get("request_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        let request = match validate_management_request(body, secret, now) {
            Ok(request) => request,
            Err(error) => {
                return BrokerReply::transport_error(request_id, error.code(), error.message())
            }
        };
        if let Err(error) = self.replay.consume(&request.request_id, request.ts, now) {
            return BrokerReply::handler(
                request.request_id.clone(),
                BrokerResult::err(error.code(), error.message()),
            );
        }
        let result = self.handler.handle(&request);
        BrokerReply::handler(request.request_id, result)
    }
}

/// Map an instance-channel refusal exactly like the broker's maintenance path:
/// an old instance that does not know the verb answers `capability_missing`, a
/// terminal stop stays `blocked`, and an unknown code is `unconfirmed`.
pub fn map_instance_refusal(response: &Value) -> BrokerResult {
    let code = response.get("error_code").and_then(Value::as_str);
    let detail = response
        .get("result")
        .and_then(|result| result.get("detail"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if code == Some("bad_request") && detail == "unknown action" {
        return BrokerResult::err(
            ResultCode::CapabilityMissing,
            "instance does not support the verb",
        );
    }
    let parsed = code
        .and_then(ResultCode::parse)
        .unwrap_or(ResultCode::Unconfirmed);
    if parsed == ResultCode::Ok {
        return BrokerResult::err(ResultCode::Unconfirmed, detail);
    }
    BrokerResult::err(parsed, detail)
}

/// Classify a failed AF_UNIX connect: a denied connect is an explicit
/// `access_denied`, never folded into `not_installed`.
pub fn classify_connect_error(kind: io::ErrorKind) -> ResultCode {
    match kind {
        io::ErrorKind::PermissionDenied => ResultCode::AccessDenied,
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => ResultCode::DeadlineExceeded,
        _ => ResultCode::NotInstalled,
    }
}

/// The frame shape a successful handler reply is encoded into. Kept here so a
/// caller can build the same map the Python `_reply` helper does.
pub fn handler_frame(request_id: &str, result: &BrokerResult) -> Value {
    let mut frame = result.to_value();
    if let Some(map) = frame.as_object_mut() {
        map.insert(
            "request_id".to_string(),
            Value::String(request_id.to_string()),
        );
    }
    frame
}

/// A decode/shape rejection frame (`v: 2`, no handler side effect).
pub fn rejection_frame(request_id: Option<&str>, error: &ProtocolError) -> Value {
    let mut map = Map::new();
    map.insert("v".to_string(), Value::from(2));
    map.insert(
        "request_id".to_string(),
        request_id
            .map(|value| Value::String(value.to_string()))
            .unwrap_or(Value::Null),
    );
    map.insert("ok".to_string(), Value::Bool(false));
    map.insert(
        "error_code".to_string(),
        Value::String(error.code().as_str().to_string()),
    );
    let mut result = Map::new();
    result.insert(
        "detail".to_string(),
        Value::String(error.message().to_string()),
    );
    map.insert("result".to_string(), Value::Object(result));
    Value::Object(map)
}
