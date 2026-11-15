//! S2a acceptance 2: the shared Messages body is shape-checked against the
//! Python `MESSAGES_FIELDS` whitelist, `max_tokens` is a required positive
//! integer, `stream` is optional but must be a bool, and unknown fields are
//! refused without reaching the upstream.
//!
//! Python source: `shared/responses_protocol.py:14-17` (`MESSAGES_FIELDS`) and
//! `chat2api/share_auth.py:1159-1190` (`valid_messages_request`). The wire
//! field list is frozen in `tests/fixtures/rust_contract_rev1.json`
//! (`shareAuth.messagesFields`).

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{client_request, start_chat_gateway, ChatStub, ClientRequest, TempDir, SHARE_SECRET};

fn valid_body() -> Value {
    json!({
        "model": "claude-x",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "stream": false,
    })
}

fn request(body: &Value) -> ClientRequest {
    ClientRequest::post("/v1/messages", &body.to_string())
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
}

fn assert_rejected(body: Value) {
    let temp = TempDir::new("um-rust-messages-validation-");
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(gateway.addr, &request(&body)).expect("messages request");
    assert_eq!(response.status, 400, "body: {body}");
    assert_eq!(
        response.json()["error"]["type"],
        json!("unsupported_messages_request"),
        "body: {body}"
    );
    assert_eq!(
        stub.attempts(),
        0,
        "an invalid Messages body must not reach the upstream: {body}"
    );
}

#[test]
fn unknown_fields_are_rejected_without_an_upstream_request() {
    let mut body = valid_body();
    body["unknown"] = json!(1);
    assert_rejected(body);
}

#[test]
fn max_tokens_is_required_and_must_be_a_positive_integer() {
    let mut missing = valid_body();
    missing
        .as_object_mut()
        .expect("body object")
        .remove("max_tokens");
    assert_rejected(missing);
    for invalid in [json!(0), json!(-1), json!(true), json!("16"), json!(1.5)] {
        let mut body = valid_body();
        body["max_tokens"] = invalid.clone();
        assert_rejected(body);
    }
}

#[test]
fn stream_is_optional_but_must_be_a_bool_when_present() {
    for invalid in [json!("true"), json!(1), json!(null), json!([])] {
        let mut body = valid_body();
        body["stream"] = invalid.clone();
        assert_rejected(body);
    }
}

#[test]
fn model_and_messages_keep_the_python_shape_rules() {
    let mut missing_model = valid_body();
    missing_model
        .as_object_mut()
        .expect("body object")
        .remove("model");
    assert_rejected(missing_model);

    for invalid in [json!(""), json!(1), json!(null)] {
        let mut body = valid_body();
        body["model"] = invalid.clone();
        assert_rejected(body);
    }

    let mut missing_messages = valid_body();
    missing_messages
        .as_object_mut()
        .expect("body object")
        .remove("messages");
    assert_rejected(missing_messages);

    for invalid in [json!([]), json!("hi"), json!({}), json!(null)] {
        let mut body = valid_body();
        body["messages"] = invalid.clone();
        assert_rejected(body);
    }
}

#[test]
fn every_whitelisted_field_is_accepted() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    let mut actual = um_gateway::messages::MESSAGES_FIELDS.to_vec();
    actual.sort();
    assert_eq!(actual, fixture.share_auth.messages_fields);

    let mut body = valid_body();
    for field in &fixture.share_auth.messages_fields {
        if body.get(field).is_some() {
            continue;
        }
        body[field] = match field.as_str() {
            "system" => json!("be brief"),
            "temperature" => json!(0.2),
            "top_p" => json!(0.9),
            "top_k" => json!(5),
            "stop_sequences" => json!(["stop"]),
            "metadata" => json!({"user_id": "fixture"}),
            "tools" => json!([{"name": "tool", "input_schema": {"type": "object"}}]),
            "tool_choice" => json!({"type": "auto"}),
            "thinking" => json!({"type": "enabled", "budget_tokens": 1024}),
            other => panic!("fixture field without a test value: {other}"),
        };
    }
    assert!(
        um_gateway::messages::valid_messages_request(body.as_object().expect("body object")),
        "every frozen MESSAGES_FIELDS entry must be accepted"
    );
}
