//! Acceptance 1: the 8003 read-only surface keeps the Python shapes.

mod support;

use serde_json::{json, Value};
use um_gateway::config::GatewayConfig;

use std::net::TcpListener;

use support::*;

fn get(addr: std::net::SocketAddr, target: &str) -> ClientResponse {
    client_request(addr, &ClientRequest::get(target)).expect("request")
}

#[test]
fn health_ready_version_and_models_match_the_python_shapes() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let health = get(gateway.addr, "/health");
    assert_eq!(
        u64::from(health.status),
        fixture["readOnly"]["health"]["status"].as_u64().unwrap()
    );
    assert_eq!(
        shape(&health.json()),
        fixture["readOnly"]["health"]["shape"]
    );

    let ready = get(gateway.addr, "/ready");
    assert_eq!(
        u64::from(ready.status),
        fixture["readOnly"]["readyValid"]["status"]
            .as_u64()
            .unwrap()
    );
    assert_eq!(
        shape(&ready.json()),
        fixture["readOnly"]["readyValid"]["shape"]
    );

    let version_share = get(gateway.addr, "/v1/version");
    assert_eq!(
        u64::from(version_share.status),
        fixture["readOnly"]["versionShare"]["status"]
            .as_u64()
            .unwrap()
    );
    assert_eq!(
        version_share.json(),
        json!({"error": {
            "message": fixture["readOnly"]["versionShare"]["body"]["error"]["message"],
            "type": "not_found_error",
            "code": "route_not_shared",
            "param": null,
            "detail": null,
        }})
    );

    let models = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("models");
    assert_eq!(
        u64::from(models.status),
        fixture["readOnly"]["models"]["status"].as_u64().unwrap()
    );
    assert_eq!(models.json(), fixture["readOnly"]["models"]["body"]);

    let raw_fresh = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models")
            .bearer(SHARE_SECRET)
            .header("X-Chat2Api-Models", "raw-fresh"),
    )
    .expect("raw fresh models");
    assert_eq!(
        u64::from(raw_fresh.status),
        fixture["readOnly"]["modelsRawFresh"]["status"]
            .as_u64()
            .unwrap()
    );
    assert_eq!(
        raw_fresh.json(),
        fixture["readOnly"]["modelsRawFresh"]["body"]
    );
    assert_eq!(
        raw_fresh.header("x-chat2api-raw-models"),
        fixture["readOnly"]["modelsRawFresh"]["marker"].as_str()
    );

    let unauthenticated = get(gateway.addr, "/v1/models");
    assert_eq!(unauthenticated.status, 401);
    assert_eq!(
        unauthenticated.json(),
        json!({"error": {
            "message": "invalid_api_key",
            "type": "authentication_error",
            "detail": null,
            "code": "invalid_api_key",
            "param": null,
        }})
    );
}

#[test]
fn version_success_requires_non_share_mode() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, false);

    let version = get(gateway.addr, "/v1/version");
    assert_eq!(
        u64::from(version.status),
        fixture["readOnly"]["version"]["status"].as_u64().unwrap()
    );
    assert_eq!(
        shape(&version.json()),
        fixture["readOnly"]["version"]["shape"]
    );
}

#[test]
fn upstream_version_failure_is_a_502() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let closed = TcpListener::bind(("127.0.0.1", 0)).expect("closed port");
    let closed_addr = closed.local_addr().expect("closed addr");
    drop(closed);
    let mut config = GatewayConfig {
        upstream_base_url: format!("http://{closed_addr}"),
        upstream_token: STUB_TOKEN.to_string(),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        ..GatewayConfig::default()
    };
    config.candidate = true;
    let gateway = um_gateway::GatewayServer::start(config).expect("start gateway");

    let version = get(gateway.addr(), "/v1/version");
    assert_eq!(version.status, 502);
    assert_eq!(
        body_field(&version.json(), "message"),
        &json!("Failed to fetch upstream version")
    );
    assert_eq!(
        body_field(&version.json(), "type"),
        &json!("upstream_error")
    );
    assert_eq!(
        body_field(&version.json(), "code"),
        &json!("ConnectionError")
    );
    assert_eq!(body_field(&version.json(), "param"), &Value::Null);
    let _ = stub;
}

#[test]
fn ready_is_503_when_the_credential_is_rejected() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, false);
    // The candidate was built with the fixture token; the stub rejects it.
    let mut config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: "rejected-token".to_string(),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        ..GatewayConfig::default()
    };
    config.candidate = true;
    let rejected = um_gateway::GatewayServer::start(config).expect("start rejected gateway");

    let ready = get(rejected.addr(), "/ready");
    assert_eq!(ready.status, 503);
    assert_eq!(ready.json()["status"], json!("not_ready"));
    assert_eq!(ready.json()["authentication"], json!("rejected"));
    assert_eq!(ready.json()["reason"], json!("upstream_rejected"));
    assert_eq!(ready.json()["upstream_status"], json!(401));
    let _ = gateway;
}

#[test]
fn models_do_not_leak_the_upstream_bearer() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let _ = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("models");

    let seen = stub.seen();
    assert!(seen.iter().any(|request| request.path == "/api/config"));
    let models = seen
        .iter()
        .find(|request| request.path == "/api/models")
        .expect("models request");
    assert_eq!(
        models.authorization.as_deref(),
        Some(format!("Bearer {STUB_TOKEN}").as_str())
    );
}

#[test]
fn candidate_config_refuses_production_ports_before_bind() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        bind_port: 8003,
        ..GatewayConfig::default()
    };
    let error = um_gateway::GatewayServer::start(config).expect_err("8003 must fail");
    assert!(error.to_string().contains("8003"));

    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        bind_host: "0.0.0.0".to_string(),
        ..GatewayConfig::default()
    };
    let error = um_gateway::GatewayServer::start(config).expect_err("non-loopback must fail");
    assert!(error.to_string().contains("loopback"));
    let _ = temp;
}

#[test]
fn options_response_matches_python() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let response = client_request(
        gateway.addr,
        &ClientRequest::options("/v1/models").header("Origin", ALLOWED_ORIGIN),
    )
    .expect("options");
    assert_eq!(response.status, 204);
    assert_eq!(
        response.header("access-control-allow-origin"),
        Some(ALLOWED_ORIGIN)
    );
    assert_eq!(
        response.header("access-control-allow-methods"),
        Some("GET, POST, OPTIONS")
    );
    assert_eq!(
        response.header("access-control-allow-headers"),
        Some("Content-Type, Authorization, X-Api-Key, Anthropic-Version, Anthropic-Beta")
    );
}

#[test]
fn error_response_always_carries_the_unified_shape() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/embeddings", "{}").bearer(SHARE_SECRET),
    )
    .expect("embeddings");
    let body = response.json();
    assert_eq!(response.status, 404);
    assert_eq!(body_field(&body, "message"), &json!("route_not_shared"));
    assert_eq!(body_field(&body, "type"), &json!("not_found_error"));
    assert_eq!(body_field(&body, "code"), &json!("route_not_shared"));
    assert_eq!(body_field(&body, "param"), &Value::Null);
    assert_eq!(body_field(&body, "detail"), &Value::Null);
    assert_eq!(response.header("connection"), Some("close"));
}
