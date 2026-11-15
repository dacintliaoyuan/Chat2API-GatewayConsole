//! AC4: one-line JSON frames, the 1 MiB bound and the fixed result-code table.
//!
//! Expected canonical strings are Python `json.dumps(..., sort_keys=True)` output
//! (the exact serializer the Python broker signs with), produced with:
//!
//! ```text
//! python -c "import json; print(json.dumps(value, sort_keys=True))"
//! ```

use std::path::Path;

use serde_json::{json, Value};
use um_broker::codec::{canonical_json, decode_frame, encode_frame, MAX_MESSAGE};
use um_broker::error::{BrokerResult, ProtocolError, ResultCode};

fn broker_fixture() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/fixtures/rust_broker_wire_rev1.json");
    let text = std::fs::read_to_string(&path).expect("broker wire fixture");
    serde_json::from_str(&text).expect("broker wire fixture is JSON")
}

#[test]
fn canonical_json_matches_every_python_generated_vector() {
    let fixture = broker_fixture();
    let vectors = fixture["canonicalJson"].as_array().expect("vectors");
    assert!(!vectors.is_empty());
    for vector in vectors {
        assert_eq!(
            canonical_json(&vector["value"]),
            vector["encoded"].as_str().expect("encoded string"),
            "value: {}",
            vector["value"]
        );
    }
}

#[test]
fn encode_frame_matches_every_python_generated_frame() {
    let fixture = broker_fixture();
    let frames = fixture["frames"].as_array().expect("frames");
    assert!(!frames.is_empty());
    for frame in frames {
        let encoded = encode_frame(&frame["value"]).expect("encode");
        assert_eq!(
            std::str::from_utf8(&encoded).expect("utf-8"),
            frame["encoded"].as_str().expect("encoded string")
        );
    }
}

#[test]
fn canonical_json_sorts_keys_and_uses_python_separators() {
    let value = json!({
        "b": 1,
        "a": [1, true, null, "é"],
        "c": {"z": 2, "y": "x"},
    });
    assert_eq!(
        canonical_json(&value),
        r#"{"a": [1, true, null, "\u00e9"], "b": 1, "c": {"y": "x", "z": 2}}"#
    );
}

#[test]
fn canonical_json_escapes_like_python_ensure_ascii() {
    let value = json!({
        "quote": "a\"b",
        "backslash": "a\\b",
        "controls": "\u{08}\u{0c}\n\r\t\u{1f}\u{7f}",
        "latin": "\u{80}\u{e9}",
        "line_separator": "\u{2028}",
        "astral": "\u{1f600}",
    });
    assert_eq!(
        canonical_json(&value),
        concat!(
            r#"{"astral": "\ud83d\ude00", "backslash": "a\\b", "#,
            r#""controls": "\b\f\n\r\t\u001f\u007f", "latin": "\u0080\u00e9", "#,
            r#""line_separator": "\u2028", "quote": "a\"b"}"#
        )
    );
}

#[test]
fn canonical_json_formats_floats_like_python() {
    let value = json!({
        "a": 1.0,
        "b": 1e16,
        "c": 1e-5,
        "d": 1.2345,
        "e": 0.0,
        "f": -0.0,
        "g": 1.234e-7,
        "h": 1.5e20,
    });
    assert_eq!(
        canonical_json(&value),
        r#"{"a": 1.0, "b": 1e+16, "c": 1e-05, "d": 1.2345, "e": 0.0, "f": -0.0, "g": 1.234e-07, "h": 1.5e+20}"#
    );
}

#[test]
fn canonical_json_keeps_negative_and_large_integers_exact() {
    let value = json!({"z": -1, "y": 9007199254740993i64, "x": 18446744073709551615u64});
    assert_eq!(
        canonical_json(&value),
        r#"{"x": 18446744073709551615, "y": 9007199254740993, "z": -1}"#
    );
}

#[test]
fn canonical_json_recurses_into_every_object() {
    let value = json!([{"b": {"d": 1, "c": 2}, "a": 3}, [{"f": 4, "e": 5}]]);
    assert_eq!(
        canonical_json(&value),
        r#"[{"a": 3, "b": {"c": 2, "d": 1}}, [{"e": 5, "f": 4}]]"#
    );
}

#[test]
fn encode_frame_appends_one_newline_and_sorts_keys() {
    let frame = encode_frame(&json!({"b": 2, "a": 1})).expect("encode");
    assert_eq!(frame, b"{\"a\": 1, \"b\": 2}\n");
    assert_eq!(frame.iter().filter(|byte| **byte == b'\n').count(), 1);
}

#[test]
fn encode_frame_accepts_exactly_the_bound() {
    let body_len = MAX_MESSAGE - 10; // {"a": ""} is 9 bytes plus the newline.
    let value = json!({"a": "x".repeat(body_len)});
    let frame = encode_frame(&value).expect("exactly the bound is accepted");
    assert_eq!(frame.len(), MAX_MESSAGE);
}

#[test]
fn encode_frame_rejects_a_frame_over_the_bound_before_writing() {
    let value = json!({"a": "x".repeat(MAX_MESSAGE - 9)});
    let error = encode_frame(&value).unwrap_err();
    assert_eq!(error.code(), ResultCode::Overlimit);
}

#[test]
fn decode_frame_accepts_exactly_the_bound() {
    let body_len = MAX_MESSAGE - 10;
    let line = format!("{{\"a\": \"{}\"}}\n", "x".repeat(body_len));
    assert_eq!(line.len(), MAX_MESSAGE);
    let value = decode_frame(line.as_bytes()).expect("exactly the bound is accepted");
    assert_eq!(value["a"].as_str().unwrap().len(), body_len);
}

#[test]
fn decode_frame_rejects_an_overlimit_frame() {
    let line = format!("{{\"a\": \"{}\"}}\n", "x".repeat(MAX_MESSAGE));
    let error = decode_frame(line.as_bytes()).unwrap_err();
    assert_eq!(error.code(), ResultCode::Overlimit);
}

#[test]
fn decode_frame_rejects_invalid_encoding_json_and_non_objects() {
    let invalid_utf8 = decode_frame(&[0xff, b'\n']).unwrap_err();
    assert_eq!(invalid_utf8.code(), ResultCode::BadRequest);
    let invalid_json = decode_frame(b"not-json\n").unwrap_err();
    assert_eq!(invalid_json.code(), ResultCode::BadRequest);
    let non_object = decode_frame(b"[1, 2]\n").unwrap_err();
    assert_eq!(non_object.code(), ResultCode::BadRequest);
    assert!(decode_frame(b"{\"a\": 1}\n").is_ok());
}

#[test]
fn the_frame_bound_and_schema_match_the_fixture() {
    let fixture = um_contract::ContractFixture::load_default().expect("fixture");
    assert_eq!(MAX_MESSAGE as u64, fixture.broker.max_message_bytes);
    assert_eq!(fixture.broker.schema_version, 2);
}

#[test]
fn the_result_code_table_matches_the_fixture() {
    let fixture = um_contract::ContractFixture::load_default().expect("fixture");
    let mut fixture_codes = fixture.broker.result_codes.clone();
    fixture_codes.sort();
    let mut codes: Vec<&str> = ResultCode::ALL.iter().map(|code| code.as_str()).collect();
    codes.sort_unstable();
    assert_eq!(codes, fixture_codes);
    assert_eq!(ResultCode::ALL.len(), 18);
}

#[test]
fn unknown_result_codes_are_never_completion() {
    assert!(ResultCode::parse("not_a_code").is_none());
    let decoded = BrokerResult::from_value(&json!({
        "ok": true,
        "error_code": "not_a_code",
        "result": {},
    }));
    assert!(!decoded.ok_flag());
    assert_eq!(decoded.error_code(), ResultCode::InternalError);
}

#[test]
fn broker_result_round_trips_its_structured_shape() {
    let ok = BrokerResult::ok(json!({"state": "running"}));
    assert_eq!(
        ok.to_value(),
        json!({"ok": true, "error_code": "ok", "result": {"state": "running"}})
    );
    let err = BrokerResult::err(ResultCode::Forbidden, "forged or missing mac");
    assert_eq!(
        err.to_value(),
        json!({
            "ok": false,
            "error_code": "forbidden",
            "result": {"detail": "forged or missing mac"},
        })
    );
    let empty = BrokerResult::err(ResultCode::Blocked, "");
    assert_eq!(
        empty.to_value(),
        json!({"ok": false, "error_code": "blocked", "result": {}})
    );
    let decoded = BrokerResult::from_value(&err.to_value());
    assert_eq!(decoded, err);
}

#[test]
fn a_broker_result_ok_requires_the_literal_true() {
    let decoded = BrokerResult::from_value(&json!({
        "ok": 1,
        "error_code": "ok",
        "result": {},
    }));
    assert!(!decoded.ok_flag());
    let decoded = BrokerResult::from_value(&json!({
        "ok": true,
        "error_code": "ok",
        "result": null,
    }));
    assert!(decoded.ok_flag());
    assert_eq!(decoded.result(), &json!({}));
}

#[test]
fn a_non_object_reply_is_an_internal_error() {
    let decoded = BrokerResult::from_value(&json!([1, 2, 3]));
    assert_eq!(decoded.error_code(), ResultCode::InternalError);
    assert!(!decoded.ok_flag());
}

#[test]
fn a_protocol_error_carries_a_fixed_code() {
    let error = ProtocolError::new(ResultCode::StaleTs, "outside window");
    assert_eq!(error.code(), ResultCode::StaleTs);
    assert_eq!(error.message(), "outside window");
}
