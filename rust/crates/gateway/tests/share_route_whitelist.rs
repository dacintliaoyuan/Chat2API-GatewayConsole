//! Acceptance 4: share mode only exposes the four `/v1` paths.
//!
//! Expected statuses and bodies are recorded from the real Python handler in
//! `tests/fixtures/rust_gateway_rev1.json`; every other `/v1` path must answer
//! `404 route_not_shared` even with a valid share key.

mod support;

use serde_json::{json, Value};

use support::*;

fn expected_error(case: &Value) -> Value {
    let old = &case["body"]["error"];
    let code = old["code"]
        .as_str()
        .or_else(|| {
            old["type"]
                .as_str()
                .filter(|value| *value != "chat2api_error")
        })
        .or_else(|| old["message"].as_str())
        .expect("old error code");
    let category = match code {
        "route_not_shared" => "not_found_error",
        "unsupported_chat_request"
        | "unsupported_messages_request"
        | "unsupported_responses_request" => "invalid_request_error",
        other => panic!("unexpected route error code: {other}"),
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

fn send_case(gateway: &Gateway, case: &Value) -> ClientResponse {
    let method = case["method"].as_str().expect("method");
    let target = case["target"].as_str().expect("target");
    let request = if method == "GET" {
        ClientRequest::get(target)
    } else {
        ClientRequest::post(target, "")
    };
    client_request(gateway.addr, &request.bearer(SHARE_SECRET)).expect("route case")
}

#[test]
fn share_mode_route_cases_match_python() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let cases = fixture["shareGate"]["routeCases"]
        .as_array()
        .expect("route cases");
    assert!(!cases.is_empty());
    for case in cases {
        let response = send_case(&gateway, case);
        assert_eq!(
            u64::from(response.status),
            case["status"].as_u64().unwrap(),
            "status for {}",
            case["name"]
        );
        if case["status"] == json!(200) {
            assert_eq!(response.json(), case["body"], "body for {}", case["name"]);
        } else {
            assert_eq!(
                response.json(),
                expected_error(case),
                "body for {}",
                case["name"]
            );
        }
    }
}

#[test]
fn every_unlisted_v1_path_is_route_not_shared() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let expected = json!({"error": {
        "message": "route_not_shared",
        "type": "not_found_error",
        "code": "route_not_shared",
        "param": null,
        "detail": null,
    }});
    for target in [
        "/v1/version",
        "/v1/embeddings",
        "/v1/completions",
        "/v1/audio/transcriptions",
        "/v1/unknown",
        "/v1/models/extra",
    ] {
        let response = client_request(
            gateway.addr,
            &ClientRequest::get(target).bearer(SHARE_SECRET),
        )
        .expect("unlisted route");
        assert_eq!(response.status, 404, "{target}");
        assert_eq!(response.json(), expected, "{target}");
        assert_eq!(response.header("connection"), Some("close"), "{target}");
    }
    let _ = fixture;
}

#[test]
fn the_four_shared_paths_are_not_refused_by_the_route_gate() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let models = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("models");
    assert_eq!(models.status, 200);

    for (target, expected_code) in [
        ("/v1/chat/completions", "unsupported_chat_request"),
        ("/v1/messages", "unsupported_messages_request"),
        ("/v1/responses", "unsupported_responses_request"),
    ] {
        let response = client_request(
            gateway.addr,
            &ClientRequest::post(target, "").bearer(SHARE_SECRET),
        )
        .expect("allowed route");
        assert_eq!(response.status, 400, "{target}");
        let body = response.json();
        let error = if target == "/v1/messages" {
            assert_eq!(body["type"], json!("error"), "{target}");
            &body["error"]
        } else {
            &body["error"]
        };
        assert_eq!(error["type"], json!("invalid_request_error"), "{target}");
        assert_eq!(error["code"], json!(expected_code), "{target}");
        assert_eq!(error["param"], Value::Null, "{target}");
        assert_eq!(error["detail"], Value::Null, "{target}");
    }
}
