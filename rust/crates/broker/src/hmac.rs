//! HMAC-SHA256 over the Python canonical signed payload.
//!
//! Python signs `json.dumps({k: body[k] for k in signed_keys if k in body},
//! sort_keys=True)` with the channel secret. The signed subset and the canonical
//! bytes are both reproduced here; comparison is constant time.

use hmac::{Hmac, Mac};
use serde_json::{Map, Value};
use sha2::Sha256;

use crate::codec::canonical_json;
use crate::error::{ProtocolError, ResultCode};

/// The lowercase hex HMAC-SHA256 of `payload` under `secret`.
pub fn mac(secret: &str, payload: &[u8]) -> String {
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(secret.as_bytes())
        .expect("HMAC accepts any key length");
    mac.update(payload);
    hex_lower(&mac.finalize().into_bytes())
}

/// The canonical JSON of the signed subset (only keys present in the body).
pub fn signed_payload(body: &Value, signed_keys: &[&str]) -> String {
    let mut subset = Map::new();
    if let Some(map) = body.as_object() {
        for key in signed_keys {
            if let Some(value) = map.get(*key) {
                subset.insert((*key).to_string(), value.clone());
            }
        }
    }
    canonical_json(&Value::Object(subset))
}

/// Sign `body` in place: remove any stale `mac`, then add this payload's MAC.
pub fn sign_value(secret: &str, body: &mut Map<String, Value>, signed_keys: &[&str]) -> String {
    body.remove("mac");
    let mac = mac(
        secret,
        signed_payload(&Value::Object(body.clone()), signed_keys).as_bytes(),
    );
    body.insert("mac".to_string(), Value::String(mac.clone()));
    mac
}

/// Verify a supplied MAC against the canonical signed payload. A missing,
/// malformed or forged MAC is `forbidden`, never accepted.
pub fn verify_value(secret: &str, body: &Value, signed_keys: &[&str]) -> Result<(), ProtocolError> {
    let supplied = body.get("mac").and_then(Value::as_str).unwrap_or("");
    let Some(supplied) = decode_hex(supplied) else {
        return Err(ProtocolError::new(
            ResultCode::Forbidden,
            "forged or missing mac",
        ));
    };
    let payload = signed_payload(body, signed_keys);
    let mut verifier = <Hmac<Sha256> as Mac>::new_from_slice(secret.as_bytes())
        .expect("HMAC accepts any key length");
    verifier.update(payload.as_bytes());
    if verifier.verify_slice(&supplied).is_err() {
        return Err(ProtocolError::new(
            ResultCode::Forbidden,
            "forged or missing mac",
        ));
    }
    Ok(())
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn decode_hex(text: &str) -> Option<Vec<u8>> {
    if text.len() % 2 != 0 {
        return None;
    }
    let bytes = text.as_bytes();
    let mut output = Vec::with_capacity(bytes.len() / 2);
    let mut index = 0;
    while index < bytes.len() {
        let high = hex_digit(bytes[index])?;
        let low = hex_digit(bytes[index + 1])?;
        output.push((high << 4) | low);
        index += 2;
    }
    Some(output)
}

fn hex_digit(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}
