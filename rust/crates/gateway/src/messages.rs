//! Anthropic Messages request contract (`POST /v1/messages`).
//!
//! This module ports the Python share admission rule for Messages: the accepted
//! field set is frozen in `shared/responses_protocol.py:14-17`
//! (`MESSAGES_FIELDS`) and checked by `chat2api/share_auth.py:1159-1190`
//! (`valid_messages_request`). Shape-only admission never rewrites the body:
//! an accepted request is forwarded unchanged.

use serde_json::{Map, Value};

/// The frozen Anthropic Messages field whitelist.
///
/// `max_tokens` is required and must be a positive integer; `stream` is optional
/// but must be a bool when present. Unknown fields are refused, never ignored.
pub const MESSAGES_FIELDS: [&str; 13] = [
    "max_tokens",
    "messages",
    "metadata",
    "model",
    "stop_sequences",
    "stream",
    "system",
    "temperature",
    "thinking",
    "tool_choice",
    "tools",
    "top_k",
    "top_p",
];

/// Python `valid_messages_request`: shape-only admission for one shared body.
///
/// Python's `bool` is an `int` subclass, so `max_tokens: true` is explicitly
/// not a positive integer; `serde_json`'s `as_u64` already refuses bools,
/// floats, strings and negative numbers.
pub fn valid_messages_request(body: &Map<String, Value>) -> bool {
    if body
        .keys()
        .any(|key| !MESSAGES_FIELDS.contains(&key.as_str()))
    {
        return false;
    }
    if !body
        .get("model")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.is_empty())
    {
        return false;
    }
    if !body
        .get("messages")
        .and_then(Value::as_array)
        .is_some_and(|value| !value.is_empty())
    {
        return false;
    }
    if !body
        .get("max_tokens")
        .and_then(Value::as_u64)
        .is_some_and(|value| value >= 1)
    {
        return false;
    }
    body.get("stream").map_or(true, Value::is_boolean)
}
