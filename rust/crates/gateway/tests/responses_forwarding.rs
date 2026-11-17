//! S2b acceptance 1: the Responses non-stream JSON surface is translated onto
//! the chat upstream and back into the Responses object, with tools and usage.
//!
//! Python source: `chat2api/chat2api.py:7509-7756` (`_responses`), the pure
//! translations in `shared/responses_protocol.py`, and
//! `tests/test_r19_shared_responses.py:380-457` for the object shape.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, start_chat_gateway, wait_jsonl, ChatAttempt, ChatStub, ClientRequest, TempDir,
    SHARE_SECRET,
};

fn request(body: &Value) -> ClientRequest {
    ClientRequest::post("/v1/responses", &body.to_string())
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
}

fn chat_completion() -> Value {
    json!({
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "um-model",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "hello",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{\"q\": 1}"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    })
}

#[test]
fn non_stream_responses_translates_chat_json_tools_and_usage() {
    let temp = TempDir::new("um-rust-responses-json-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: chat_completion(),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let body = json!({
        "model": "responses-model",
        "input": [{"role": "user",
                   "content": [{"type": "input_text", "text": "hi"}]}],
        "tools": [{"type": "function", "name": "lookup",
                   "parameters": {"type": "object"}}],
        "reasoning": {"effort": "low"},
    });
    let response = client_request(gateway.addr, &request(&body)).expect("responses request");
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
    assert_eq!(payload["object"], json!("response"));
    assert_eq!(payload["status"], json!("completed"));
    assert_eq!(payload["model"], json!("responses-model"));
    assert!(payload["id"]
        .as_str()
        .is_some_and(|value| value.starts_with("resp_") && value.len() == 29));
    assert_eq!(payload["output"][0]["type"], json!("message"));
    assert_eq!(
        payload["output"][0]["content"][0],
        json!({"type": "output_text", "text": "hello", "annotations": []})
    );
    assert_eq!(
        payload["output"][1],
        json!({
            "type": "function_call",
            "id": payload["output"][1]["id"],
            "call_id": "call_1",
            "name": "lookup",
            "arguments": "{\"q\": 1}",
            "status": "completed",
        })
    );
    assert_eq!(
        payload["usage"],
        json!({
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        })
    );
    assert_eq!(payload["status"], json!("completed"));
    assert!(payload.get("incomplete_details").is_none());

    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/chat/completions");
    assert_eq!(
        seen[0].authorization.as_deref(),
        Some("Bearer fixture-upstream-token")
    );
    assert_eq!(
        seen[0].body["messages"],
        json!([{"role": "user", "content": "hi"}])
    );
    assert_eq!(
        seen[0].body["tools"],
        json!([{"type": "function",
                "function": {"name": "lookup",
                              "parameters": {"type": "object"}}}])
    );
    assert_eq!(seen[0].body["reasoning_effort"], json!("low"));
    assert_eq!(seen[0].body["stream"], Value::Null);

    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["protocol"], json!("responses"));
    assert_eq!(rows[0]["model"], json!("responses-model"));
    assert_eq!(rows[0]["status"], json!("ok"));
    assert_eq!(rows[0]["input_tokens"], json!(10));
    assert_eq!(rows[0]["output_tokens"], json!(5));
    assert_eq!(rows[0]["cached_input_tokens"], json!(3));
    assert_eq!(rows[0]["reasoning_tokens"], json!(2));
    assert_eq!(rows[0]["attribution"], json!("verified"));
}

#[test]
fn an_invalid_responses_body_is_refused_before_the_upstream() {
    for invalid in [
        json!({"model": "m", "input": "hi", "unknown": true}),
        json!({"input": "hi"}),
        json!({"model": "", "input": "hi"}),
        json!({"model": "m", "input": []}),
        json!({"model": "m", "input": "hi", "stream": "true"}),
        json!({"model": "m", "input": "hi", "store": true}),
        json!({"model": "m", "input": "hi", "max_output_tokens": 0}),
        json!({"model": "m", "input": "hi", "temperature": true}),
        json!({"model": "m", "input": [], "instructions": 3}),
        json!({"model": "m", "input": "hi", "tools": [{"type": "function"}]}),
        json!({"model": "m", "input": "hi", "include": [1]}),
        json!({"model": "m", "input": [{"type": "reasoning"}]}),
    ] {
        let temp = TempDir::new("um-rust-responses-invalid-");
        let stub = ChatStub::start();
        let gateway = start_chat_gateway(&stub, &temp, true, |config| {
            config.request_total = Duration::from_secs(5);
        });
        let response = client_request(gateway.addr, &request(&invalid)).expect("responses request");
        assert_eq!(response.status, 400, "body: {invalid}");
        assert_eq!(
            response.json()["error"]["message"],
            json!("unsupported_responses_request"),
            "body: {invalid}"
        );
        assert_eq!(
            stub.attempts(),
            0,
            "invalid body reached upstream: {invalid}"
        );
    }
}

#[test]
fn a_non_200_upstream_response_preserves_the_status_and_rebuilds_the_body() {
    let temp = TempDir::new("um-rust-responses-status-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 400,
        body: "{\"detail\":\"bad\"}".to_string(),
        retry_after: None,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.max_physical_attempts = 1;
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &request(&json!({"model": "responses-model", "input": "hi"})),
    )
    .expect("responses request");
    assert_eq!(response.status, 400);
    assert_eq!(
        response.json(),
        json!({"error": {
            "message": "Upstream rejected the request",
            "type": "invalid_request_error",
            "code": "upstream_request_rejected",
            "param": null,
            "detail": {
                "upstream_status": 400,
                "upstream_message": "{\"detail\":\"bad\"}",
            },
            "upstream_status": 400,
        }})
    );
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("HTTP400"));
    assert_eq!(rows[0]["protocol"], json!("responses"));
}

#[test]
fn a_malformed_upstream_body_is_unexpected_upstream_response() {
    let temp = TempDir::new("um-rust-responses-bad-json-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({"not": "a completion"}),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &request(&json!({"model": "responses-model", "input": "hi"})),
    )
    .expect("responses request");
    assert_eq!(response.status, 502);
    assert_eq!(
        response.json()["error"]["message"],
        json!("unexpected_upstream_response")
    );
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("unexpected_upstream_response"));
}

#[test]
fn an_image_tool_result_uses_the_anthropic_messages_upstream() {
    let temp = TempDir::new("um-rust-responses-image-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "um-model",
            "content": [{"type": "text", "text": "shot"}],
            "stop_reason": "end_turn",
            "stop_sequence": null,
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
    });
    let body = json!({
        "model": "responses-model",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "c1", "name": "shot",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "shot"},
                         {"type": "input_image",
                          "image_url": "data:image/png;base64,AAAA"}]},
        ],
        "tools": [{"type": "function", "name": "shot",
                    "parameters": {"type": "object"}}],
        "max_output_tokens": 64,
    });
    let response = client_request(gateway.addr, &request(&body)).expect("responses request");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    let payload = response.json();
    assert_eq!(payload["output"][0]["content"][0]["text"], json!("shot"));
    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/v1/messages");
    assert_eq!(seen[0].body["max_tokens"], json!(64));
    let tool_result = &seen[0].body["messages"][2]["content"][0];
    assert_eq!(tool_result["type"], json!("tool_result"));
    assert_eq!(tool_result["content"][1]["type"], json!("image"));
    assert_eq!(
        tool_result["content"][1]["source"],
        json!({"type": "base64", "media_type": "image/png", "data": "AAAA"})
    );
    assert_eq!(seen[0].anthropic_version.as_deref(), Some("2023-06-01"));
}

#[test]
fn an_empty_answer_keeps_a_zero_output_estimate() {
    let temp = TempDir::new("um-rust-responses-empty-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "message": {"role": "assistant",
                          "content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        }),
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.usage_log = Some(ledger.clone());
        config.request_total = Duration::from_secs(5);
    });
    let response = client_request(
        gateway.addr,
        &request(&json!({"model": "responses-model", "input": "hi"})),
    )
    .expect("responses request");
    assert_eq!(response.status, 200);
    assert_eq!(response.json()["output"], json!([]));
    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("ok"));
    assert_eq!(rows[0]["input_tokens"], json!(10));
    assert_eq!(rows[0]["output_tokens"], json!(0));
    assert_eq!(rows[0]["usage_source"], json!("partial"));
}
