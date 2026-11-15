//! Acceptance 2: share-mode Bearer/X-Api-Key resolution fails closed.
//!
//! Every expected status and body is recorded from the real Python handler in
//! `tests/fixtures/rust_gateway_rev1.json`; the Rust test only reads it.

mod support;

use serde_json::{json, Value};

use support::*;

fn request_case(
    gateway: &Gateway,
    case: &Value,
) -> ClientResponse {
    let method = case["method"].as_str().expect("method");
    let target = case["target"].as_str().expect("target");
    let mut request = if method == "GET" {
        ClientRequest::get(target)
    } else {
        ClientRequest::post(target, "")
    };
    request.body = None;
    match case["name"].as_str().expect("name") {
        "bearer" => {
            request = request.bearer(SHARE_SECRET);
        }
        "duplicate-authorization" => {
            request = request
                .header("Authorization", &format!("Bearer {SHARE_SECRET}"))
                .header("Authorization", &format!("Bearer {SHARE_SECRET}"));
        }
        "empty-authorization" => {
            request = request.header("Authorization", "Bearer ");
        }
        "overlong-authorization" => {
            request = request.header(
                "Authorization",
                &format!("Bearer {}", "x".repeat(257)),
            );
        }
        "wrong-prefix" => {
            request = request.header("Authorization", &format!("Token {SHARE_SECRET}"));
        }
        "different-values" => {
            request = request
                .header("Authorization", &format!("Bearer {SHARE_SECRET}"))
                .header("X-Api-Key", SECOND_SECRET);
        }
        "same-values" => {
            request = request
                .header("Authorization", &format!("Bearer {SHARE_SECRET}"))
                .header("X-Api-Key", SHARE_SECRET);
        }
        "api-key-only-messages" => {
            request = request.header("X-Api-Key", SHARE_SECRET);
        }
        "api-key-only-chat" => {
            request = request.header("X-Api-Key", SHARE_SECRET);
        }
        "duplicate-api-key" => {
            request = request
                .header("X-Api-Key", SHARE_SECRET)
                .header("X-Api-Key", SHARE_SECRET);
        }
        "unknown-key" => {
            request = request.bearer("wrong");
        }
        other => panic!("unhandled credential case: {other}"),
    }
    client_request(gateway.addr, &request).expect("credential request")
}

#[test]
fn every_credential_shape_matches_the_python_gate() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let cases = fixture["shareGate"]["credentialCases"]
        .as_array()
        .expect("credential cases");
    assert!(!cases.is_empty());
    for case in cases {
        let response = request_case(&gateway, case);
        assert_eq!(
            u64::from(response.status),
            case["status"].as_u64().unwrap(),
            "status for {}",
            case["name"]
        );
        assert_eq!(response.json(), case["body"], "body for {}", case["name"]);
    }
}

#[test]
fn duplicate_and_overlong_credentials_never_reach_the_digest_lookup() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let cases = fixture["shareGate"]["credentialCases"]
        .as_array()
        .expect("credential cases");
    for name in [
        "duplicate-authorization",
        "empty-authorization",
        "overlong-authorization",
        "wrong-prefix",
        "different-values",
        "api-key-only-chat",
        "duplicate-api-key",
    ] {
        let case = cases
            .iter()
            .find(|case| case["name"] == json!(name))
            .unwrap_or_else(|| panic!("missing fixture case {name}"));
        let response = request_case(&gateway, case);
        assert_eq!(
            response.json()["error"]["code"],
            json!("invalid_api_key"),
            "{name}"
        );
        assert_eq!(response.json()["error"]["type"], json!("share_gateway_error"));
        assert_eq!(response.header("connection"), Some("close"));
    }
}

#[test]
fn projection_config_rechecks_generation_and_secret_version() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_projection_gateway(&stub, &temp);
    let models = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("projection models");
    assert_eq!(models.status, 200);
    assert_eq!(models.json()["data"].as_array().unwrap().len(), 2);
}

#[test]
fn projection_config_is_not_downgraded_to_legacy() {
    let document = json!({
        "schema_version": 2,
        "config_generation": 1,
        "data": [{
            "key_id": "friend", "name": "friend",
            "secret_hash": sha256_hex(SHARE_SECRET),
            "secret_version": 1, "enabled": true,
        }],
    })
    .to_string();
    let config = um_gateway::config::parse_share_config(
        &document,
        um_gateway::config::AuthMode::Projection,
    )
    .expect("projection parses");
    assert_eq!(config.config_generation, 1);
    assert_eq!(config.entries[0].max_concurrency, 1);
    assert_eq!(config.share_max_concurrency, 2);

    let error = um_gateway::config::parse_share_config(
        &legacy_share_config(),
        um_gateway::config::AuthMode::Projection,
    )
    .expect_err("legacy config must fail in projection mode");
    assert!(error.to_string().contains("projection mode"));
}
