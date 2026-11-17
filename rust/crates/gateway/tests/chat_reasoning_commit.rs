//! 思考内容也是业务输出：长思考流不能被计入无事件前导缓冲。

mod support;

use std::time::Duration;

use serde_json::json;
use support::{
    start_chat_gateway, ChatAttempt, ChatChunk, ChatStub, ClientRequest, TempDir, SHARE_SECRET,
};

#[test]
fn long_reasoning_stream_is_forwarded_without_prelude_overflow() {
    for field in ["reasoning", "reasoning_content"] {
        let temp = TempDir::new("um-rust-chat-long-reasoning-");
        let stub = ChatStub::start();
        let mut delta = serde_json::Map::new();
        delta.insert(field.to_string(), json!("thinking ".repeat(1024)));
        let reasoning = format!(
            "data: {}\n\n",
            json!({"choices": [{"delta": delta, "finish_reason": null}]})
        )
        .into_bytes();
        let mut chunks = vec![ChatChunk::now(reasoning.clone()); 9];
        let finish = b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n".to_vec();
        chunks.push(ChatChunk::now(finish.clone()));
        stub.push(ChatAttempt::Sse {
            chunks,
            hold: Duration::ZERO,
        });
        let gateway = start_chat_gateway(&stub, &temp, true, |config| {
            config.request_total = Duration::from_secs(5);
        });
        let request = ClientRequest::post("/v1/chat/completions", &json!({
            "model": "fixture-model", "messages": [{"role": "user", "content": "hi"}], "stream": true,
        }).to_string()).bearer(SHARE_SECRET).header("Content-Type", "application/json");
        let response = support::client_request(gateway.addr, &request).expect("reasoning stream");
        assert_eq!(
            response.status,
            200,
            "{field}: {}",
            String::from_utf8_lossy(&response.body)
        );
        let mut expected = reasoning.repeat(9);
        expected.extend_from_slice(&finish);
        assert_eq!(
            response.body, expected,
            "{field}: 思考和回答必须完整且仅转发一次"
        );
    }
}

#[test]
fn delivered_reasoning_is_not_replayed_after_an_upstream_disconnect() {
    let temp = TempDir::new("um-rust-chat-reasoning-disconnect-");
    let stub = ChatStub::start();
    let thinking =
        b"data: {\"choices\":[{\"delta\":{\"reasoning\":\"think\"},\"finish_reason\":null}]}\n\n";
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(thinking.to_vec())],
        hold: Duration::ZERO,
    });
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(b"data: {\"choices\":[{\"delta\":{\"content\":\"retry\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n".to_vec())],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.request_total = Duration::from_secs(5);
        config.retry_delay_base = Duration::ZERO;
    });
    let request = ClientRequest::post("/v1/chat/completions", &json!({
        "model": "fixture-model", "messages": [{"role": "user", "content": "hi"}], "stream": true,
    }).to_string()).bearer(SHARE_SECRET).header("Content-Type", "application/json");
    let response = support::client_request(gateway.addr, &request).expect("reasoning disconnect");
    assert_eq!(response.status, 200);
    assert_eq!(stub.attempts(), 1, "已交付思考内容后不能隐式重试并重放请求");
    assert!(response.body.starts_with(thinking));
    let body = String::from_utf8_lossy(&response.body);
    assert!(body.contains("upstream_eof_without_done"));
    assert!(!body.contains("data: [DONE]"));
}
