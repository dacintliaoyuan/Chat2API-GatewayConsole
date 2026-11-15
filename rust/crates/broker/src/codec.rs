//! AC4: one-line JSON framing bounded by 1 MiB.
//!
//! The Python broker signs `json.dumps(signed_subset, sort_keys=True)` and
//! writes `json.dumps(reply, sort_keys=True) + "\n"`. Rust must produce the
//! SAME bytes, so [`canonical_json`] reproduces Python's default
//! `json.dumps` formatting (`ensure_ascii=True`, `", "`/`": "` separators,
//! sorted object keys) instead of `serde_json::to_string`.

use serde_json::Value;

use crate::error::{ProtocolError, ResultCode};

/// Request/response bound (contract §7.1; `share/broker_protocol.py:30`).
pub const MAX_MESSAGE: usize = 1024 * 1024;

/// Serialize one value exactly like Python `json.dumps(value, sort_keys=True)`.
///
/// The single canonical encoder lives in `um-contract` so the broker wire and the
/// projection fingerprint can never disagree.
pub use um_contract::canonical::canonical_json;

/// Encode one JSON line, rejecting anything that would exceed the bound
/// before it ever reaches the wire.
pub fn encode_frame(value: &Value) -> Result<Vec<u8>, ProtocolError> {
    let mut data = canonical_json(value).into_bytes();
    data.push(b'\n');
    if data.len() > MAX_MESSAGE {
        return Err(ProtocolError::new(
            ResultCode::Overlimit,
            "encoded message exceeds bound",
        ));
    }
    Ok(data)
}

/// Decode one line bounded by 1 MiB. An unparsable or non-object frame is
/// `bad_request`; an oversized frame is `overlimit`.
pub fn decode_frame(line: &[u8]) -> Result<Value, ProtocolError> {
    if line.len() > MAX_MESSAGE {
        return Err(ProtocolError::new(
            ResultCode::Overlimit,
            "frame exceeds bound",
        ));
    }
    let text = std::str::from_utf8(line)
        .map_err(|_| ProtocolError::new(ResultCode::BadRequest, "invalid frame encoding"))?;
    let value: Value = serde_json::from_str(text)
        .map_err(|_| ProtocolError::new(ResultCode::BadRequest, "invalid frame encoding"))?;
    if !value.is_object() {
        return Err(ProtocolError::new(
            ResultCode::BadRequest,
            "frame must be an object",
        ));
    }
    Ok(value)
}
