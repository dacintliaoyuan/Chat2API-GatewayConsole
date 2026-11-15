//! The 8002 console request boundary.
//!
//! This is `usagesrv.py::_duplicate_header_error` and
//! `_read_share_json_body`, with the Python error codes kept separate from
//! the 8003 gateway body vocabulary:
//!
//! * duplicate sensitive header -> `duplicate header: <name>`;
//! * `Transfer-Encoding`/`Content-Encoding` -> `unsupported_body_encoding`;
//! * non-unique or non-numeric `Content-Length` -> `invalid_content_length`;
//! * length outside 0..65536 -> `request_too_large`;
//! * a body (or a `Content-Type`) without exactly one JSON content type ->
//!   `content_type_required`;
//! * a short body -> `incomplete_body`;
//! * invalid JSON -> `invalid_json` (a bare `null` is the empty object, like
//!   Python);
//! * a non-object JSON value -> `invalid_json`.

use serde_json::{Map, Value};

use um_gateway::http::{BodyRead, Headers, Request};

use crate::error::ConsoleError;

pub const CONSOLE_MAX_REQUEST_BYTES: u64 = 65536;

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

pub fn reject_duplicate_sensitive_header(headers: &Headers) -> Result<(), ConsoleError> {
    for name in SINGLE_VALUE_HEADERS {
        if headers.get_all(name).len() > 1 {
            return Err(ConsoleError::duplicate_header(name));
        }
    }
    Ok(())
}

pub fn read_console_body(request: &Request) -> Result<Map<String, Value>, ConsoleError> {
    reject_duplicate_sensitive_header(&request.headers)?;
    if request.headers.has("Transfer-Encoding") || request.headers.has("Content-Encoding") {
        return Err(ConsoleError::body(400, "unsupported_body_encoding"));
    }
    let lengths = request.headers.get_all("Content-Length");
    if lengths.len() > 1 {
        return Err(ConsoleError::body(400, "invalid_content_length"));
    }
    let length = match lengths.first() {
        Some(value) => value
            .parse::<i64>()
            .map_err(|_| ConsoleError::body(400, "invalid_content_length"))?,
        None => 0,
    };
    if length < 0 || length as u64 > CONSOLE_MAX_REQUEST_BYTES {
        return Err(ConsoleError::body(413, "request_too_large"));
    }
    let content_types = request.headers.get_all("Content-Type");
    if length > 0 || !content_types.is_empty() {
        if content_types.len() != 1 {
            return Err(ConsoleError::body(400, "content_type_required"));
        }
        let content_type = content_types[0]
            .split(';')
            .next()
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        if content_type != "application/json" {
            return Err(ConsoleError::body(400, "content_type_required"));
        }
    }
    match &request.body {
        BodyRead::Complete(bytes) => {
            if bytes.len() as i64 != length {
                return Err(ConsoleError::body(400, "incomplete_body"));
            }
            if bytes.is_empty() {
                return Ok(Map::new());
            }
            let value: Value = serde_json::from_slice(bytes)
                .map_err(|_| ConsoleError::body(400, "invalid_json"))?;
            match value {
                Value::Null => Ok(Map::new()),
                Value::Object(object) => Ok(object),
                _ => Err(ConsoleError::body(400, "invalid_json")),
            }
        }
        BodyRead::InvalidLength => Err(ConsoleError::body(400, "invalid_content_length")),
        BodyRead::NegativeLength => Err(ConsoleError::body(413, "request_too_large")),
        BodyRead::TooLarge(_) => Err(ConsoleError::body(413, "request_too_large")),
        BodyRead::Incomplete => Err(ConsoleError::body(400, "incomplete_body")),
    }
}

/// Python `_reject_unknown_fields`: sorted unknown field names.
pub fn reject_unknown_fields(
    body: &Map<String, Value>,
    allowed: &[&str],
) -> Result<(), ConsoleError> {
    let mut unknown: Vec<String> = body
        .keys()
        .filter(|key| !allowed.contains(&key.as_str()))
        .cloned()
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort();
    let list = unknown
        .iter()
        .map(|field| format!("'{field}'"))
        .collect::<Vec<_>>()
        .join(", ");
    Err(ConsoleError::body(
        400,
        &format!("unknown field(s): [{list}]"),
    ))
}
