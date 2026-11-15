//! S2b acceptance 4: a `stream=false` Responses client is served from an
//! aggregated upstream SSE only when the switch is on and the exact
//! `(verified key, model, protocol)` triple is in the trusted startup scope.
//!
//! Python source: `chat2api/chat2api.py:7543-7555` (the Responses gate),
//! `nonstream_aggregate_allowed` at `chat2api/chat2api.py:10486-10530`, the
//! scope parser at `10390-10480`, and
//! `tests/test_chat2api_nonstream_aggregate.py:1152-1315`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, start_chat_gateway, wait_jsonl, ChatAttempt, ChatChunk, ChatStub,
    ClientRequest, TempDir, SHARE_SECRET,
};

fn aggregate_scope() -> um_gateway::config::AggregateScope {
    um_gateway::config::parse_nonstream_aggregate_scope(Some(
        r#"{"key_ids":["friend"],"models":["responses-model"],"protocols":["responses"]}"#,
    ))
    .expect("valid scope")
}

fn body(stream: bool) -> String {
    json!({"model": "responses-model", "input": "hi", "stream": stream}).to_string()
}

fn request(stream: bool) -> ClientRequest {
    ClientRequest::post("/v1/responses", &body(stream))
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
}

fn chat_sse() -> Vec<ChatChunk> {
    vec![
        ChatChunk::now(
            b"data: {\"id\":\"chatcmpl-1\",\"created\":1700000000,\"model\":\"um-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hi\"}}]}\n\n"
                .to_vec(),
        ),
        ChatChunk::now(
            b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\" there\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5,\"total_tokens\":15}}\n\n"
                .to_vec(),
        ),
        ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
    ]
}

#[test]
fn a_selected_triple_aggregates_the_upstream_sse_into_one_json_response() {
    let temp = TempDir::new("um-rust-responses-aggregate-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: chat_sse(),
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.nonstream_upstream_aggregate = true;
        config.nonstream_aggregate_scope = Some(aggregate_scope());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    eprintln!("gateway addr = {}", gateway.addr);
    let response = client_request(gateway.addr, &request(false)).expect("responses request");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        response.header("content-type"),
        Some("application/json; charset=utf-8")
    );
    let payload = response.json();
    assert_eq!(payload["status"], json!("completed"));
    assert_eq!(
        payload["output"][0]["content"][0]["text"],
        json!("Hi there")
    );
    assert_eq!(
        payload["usage"],
        json!({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    );

    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].body["stream"], json!(true));
    assert_eq!(
        seen[0].body["stream_options"],
        json!({"include_usage": true})
    );

    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("ok"));
    assert_eq!(rows[0]["stream"], json!(false));
    assert_eq!(rows[0]["protocol"], json!("responses"));
    assert_eq!(rows[0]["input_tokens"], json!(10));
    assert_eq!(rows[0]["output_tokens"], json!(5));
}

#[test]
fn an_unselected_model_stays_on_the_native_non_stream_transport() {
    let temp = TempDir::new("um-rust-responses-native-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "message": {"role": "assistant",
                          "content": "native"}, "finish_reason": "stop"}],
        }),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.nonstream_upstream_aggregate = true;
        config.nonstream_aggregate_scope = Some(aggregate_scope());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post(
            "/v1/responses",
            &json!({"model": "unselected-model", "input": "hi"}).to_string(),
        )
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json"),
    )
    .expect("responses request");
    assert_eq!(response.status, 200);
    assert_eq!(
        response.json()["output"][0]["content"][0]["text"],
        json!("native")
    );
    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].body["stream"], Value::Null);
}

#[test]
fn a_local_alias_has_no_verified_key_and_stays_native() {
    let temp = TempDir::new("um-rust-responses-local-native-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "message": {"role": "assistant",
                          "content": "local"}, "finish_reason": "stop"}],
        }),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.nonstream_upstream_aggregate = true;
        config.nonstream_aggregate_scope = Some(aggregate_scope());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::post(
            "/api/v1/responses",
            &json!({"model": "responses-model", "input": "hi"}).to_string(),
        )
        .header("Content-Type", "application/json"),
    )
    .expect("responses alias");
    assert_eq!(response.status, 200);
    assert_eq!(
        response.json()["output"][0]["content"][0]["text"],
        json!("local")
    );
    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].body["stream"], Value::Null);
}

#[test]
fn an_aggregate_failure_is_a_502_and_never_a_partial_success() {
    let temp = TempDir::new("um-rust-responses-aggregate-failure-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(
            b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"},\"finish_reason\":\"stop\"}]}\n\n"
                .to_vec(),
        )],
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.nonstream_upstream_aggregate = true;
        config.nonstream_aggregate_scope = Some(aggregate_scope());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let response = client_request(gateway.addr, &request(false)).expect("responses request");
    assert_eq!(response.status, 502);
    assert_eq!(
        response.json()["error"]["message"],
        json!("Upstream stream ended without a complete terminal")
    );
    assert_eq!(
        response.json()["error"]["detail"],
        json!({"reason": "upstream_missing_terminal"})
    );
    assert_eq!(
        response.json()["error"]["type"],
        json!("upstream_stream_error")
    );
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(
        rows[0]["status"],
        json!("aggregate_upstream_missing_terminal")
    );
    assert_eq!(rows[0]["stream"], json!(false));
}

#[test]
fn an_explicit_error_chunk_is_an_upstream_explicit_error() {
    let temp = TempDir::new("um-rust-responses-aggregate-explicit-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(
            b"data: {\"error\":{\"message\":\"boom\"}}\n\n".to_vec(),
        )],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.nonstream_upstream_aggregate = true;
        config.nonstream_aggregate_scope = Some(aggregate_scope());
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    });
    let response = client_request(gateway.addr, &request(false)).expect("responses request");
    assert_eq!(response.status, 502);
    assert_eq!(
        response.json()["error"]["message"],
        json!("Upstream request failed")
    );
    assert_eq!(
        response.json()["error"]["detail"]["reason"],
        json!("upstream_explicit_error")
    );
    assert_eq!(
        response.json()["error"]["detail"]["upstream_message"],
        json!("boom")
    );
    assert_eq!(
        response.json()["error"]["type"],
        json!("upstream_explicit_error")
    );
}

#[test]
fn the_scope_parser_is_strict_and_fails_closed() {
    let valid = um_gateway::config::parse_nonstream_aggregate_scope(Some(
        r#"{"key_ids":["key-a","key-b"],"models":["model-a"],"protocols":["chat","responses"]}"#,
    ))
    .expect("valid scope");
    assert_eq!(
        valid.key_ids,
        ["key-a".to_string(), "key-b".to_string()]
            .into_iter()
            .collect()
    );
    assert_eq!(valid.models, ["model-a".to_string()].into_iter().collect());
    assert_eq!(
        valid.protocols,
        ["chat".to_string(), "responses".to_string()]
            .into_iter()
            .collect()
    );
    for raw in [
        None,
        Some(""),
        Some("   "),
        Some("{"),
        Some("[]"),
        Some("\"x\""),
        Some("null"),
        Some("42"),
        Some("true"),
        Some("{}"),
    ] {
        assert!(
            um_gateway::config::parse_nonstream_aggregate_scope(raw).is_none(),
            "{raw:?}"
        );
    }
    for raw in [
        r#"{"key_ids":["k"]}"#,
        r#"{"models":["m"],"protocols":["chat"]}"#,
        r#"{"key_ids":["k"],"models":["m"]}"#,
        r#"{"key_ids":["k"],"models":["m"],"protocols":["chat"],"extra":[]}"#,
        r#"{"key_ids":"k","models":["m"],"protocols":["chat"]}"#,
        r#"{"key_ids":["k"],"models":{"m":true},"protocols":["chat"]}"#,
        r#"{"key_ids":["k"],"models":["m"],"protocols":[1]}"#,
        r#"{"key_ids":[""],"models":["m"],"protocols":["chat"]}"#,
        r#"{"key_ids":["k"],"models":["m","m"],"protocols":["chat"]}"#,
        r#"{"key_ids":["k"],"models":[" m"],"protocols":["chat"]}"#,
        r#"{"key_ids":[],"models":["m"],"protocols":["chat"]}"#,
        r#"{"key_ids":["k"],"models":[],"protocols":["chat"]}"#,
        r#"{"key_ids":["k"],"models":["m"],"protocols":[]}"#,
        r#"{"key_ids":["k"],"models":["m"],"protocols":["embeddings"]}"#,
    ] {
        assert!(
            um_gateway::config::parse_nonstream_aggregate_scope(Some(raw)).is_none(),
            "{raw}"
        );
    }
    let disabled = um_gateway::config::parse_nonstream_aggregate_scope(Some(
        r#"{"key_ids":[],"models":[],"protocols":[]}"#,
    ))
    .expect("the all-empty scope is the explicit disabled state");
    assert!(disabled.key_ids.is_empty());
    assert!(disabled.models.is_empty());
    assert!(disabled.protocols.is_empty());
}
