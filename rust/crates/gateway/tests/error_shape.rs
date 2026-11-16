//! Acceptance 8: one unified error envelope for every failure.
//!
//! OpenAI-shaped errors always carry `message`, `type` (category), `code`
//! (machine code), `param` and `detail`; `reason` and `upstream_status` are the
//! only optional diagnostics. `/v1/messages` errors use the Anthropic outer shape
//! while keeping the same category/code vocabulary inside `error`.

mod support;

use serde_json::{json, Value};

use um_gateway::error::GatewayError;

fn assert_error_object(error: &Value) {
    let error = error
        .as_object()
        .unwrap_or_else(|| panic!("error is not an object: {error}"));
    let allowed = [
        "message",
        "type",
        "code",
        "param",
        "detail",
        "reason",
        "upstream_status",
    ];
    for key in error.keys() {
        assert!(allowed.contains(&key.as_str()), "unexpected key: {key}");
    }
    assert!(
        error.get("message").is_some_and(Value::is_string),
        "{error:?}"
    );
    assert!(error.get("type").is_some_and(Value::is_string), "{error:?}");
    assert!(error.get("code").is_some_and(Value::is_string), "{error:?}");
    assert!(
        error
            .get("param")
            .is_some_and(|value| value.is_null() || value.is_string()),
        "{error:?}"
    );
    assert!(error.get("detail").is_some(), "{error:?}");
    if let Some(value) = error.get("reason") {
        assert!(value.is_string(), "reason: {error:?}");
    }
    if let Some(value) = error.get("upstream_status") {
        assert!(value.is_u64(), "upstream_status: {error:?}");
    }
}

fn assert_unified(body: &Value) {
    let error = body
        .get("error")
        .unwrap_or_else(|| panic!("error is missing: {body}"));
    assert_error_object(error);
}

fn assert_anthropic(body: &Value) {
    assert_eq!(body["type"], json!("error"), "{body}");
    assert_error_object(&body["error"]);
}

#[test]
fn every_gateway_error_constructor_uses_the_unified_shape() {
    let bodies = [
        GatewayError::invalid_request("bad request").body(),
        GatewayError::invalid_json().body(),
        GatewayError::invalid_body().body(),
        GatewayError::payload_too_large().body(),
        GatewayError::unsupported_chat_request().body(),
        GatewayError::unsupported_messages_request().body(),
        GatewayError::unsupported_responses_request().body(),
        GatewayError::route_not_shared().body(),
        GatewayError::unknown_path("/v1/unknown").body(),
        GatewayError::invalid_api_key().body(),
        GatewayError::local_auth_error().body(),
        GatewayError::origin_denied().body(),
        GatewayError::control_denied_endpoint().body(),
        GatewayError::control_denied_token().body(),
        GatewayError::share_config_unavailable("Invalid sharing configuration").body(),
        GatewayError::sharing_limit_reached(Some("per_key_concurrency"), Some("detail")).body(),
        GatewayError::reauthentication_required(Some("upstream_rejected")).body(),
        GatewayError::upstream_unavailable(Some(500)).body(),
        GatewayError::models_unavailable().body(),
        GatewayError::upstream_rejected(400).body(),
        GatewayError::upstream_request_failed(500, "upstream exploded").body(),
        GatewayError::upstream_status_error(403, Some("forbidden"), None).body(),
        GatewayError::request_deadline_exceeded(Some(502)).body(),
        GatewayError::upstream_forbidden().body(),
        GatewayError::upstream_quota_exhausted().body(),
        GatewayError::reauthentication_failed().body(),
        GatewayError::method_not_allowed().body(),
        GatewayError::unsupported_content_encoding().body(),
        GatewayError::request_timeout().body(),
        GatewayError::upstream_timeout().body(),
        GatewayError::not_implemented("chat_forwarding_s1b").body(),
    ];
    assert!(bodies.len() >= 20);
    for body in bodies {
        assert_unified(&body);
    }
}

#[test]
fn messages_errors_use_the_anthropic_outer_shape() {
    for error in [
        GatewayError::unsupported_messages_request(),
        GatewayError::invalid_json(),
        GatewayError::invalid_api_key(),
        GatewayError::reauthentication_required(Some("upstream_rejected")),
    ] {
        let body = error.anthropic_body();
        assert_anthropic(&body);
        assert_eq!(body["error"]["message"], json!(error.message));
        assert_eq!(body["error"]["type"], json!(error.error_type));
        assert_eq!(body["error"]["code"], json!(error.code));
        assert_eq!(body["error"]["param"], Value::Null);
    }
}

#[test]
fn sharing_limit_carries_category_code_reason_and_detail() {
    let error = GatewayError::sharing_limit_reached(
        Some("per_key_concurrency"),
        Some("per-key concurrency limit reached (max 1 in-flight requests for this key)"),
    );
    let body = error.body();
    assert_unified(&body);
    assert_eq!(body["error"]["message"], json!("sharing_limit_reached"));
    assert_eq!(body["error"]["type"], json!("rate_limit_error"));
    assert_eq!(body["error"]["code"], json!("sharing_limit_reached"));
    assert_eq!(body["error"]["param"], Value::Null);
    assert_eq!(body["error"]["reason"], json!("per_key_concurrency"));
    assert_eq!(
        body["error"]["detail"],
        json!("per-key concurrency limit reached (max 1 in-flight requests for this key)")
    );
}

#[test]
fn invalid_api_key_carries_category_code_and_null_param() {
    let body = GatewayError::invalid_api_key().body();
    assert_unified(&body);
    assert_eq!(body["error"]["message"], json!("invalid_api_key"));
    assert_eq!(body["error"]["type"], json!("authentication_error"));
    assert_eq!(body["error"]["code"], json!("invalid_api_key"));
    assert_eq!(body["error"]["param"], Value::Null);
    assert_eq!(body["error"]["detail"], Value::Null);
}

#[test]
fn upstream_unavailable_carries_upstream_status() {
    let body = GatewayError::upstream_unavailable(Some(500)).body();
    assert_unified(&body);
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("upstream_unavailable"));
    assert_eq!(body["error"]["param"], Value::Null);
    assert_eq!(body["error"]["upstream_status"], json!(500));

    let body = GatewayError::upstream_unavailable(None).body();
    assert_unified(&body);
    assert_eq!(body["error"]["upstream_status"], Value::Null);
}

#[test]
fn preserved_upstream_status_errors_keep_their_category_code_and_detail() {
    for (status, category, code) in [
        (402, "rate_limit_error", "upstream_quota_exhausted"),
        (403, "permission_error", "upstream_forbidden"),
        (404, "upstream_error", "upstream_request_rejected"),
        (409, "upstream_error", "upstream_request_rejected"),
        (413, "upstream_error", "upstream_request_rejected"),
        (415, "upstream_error", "upstream_request_rejected"),
        (429, "rate_limit_error", "upstream_rate_limited"),
        (500, "upstream_error", "upstream_unavailable"),
        (502, "upstream_error", "upstream_unavailable"),
        (503, "upstream_error", "upstream_unavailable"),
        (504, "upstream_error", "upstream_timeout"),
    ] {
        let body = GatewayError::upstream_status_error(status, Some("upstream text"), None).body();
        assert_unified(&body);
        assert_eq!(body["error"]["type"], json!(category), "{status}: {body}");
        assert_eq!(body["error"]["code"], json!(code), "{status}: {body}");
        assert_eq!(body["error"]["param"], Value::Null, "{status}: {body}");
        assert_eq!(
            body["error"]["detail"]["upstream_status"],
            json!(status),
            "{status}: {body}"
        );
        assert_eq!(
            body["error"]["detail"]["upstream_message"],
            json!("upstream text"),
            "{status}: {body}"
        );
        assert_eq!(
            body["error"]["upstream_status"],
            json!(status),
            "{status}: {body}"
        );
    }
}

#[test]
fn a_401_upstream_status_is_a_503_reauthentication_error() {
    let error = GatewayError::upstream_status_error(401, Some("expired"), Some("12"));
    let body = error.body();
    assert_unified(&body);
    assert_eq!(body["error"]["type"], json!("authentication_error"));
    assert_eq!(body["error"]["code"], json!("reauthentication_required"));
    assert_eq!(body["error"]["detail"], json!("upstream_rejected"));
    assert!(body["error"].get("upstream_status").is_none());
    let response = error.response();
    assert_eq!(response.status, 503);
    assert_eq!(response.header("retry-after"), Some("12"));
}

#[test]
fn a_total_deadline_keeps_the_last_upstream_status_in_detail() {
    let body = GatewayError::request_deadline_exceeded(Some(503)).body();
    assert_unified(&body);
    assert_eq!(body["error"]["type"], json!("upstream_error"));
    assert_eq!(body["error"]["code"], json!("request_deadline_exceeded"));
    assert_eq!(body["error"]["detail"]["last_upstream_status"], json!(503));
    assert_eq!(body["error"]["upstream_status"], json!(503));
}

#[test]
fn a_501_not_implemented_error_still_uses_the_unified_shape() {
    let body = GatewayError::not_implemented("chat_forwarding_s1b").body();
    assert_unified(&body);
    assert_eq!(body["error"]["type"], json!("api_error"));
    assert_eq!(body["error"]["code"], json!("not_implemented"));
    assert_eq!(body["error"]["detail"], json!("chat_forwarding_s1b"));
}

#[test]
fn a_field_error_carries_the_field_name_in_param() {
    let body = GatewayError::unknown_fields(&["unknown".to_string()]).body();
    assert_unified(&body);
    assert_eq!(body["error"]["type"], json!("invalid_request_error"));
    assert_eq!(body["error"]["code"], json!("invalid_request"));
    assert_eq!(body["error"]["param"], json!("unknown"));
}

#[test]
fn the_new_machine_codes_have_their_categories() {
    for (error, category) in [
        (GatewayError::upstream_forbidden(), "permission_error"),
        (GatewayError::upstream_quota_exhausted(), "rate_limit_error"),
        (
            GatewayError::reauthentication_failed(),
            "authentication_error",
        ),
        (GatewayError::method_not_allowed(), "invalid_request_error"),
        (
            GatewayError::unsupported_content_encoding(),
            "invalid_request_error",
        ),
        (GatewayError::request_timeout(), "invalid_request_error"),
        (GatewayError::upstream_timeout(), "upstream_error"),
    ] {
        let body = error.body();
        assert_unified(&body);
        assert_eq!(body["error"]["type"], json!(category));
        assert_eq!(body["error"]["code"], json!(error.code));
    }
}
