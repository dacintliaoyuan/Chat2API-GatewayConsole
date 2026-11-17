//! Ticket #63: re-authentication triggers and the authentication-wait budget.
//!
//! The fake UM scripts one upstream status per attempt; a scripted provider
//! records how many sessions actually started. The assertions are external: final
//! HTTP status, error envelope, `detail.reauthentication_attempted`, the number of
//! upstream attempts and the number of provider sessions. No real browser opens.

mod support;

use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use um_gateway::reauth::{ReauthProviderResult, ReauthenticationProvider};

use support::{
    client_request, parse_raw_response, start_chat_gateway, start_chat_gateway_with_reauth,
    ChatAttempt, ChatChunk, ChatStub, ClientRequest, StreamingClient, TempDir, SHARE_SECRET,
};

fn chat_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn messages_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/messages",
        &json!({
            "model": "claude-x",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        })
        .to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
    .header("Anthropic-Version", "2023-06-01")
}

fn responses_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/responses",
        &json!({"model": "responses-model", "input": "hi"}).to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn embeddings_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/embeddings",
        &json!({"model": "embedding-model", "input": "hi"}).to_string(),
    )
    .bearer(SHARE_SECRET)
    .header("Content-Type", "application/json")
}

fn sse_success() -> ChatAttempt {
    ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"OK\"},\"finish_reason\":\"stop\"}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::now(b"data: [DONE]\n\n".to_vec()),
        ],
        hold: Duration::ZERO,
    }
}

fn chat_success() -> Value {
    json!({
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
}

fn push_status(stub: &ChatStub, status: u16, body: &str) {
    stub.push(ChatAttempt::HttpStatus {
        status,
        body: body.to_string(),
        retry_after: None,
    });
}

fn push_success(stub: &ChatStub) {
    stub.push(ChatAttempt::Json {
        status: 200,
        body: chat_success(),
    });
}

fn retry_budget(config: &mut um_gateway::config::GatewayConfig) {
    config.max_fast_retries = 0;
    config.max_slow_retries = 0;
    config.max_physical_attempts = 4;
    config.retry_delay_base = Duration::ZERO;
    config.request_total = Duration::from_secs(5);
}

struct Gate {
    open: Mutex<bool>,
    condvar: Condvar,
}

#[derive(Clone)]
struct ScriptedProvider {
    results: Arc<Mutex<VecDeque<ReauthProviderResult>>>,
    calls: Arc<AtomicUsize>,
    gate: Option<Arc<Gate>>,
}

impl ScriptedProvider {
    fn new(results: Vec<ReauthProviderResult>) -> Self {
        Self {
            results: Arc::new(Mutex::new(results.into())),
            calls: Arc::new(AtomicUsize::new(0)),
            gate: None,
        }
    }

    fn blocking() -> Self {
        Self {
            results: Arc::new(Mutex::new(VecDeque::from([ReauthProviderResult::Restored]))),
            calls: Arc::new(AtomicUsize::new(0)),
            gate: Some(Arc::new(Gate {
                open: Mutex::new(false),
                condvar: Condvar::new(),
            })),
        }
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn release(&self) {
        if let Some(gate) = &self.gate {
            let mut open = gate.open.lock().expect("gate");
            *open = true;
            gate.condvar.notify_all();
        }
    }
}

impl ReauthenticationProvider for ScriptedProvider {
    fn reauthenticate(&self, _deadline: Instant) -> ReauthProviderResult {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if let Some(gate) = &self.gate {
            let mut open = gate.open.lock().expect("gate");
            while !*open {
                open = gate.condvar.wait(open).expect("gate wait");
            }
        }
        self.results
            .lock()
            .expect("provider results")
            .pop_front()
            .unwrap_or(ReauthProviderResult::Failed {
                reason: "script_exhausted".to_string(),
            })
    }
}

#[test]
fn a_401_starts_one_session_and_resends_once() {
    let temp = TempDir::new("um-rust-reauth-401-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("401 reauth");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 2, "401 is resent exactly once");
}

#[test]
fn a_401_that_stays_401_is_a_503_without_a_second_session() {
    let temp = TempDir::new("um-rust-reauth-401-final-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    push_status(&stub, 401, "upstream rejected");
    push_status(&stub, 401, "upstream rejected");
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("401 final");
    assert_eq!(response.status, 503);
    assert_eq!(
        response.json()["error"]["code"],
        json!("reauthentication_required")
    );
    assert_eq!(
        response.json()["error"]["detail"],
        json!("upstream_rejected")
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(
        stub.attempts(),
        2,
        "the second 401 must not start a session"
    );
}

#[test]
fn a_403_authentication_text_starts_one_session_and_resends_once() {
    let temp = TempDir::new("um-rust-reauth-403-text-");
    let stub = ChatStub::start();
    push_status(&stub, 403, "Not authenticated");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("403 reauth");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn a_403_authentication_text_that_stays_403_is_reauthentication_failed() {
    let temp = TempDir::new("um-rust-reauth-403-final-");
    let stub = ChatStub::start();
    push_status(&stub, 403, "session expired");
    push_status(&stub, 403, "please log in");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("403 final");
    assert_eq!(response.status, 403);
    let body = response.json();
    assert_eq!(
        body["error"]["type"],
        json!("authentication_error"),
        "{body}"
    );
    assert_eq!(
        body["error"]["code"],
        json!("reauthentication_failed"),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["reauthentication_attempted"],
        json!(true),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["upstream_status"],
        json!(403),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["upstream_message"],
        json!("please log in"),
        "{body}"
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(
        stub.attempts(),
        2,
        "the second 403 must not start a session"
    );
}

#[test]
fn a_403_authentication_text_at_the_physical_budget_is_reauthentication_failed() {
    let temp = TempDir::new("um-rust-reauth-403-budget-");
    let stub = ChatStub::start();
    push_status(&stub, 403, "Not authenticated");
    push_status(&stub, 403, "Not authenticated");
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
            config.max_physical_attempts = 2;
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("403 at physical budget");
    assert_eq!(response.status, 403);
    let body = response.json();
    assert_eq!(
        body["error"]["code"],
        json!("reauthentication_failed"),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["reauthentication_attempted"],
        json!(true),
        "{body}"
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn a_403_permission_text_never_starts_a_session() {
    let temp = TempDir::new("um-rust-reauth-403-permission-");
    let stub = ChatStub::start();
    push_status(&stub, 403, "forbidden");
    push_status(&stub, 403, "forbidden");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("403 permission");
    assert_eq!(response.status, 403);
    assert_eq!(
        response.json()["error"]["code"],
        json!("upstream_forbidden")
    );
    assert_eq!(provider.calls(), 0);
    assert_eq!(
        stub.attempts(),
        2,
        "403 permission is probed once, never reauthenticated"
    );
}

#[test]
fn a_cloudflare_challenge_never_starts_a_session() {
    let temp = TempDir::new("um-rust-reauth-cloudflare-");
    let stub = ChatStub::start();
    push_status(&stub, 403, "Cloudflare challenge");
    push_status(&stub, 403, "Cloudflare challenge");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("cloudflare 403");
    assert_eq!(response.status, 403);
    assert_eq!(
        response.json()["error"]["code"],
        json!("upstream_forbidden")
    );
    assert_eq!(provider.calls(), 0);
}

#[test]
fn a_402_never_starts_a_session() {
    let temp = TempDir::new("um-rust-reauth-402-");
    let stub = ChatStub::start();
    push_status(&stub, 402, "quota exhausted");
    push_status(&stub, 402, "quota exhausted");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("402");
    assert_eq!(response.status, 402);
    assert_eq!(
        response.json()["error"]["code"],
        json!("upstream_quota_exhausted")
    );
    assert_eq!(provider.calls(), 0);
    assert_eq!(
        stub.attempts(),
        2,
        "402 is probed once, never reauthenticated"
    );
}

#[test]
fn a_404_never_starts_a_session() {
    let temp = TempDir::new("um-rust-reauth-404-");
    let stub = ChatStub::start();
    push_status(&stub, 404, "not found");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("404");
    assert_eq!(response.status, 404);
    assert_eq!(provider.calls(), 0);
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn no_browser_reports_the_browser_disabled_reason() {
    let temp = TempDir::new("um-rust-reauth-no-browser-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        retry_budget(config);
        config.browser_disabled = true;
    });
    let response = client_request(gateway.addr, &chat_request()).expect("no browser");
    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(
        body["error"]["code"],
        json!("reauthentication_required"),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"],
        json!("upstream_rejected"),
        "{body}"
    );
    assert_eq!(body["error"]["reason"], json!("BrowserDisabled"), "{body}");
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn auth_wait_timeout_is_a_503_reauthentication_required() {
    let temp = TempDir::new("um-rust-reauth-wait-timeout-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    let provider = ScriptedProvider::blocking();
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
            config.auth_wait_budget = Some(Duration::from_millis(50));
            config.auth_wait_recheck = Duration::from_millis(10);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("auth wait timeout");
    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(
        body["error"]["code"],
        json!("reauthentication_required"),
        "{body}"
    );
    assert_eq!(
        body["error"]["reason"],
        json!("auth_wait_timeout"),
        "{body}"
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 1);
    provider.release();
}

#[test]
fn auth_wait_capacity_is_a_503_reauthentication_required() {
    let temp = TempDir::new("um-rust-reauth-wait-capacity-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    push_status(&stub, 401, "upstream rejected");
    push_success(&stub);
    let provider = ScriptedProvider::blocking();
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        false,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
            config.auth_wait_max_concurrent = 1;
            config.request_total = Duration::from_secs(10);
        },
    );
    let addr = gateway.addr;
    let first = thread::spawn(move || client_request(addr, &chat_request()));
    let deadline = Instant::now() + Duration::from_secs(2);
    while provider.calls() == 0 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(5));
    }
    thread::sleep(Duration::from_millis(100));
    let second = client_request(gateway.addr, &chat_request()).expect("wait capacity");
    assert_eq!(second.status, 503);
    assert_eq!(
        second.json()["error"]["reason"],
        json!("auth_wait_capacity")
    );
    provider.release();
    let first = first
        .join()
        .expect("first request thread")
        .expect("first request");
    assert_eq!(first.status, 200);
    assert_eq!(provider.calls(), 1);
}

#[test]
fn the_global_attempt_cap_is_a_503_with_a_retry_after() {
    let temp = TempDir::new("um-rust-reauth-global-cap-");
    let stub = ChatStub::start();
    for _ in 0..7 {
        push_status(&stub, 401, "upstream rejected");
    }
    let provider = ScriptedProvider::new(vec![
        ReauthProviderResult::Restored,
        ReauthProviderResult::Restored,
        ReauthProviderResult::Restored,
    ]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
            config.reauth_max_attempts_per_window = 3;
            config.reauth_attempt_window = Duration::from_secs(900);
        },
    );
    for _ in 0..3 {
        let response = client_request(gateway.addr, &chat_request()).expect("capped request");
        assert_eq!(response.status, 503);
        assert_eq!(
            response.json()["error"]["code"],
            json!("reauthentication_required")
        );
    }
    assert_eq!(provider.calls(), 3);
    let response = client_request(gateway.addr, &chat_request()).expect("global cap");
    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(
        body["error"]["code"],
        json!("reauthentication_required"),
        "{body}"
    );
    assert_eq!(
        body["error"]["reason"],
        json!("global_attempt_cap"),
        "{body}"
    );
    let retry_after = response
        .header("retry-after")
        .and_then(|value| value.parse::<u64>().ok())
        .expect("global cap has a definite Retry-After");
    assert!((1..=900).contains(&retry_after), "{retry_after}");
    assert_eq!(
        provider.calls(),
        3,
        "the fourth request must not start a session"
    );
    assert_eq!(stub.attempts(), 7, "the fourth request must not resend");
}

#[test]
fn a_failed_session_is_a_503_reauthentication_required() {
    let temp = TempDir::new("um-rust-reauth-session-failed-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Failed {
        reason: "manual_takeover_required:umpass_mfa_required".to_string(),
    }]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("session failed");
    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(
        body["error"]["code"],
        json!("reauthentication_required"),
        "{body}"
    );
    assert_eq!(
        body["error"]["reason"],
        json!("manual_takeover_required:umpass_mfa_required"),
        "{body}"
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 1);
}

#[test]
fn authentication_wait_counts_toward_the_total_request_budget() {
    let temp = TempDir::new("um-rust-reauth-total-budget-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    let provider = ScriptedProvider::blocking();
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
            config.auth_wait_budget = Some(Duration::from_secs(5));
            config.auth_wait_recheck = Duration::from_millis(10);
            config.request_total = Duration::from_millis(100);
        },
    );
    let response = client_request(gateway.addr, &chat_request()).expect("total budget");
    assert_eq!(response.status, 504);
    let body = response.json();
    assert_eq!(
        body["error"]["code"],
        json!("request_deadline_exceeded"),
        "{body}"
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 1);
    provider.release();
}

#[test]
fn a_streaming_401_starts_one_session_and_resends_once() {
    let temp = TempDir::new("um-rust-reauth-stream-401-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    stub.push(sse_success());
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
            config.first_byte_timeout = Duration::from_secs(2);
            config.socket_silence_timeout = Duration::from_secs(2);
        },
    );
    let request = ClientRequest::post(
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
    .header("Content-Type", "application/json");
    let mut client = StreamingClient::send(gateway.addr, &request).expect("stream request");
    let raw = client
        .read_all(Duration::from_secs(5))
        .expect("read stream");
    let (status, _, body) = parse_raw_response(&raw);
    assert_eq!(status, 200, "{}", String::from_utf8_lossy(&body));
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn a_messages_403_authentication_text_is_reauthentication_failed_in_the_anthropic_shape() {
    let temp = TempDir::new("um-rust-reauth-messages-403-");
    let stub = ChatStub::start();
    push_status(&stub, 403, "authentication required");
    push_status(&stub, 403, "authentication required");
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &messages_request()).expect("messages 403");
    assert_eq!(response.status, 403);
    let body = response.json();
    assert_eq!(body["type"], json!("error"), "{body}");
    assert_eq!(
        body["error"]["code"],
        json!("reauthentication_failed"),
        "{body}"
    );
    assert_eq!(
        body["error"]["detail"]["reauthentication_attempted"],
        json!(true),
        "{body}"
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn a_responses_401_starts_one_session_and_resends_once() {
    let temp = TempDir::new("um-rust-reauth-responses-401-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    push_success(&stub);
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        true,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &responses_request()).expect("responses 401");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 2);
}

#[test]
fn an_embeddings_401_starts_one_session_and_resends_once() {
    let temp = TempDir::new("um-rust-reauth-embeddings-401-");
    let stub = ChatStub::start();
    push_status(&stub, 401, "upstream rejected");
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({"object": "list", "data": [{"embedding": [0.1]}]}),
    });
    let provider = ScriptedProvider::new(vec![ReauthProviderResult::Restored]);
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        false,
        Some(Arc::new(provider.clone())),
        |config| {
            retry_budget(config);
        },
    );
    let response = client_request(gateway.addr, &embeddings_request()).expect("embeddings 401");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(provider.calls(), 1);
    assert_eq!(stub.attempts(), 2);
}
