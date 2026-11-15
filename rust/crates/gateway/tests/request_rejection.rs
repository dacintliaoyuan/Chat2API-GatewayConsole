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
            request = request.json_body(
                r#"{"model":"m","max_tokens":1,"messages":[],"unknown":1}"#,
            );
        }
        "unknown-responses-field" => {
            request = request.json_body(
                r#"{"model":"m","input":"hi","unknown":1}"#,
            );
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

fn console_request(name: &str, _case: &Value) -> ClientRequest {
    let mut request = ClientRequest::post("/admin/share/revoke", "")
        .header("X-Admin-Control", CONTROL_TOKEN);
    request.body = None;
    match name {
        "invalid-content-length" => {
            request = request.header("Content-Length", "not-a-number");
        }
        "duplicate-content-length" => {
            request = request
                .header("Content-Length", "2")
                .header("Content-Length", "2");
            request.body = Some(b"{}".to_vec());
        }
        "transfer-encoding" => {
            request = request
                .header("Transfer-Encoding", "chunked")
                .header("Content-Type", "application/json");
            request.body = Some(b"{}".to_vec());
        }
        "content-encoding" => {
            request = request
                .header("Content-Encoding", "gzip")
                .header("Content-Type", "application/json");
            request.body = Some(b"{}".to_vec());
        }
        "content-type-missing" => {
            request.body = Some(b"{}".to_vec());
        }
        "content-type-wrong" => {
            request = request.header("Content-Type", "text/plain");
            request.body = Some(b"{}".to_vec());
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
        "request-too-large" => {
            request = request
                .header("Content-Length", "65537")
                .header("Content-Type", "application/json");
            request.body = Some(vec![b'x'; 65537]);
        }
        "unknown-field" => {
            request = request.json_body(r#"{"key_id":"friend","unknown":1}"#);
        }
        "duplicate-sensitive-header" => {
            request = request
                .header("Content-Type", "application/json")
                .header("Content-Type", "application/json");
            request.body = Some(b"{}".to_vec());
        }
        other => panic!("unhandled console body case: {other}"),
    }
    request
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
        assert_eq!(response.json(), case["body"], "body for {name}");
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
    let response = client_request(addr, &share_request("payload-too-large", case)).expect("oversize");
    assert_eq!(response.status, 413);
    assert_eq!(response.json(), case["body"]);
}

#[test]
fn console_body_cases_match_python() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let cases = fixture["bodyRejection"]["console"]
        .as_object()
        .expect("console body cases");
    assert!(!cases.is_empty());
    for (name, case) in cases {
        let response = client_request(gateway.addr, &console_request(name, case))
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        assert_eq!(
            u64::from(response.status),
            case["status"].as_u64().unwrap(),
            "status for {name}"
        );
        let body = response.json();
        if name == "duplicate-sensitive-header" {
            assert_eq!(
                body["error"]["message"],
                case["message"],
                "message for {name}"
            );
        } else if name == "duplicate-content-length" {
            assert_eq!(
                body["error"]["message"],
                json!("duplicate header: Content-Length"),
                "message for {name}"
            );
        } else if name == "unknown-field" {
            assert_eq!(
                body["error"]["message"],
                case["body"]["error"],
                "message for {name}"
            );
            assert_eq!(body["error"]["type"], json!("invalid_request"));
        } else {
            assert_eq!(
                body["error"]["message"],
                case["code"],
                "code for {name}"
            );
        }
        assert_eq!(response.header("connection"), Some("close"), "{name}");
    }
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
    assert_eq!(share.json()["error"]["type"], json!("payload_too_large"));

    let console = client_request(
        gateway.addr,
        &ClientRequest::post("/admin/share/revoke", "")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .header("Content-Length", "-1"),
    )
    .expect("negative console length");
    assert_eq!(console.status, 413);
    assert_eq!(
        console.json()["error"]["message"],
        json!("request_too_large")
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
            "type": "unsupported_chat_request",
            "detail": null,
        }})
    );
}
