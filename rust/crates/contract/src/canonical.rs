//! Python-compatible canonical JSON.
//!
//! The broker signs `json.dumps(signed_subset, sort_keys=True)` and writes
//! `json.dumps(reply, sort_keys=True) + "\n"`; the store fingerprints the
//! projection with the same call. Both must produce the SAME bytes, so this module
//! reproduces Python's default `json.dumps` formatting (`ensure_ascii=True`,
//! `", "`/`": "` separators, sorted object keys) instead of
//! `serde_json::to_string`.

use serde_json::{Map, Value};

/// Serialize one value exactly like Python `json.dumps(value, sort_keys=True)`.
pub fn canonical_json(value: &Value) -> String {
    let mut output = String::new();
    write_value(&mut output, value);
    output
}

fn write_value(output: &mut String, value: &Value) {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(true) => output.push_str("true"),
        Value::Bool(false) => output.push_str("false"),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                output.push_str(&value.to_string());
            } else if let Some(value) = number.as_u64() {
                output.push_str(&value.to_string());
            } else if let Some(value) = number.as_f64() {
                output.push_str(&python_float(value));
            } else {
                output.push_str("null");
            }
        }
        Value::String(text) => write_string(output, text),
        Value::Array(items) => {
            output.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    output.push_str(", ");
                }
                write_value(output, item);
            }
            output.push(']');
        }
        Value::Object(map) => write_object(output, map),
    }
}

fn write_object(output: &mut String, map: &Map<String, Value>) {
    let mut entries: Vec<(&String, &Value)> = map.iter().collect();
    entries.sort_by(|left, right| left.0.cmp(right.0));
    output.push('{');
    for (index, (key, value)) in entries.into_iter().enumerate() {
        if index > 0 {
            output.push_str(", ");
        }
        write_string(output, key);
        output.push_str(": ");
        write_value(output, value);
    }
    output.push('}');
}

fn write_string(output: &mut String, value: &str) {
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{08}' => output.push_str("\\b"),
            '\u{0c}' => output.push_str("\\f"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            character if (character as u32) < 0x20 || (character as u32) == 0x7f => {
                output.push_str(&format!("\\u{:04x}", character as u32));
            }
            character if (character as u32) < 0x80 => output.push(character),
            character => {
                let code = character as u32;
                if code <= 0xffff {
                    output.push_str(&format!("\\u{code:04x}"));
                } else {
                    let code = code - 0x10000;
                    let high = 0xd800 + (code >> 10);
                    let low = 0xdc00 + (code & 0x3ff);
                    output.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
                }
            }
        }
    }
    output.push('"');
}

/// Python's `repr(float)`/`json.dumps` exponent spelling (`1e+16`,
/// `1e-05`); Rust's shortest round-trip digits are the same, only the
/// exponent sign and padding differ.
fn python_float(value: f64) -> String {
    let text = format!("{value:?}");
    let Some((mantissa, exponent)) = text.split_once('e') else {
        return text;
    };
    let Ok(exponent) = exponent.parse::<i32>() else {
        return text;
    };
    let sign = if exponent < 0 { '-' } else { '+' };
    format!("{mantissa}e{sign}{:02}", exponent.abs())
}
