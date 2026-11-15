//! Acceptance 4: share mode only exposes the four `/v1` paths.
//!
//! Expected statuses and bodies are recorded from the real Python handler in
//! `tests/fixtures/rust_gateway_rev1.json`; every other `/v1` path must answer
//! `404 route_not_shared` even with a valid share key.

mod support;

use serde_json::{json, Value};

use support::*;

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
        assert_eq!(
            response.json(),
            case["body"],
            "body for {}",
            case["name"]
        );
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
        "type": "route_not_shared",
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

    for (target, expected_type) in [
        ("/v1/chat/completions", "unsupported_chat_request"),
        ("/v1/messages", "unsupported_messages_request"),
        ("/v1/responses", "chat2api_error"),
    ] {
        let response = client_request(
            gateway.addr,
            &ClientRequest::post(target, "").bearer(SHARE_SECRET),
        )
        .expect("allowed route");
        assert_eq!(response.status, 400, "{target}");
        assert_eq!(response.json()["error"]["type"], json!(expected_type));
    }
}
