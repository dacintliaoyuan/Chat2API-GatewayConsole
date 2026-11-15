//! Request-body boundaries.
//!
//! Two Python policies are ported without merging their error vocabularies:
//!
//! * [`BodyPolicy::Share`] is `chat2api/chat2api.py::Handler._read_body` (the
//!   8003 share routes): invalid length `invalid_request`, over the share limit
//!   `payload_too_large`, invalid JSON `invalid_json`, non-object
//!   `invalid_body`.
//! * [`BodyPolicy::Console`] is
//!   `usagesrv.py::Handler._read_share_json_body` plus
//!   `_duplicate_header_error` (the local control-token routes): duplicate
//!   sensitive headers, unsupported transfer/content encodings, unique
//!   Content-Length 0..65536, required `application/json`, incomplete body
//!   and the console `invalid_json`/`request_too_large` codes.
//!
//! Both policies produce the same unified error body from
//! [`crate::error::GatewayError`].

use serde_json::Value;

use crate::config::CONSOLE_MAX_REQUEST_BYTES;
use crate::error::GatewayError;
use crate::http::{BodyRead, Headers, Request};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BodyPolicy {
    Share { max_request_bytes: u64 },
    Console { max_request_bytes: u64 },
}

/// Headers the console request boundary refuses to see twice.
pub const SINGLE_VALUE_HEADERS: [&str; 8] = [
    "Content-Type",
    "Content-Length",
    "Authorization",
    "Cookie",
    "X-Admin-Control",
    "X-Requested-With",
    "Transfer-Encoding",
    "Content-Encoding",
];

/// Reject a duplicate sensitive header; the console route layer does this
/// before reading the body, so the error keeps its Python message.
pub fn reject_duplicate_sensitive_header(headers: &Headers) -> Result<(), GatewayError> {
    for name in SINGLE_VALUE_HEADERS {
        if headers.get_all(name).len() > 1 {
            return Err(GatewayError::duplicate_header(name));
        }
    }
    Ok(())
}

/// Parse one JSON object body according to the selected Python policy.
pub fn read_json_body(
    request: &Request,
    policy: BodyPolicy,
) -> Result<Option<serde_json::Map<String, Value>>, GatewayError> {
    match policy {
        BodyPolicy::Share { max_request_bytes } => {
            share_body(request, max_request_bytes)
        }
        BodyPolicy::Console { max_request_bytes } => {
            console_body(request, max_request_bytes)
        }
    }
}

fn share_body(
    request: &Request,
    max_request_bytes: u64,
) -> Result<Option<serde_json::Map<String, Value>>, GatewayError> {
    match &request.body {
        BodyRead::InvalidLength => Err(GatewayError::invalid_request(
            "Invalid Content-Length header",
        )),
        BodyRead::TooLarge(length) if *length > max_request_bytes => {
            Err(GatewayError::payload_too_large())
        }
        BodyRead::TooLarge(_) => Err(GatewayError::payload_too_large()),
        BodyRead::Incomplete => Err(GatewayError::invalid_json()),
        BodyRead::Complete(bytes) => {
            if bytes.is_empty() {
                return Ok(None);
            }
            let payload: Value = serde_json::from_slice(bytes)
                .map_err(|_| GatewayError::invalid_json())?;
            match payload {
                Value::Object(object) => Ok(Some(object)),
                _ => Err(GatewayError::invalid_body()),
            }
        }
    }
}

fn console_body(
    request: &Request,
    max_request_bytes: u64,
) -> Result<Option<serde_json::Map<String, Value>>, GatewayError> {
    reject_duplicate_sensitive_header(&request.headers)?;
    if request.headers.has("Transfer-Encoding")
        || request.headers.has("Content-Encoding")
    {
        return Err(GatewayError::console_body(400, "unsupported_body_encoding"));
    }
    let lengths = request.headers.get_all("Content-Length");
    if lengths.len() > 1 {
        return Err(GatewayError::console_body(400, "invalid_content_length"));
    }
    let length = match lengths.first() {
        Some(value) => value
            .parse::<i64>()
            .map_err(|_| GatewayError::console_body(400, "invalid_content_length"))?,
        None => 0,
    };
    if length < 0 {
        return Err(GatewayError::console_body(400, "invalid_content_length"));
    }
    if length as u64 > max_request_bytes {
        return Err(GatewayError::console_body(413, "request_too_large"));
    }
    let content_types = request.headers.get_all("Content-Type");
    if length > 0 || !content_types.is_empty() {
        if content_types.len() != 1 {
            return Err(GatewayError::console_body(400, "content_type_required"));
        }
        let content_type = content_types[0]
            .split(';')
            .next()
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        if content_type != "application/json" {
            return Err(GatewayError::console_body(400, "content_type_required"));
        }
    }
    match &request.body {
        BodyRead::Complete(bytes) => {
            if bytes.len() as i64 != length {
                return Err(GatewayError::console_body(400, "incomplete_body"));
            }
            if bytes.is_empty() {
                return Ok(Some(serde_json::Map::new()));
            }
            let payload: Value = serde_json::from_slice(bytes)
                .map_err(|_| GatewayError::console_body(400, "invalid_json"))?;
            match payload {
                Value::Object(object) => Ok(Some(object)),
                _ => Err(GatewayError::console_body(400, "invalid_json")),
            }
        }
        BodyRead::InvalidLength => {
            Err(GatewayError::console_body(400, "invalid_content_length"))
        }
        BodyRead::TooLarge(_) => {
            Err(GatewayError::console_body(413, "request_too_large"))
        }
        BodyRead::Incomplete => {
            Err(GatewayError::console_body(400, "incomplete_body"))
        }
    }
}

/// Reject object fields outside the allowed whitelist.
pub fn reject_unknown_fields(
    body: &serde_json::Map<String, Value>,
    allowed: &[&str],
) -> Result<(), GatewayError> {
    let mut unknown: Vec<String> = body
        .keys()
        .filter(|key| !allowed.contains(&key.as_str()))
        .cloned()
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort();
    Err(GatewayError::unknown_fields(&unknown))
}

/// The console body limit used by control-token routes.
pub fn console_max_request_bytes() -> u64 {
    CONSOLE_MAX_REQUEST_BYTES
}
