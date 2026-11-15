//! Acceptance 6: request-body and field rejection.
//!
//! Share routes keep the 8003 `_read_body` codes; the local control-token
//! route keeps the console `_read_share_json_body` codes (duplicate sensitive
//! headers, unsupported encoding, unique Content-Length 0..65536, required
//! `application/json`, incomplete body). Both are recorded from Python in
//! `tests/fixtures/rust_gateway_rev1.json`.

mod support;

use serde_json::{json, Value};
use um_gateway::config::GatewayConfig;
use um_gateway::GatewayServer;

use support::*;

fn expected_error(case: &Value) -> Value {
    let old = &case["body"]["error"];
    let code = old["code"]
        .as_str()
        .or_else(|| {
            old["type"].as_str().filter(|value| {
                matches!(
                    *value,
                    "invalid_request"
                        | "invalid_json"
                        | "invalid_body"
                        | "payload_too_large"
                        | "unsupported_chat_request"
                        | "unsupported_messages_request"
                )
            })
        })
        .or_else(|| {
            old["message"]
                .as_str()
                .filter(|value| *value == "unsupported_responses_request")
        })
        .expect("old body error code");
    let category = match code {
        "invalid_request"
        | "invalid_json"
        | "invalid_body"
        | "payload_too_large"
        | "unsupported_chat_request"
        | "unsupported_messages_request"
        | "unsupported_responses_request" => "invalid_request_error",
        other => panic!("unexpected body error code: {other}"),
    };
    let error = json!({
        "message": old["message"],
        "type": category,
        "code": code,
        "param": null,
        "detail": null,
    });
    if case["target"]
        .as_str()
        .is_some_and(|target| target.starts_with("/v1/messages"))
    {
        json!({"type": "error", "error": error})
    } else {
        json!({"error": error})
    }
}

fn share_request(name: &str, case: &Value) -> ClientRequest {
    let target = case["target"].as_str().expect("target");
    let mut request = ClientRequest::post(target, "");
    request.body = None;
    match name {
        "invalid-content-length" => {
            request = request.header("Content-Length", "not-a-number");
        }
        "invalid-json" => {
            request = request
                .header("Content-Length", "1")
                .header("Content-Type", "application/json");
            request.body = Some(b"{".to_vec());
        }
        "non-object" => {
            request = request
                .header("Content-Length", "2")
                .header("Content-Type", "application/json");
            request.body = Some(b"[]".to_vec());
        }
        "unknown-chat-field" => {
            request = request.json_body(
                r#"{"model":"m","messages":[{"role":"user","content":"hi"}],"unknown":1}"#,
            );
        }
        "unknown-messages-field" => {
            request =
                request.json_body(r#"{"model":"m","max_tokens":1,"messages":[],"unknown":1}"#);
        }
        "unknown-responses-field" => {
            request = request.json_body(r#"{"model":"m","input":"hi","unknown":1}"#);
        }
        "payload-too-large" => {
            request = request
                .header("Content-Length", "11")
                .header("Content-Type", "application/json");
            request.body = Some(vec![b'x'; 11]);
        }
        other => panic!("unhandled share body case: {other}"),
    }
    request.bearer(SHARE_SECRET)
}

fn revoke_request() -> ClientRequest {
    ClientRequest::post("/admin/share/revoke", "").header("X-Admin-Control", CONTROL_TOKEN)
}

#[test]
fn share_route_body_cases_match_python() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let cases = fixture["bodyRejection"]["share"]
        .as_object()
        .expect("share body cases");
    assert!(!cases.is_empty());
    for (name, case) in cases {
        if name == "payload-too-large" {
            continue;
        }
        let response = client_request(gateway.addr, &share_request(name, case))
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(
            u64::from(response.status),
            case["status"].as_u64().unwrap(),
            "status for {name}"
        );
        assert_eq!(response.json(), expected_error(case), "body for {name}");
    }
}

#[test]
fn the_share_body_limit_is_32_mib_by_default() {
    assert_eq!(
        GatewayConfig::default().share_max_request_bytes,
        32 * 1024 * 1024
    );
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let mut config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        share_config: Some(temp.write("share.json", &legacy_share_config())),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        share_max_request_bytes: 10,
        ..GatewayConfig::default()
    };
    config.candidate = true;
    let server = GatewayServer::start(config).expect("small limit gateway");
    let addr = server.addr();
    let case = &fixture["bodyRejection"]["share"]["payload-too-large"];
    let response =
        client_request(addr, &share_request("payload-too-large", case)).expect("oversize");
    assert_eq!(response.status, 413);
    assert_eq!(response.json(), expected_error(case));
}

#[test]
fn revoke_uses_the_share_body_boundary_like_python() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    // No Content-Type: Python `_read_body` never requires one.
    let mut request = revoke_request();
    request.body = Some(r#"{"key_id":"friend"}"#.as_bytes().to_vec());
    let response = client_request(gateway.addr, &request).expect("revoke without content type");
    assert_eq!(response.status, 200);
    assert_eq!(response.json(), json!({"released": 0}));

    // Duplicate sensitive headers are tolerated by `_read_body`.
    let mut request = revoke_request()
        .header("Content-Type", "application/json")
        .header("Content-Type", "text/plain")
        .header("Authorization", "Bearer ignored")
        .header("Authorization", "Bearer also-ignored")
        .header("Cookie", "a=1")
        .header("Cookie", "b=2")
        .header("Content-Length", "19")
        .header("Content-Length", "19");
    request.body = Some(r#"{"key_id":"friend"}"#.as_bytes().to_vec());
    let response = client_request(gateway.addr, &request).expect("revoke with duplicate headers");
    assert_eq!(response.status, 200);
    assert_eq!(response.json(), json!({"released": 0}));

    // Unknown fields are ignored, exactly like the Python handler.
    let response = client_request(
        gateway.addr,
        &revoke_request().json_body(r#"{"key_id":"friend","unknown":1}"#),
    )
    .expect("revoke with unknown field");
    assert_eq!(response.status, 200);
    assert_eq!(response.json(), json!({"released": 0}));

    // The share limit, not the 64 KiB console limit, bounds the body.
    let oversized = format!(
        r#"{{"key_id":"friend","padding":"{}"}}"#,
        "x".repeat(70_000)
    );
    let response = client_request(gateway.addr, &revoke_request().json_body(&oversized))
        .expect("revoke above the console limit");
    assert_eq!(response.status, 200);
    assert_eq!(response.json(), json!({"released": 0}));

    // Invalid and negative lengths keep the `_read_body` vocabulary.
    let response = client_request(
        gateway.addr,
        &revoke_request().header("Content-Length", "not-a-number"),
    )
    .expect("revoke with invalid length");
    assert_eq!(response.status, 400);
    assert_eq!(
        response.json()["error"]["message"],
        json!("Invalid Content-Length header")
    );
    let response = client_request(
        gateway.addr,
        &revoke_request().header("Content-Length", "-1"),
    )
    .expect("revoke with negative length");
    assert_eq!(response.status, 413);
    assert_eq!(
        response.json()["error"]["type"],
        json!("invalid_request_error")
    );
    assert_eq!(response.json()["error"]["code"], json!("payload_too_large"));
}

#[test]
fn share_route_refuses_a_declared_oversize_body_before_reading_it() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let mut config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        share_config: Some(temp.write("share.json", &legacy_share_config())),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        share_max_request_bytes: 10,
        ..GatewayConfig::default()
    };
    config.candidate = true;
    let server = GatewayServer::start(config).expect("small share limit gateway");
    let mut request = ClientRequest::post("/v1/chat/completions", "")
        .bearer(SHARE_SECRET)
        .header("Content-Length", "11");
    request.body = None;
    let response =
        client_request_with_timeout(server.addr(), &request, std::time::Duration::from_secs(2))
            .expect("declared oversize without body bytes");
    assert_eq!(response.status, 413);
    assert_eq!(
        response.json(),
        json!({"error": {
            "message": "Request body too large",
            "type": "invalid_request_error",
            "code": "payload_too_large",
            "param": null,
            "detail": null,
        }})
    );
}

#[test]
fn a_negative_content_length_is_payload_too_large_like_python() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let share = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", "")
            .bearer(SHARE_SECRET)
            .header("Content-Length", "-1"),
    )
    .expect("negative share length");
    assert_eq!(share.status, 413);
    assert_eq!(
        share.json()["error"]["type"],
        json!("invalid_request_error")
    );

    let revoke = client_request(
        gateway.addr,
        &ClientRequest::post("/admin/share/revoke", "")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .header("Content-Length", "-1"),
    )
    .expect("negative revoke length");
    assert_eq!(revoke.status, 413);
    assert_eq!(
        revoke.json()["error"]["message"],
        json!("Request body too large")
    );
}

#[test]
fn share_route_duplicate_content_type_is_not_a_share_key_error() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let request = ClientRequest::post("/v1/chat/completions", "")
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
        .header("Content-Type", "application/json");
    let response = client_request(gateway.addr, &request).expect("duplicate content type");
    // The 8003 contract does not reject duplicate Content-Type; it reaches
    // the JSON body reader and the empty body is an unsupported Chat request.
    assert_eq!(response.status, 400);
    assert_eq!(
        response.json(),
        json!({"error": {
            "message": "unsupported_chat_request",
            "type": "invalid_request_error",
            "code": "unsupported_chat_request",
            "param": null,
            "detail": null,
        }})
    );
}
