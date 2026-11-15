//! Acceptance 8: one unified error body for every failure.
//!
//! `{"error": {"message", "type", "detail"}}` is always present; `code`,
//! `reason` and `upstream_status` are the only optional keys. The messages
//! come from the Python fixture recorded in
//! `tests/fixtures/rust_gateway_rev1.json`; the console `{"error": "..."}`
//! string shape is deliberately not used.

mod support;

use serde_json::{json, Value};

use um_gateway::error::GatewayError;

use support::*;

fn assert_unified(body: &Value) {
    let error = body
        .get("error")
        .and_then(Value::as_object)
        .unwrap_or_else(|| panic!("error is not an object: {body}"));
    let allowed = [
        "message",
        "type",
        "detail",
        "code",
        "reason",
        "upstream_status",
    ];
    for key in error.keys() {
        assert!(allowed.contains(&key.as_str()), "unexpected key: {key}");
    }
    assert!(error.get("message").is_some_and(Value::is_string), "{body}");
    assert!(error.get("type").is_some_and(Value::is_string), "{body}");
    assert!(error.get("detail").is_some(), "{body}");
    for optional in ["code", "reason"] {
        if let Some(value) = error.get(optional) {
            assert!(value.is_string(), "{optional}: {body}");
        }
    }
    if let Some(value) = error.get("upstream_status") {
        assert!(value.is_u64(), "upstream_status: {body}");
    }
}

#[test]
fn every_recorded_error_body_uses_the_unified_shape() {
    let fixture = fixture();
    let mut bodies = Vec::new();
    for case in fixture["shareGate"]["credentialCases"]
        .as_array()
        .expect("credential cases")
    {
        bodies.push(case["body"].clone());
    }
    for case in fixture["shareGate"]["routeCases"]
        .as_array()
        .expect("route cases")
    {
        bodies.push(case["body"].clone());
    }
    for case in fixture["bodyRejection"]["share"]
        .as_object()
        .expect("share body cases")
        .values()
    {
        bodies.push(case["body"].clone());
    }
    for case in fixture["control"]
        .as_object()
        .expect("control cases")
        .values()
    {
        bodies.push(case["body"].clone());
    }
    bodies.push(fixture["cors"]["originDenied"]["body"].clone());
    assert!(bodies.len() > 20);
    for body in bodies {
        if body.get("error").is_some() {
            assert_unified(&body);
        }
    }
}

#[test]
fn the_console_unknown_field_response_is_an_object() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/admin/share/revoke", "")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(r#"{"key_id":"friend","unknown":1}"#),
    )
    .expect("console unknown field");
    assert_eq!(response.status, 400);
    let body = response.json();
    assert_unified(&body);
    assert_eq!(
        body["error"]["message"],
        json!("unknown field(s): ['unknown']")
    );
    assert_eq!(body["error"]["type"], json!("invalid_request"));
}

#[test]
fn sharing_limit_carries_code_reason_and_detail() {
    let error = GatewayError::sharing_limit_reached(
        Some("per_key_concurrency"),
        Some("per-key concurrency limit reached (max 1 in-flight requests for this key)"),
    );
    let body = error.body();
    assert_unified(&body);
    assert_eq!(body["error"]["message"], json!("sharing_limit_reached"));
    assert_eq!(body["error"]["type"], json!("share_gateway_error"));
    assert_eq!(body["error"]["code"], json!("sharing_limit_reached"));
    assert_eq!(body["error"]["reason"], json!("per_key_concurrency"));
    assert_eq!(
        body["error"]["detail"],
        json!("per-key concurrency limit reached (max 1 in-flight requests for this key)")
    );
}

#[test]
fn invalid_api_key_carries_code_and_null_detail() {
    let body = GatewayError::invalid_api_key().body();
    assert_unified(&body);
    assert_eq!(body["error"]["message"], json!("invalid_api_key"));
    assert_eq!(body["error"]["type"], json!("share_gateway_error"));
    assert_eq!(body["error"]["detail"], Value::Null);
    assert_eq!(body["error"]["code"], json!("invalid_api_key"));
}

#[test]
fn upstream_unavailable_carries_upstream_status() {
    let body = GatewayError::upstream_unavailable(Some(500)).body();
    assert_unified(&body);
    assert_eq!(body["error"]["type"], json!("upstream_unavailable"));
    assert_eq!(body["error"]["upstream_status"], json!(500));

    let body = GatewayError::upstream_unavailable(None).body();
    assert_unified(&body);
    assert_eq!(body["error"]["upstream_status"], Value::Null);
}

#[test]
fn a_501_not_implemented_error_still_uses_the_unified_shape() {
    let body = GatewayError::not_implemented("chat_forwarding_s1b").body();
    assert_unified(&body);
    assert_eq!(body["error"]["type"], json!("not_implemented"));
    assert_eq!(body["error"]["detail"], json!("chat_forwarding_s1b"));
}
