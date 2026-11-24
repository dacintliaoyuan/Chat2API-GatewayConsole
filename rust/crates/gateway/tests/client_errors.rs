//! Client-error semantics from the protocol-aware error contract.
//!
//! These are the HTTP-boundary checks for 405 + `Allow`, 415 unsupported
//! `Content-Encoding`, 408 body-read timeout, the preserved 32 MiB 413, the
//! error-response headers and the final-failure event log counters.

mod support;

use std::io::Write;
use std::net::TcpStream;
use std::time::Duration;

use serde_json::{json, Value};

use support::*;

fn header<'a>(headers: &'a [(String, String)], name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(key, _)| key.eq_ignore_ascii_case(name))
        .map(|(_, value)| value.as_str())
}

fn assert_error_headers(response: &ClientResponse) -> String {
    assert_eq!(
        response.header("cache-control"),
        Some("no-store"),
        "every self-produced error is no-store"
    );
    let request_id = response
        .header("x-request-id")
        .expect("every self-produced error carries X-Request-Id")
        .to_string();
    assert!(!request_id.is_empty());
    request_id
}

fn chat_body() -> String {
    json!({
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
        "stream": true,
    })
    .to_string()
}

fn chat_request() -> ClientRequest {
    ClientRequest::post("/v1/chat/completions", &chat_body())
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json")
}

fn content_event() -> Vec<u8> {
    b"data: {\"choices\":[{\"delta\":{\"content\":\"partial\"},\"finish_reason\":null}]}\n\n"
        .to_vec()
}

#[test]
fn an_unimplemented_method_on_a_chat_path_is_405_with_allow() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |_| {});

    let mut request = ClientRequest::post("/v1/chat/completions", "");
    request.method = "PUT".to_string();
    request.body = None;
    let response = client_request(gateway.addr, &request).expect("405 response");
    assert_eq!(response.status, 405);
    assert_eq!(response.header("allow"), Some("POST"));
    assert_eq!(
        response.json()["error"]["code"],
        json!("method_not_allowed")
    );
    assert_error_headers(&response);
}

#[test]
fn a_messages_method_error_uses_the_anthropic_shape() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |_| {});

    let mut request = ClientRequest::post("/v1/messages", "");
    request.method = "PUT".to_string();
    request.body = None;
    let response = client_request(gateway.addr, &request).expect("405 response");
    assert_eq!(response.status, 405);
    assert_eq!(response.header("allow"), Some("POST"));
    let body = response.json();
    assert_eq!(body["type"], json!("error"));
    assert_eq!(body["error"]["code"], json!("method_not_allowed"));
    assert_error_headers(&response);
}

#[test]
fn an_unsupported_content_encoding_is_415_not_invalid_json() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |_| {});

    let response = client_request(
        gateway.addr,
        &chat_request().header("Content-Encoding", "br"),
    )
    .expect("415 response");
    assert_eq!(response.status, 415);
    assert_eq!(
        response.json()["error"]["code"],
        json!("unsupported_content_encoding")
    );
    assert_error_headers(&response);
}

#[test]
fn a_slow_request_body_is_408_after_the_read_budget() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.body_read_timeout = Duration::from_millis(200);
        config.max_fast_retries = 0;
    });

    let mut stream =
        TcpStream::connect_timeout(&gateway.addr, Duration::from_secs(5)).expect("connect gateway");
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let head = format!(
        "POST /v1/chat/completions HTTP/1.1\r\nHost: {}\r\n\
         Authorization: Bearer {SHARE_SECRET}\r\nContent-Type: application/json\r\n\
         Content-Length: 10\r\n\r\n",
        gateway.addr
    );
    stream.write_all(head.as_bytes()).expect("write head");
    stream.write_all(b"{").expect("write partial body");
    stream.flush().expect("flush partial body");

    let response = read_client_response(&mut stream).expect("408 response");
    assert_eq!(response.status, 408);
    assert_eq!(response.json()["error"]["code"], json!("request_timeout"));
    assert_eq!(response.header("connection"), Some("close"));
    assert_error_headers(&response);
}

#[test]
fn a_33_mib_declared_body_is_413_and_closes() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let gateway = start_chat_gateway(&stub, &temp, true, |_| {});

    let mut request = ClientRequest::post("/v1/chat/completions", "");
    request.body = None;
    request = request
        .header("Content-Length", &(33 * 1024 * 1024).to_string())
        .bearer(SHARE_SECRET)
        .header("Content-Type", "application/json");
    let response = client_request(gateway.addr, &request).expect("413 response");
    assert_eq!(response.status, 413);
    assert_eq!(response.json()["error"]["code"], json!("payload_too_large"));
    assert_eq!(response.header("connection"), Some("close"));
    assert_error_headers(&response);
}

#[test]
fn a_final_failure_log_records_tries_and_physical_attempts() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let log_path = temp.path().join("service.log");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.service_log = Some(log_path.clone());
        config.max_fast_retries = 1;
        config.max_slow_retries = 0;
        config.retry_delay_base = Duration::ZERO;
    });
    for _ in 0..2 {
        stub.push(ChatAttempt::Json {
            status: 500,
            body: json!({"detail": "upstream exploded"}),
        });
    }

    let response = client_request(gateway.addr, &chat_request()).expect("500 response");
    assert_eq!(response.status, 500);
    assert_error_headers(&response);
    assert_eq!(stub.attempts(), 2);

    let log = std::fs::read_to_string(&log_path).expect("read service log");
    let summary = log
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .find(|row| row["event"] == json!("request_summary"))
        .expect("one request summary row");
    assert_eq!(summary["protocol"], json!("chat"));
    assert_eq!(summary["final"], json!("HTTP500"));
    assert_eq!(summary["tries"], json!(2));
    assert_eq!(summary["physical_attempts"], json!(2));
}

#[test]
fn local_chat_records_each_400_and_its_compatibility_attempt_by_request_id() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let log_path = temp.path().join("service.log");
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.role = um_gateway::config::Role::Local;
        config.service_log = Some(log_path.clone());
    });

    for (stream, second, expected_body) in [
        (true, "Content-Length: 0\r\n\r\n", "empty"),
        (false, "Transfer-Encoding: chunked\r\n\r\ninvalid\r\n", "read_failed"),
    ] {
        let body = r#"{"error":{"code":"context_length_exceeded","message":"fixture-sensitive-text"}}"#;
        stub.push(ChatAttempt::RawResponse {
            bytes: format!(
                "HTTP/1.1 400 Bad Request\r\nX-Request-Id: 59766032-437e-4a05-9839-8399bfa2a454\r\n\
                 Content-Length: {}\r\n\r\n{body}",
                body.len(),
            )
            .into_bytes(),
            hold: Duration::ZERO,
        });
        stub.push(ChatAttempt::RawResponse {
            bytes: format!("HTTP/1.1 400 Bad Request\r\n{second}").into_bytes(),
            hold: Duration::ZERO,
        });
        let request = ClientRequest::post(
            "/v1/chat/completions",
            &json!({
                "model": "fixture-model",
                "messages": [{"role": "user", "content": "fixture-prompt"}],
                "stream": stream,
                "stream_options": {"include_usage": true},
            }).to_string(),
        ).header("Content-Type", "application/json");
        let response = client_request(gateway.addr, &request).unwrap();
        assert_eq!(response.status, 400);
        let id = assert_error_headers(&response);

        let log = std::fs::read_to_string(&log_path).unwrap();
        assert!(!log.contains("fixture-sensitive-text"));
        assert!(!log.contains("fixture-prompt"));
        let rows: Vec<Value> = log.lines()
            .filter_map(|line| serde_json::from_str::<Value>(line).ok())
            .filter(|row| row["request_id"] == id && row["event"] == "upstream_chat_attempt")
            .collect();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["attempt"], 1);
        assert_eq!(rows[0]["upstream_status"], 400);
        assert_eq!(rows[0]["upstream_request_id"], "59766032-437e-4a05-9839-8399bfa2a454");
        assert_eq!(rows[0]["reason"], "context_length_exceeded");
        assert_eq!(rows[0]["retry_without_stream_options"], true);
        assert_eq!(rows[1]["attempt"], 2);
        assert_eq!(rows[1]["upstream_status"], 400);
        assert_eq!(rows[1]["stream_options"], "removed_for_retry");
        assert_eq!(rows[1]["reason"], "unknown");
        assert_eq!(rows[1]["error_body"], expected_body);
    }

    let seen = stub.seen();
    assert_eq!(seen.len(), 4);
    for pair in seen.chunks_exact(2) {
        assert!(pair[0].body.get("stream_options").is_some());
        assert!(pair[1].body.get("stream_options").is_none());
    }
}

#[test]
fn a_delayed_400_body_does_not_block_chat_compatibility_retry() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let log_path = temp.path().join("service.log");
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.role = um_gateway::config::Role::Local;
        config.service_log = Some(log_path.clone());
        config.request_total = Duration::from_secs(1);
    });
    stub.push(ChatAttempt::RawResponse {
        bytes: b"HTTP/1.1 400 Bad Request\r\nContent-Length: 12\r\n\r\n".to_vec(),
        hold: Duration::from_secs(2),
    });
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({"choices": [{"message": {"content": "ok"}}]}),
    });

    let request = ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "fixture-prompt"}],
            "stream": false,
            "stream_options": {"include_usage": true},
        })
        .to_string(),
    )
    .header("Content-Type", "application/json");
    let response = client_request(gateway.addr, &request).unwrap();
    assert_eq!(response.status, 200);
    assert_eq!(stub.attempts(), 2);

    let log = std::fs::read_to_string(&log_path).unwrap();
    let attempts: Vec<Value> = log
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|row| row["event"] == "upstream_chat_attempt")
        .collect();
    assert_eq!(attempts.len(), 2);
    assert_eq!(attempts[0]["error_body"], "not_ready");
    assert_eq!(attempts[1]["upstream_status"], 200);
    assert_eq!(attempts[1]["stream_options"], "removed_for_retry");
    assert_eq!(attempts[0]["request_id"], attempts[1]["request_id"]);
}

#[test]
fn an_in_band_error_carries_the_same_request_id_as_the_stream() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![ChatChunk::now(content_event())],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
    });

    let mut client = StreamingClient::send(gateway.addr, &chat_request()).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, headers, body) = parse_raw_response(&raw);
    assert_eq!(status, 200);
    let stream_request_id = header(&headers, "x-request-id")
        .expect("committed stream carries X-Request-Id")
        .to_string();
    let error = sse_payloads(&body)
        .into_iter()
        .find(|payload| payload.get("error").is_some())
        .expect("one in-band error");
    assert_eq!(
        error["error"]["detail"]["request_id"],
        json!(stream_request_id)
    );
}
