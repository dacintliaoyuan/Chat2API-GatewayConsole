//! AC2: HMAC, timestamp window and request-shape validation.
//!
//! Expected MACs and signed payloads come from
//! `tests/fixtures/rust_broker_wire_rev1.json`, generated from
//! `share/broker_protocol.py`; the Rust side never recomputes the expectation
//! with its own signer.

use std::path::Path;

use serde_json::{json, Map, Value};
use um_broker::error::ResultCode;
use um_broker::request::{
    validate_instance_request, validate_management_request, MANAGEMENT_ACTIONS,
};

fn broker_fixture() -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/fixtures/rust_broker_wire_rev1.json");
    let text = std::fs::read_to_string(&path).expect("broker wire fixture");
    serde_json::from_str(&text).expect("broker wire fixture is JSON")
}

fn signed_keys(body: &Value) -> Vec<String> {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    let instance = body.get("instance_id").is_some();
    let keys = if instance {
        &fixture.broker.instance_signed_keys
    } else {
        &fixture.broker.management_signed_keys
    };
    keys.clone()
}

fn vector(index: usize) -> (String, Value) {
    let fixture = broker_fixture();
    let vector = &fixture["hmac"][index];
    let secret = vector["secret"].as_str().expect("secret").to_string();
    let body = vector["body"].clone();
    (secret, body)
}

fn resign(secret: &str, body: &Value) -> Value {
    let keys = signed_keys(body);
    let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
    let mut body = body.as_object().expect("object").clone();
    um_broker::hmac::sign_value(secret, &mut body, &keys);
    Value::Object(body)
}

fn code(error: &um_broker::error::ProtocolError) -> ResultCode {
    error.code()
}

#[test]
fn python_generated_mac_vectors_validate() {
    let fixture = broker_fixture();
    let vectors = fixture["hmac"].as_array().expect("vectors");
    assert_eq!(vectors.len(), 3);
    for vector in vectors {
        let secret = vector["secret"].as_str().expect("secret");
        let body = &vector["body"];
        let keys: Vec<String> = vector["signedKeys"]
            .as_array()
            .expect("signedKeys")
            .iter()
            .map(|value| value.as_str().expect("key").to_string())
            .collect();
        let keys: Vec<&str> = keys.iter().map(String::as_str).collect();
        let payload = um_broker::hmac::signed_payload(body, &keys);
        assert_eq!(payload, vector["payload"].as_str().expect("payload"));
        let expected = vector["mac"].as_str().expect("mac");
        assert_eq!(um_broker::hmac::mac(secret, payload.as_bytes()), expected);
        if body.get("instance_id").is_some() {
            let request = validate_instance_request(body, secret, body["ts"].as_i64().unwrap())
                .expect("instance vector validates");
            assert_eq!(request.request_id, body["request_id"].as_str().unwrap());
        } else {
            let request = validate_management_request(body, secret, body["ts"].as_i64().unwrap())
                .expect("management vector validates");
            assert_eq!(request.request_id, body["request_id"].as_str().unwrap());
        }
    }
}

#[test]
fn a_forged_mac_is_forbidden() {
    let (secret, mut body) = vector(0);
    body["mac"] = Value::String("0".repeat(64));
    let error =
        validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
    assert_eq!(code(&error), ResultCode::Forbidden);
    assert!(error.message().contains("forged or missing mac"));
}

#[test]
fn a_missing_or_non_string_mac_is_forbidden() {
    let (secret, mut body) = vector(0);
    body.as_object_mut().unwrap().remove("mac");
    let error =
        validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
    assert_eq!(code(&error), ResultCode::Forbidden);

    let (secret, mut body) = vector(0);
    body["mac"] = json!(1234);
    let error =
        validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
    assert_eq!(code(&error), ResultCode::Forbidden);
}

#[test]
fn the_wrong_secret_is_forbidden() {
    let (_secret, body) = vector(0);
    let error = validate_management_request(&body, "another-secret", body["ts"].as_i64().unwrap())
        .unwrap_err();
    assert_eq!(code(&error), ResultCode::Forbidden);
}

#[test]
fn a_timestamp_outside_the_window_is_stale() {
    let (secret, body) = vector(0);
    let ts = body["ts"].as_i64().unwrap();
    for now in [ts - 301, ts + 301, ts + 10_000] {
        let error = validate_management_request(&body, &secret, now).unwrap_err();
        assert_eq!(code(&error), ResultCode::StaleTs, "now={now}");
        assert!(error.message().contains("timestamp outside window"));
    }
    for now in [ts - 300, ts, ts + 300] {
        assert!(
            validate_management_request(&body, &secret, now).is_ok(),
            "now={now}"
        );
    }
}

#[test]
fn a_missing_or_boolean_timestamp_is_a_bad_request() {
    let (secret, mut body) = vector(0);
    body.as_object_mut().unwrap().remove("ts");
    let error = validate_management_request(&body, &secret, 0).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);

    let (secret, mut body) = vector(0);
    body["ts"] = json!(true);
    let error = validate_management_request(&body, &secret, 1).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
}

#[test]
fn an_unsupported_version_is_a_bad_request() {
    let (secret, mut body) = vector(0);
    body["v"] = json!(1);
    let error =
        validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    assert!(error.message().contains("protocol version"));
}

#[test]
fn an_unknown_action_is_a_bad_request() {
    let (secret, mut body) = vector(0);
    body["action"] = json!("shell");
    let error =
        validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    assert!(error.message().contains("unknown action"));
}

#[test]
fn request_id_is_bounded() {
    let (secret, body) = vector(0);
    let ts = body["ts"].as_i64().unwrap();
    for request_id in ["", &"x".repeat(129)] {
        let mut candidate = body.clone();
        candidate["request_id"] = json!(request_id);
        let candidate = resign(&secret, &candidate);
        let error = validate_management_request(&candidate, &secret, ts).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest);
        assert!(error.message().contains("request_id"));
    }
    let mut candidate = body.clone();
    candidate["request_id"] = json!("x".repeat(128));
    let candidate = resign(&secret, &candidate);
    assert!(validate_management_request(&candidate, &secret, ts).is_ok());
}

#[test]
fn the_management_channel_rejects_instance_id() {
    let (secret, mut body) = vector(0);
    body["instance_id"] = json!("i-1");
    let error =
        validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    assert!(error.message().contains("instance_id"));
}

#[test]
fn the_instance_channel_requires_its_own_id() {
    let (secret, body) = vector(1);
    let ts = body["ts"].as_i64().unwrap();
    let mut missing = body.clone();
    missing.as_object_mut().unwrap().remove("instance_id");
    let error = validate_instance_request(&missing, &secret, ts).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    assert!(error.message().contains("instance_id"));

    let mut long = body.clone();
    long["instance_id"] = json!("x".repeat(65));
    let error = validate_instance_request(&long, &secret, ts).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    let mut valid = body.clone();
    valid["instance_id"] = json!("x".repeat(64));
    let valid = resign(&secret, &valid);
    assert!(validate_instance_request(&valid, &secret, ts).is_ok());
}

#[test]
fn fields_must_be_an_object() {
    let (secret, mut body) = vector(0);
    body["fields"] = json!([]);
    let body = resign(&secret, &body);
    let error =
        validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    assert!(error.message().contains("fields"));
}

#[test]
fn unknown_fields_are_rejected_not_ignored() {
    for field in ["path", "command", "service", "shell", "argv", "pid", "port"] {
        let (secret, mut body) = vector(0);
        body["fields"] = json!({field: "/bin/sh"});
        let body = resign(&secret, &body);
        let error =
            validate_management_request(&body, &secret, body["ts"].as_i64().unwrap()).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest, "field={field}");
        assert!(
            error.message().contains("unknown field"),
            "field={field}: {error}"
        );
    }
}

#[test]
fn a_missing_required_field_is_rejected() {
    let (secret, body) = vector(2); // publish
    let ts = body["ts"].as_i64().unwrap();
    for required in ["generation", "fingerprint", "projection"] {
        let mut candidate = body.clone();
        candidate["fields"]
            .as_object_mut()
            .unwrap()
            .remove(required);
        let candidate = resign(&secret, &candidate);
        let error = validate_management_request(&candidate, &secret, ts).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest, "field={required}");
        assert!(error.message().contains(required));
    }
}

#[test]
fn a_boolean_is_not_an_integer_field() {
    let (secret, body) = vector(2);
    let ts = body["ts"].as_i64().unwrap();
    let mut candidate = body.clone();
    candidate["fields"]["generation"] = json!(true);
    let candidate = resign(&secret, &candidate);
    let error = validate_management_request(&candidate, &secret, ts).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    assert!(error.message().contains("positive integer"));
}

#[test]
fn terminate_keys_are_bounded_key_ids() {
    let (secret, body) = vector(2);
    let ts = body["ts"].as_i64().unwrap();
    for keys in [
        json!([]),
        json!(["x".repeat(65)]),
        Value::Array(vec![json!("x"); 17]),
        json!([1]),
    ] {
        let mut candidate = body.clone();
        candidate["fields"]["terminate_keys"] = keys;
        let candidate = resign(&secret, &candidate);
        let error = validate_management_request(&candidate, &secret, ts).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest);
    }
    let mut candidate = body.clone();
    candidate["fields"]["terminate_keys"] = json!(["k-1", "k-2"]);
    let candidate = resign(&secret, &candidate);
    assert!(validate_management_request(&candidate, &secret, ts).is_ok());
}

#[test]
fn optional_fields_are_omitted_when_absent() {
    let (secret, body) = vector(2);
    let ts = body["ts"].as_i64().unwrap();
    let mut candidate = body.clone();
    candidate["fields"]
        .as_object_mut()
        .unwrap()
        .remove("terminate_keys");
    let candidate = resign(&secret, &candidate);
    let request = validate_management_request(&candidate, &secret, ts).unwrap();
    assert!(!request.fields.contains_key("terminate_keys"));

    let (secret, body) = vector(1); // instance load
    let ts = body["ts"].as_i64().unwrap();
    let mut candidate = body.clone();
    candidate["fields"]
        .as_object_mut()
        .unwrap()
        .remove("terminate_keys");
    let candidate = resign(&secret, &candidate);
    let request = validate_instance_request(&candidate, &secret, ts).unwrap();
    assert!(!request.fields.contains_key("terminate_keys"));
}

#[test]
fn the_action_tables_match_the_fixture() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    let fixture_management: Vec<String> = fixture.broker.management_actions.clone();
    let mut management: Vec<&str> = MANAGEMENT_ACTIONS
        .iter()
        .map(|action| action.name())
        .collect();
    management.sort_unstable();
    assert_eq!(management, fixture_management);
}

#[test]
fn the_instance_vector_validates_on_the_instance_channel_only() {
    let (secret, body) = vector(1);
    let ts = body["ts"].as_i64().unwrap();
    let error = validate_management_request(&body, &secret, ts).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    let request = validate_instance_request(&body, &secret, ts).unwrap();
    assert_eq!(request.instance_id.as_deref(), Some("i-1"));
    assert_eq!(request.fields["generation"], json!(3));
}

#[test]
fn a_non_object_request_is_a_bad_request() {
    let error = validate_management_request(&json!([1, 2]), "secret", 0).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
    assert!(error.message().contains("object"));
}

#[test]
fn the_signed_payload_uses_only_the_signed_keys() {
    let body = json!({
        "v": 2,
        "action": "status",
        "request_id": "r-1",
        "ts": 1,
        "fields": {},
        "mac": "ignored",
        "extra": "not signed",
    });
    let payload =
        um_broker::hmac::signed_payload(&body, &["v", "action", "request_id", "ts", "fields"]);
    assert_eq!(
        payload,
        r#"{"action": "status", "fields": {}, "request_id": "r-1", "ts": 1, "v": 2}"#
    );
}

#[test]
fn sign_value_removes_a_stale_mac_before_signing() {
    let mut body = Map::new();
    body.insert("v".to_string(), json!(2));
    body.insert("action".to_string(), json!("status"));
    body.insert("request_id".to_string(), json!("r-1"));
    body.insert("ts".to_string(), json!(1));
    body.insert("fields".to_string(), json!({}));
    body.insert("mac".to_string(), json!("stale"));
    let mac = um_broker::hmac::sign_value(
        "test-broker-secret",
        &mut body,
        &["v", "action", "request_id", "ts", "fields"],
    );
    assert_eq!(mac.len(), 64);
    assert_eq!(body["mac"], json!(mac));
    assert!(validate_management_request(&Value::Object(body), "test-broker-secret", 1).is_ok());
}
