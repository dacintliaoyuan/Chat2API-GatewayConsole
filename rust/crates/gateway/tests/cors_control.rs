//! Acceptance 7: CORS/Origin, the control-token gate and close-on-error.
//!
//! Expected CORS and control responses are recorded from the real Python
//! handler in `tests/fixtures/rust_gateway_rev1.json`. The control gate keeps
//! the Python `is_local_control_request` + `X-Admin-Control` rules; every error
//! response sends `Connection: close` and actually closes the socket.

mod support;

use std::net::IpAddr;

use serde_json::{json, Value};

use support::*;

fn post_with_origin(target: &str, origin: &str) -> ClientRequest {
    ClientRequest::post(target, "{}")
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
        .header("Origin", origin)
}

#[test]
fn cors_options_and_origin_cases_match_python() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let allowed = client_request(
        gateway.addr,
        &ClientRequest::options("/v1/models").header("Origin", ALLOWED_ORIGIN),
    )
    .expect("options allowed");
    assert_eq!(
        u64::from(allowed.status),
        fixture["cors"]["optionsAllowed"]["status"]
            .as_u64()
            .unwrap()
    );
    assert_eq!(
        allowed.header("access-control-allow-origin"),
        fixture["cors"]["optionsAllowed"]["allowOrigin"].as_str(),
        "{:?}",
        allowed.headers
    );
    assert_eq!(
        allowed.header("access-control-allow-methods"),
        fixture["cors"]["optionsAllowed"]["allowMethods"].as_str()
    );
    assert_eq!(
        allowed.header("access-control-allow-headers"),
        fixture["cors"]["optionsAllowed"]["allowHeaders"].as_str()
    );

    let denied = client_request(
        gateway.addr,
        &ClientRequest::options("/v1/models").header("Origin", DENIED_ORIGIN),
    )
    .expect("options denied");
    assert_eq!(
        u64::from(denied.status),
        fixture["cors"]["optionsDenied"]["status"].as_u64().unwrap()
    );
    assert_eq!(
        denied.header("access-control-allow-origin"),
        fixture["cors"]["optionsDenied"]["allowOrigin"].as_str()
    );

    let origin_allowed = client_request(
        gateway.addr,
        &post_with_origin("/v1/chat/completions", ALLOWED_ORIGIN),
    )
    .expect("origin allowed");
    assert_eq!(
        u64::from(origin_allowed.status),
        fixture["cors"]["originAllowed"]["status"].as_u64().unwrap()
    );
    assert_eq!(
        origin_allowed.header("access-control-allow-origin"),
        fixture["cors"]["originAllowed"]["allowOrigin"].as_str()
    );

    let origin_denied = client_request(
        gateway.addr,
        &post_with_origin("/v1/chat/completions", DENIED_ORIGIN),
    )
    .expect("origin denied");
    assert_eq!(
        u64::from(origin_denied.status),
        fixture["cors"]["originDenied"]["status"].as_u64().unwrap()
    );
    assert_eq!(
        origin_denied.json(),
        fixture["cors"]["originDenied"]["body"]
    );
    assert_eq!(
        origin_denied.header("access-control-allow-origin"),
        fixture["cors"]["originDenied"]["allowOrigin"].as_str()
    );

    let models_allowed = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models")
            .bearer(SHARE_SECRET)
            .header("Origin", ALLOWED_ORIGIN),
    )
    .expect("models allowed origin");
    assert_eq!(models_allowed.status, 200);
    assert_eq!(
        models_allowed.header("access-control-allow-origin"),
        Some(ALLOWED_ORIGIN)
    );

    let models_denied = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models")
            .bearer(SHARE_SECRET)
            .header("Origin", DENIED_ORIGIN),
    )
    .expect("models denied origin");
    assert_eq!(models_denied.status, 200);
    assert_eq!(models_denied.header("access-control-allow-origin"), None);
}

fn control_request(name: &str, _case: &Value) -> ClientRequest {
    let mut request = ClientRequest::post("/admin/share/revoke", "");
    request.body = None;
    match name {
        "missing" => {}
        "wrong-token" => {
            request = request.header("X-Admin-Control", "wrong").json_body("{}");
        }
        "origin-present" => {
            request = request
                .header("X-Admin-Control", CONTROL_TOKEN)
                .header("Origin", ALLOWED_ORIGIN)
                .json_body("{}");
        }
        "host-foreign" => {
            request = request
                .header("X-Admin-Control", CONTROL_TOKEN)
                .header("Host", "example.com")
                .json_body("{}");
        }
        "valid" => {
            request = request
                .header("X-Admin-Control", CONTROL_TOKEN)
                .json_body(r#"{"key_id":"friend"}"#);
        }
        other => panic!("unhandled control case: {other}"),
    }
    request
}

#[test]
fn control_token_gate_cases_match_python() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let cases = fixture["control"].as_object().expect("control cases");
    assert!(!cases.is_empty());
    for (name, case) in cases {
        let response = client_request(gateway.addr, &control_request(name, case))
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
fn a_missing_control_token_file_is_created_on_first_use() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let token_path = temp.path().join("control.token");
    assert!(!token_path.exists());
    let config = um_gateway::config::GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        share_config: Some(temp.write("share.json", &legacy_share_config())),
        control_token_file: Some(token_path.clone()),
        ..um_gateway::config::GatewayConfig::default()
    };
    let server = um_gateway::GatewayServer::start(config).expect("gateway creates token");
    let addr = server.addr();
    let created = std::fs::read_to_string(&token_path).expect("created control token");
    assert!(!created.trim().is_empty());
    let accepted = client_request(
        addr,
        &ClientRequest::post("/admin/share/revoke", "")
            .header("X-Admin-Control", created.trim())
            .json_body(r#"{"key_id":"friend"}"#),
    )
    .expect("control with created token");
    assert_eq!(accepted.status, 200);

    let refused = client_request(
        addr,
        &ClientRequest::post("/admin/share/revoke", "")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(r#"{"key_id":"friend"}"#),
    )
    .expect("control with wrong token");
    assert_eq!(refused.status, 403);
    assert_eq!(
        refused.json()["error"]["message"],
        json!("Local management token missing or invalid")
    );
}

#[test]
fn control_gate_rejects_non_loopback_peers() {
    assert!(!um_gateway::server::is_local_control_request(
        IpAddr::from([192, 0, 2, 1]),
        "127.0.0.1:8001",
        None,
    ));
    assert!(um_gateway::server::is_local_control_request(
        IpAddr::from([127, 0, 0, 1]),
        "127.0.0.1:8001",
        None,
    ));
    assert!(!um_gateway::server::is_local_control_request(
        IpAddr::from([127, 0, 0, 1]),
        "example.com:8001",
        None,
    ));
    assert!(!um_gateway::server::is_local_control_request(
        IpAddr::from([127, 0, 0, 1]),
        "127.0.0.1:8001",
        Some(ALLOWED_ORIGIN),
    ));
}

#[test]
fn every_error_response_actively_closes_the_connection() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let (missing, closed) = client_request_and_check_close(
        gateway.addr,
        &ClientRequest::post("/admin/share/revoke", "").json_body("{}"),
    )
    .expect("missing token");
    assert_eq!(
        u64::from(missing.status),
        fixture["closeOnError"]["status"].as_u64().unwrap()
    );
    assert_eq!(
        missing.header("connection"),
        fixture["closeOnError"]["connection"].as_str()
    );
    assert!(closed, "an error response must close the socket");

    let (denied, closed) = client_request_and_check_close(
        gateway.addr,
        &post_with_origin("/v1/chat/completions", DENIED_ORIGIN),
    )
    .expect("origin denied");
    assert_eq!(denied.status, 403);
    assert_eq!(denied.header("connection"), Some("close"));
    assert!(closed, "an origin error must close the socket");
}
