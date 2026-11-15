//! S2a acceptance 3: the Anthropic SSE event sequence is relayed byte for
//! byte, `message_stop` is the only successful terminal event, EOF before it is
//! never a success, and an `error` event terminates the relay immediately.
//!
//! Python source: `chat2api/chat2api.py:9710-10030` (`_passthrough_stream`) and
//! `tests/test_chat2api_endpoints.py:501-610` (the official sequence, a
//! `message_delta` stop_reason is not terminal, EOF before `message_stop`, and an
//! `error` event terminates immediately).

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::{
    parse_raw_response, start_chat_gateway, ChatAttempt, ChatChunk, ChatStub, ClientRequest,
    StreamingClient, TempDir, SHARE_SECRET,
};

fn request() -> ClientRequest {
    ClientRequest::post(
        "/v1/messages",
        &json!({
            "model": "claude-x",
            "max_tokens": 16,
            "stream": true,
            "messages": [{"role": "user", "content": "hi"}],
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
    .header("Anthropic-Version", "2023-06-01")
}

fn event(name: &str, data: Value) -> Vec<u8> {
    format!(
        "event: {name}\ndata: {}\n\n",
        serde_json::to_string(&data).unwrap()
    )
    .into_bytes()
}

fn official_events() -> Vec<Vec<u8>> {
    vec![
        event(
            "message_start",
            json!({
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-x",
                    "content": [],
                    "stop_reason": null,
                    "stop_sequence": null,
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 3,
                    },
                },
            }),
        ),
        event(
            "content_block_start",
            json!({
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }),
        ),
        event("ping", json!({"type": "ping"})),
        event(
            "content_block_delta",
            json!({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            }),
        ),
        event(
            "content_block_stop",
            json!({"type": "content_block_stop", "index": 0}),
        ),
        event(
            "message_delta",
            json!({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": null},
                "usage": {"output_tokens": 16},
            }),
        ),
        event("message_stop", json!({"type": "message_stop"})),
    ]
}

fn start_stream(gateway: &support::Gateway) -> Vec<u8> {
    let mut client = StreamingClient::send(gateway.addr, &request()).expect("streaming request");
    client.read_all(Duration::from_secs(5)).expect("read SSE")
}

#[test]
fn the_official_event_sequence_is_forwarded_byte_for_byte() {
    let temp = TempDir::new("um-rust-messages-stream-");
    let stub = ChatStub::start();
    let events = official_events();
    stub.push(ChatAttempt::Sse {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let raw = start_stream(&gateway);
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
    assert_eq!(
        body,
        events.concat(),
        "the upstream bytes are relayed verbatim"
    );

    let seen = stub.seen();
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].path, "/api/v1/messages");
    assert_eq!(seen[0].body["stream"], json!(true));
}

#[test]
fn a_chunked_upstream_sse_body_is_decoded_before_forwarding() {
    let temp = TempDir::new("um-rust-messages-stream-");
    let stub = ChatStub::start();
    let events = official_events();
    stub.push(ChatAttempt::SseChunked {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let raw = start_stream(&gateway);
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(body, events.concat());
}

#[test]
fn eof_before_message_stop_is_not_a_success() {
    let temp = TempDir::new("um-rust-messages-stream-");
    let stub = ChatStub::start();
    let mut events = official_events();
    events.pop();
    stub.push(ChatAttempt::Sse {
        chunks: events.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let raw = start_stream(&gateway);
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(body, events.concat());
    assert!(
        !body
            .windows(b"message_stop".len())
            .any(|window| window == b"message_stop"),
        "the relay must never synthesize a missing message_stop"
    );
}

#[test]
fn an_error_event_terminates_immediately() {
    let temp = TempDir::new("um-rust-messages-stream-");
    let stub = ChatStub::start();
    let mut events = official_events();
    events.truncate(4);
    let error = event(
        "error",
        json!({
            "type": "error",
            "error": {"type": "overloaded_error", "message": "overloaded"},
        }),
    );
    events.push(error);
    let after_error = event(
        "content_block_delta",
        json!({
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "after-error"},
        }),
    );
    let mut chunks: Vec<ChatChunk> = events.iter().cloned().map(ChatChunk::now).collect();
    chunks.push(ChatChunk::now(after_error.clone()));
    stub.push(ChatAttempt::Sse {
        chunks,
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let raw = start_stream(&gateway);
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(body, events.concat());
    assert!(
        !body
            .windows(b"after-error".len())
            .any(|window| window == b"after-error"),
        "the relay must stop at the error event"
    );
}

#[test]
fn a_message_delta_stop_reason_is_not_a_terminal_event() {
    let temp = TempDir::new("um-rust-messages-stream-");
    let stub = ChatStub::start();
    let mut events = official_events();
    let first_delta = events.remove(5);
    let stop = events.pop().expect("message_stop");
    events.push(event(
        "message_delta",
        json!({
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": null},
            "usage": {"output_tokens": 3},
        }),
    ));
    events.push(event(
        "message_delta",
        json!({
            "type": "message_delta",
            "delta": {"stop_reason": null, "stop_sequence": null},
            "usage": {"output_tokens": 19},
        }),
    ));
    events.push(stop);
    let mut all = vec![first_delta];
    all.extend(events);
    stub.push(ChatAttempt::Sse {
        chunks: all.iter().cloned().map(ChatChunk::now).collect(),
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });
    let raw = start_stream(&gateway);
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    assert_eq!(body, all.concat());
}
