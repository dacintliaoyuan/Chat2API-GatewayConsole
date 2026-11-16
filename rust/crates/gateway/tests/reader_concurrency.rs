//! ADR 0004: the gateway no longer caps concurrent streaming readers.
//!
//! `too_many_concurrent_streams` is retired. More than the old 128-reader cap
//! must be admitted by the non-stream aggregation path, which previously used an
//! immediate `try_acquire` and returned 503 as soon as the cap was reached.

mod support;

use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::Duration;

use serde_json::json;

use um_gateway::chat::ChatResult;
use um_gateway::config::{parse_nonstream_aggregate_scope, GatewayConfig};
use um_gateway::event_log::EventLog;
use um_gateway::gate::{Admission, InflightCtx};
use um_gateway::http::{BodyRead, Headers, Request};
use um_gateway::ledger::Ledger;
use um_gateway::readonly::ProxyState;
use um_gateway::reauth::{MissingLoginProvider, ReauthLimits, ReauthenticationService};
use um_gateway::responses::handle_responses;
use um_gateway::server::AppState;

use support::{ChatAttempt, ChatChunk, ChatStub, TempDir};

const CONCURRENCY: usize = 160;

fn aggregate_scope() -> um_gateway::config::AggregateScope {
    parse_nonstream_aggregate_scope(Some(
        r#"{"key_ids":["friend"],"models":["responses-model"],"protocols":["responses"]}"#,
    ))
    .expect("valid aggregate scope")
}

fn request() -> Request {
    let body = json!({"model": "responses-model", "input": "hi", "stream": false})
        .to_string()
        .into_bytes();
    let mut headers = Headers::new();
    headers.push("Content-Type", "application/json");
    headers.push("Content-Length", body.len().to_string());
    Request {
        method: "POST".to_string(),
        target: "/v1/responses".to_string(),
        headers,
        body: BodyRead::Complete(body),
        peer_ip: "127.0.0.1".parse().expect("loopback peer"),
        request_id: "fixture-request-id".to_string(),
    }
}

fn gateway_state(stub: &ChatStub) -> Arc<AppState> {
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: "fixture-token".to_string(),
        nonstream_upstream_aggregate: true,
        nonstream_aggregate_scope: Some(aggregate_scope()),
        request_total: Duration::from_secs(10),
        first_byte_timeout: Duration::from_secs(2),
        socket_silence_timeout: Duration::from_secs(5),
        ..GatewayConfig::default()
    };
    let proxy = Arc::new(ProxyState::new(&config).expect("proxy state"));
    let ledger = Arc::new(Ledger::new(None));
    let event_log = Arc::new(EventLog::new(None));
    let reauth = Arc::new(ReauthenticationService::new(
        Arc::new(MissingLoginProvider),
        ReauthLimits::from_config(&config),
    ));
    Arc::new(AppState {
        config,
        proxy,
        gate: None,
        control_token: None,
        local_api_key: String::new(),
        require_local_api_key: false,
        ledger,
        event_log,
        reauth,
        login: None,
    })
}

fn socket_pair() -> (TcpStream, TcpStream) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind socket pair");
    let address = listener.local_addr().expect("socket pair address");
    let client = TcpStream::connect(address).expect("connect socket pair");
    let (server, _) = listener.accept().expect("accept socket pair");
    (server, client)
}

#[test]
fn many_concurrent_aggregate_readers_are_not_refused() {
    let _temp = TempDir::new("um-rust-reader-concurrency-");
    let stub = ChatStub::start();
    for _ in 0..CONCURRENCY {
        stub.push(ChatAttempt::Sse {
            chunks: vec![
                ChatChunk::after(
                    Duration::from_secs(1),
                    b"data: {\"id\":\"chatcmpl-1\",\"created\":1700000000,\"model\":\"um-model\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"Hi\"}}]}\n\n"
                        .to_vec(),
                ),
                ChatChunk::now(
                    b"data: {\"choices\":[{\"index\":0,\"delta\":{\"content\":\" there\"},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5,\"total_tokens\":15}}\n\n"
                        .to_vec(),
                ),
                ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
            ],
            hold: Duration::ZERO,
        });
    }
    let state = gateway_state(&stub);
    let start = Arc::new(Barrier::new(CONCURRENCY + 1));
    let mut handles = Vec::with_capacity(CONCURRENCY);
    for index in 0..CONCURRENCY {
        let state = Arc::clone(&state);
        let start = Arc::clone(&start);
        handles.push(thread::spawn(move || {
            start.wait();
            // Spread connection attempts so the stub's accept backlog is not the
            // bottleneck; the one-second first chunk keeps every reader alive
            // long enough for the later requests to overlap.
            thread::sleep(Duration::from_millis(index as u64));
            let (mut server, _client) = socket_pair();
            let admission = Admission {
                key_id: "friend".to_string(),
                ctx: InflightCtx::new(server.try_clone().expect("clone server stream")),
            };
            match handle_responses(&state, &request(), &mut server, Some(admission)) {
                ChatResult::Response(response) => {
                    if response.status != 200 {
                        eprintln!(
                            "unexpected reader response: {} {}",
                            response.status,
                            String::from_utf8_lossy(&response.body)
                        );
                    }
                    response.status
                }
                ChatResult::Written { .. } => 0,
            }
        }));
    }
    start.wait();
    let statuses: Vec<u16> = handles
        .into_iter()
        .map(|handle| handle.join().expect("reader thread"))
        .collect();
    assert_eq!(
        statuses.iter().filter(|status| **status == 200).count(),
        CONCURRENCY,
        "reader statuses: {statuses:?}"
    );
}
