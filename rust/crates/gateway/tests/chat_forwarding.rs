//! S1b acceptance 1: non-stream JSON and SSE request/response shapes.
//!
//! The expected upstream path/bearer/body and the client-visible status,
//! content type and SSE payloads are the Python `_chat` behavior recorded in
//! `docs/rust-contract.md` §5.2/§5.3 and exercised by the Python stream and
//! endpoint tests.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    client_request, parse_raw_response, sse_payloads, start_chat_gateway, ChatAttempt, ChatChunk,
    ChatStub, ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
};

fn chat_body(stream: bool) -> Value {
    json!({
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        "max_tokens": 16,
    })
}

fn success_body() -> Value {
    json!({
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "model": "fixture-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
    })
}

#[test]
fn non_stream_json_is_relayed_unchanged() {
    let temp = TempDir::new("um-rust-chat-shape-");
    let stub = ChatStub::start();
    let body = success_body();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: body.clone(),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |_| {});
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body(false).to_string())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json"),
    )
    .expect("non-stream chat");
    eprintln!(
        "seen={:?} connections={} status={}",
        stub.seen(),
        stub.connections(),
        response.status
    );
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
    assert_eq!(response.json(), body);

    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/chat/completions");
    assert_eq!(
        seen[0].authorization.as_deref(),
        Some("Bearer fixture-upstream-token")
    );
    assert_eq!(seen[0].body["model"], json!("fixture-model"));
    assert_eq!(seen[0].body["stream"], json!(false));
    assert_eq!(seen[0].body.get("stream_options"), None);
}

#[test]
fn sse_is_forwarded_with_python_event_bytes_and_headers() {
    let temp = TempDir::new("um-rust-chat-shape-");
    let stub = ChatStub::start();
    let events = [
        b"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n"
            .to_vec(),
        b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\n"
            .to_vec(),
        b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec(),
        b"data: [DONE]\n\n".to_vec(),
    ];
    stub.push(ChatAttempt::Sse {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let mut client = StreamingClient::send(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body(true).to_string())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json"),
    )
    .expect("streaming chat request");
    let raw = client.read_all(Duration::from_secs(5)).expect("read SSE");
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let header = |name: &str| {
        headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    };
    assert_eq!(
        header("content-type"),
        Some("text/event-stream; charset=utf-8")
    );
    assert_eq!(header("cache-control"), Some("no-cache"));
    assert_eq!(header("connection"), Some("close"));
    assert_eq!(body, events.concat());

    let payloads = sse_payloads(&body);
    assert_eq!(payloads.len(), 3);
    assert_eq!(
        payloads[0],
        json!({"choices": [{"delta": {"role": "assistant"}, "finish_reason": null}]})
    );
    assert_eq!(
        payloads[1],
        json!({"choices": [{"delta": {"content": "OK"}, "finish_reason": null}]})
    );
    assert_eq!(
        payloads[2],
        json!({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    );

    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/chat/completions");
    assert_eq!(seen[0].body["stream"], json!(true));
    assert_eq!(
        seen[0].body["stream_options"],
        json!({"include_usage": true})
    );
}

#[test]
fn a_chunked_upstream_sse_body_is_decoded_before_forwarding() {
    let temp = TempDir::new("um-rust-chat-shape-");
    let stub = ChatStub::start();
    let events = [
        b"data: {\"choices\":[{\"delta\":{\"content\":\"chunked\"},\"finish_reason\":null}]}\n\n"
            .to_vec(),
        b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec(),
        b"data: [DONE]\n\n".to_vec(),
    ];
    stub.push(ChatAttempt::SseChunked {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let mut client = StreamingClient::send(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body(true).to_string())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json"),
    )
    .expect("chunked streaming request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read chunked SSE");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(body, events.concat());
}
