//! Acceptance 3: per-key concurrency, global concurrency and 20/min rate limit.
//!
//! The expected 429 reasons and details are recorded from the real Python
//! `ShareGate` in `tests/fixtures/rust_gateway_rev1.json`.

mod support;

use std::sync::Arc;
use std::thread;
use std::time::Duration;

use serde_json::{json, Value};

use um_gateway::config::AuthMode;
use um_gateway::gate::{GateState, RefusalReason, ShareGate};

use support::*;

fn reason_case<'a>(fixture: &'a Value, name: &str) -> &'a Value {
    fixture["shareGate"]["gateReasons"]
        .as_array()
        .expect("gate reasons")
        .iter()
        .find(|case| case["name"] == json!(name))
        .unwrap_or_else(|| panic!("missing gate reason {name}"))
}

fn assert_429(response: &ClientResponse, fixture: &Value, name: &str) {
    let expected = reason_case(fixture, name);
    assert_eq!(response.status, 429, "{name}");
    let body = response.json();
    assert_eq!(body["error"]["message"], json!("sharing_limit_reached"));
    assert_eq!(body["error"]["type"], json!("rate_limit_error"));
    assert_eq!(body["error"]["code"], json!("sharing_limit_reached"));
    assert_eq!(body["error"]["param"], Value::Null);
    assert_eq!(body["error"]["reason"], expected["reason"], "{name}");
    assert_eq!(body["error"]["detail"], expected["detail"], "{name}");
    assert_eq!(response.header("connection"), Some("close"));
}

#[test]
fn per_key_concurrency_is_refused_with_its_python_reason() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        delay: Duration::from_millis(400),
        ..StubBehaviour::default()
    });
    let gateway = start_projection_gateway(&stub, &temp);

    let first_addr = gateway.addr;
    let first = thread::spawn(move || {
        client_request(
            first_addr,
            &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
        )
    });
    wait_until(Duration::from_secs(5), || {
        stub.seen()
            .iter()
            .any(|request| request.path == "/api/models")
    });
    let second = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("second request");
    assert_429(&second, &fixture, "per_key_concurrency");
    let first = first
        .join()
        .expect("first request")
        .expect("first response");
    assert_eq!(
        first.status,
        200,
        "{}",
        String::from_utf8_lossy(&first.body)
    );
}

#[test]
fn global_share_concurrency_is_refused_with_its_python_reason() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        delay: Duration::from_millis(500),
        ..StubBehaviour::default()
    });
    let gateway = start_gateway(&stub, &temp, true);

    let first_addr = gateway.addr;
    let second_addr = gateway.addr;
    let first = thread::spawn(move || {
        client_request(
            first_addr,
            &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
        )
    });
    let second = thread::spawn(move || {
        client_request(
            second_addr,
            &ClientRequest::get("/v1/models").bearer(SECOND_SECRET),
        )
    });
    wait_until(Duration::from_secs(5), || {
        stub.seen()
            .iter()
            .filter(|request| request.path == "/api/models")
            .count()
            >= 2
    });
    let third = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(THIRD_SECRET),
    )
    .expect("third request");
    assert_429(&third, &fixture, "share_concurrency");
    assert_eq!(first.join().unwrap().unwrap().status, 200);
    assert_eq!(second.join().unwrap().unwrap().status, 200);
}

#[test]
fn the_twenty_first_request_in_a_minute_hits_the_key_rate_limit() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    for index in 0..20 {
        let response = client_request(
            gateway.addr,
            &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
        )
        .expect("rate request");
        assert_eq!(response.status, 200, "request {index}");
    }
    let refused = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("rate limited request");
    assert_429(&refused, &fixture, "key_rate_limit");
}

#[test]
fn state_not_accepting_is_the_fourth_reason() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let share = temp.write("share.json", &legacy_share_config());
    let gate = Arc::new(ShareGate::new(&share, AuthMode::Auto).expect("gate"));
    let mut reason = RefusalReason::default();
    assert!(gate
        .acquire("friend", None, None, Some(&mut reason))
        .expect("initial acquire"));
    gate.set_state(GateState::Draining);
    let mut refused = RefusalReason::default();
    assert!(!gate
        .acquire("friend", None, None, Some(&mut refused))
        .expect("refused acquire"));
    let expected = reason_case(&fixture, "state_not_accepting");
    assert_eq!(refused.reason.as_deref(), expected["reason"].as_str());
    assert_eq!(refused.detail.as_deref(), expected["detail"].as_str());
}

#[test]
fn a_corrupt_share_config_is_a_503_not_an_invalid_key() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    std::fs::write(temp.path().join("share.json"), "{not json").expect("corrupt share config");
    let response = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("corrupt share config request");
    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(
        body["error"]["type"],
        json!("api_error"),
        "configuration corruption must not be folded into invalid_api_key"
    );
    assert_eq!(body["error"]["code"], json!("share_config_unavailable"));
    assert_eq!(
        body["error"]["detail"],
        json!("Invalid sharing configuration")
    );
}

#[test]
fn the_gate_refuses_four_distinct_reasons() {
    let fixture = fixture();
    let reasons = fixture["shareGate"]["gateReasons"]
        .as_array()
        .expect("gate reasons");
    let names: Vec<&str> = reasons
        .iter()
        .map(|case| case["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        [
            "per_key_concurrency",
            "state_not_accepting",
            "share_concurrency",
            "key_rate_limit",
        ]
    );
    for case in reasons {
        assert!(
            case["reason"].as_str().is_some(),
            "reason for {}",
            case["name"]
        );
        assert!(
            case["detail"].as_str().is_some(),
            "detail for {}",
            case["name"]
        );
    }
}
