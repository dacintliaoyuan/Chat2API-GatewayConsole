//! S2b acceptance 3: a tool call whose upstream dies mid-arguments is never
//! presented as a completed call; the announced name and the argument fragment
//! reach the client verbatim and the stream ends with `response.failed`.
//!
//! Python source: `shared/responses_protocol.py::responses_stream_events`
//! (`upstream_eof_without_done`) and the Chat commit/truncation contract locked
//! by `tests/test_chat2api_stream.py:1652-1738`.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    start_chat_gateway, wait_jsonl, ChatAttempt, ChatChunk, ChatStub, ClientRequest,
    StreamingClient, TempDir, SHARE_SECRET,
};

fn request() -> ClientRequest {
    ClientRequest::post(
        "/v1/responses",
        &json!({
            "model": "responses-model",
            "input": "hi",
            "stream": true,
            "tools": [{"type": "function", "name": "Bash",
                        "parameters": {"type": "object"}}],
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn chat_chunk(payload: Value) -> ChatChunk {
    ChatChunk::now(format!("data: {payload}\n\n").into_bytes())
}

fn event_payloads(body: &[u8]) -> Vec<(String, Value)> {
    let text = String::from_utf8_lossy(body);
    text.split("\n\n")
        .filter_map(|chunk| {
            let name = chunk
                .lines()
                .find_map(|line| line.strip_prefix("event: "))?
                .to_string();
            let data = chunk
                .lines()
                .filter(|line| line.starts_with("data:"))
                .map(|line| line.strip_prefix("data:").unwrap_or(line))
                .map(str::trim_start)
                .collect::<Vec<_>>()
                .join("\n");
            Some((name, serde_json::from_str::<Value>(&data).ok()?))
        })
        .collect()
}

fn read_stream(gateway: &support::Gateway) -> Vec<(String, Value)> {
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("streaming request");
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    event_payloads(&raw)
}

fn gateway(stub: &ChatStub, temp: &TempDir, ledger: &std::path::Path) -> support::Gateway {
    let ledger = ledger.to_path_buf();
    start_chat_gateway(stub, temp, true, move |config| {
        config.usage_log = Some(ledger);
        config.request_total = Duration::from_secs(5);
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
    })
}

#[test]
fn a_truncated_tool_call_is_forwarded_verbatim_and_never_completed() {
    let temp = TempDir::new("um-rust-responses-tool-truncated-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            chat_chunk(json!({"choices": [{"index": 0,
                "delta": {"tool_calls": [{"index": 0,
                    "id": "call_1",
                    "function": {"name": "Bash", "arguments": ""}}]},
                "finish_reason": null}]})),
            chat_chunk(json!({"choices": [{"index": 0,
                "delta": {"tool_calls": [{"index": 0,
                    "function": {"arguments": "{\"command\": \"echo tr"}}]},
                "finish_reason": null}]})),
        ],
        hold: Duration::ZERO,
    });
    let ledger = temp.path().join("usage.jsonl");
    let gateway = gateway(&stub, &temp, &ledger);
    let events = read_stream(&gateway);

    let deltas: Vec<(String, Value)> = events
        .iter()
        .filter(|(name, _)| name == "response.function_call_arguments.delta")
        .map(|(_, payload)| {
            (
                payload["item_id"].as_str().unwrap_or_default().to_string(),
                payload["delta"].clone(),
            )
        })
        .collect();
    assert_eq!(deltas.len(), 1);
    assert_eq!(deltas[0].1, json!("{\"command\": \"echo tr"));
    assert!(!events
        .iter()
        .any(|(name, payload)| name == "response.output_item.done"
            && payload["item"]["type"] == json!("function_call")));
    assert!(!events.iter().any(|(name, _)| name == "response.completed"));
    let last = events.last().expect("a terminal event");
    assert_eq!(last.0, "response.failed");
    assert_eq!(
        last.1["response"]["error"]["code"],
        json!("upstream_eof_without_done")
    );

    let rows = wait_jsonl(&ledger, 1, Duration::from_secs(2));
    assert_eq!(rows[0]["status"], json!("upstream_eof_without_done"));
    assert_eq!(rows[0]["output_tokens"], json!(5));
}

#[test]
fn an_unnamed_argument_fragment_is_held_until_the_name_arrives() {
    let temp = TempDir::new("um-rust-responses-tool-late-name-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            chat_chunk(json!({"choices": [{"index": 0,
                "delta": {"tool_calls": [{"index": 0,
                    "function": {"arguments": "{\"q\":"}}]},
                "finish_reason": null}]})),
            chat_chunk(json!({"choices": [{"index": 0,
                "delta": {"tool_calls": [{"index": 0,
                    "id": "call_1",
                    "function": {"name": "lookup", "arguments": "1}"}}]},
                "finish_reason": "tool_calls"}]})),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, &temp.path().join("usage.jsonl"));
    let events = read_stream(&gateway);

    let added: Vec<&Value> = events
        .iter()
        .filter(|(name, _)| name == "response.output_item.added")
        .map(|(_, payload)| payload)
        .collect();
    assert_eq!(added.len(), 1);
    assert_eq!(added[0]["item"]["name"], json!("lookup"));
    assert_eq!(added[0]["item"]["arguments"], json!(""));

    let deltas: Vec<&Value> = events
        .iter()
        .filter(|(name, _)| name == "response.function_call_arguments.delta")
        .map(|(_, payload)| payload)
        .collect();
    assert_eq!(deltas.len(), 1);
    assert_eq!(deltas[0]["delta"], json!("{\"q\":1}"));

    let done: Vec<&Value> = events
        .iter()
        .filter(|(name, _)| name == "response.output_item.done")
        .map(|(_, payload)| payload)
        .collect();
    assert_eq!(done.len(), 1);
    assert_eq!(done[0]["item"]["arguments"], json!("{\"q\":1}"));
    assert_eq!(done[0]["item"]["status"], json!("completed"));
    assert_eq!(events.last().expect("terminal").0, "response.completed");
}

#[test]
fn a_complete_tool_call_stream_is_completed_with_its_arguments() {
    let temp = TempDir::new("um-rust-responses-tool-complete-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            chat_chunk(json!({"choices": [{"index": 0,
                "delta": {"tool_calls": [{"index": 0, "id": "call_1",
                    "function": {"name": "Bash", "arguments": ""}}]},
                "finish_reason": null}]})),
            chat_chunk(json!({"choices": [{"index": 0,
                "delta": {"tool_calls": [{"index": 0,
                    "function": {"arguments": "{\"command\": \"echo tool-ok\"}"}}]},
                "finish_reason": "tool_calls"}]})),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = gateway(&stub, &temp, &temp.path().join("usage.jsonl"));
    let events = read_stream(&gateway);

    let done: Vec<&Value> = events
        .iter()
        .filter(|(name, _)| name == "response.output_item.done")
        .map(|(_, payload)| payload)
        .collect();
    assert_eq!(done.len(), 1);
    assert_eq!(
        done[0]["item"]["arguments"],
        json!("{\"command\": \"echo tool-ok\"}")
    );
    let last = events.last().expect("terminal");
    assert_eq!(last.0, "response.completed");
    assert!(!events.iter().any(|(name, _)| name == "response.failed"));
}
