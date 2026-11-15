//! S1b acceptance 2: the 200 headers are committed only after the first
//! substantive data event; metadata and thinking events may be staged briefly.
//!
//! The gate is Python `_reasoning_retry_preflight` + `Handler.process_event`
//! (`chat2api/chat2api.py:6638-6783,9346-9430`): held bytes are replayed once
//! the stream proves alive, and an oversized held prelude fails before any 200.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    parse_raw_response, sse_payloads, start_chat_gateway, ChatAttempt, ChatChunk, ChatStub,
    ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
};

fn request() -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": true,
            "max_tokens": 16,
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn metadata_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n"
        .to_vec()
}

fn reasoning_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"think\"},\"finish_reason\":null}]}\n\n"
        .to_vec()
}

fn content_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":null}]}\n\n".to_vec()
}

fn finish_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n".to_vec()
}

fn done_event() -> Vec<u8> {
    b"data: [DONE]\n\n".to_vec()
}

#[test]
fn metadata_is_staged_until_the_first_substantive_event() {
    let temp = TempDir::new("um-rust-chat-commit-");
    let stub = ChatStub::start();
    let staged = metadata_event();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(staged.clone()),
            ChatChunk::after(Duration::from_millis(300), content.clone()),
            ChatChunk::now(finish_event()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");

    // The metadata event is held: no status line and no body byte yet.
    assert_eq!(
        client
            .read_some(Duration::from_millis(100))
            .expect("first read"),
        None,
        "the 200 must not be committed before substantive data"
    );

    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(
        body,
        [staged.clone(), content, finish_event(), done_event()].concat(),
        "held metadata is replayed exactly once"
    );
    let payloads = sse_payloads(&body);
    assert_eq!(
        payloads[0],
        json!({"choices": [{"delta": {"role": "assistant"}, "finish_reason": null}]})
    );
    assert_eq!(
        payloads[1],
        json!({"choices": [{"delta": {"content": "OK"}, "finish_reason": null}]})
    );
}

#[test]
fn thinking_is_staged_until_the_first_visible_event() {
    let temp = TempDir::new("um-rust-chat-commit-");
    let stub = ChatStub::start();
    let thinking = reasoning_event();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(thinking.clone()),
            ChatChunk::after(Duration::from_millis(300), content.clone()),
            ChatChunk::now(finish_event()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    assert_eq!(
        client
            .read_some(Duration::from_millis(100))
            .expect("first read"),
        None,
        "thinking alone must not commit the 200"
    );
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(
        body,
        [thinking, content, finish_event(), done_event()].concat()
    );
}

#[test]
fn an_oversized_prelude_fails_before_the_200() {
    let temp = TempDir::new("um-rust-chat-commit-");
    let stub = ChatStub::start();
    let comment = b": held-back comment\n\n".to_vec();
    let mut chunks = Vec::new();
    for _ in 0..4000 {
        chunks.push(ChatChunk::now(comment.clone()));
    }
    chunks.push(ChatChunk::now(content_event()));
    stub.push(ChatAttempt::Sse {
        chunks,
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let response = support::client_request(gateway.addr, &request()).expect("prelude overflow");
    assert_eq!(response.status, 502);
    let body = response.json();
    assert_eq!(body["error"]["type"], json!("upstream_stream_error"));
    assert_eq!(
        body["error"]["detail"]["reason"],
        json!("upstream_prelude_overflow")
    );
}

#[test]
fn a_first_content_event_commits_without_staging() {
    let temp = TempDir::new("um-rust-chat-commit-");
    let stub = ChatStub::start();
    let content = content_event();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(content.clone()),
            ChatChunk::now(finish_event()),
            ChatChunk::now(done_event()),
        ],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(body, [content, finish_event(), done_event()].concat());
    let payloads: Vec<Value> = sse_payloads(&body);
    assert_eq!(payloads.len(), 2);
}
