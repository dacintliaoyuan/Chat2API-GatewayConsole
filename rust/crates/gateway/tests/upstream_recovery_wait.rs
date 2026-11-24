//! ADR 0014 acceptance through a local worker and a loopback UM fixture.

mod support;

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_gateway::{
    ingress::{start_worker, IngressConfig, IngressServer},
    GatewayConfig, Role, RunMode, ServerHandle,
};

use support::{
    client_request, parse_raw_response, ClientRequest, StreamingClient, TempDir, STUB_TOKEN,
};

const WAIT: Duration = Duration::from_secs(5);
const QUIET: Duration = Duration::from_millis(200);
const CATALOG_PATH: &str = "/api/models";
const CHAT_PATH: &str = "/api/chat/completions";
const CONTENT_EVENT: &str =
    "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"},\"finish_reason\":null}]}\n\n";

struct Fixture {
    address: SocketAddr,
    calls: Receiver<UpstreamCall>,
    stop: Arc<AtomicBool>,
    listener: Option<JoinHandle<()>>,
}

impl Fixture {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind fake UM");
        let address = listener.local_addr().expect("fake UM address");
        let (calls_tx, calls) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let listener_stop = Arc::clone(&stop);
        let listener = thread::spawn(move || {
            for stream in listener.incoming() {
                if listener_stop.load(Ordering::Acquire) {
                    break;
                }
                let stream = stream.expect("accept fake UM request");
                let calls = calls_tx.clone();
                thread::spawn(move || {
                    let call = UpstreamCall::read(stream).expect("read fake UM request");
                    if call.path == "/api/config" {
                        call.json(200, json!({"features": {"enable_api_keys": false}}));
                    } else {
                        let _ = calls.send(call);
                    }
                });
            }
        });
        Self {
            address,
            calls,
            stop,
            listener: Some(listener),
        }
    }

    fn start_worker(&self, directory: &TempDir, request_total: Duration) -> ServerHandle {
        let config = GatewayConfig {
            run_mode: RunMode::Worker,
            role: Role::Local,
            upstream_base_url: format!("http://{}", self.address),
            upstream_token: STUB_TOKEN.to_string(),
            browser_disabled: true,
            request_total,
            first_byte_timeout: WAIT,
            socket_silence_timeout: WAIT,
            max_fast_retries: 0,
            max_slow_retries: 0,
            ..GatewayConfig::default()
        };
        start_worker(config, &directory.path().join("worker.port")).expect("start local worker")
    }

    fn call(&self, path: &str) -> UpstreamCall {
        let call = self
            .calls
            .recv_timeout(WAIT)
            .expect("fake UM did not receive request");
        assert_eq!(
            call.path, path,
            "unexpected request to UM, body = {}",
            call.body
        );
        call
    }

    fn assert_quiet(&self) {
        match self.calls.recv_timeout(QUIET) {
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => panic!("fake UM stopped"),
            Ok(call) => panic!("unexpected request to UM: {} {}", call.path, call.body),
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let _ = TcpStream::connect_timeout(&self.address, QUIET);
        self.listener
            .take()
            .expect("fixture thread")
            .join()
            .expect("fake UM listener");
    }
}

struct UpstreamCall {
    path: String,
    body: Value,
    stream: TcpStream,
}

impl UpstreamCall {
    fn read(stream: TcpStream) -> io::Result<Self> {
        stream.set_read_timeout(Some(WAIT))?;
        stream.set_write_timeout(Some(WAIT))?;
        let mut stream = BufReader::new(stream);
        let mut line = String::new();
        stream.read_line(&mut line)?;
        let target = line.split_whitespace().nth(1).expect("HTTP request target");
        let path = target.split('?').next().expect("HTTP path").to_owned();
        let mut length = 0;
        loop {
            line.clear();
            if stream.read_line(&mut line)? == 0 || line == "\r\n" {
                break;
            }
            let (name, value) = line.split_once(':').expect("HTTP request header");
            if name.eq_ignore_ascii_case("content-length") {
                length = value.trim().parse().expect("HTTP content length");
            }
        }
        let mut body = vec![0; length];
        stream.read_exact(&mut body)?;
        let body = if body.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&body)?
        };
        Ok(Self {
            path,
            body,
            stream: stream.into_inner(),
        })
    }

    fn json(mut self, status: u16, body: Value) {
        let bytes = serde_json::to_vec(&body).expect("fixture response JSON");
        write!(self.stream, "HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", bytes.len()).expect("write response head");
        self.stream.write_all(&bytes).expect("write response body");
    }

    fn unavailable(self) {
        self.json(503, json!({"detail": "no healthy upstream"}));
    }

    fn catalog(self) {
        self.json(200, json!({"data": [{"id": "fixture-model"}]}));
    }

    fn success(self) {
        self.json(
            200,
            json!({"id": "fixture-chat", "choices": [{
                "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"
            }]}),
        );
    }

    fn marker(&self) -> &str {
        self.body["messages"][0]["content"]
            .as_str()
            .expect("chat request marker")
    }

    fn begin_stream(mut self) -> TcpStream {
        self.stream
            .write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n",
            )
            .expect("write stream head");
        self.stream
            .write_all(CONTENT_EVENT.as_bytes())
            .expect("write stream content");
        self.stream
    }
}

fn chat_client(addr: SocketAddr, marker: &str, stream: bool) -> StreamingClient {
    let request = ClientRequest::post(
        "/v1/chat/completions",
        &json!({"model": "fixture-model", "messages": [{"role": "user", "content": marker}], "stream": stream}).to_string(),
    )
    .header("Content-Type", "application/json")
    .header("Connection", "close");
    StreamingClient::send(addr, &request).expect("send chat request")
}

fn response(client: &mut StreamingClient) -> (u16, Vec<u8>) {
    let deadline = Instant::now() + WAIT;
    let mut raw = Vec::new();
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        assert!(
            !remaining.is_zero(),
            "worker did not send a complete response"
        );
        let bytes = client
            .read_some(remaining)
            .expect("read worker HTTP response")
            .expect("worker HTTP response timed out");
        assert!(!bytes.is_empty(), "worker closed an incomplete response");
        raw.extend(bytes);
        if raw.windows(4).any(|window| window == b"\r\n\r\n") {
            let (status, headers, body) = parse_raw_response(&raw);
            let length = headers
                .iter()
                .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                .expect("JSON response has content length")
                .1
                .parse::<usize>()
                .expect("valid response content length");
            if body.len() == length {
                return (status, body);
            }
        }
    }
}

fn error_code(body: &[u8]) -> Value {
    let data: Value = serde_json::from_slice(body).expect("JSON error response");
    data["error"]["code"].clone()
}

fn report_confirmed_outage(upstream: &Fixture, address: SocketAddr) {
    let mut triggering = chat_client(address, "outage", false);
    upstream.call(CHAT_PATH).unavailable();
    assert!(response(&mut triggering).0 >= 500);
}

#[test]
fn confirmed_outage_shares_a_probe_and_releases_only_one_request_until_a_success() {
    let directory = TempDir::new("um-upstream-recovery-");
    let upstream = Fixture::start();
    let worker = upstream.start_worker(&directory, WAIT);
    let address = worker.addr();

    report_confirmed_outage(&upstream, address);
    assert_eq!(
        client_request(address, &ClientRequest::get("/health"))
            .expect("worker health")
            .status,
        200
    );

    let mut first = chat_client(address, "first", false);
    let mut second = chat_client(address, "second", false);
    let catalog = upstream.call(CATALOG_PATH);
    upstream.assert_quiet();
    catalog.catalog();

    let trial = upstream.call(CHAT_PATH);
    upstream.assert_quiet();
    let trial_marker = trial.marker().to_owned();
    trial.success();

    let released = upstream.call(CHAT_PATH);
    let mut markers = [trial_marker.as_str(), released.marker()];
    markers.sort_unstable();
    assert_eq!(markers, ["first", "second"]);
    released.success();
    assert_eq!(response(&mut first).0, 200);
    assert_eq!(response(&mut second).0, 200);
    upstream.assert_quiet();
}

#[test]
fn ordinary_5xx_requires_a_shared_failed_recheck_before_requests_wait() {
    let directory = TempDir::new("um-upstream-recheck-");
    let upstream = Fixture::start();
    let worker = upstream.start_worker(&directory, WAIT);
    let address = worker.addr();

    let mut triggering = chat_client(address, "ordinary failure", false);
    upstream
        .call(CHAT_PATH)
        .json(503, json!({"detail": "temporarily unavailable"}));
    assert!(response(&mut triggering).0 >= 500);

    let recheck = upstream.call(CATALOG_PATH);
    let mut before_confirmation = chat_client(address, "before confirmation", false);
    let business_call = upstream.call(CHAT_PATH);
    assert_eq!(business_call.marker(), "before confirmation");
    business_call.json(503, json!({"detail": "temporarily unavailable"}));
    assert!(response(&mut before_confirmation).0 >= 500);
    upstream.assert_quiet();
    recheck.unavailable();

    let recheck = upstream.call(CATALOG_PATH);
    let mut waiting = chat_client(address, "after confirmation", false);
    upstream.assert_quiet();
    recheck.catalog();
    let trial = upstream.call(CHAT_PATH);
    assert_eq!(trial.marker(), "after confirmation");
    trial.success();
    assert_eq!(response(&mut waiting).0, 200);
}

#[test]
fn a_truncated_stream_is_not_replayed_and_a_valid_recheck_leaves_dispatch_open() {
    let directory = TempDir::new("um-upstream-truncated-");
    let upstream = Fixture::start();
    let worker = upstream.start_worker(&directory, WAIT);
    let address = worker.addr();

    let mut interrupted = chat_client(address, "interrupted", true);
    let stream = upstream.call(CHAT_PATH).begin_stream();
    let raw = interrupted
        .read_until(b"partial", WAIT)
        .expect("read partial output");
    assert!(!raw.is_empty());
    drop(stream);
    let terminal = interrupted.read_all(WAIT).expect("read stream failure");
    let terminal = String::from_utf8_lossy(&terminal);
    assert!(terminal.contains("upstream_eof_without_done"), "{terminal}");
    assert!(
        !terminal.lines().any(|line| line == "data: [DONE]"),
        "{terminal}"
    );
    upstream.call(CATALOG_PATH).catalog();

    let mut first = chat_client(address, "new first", false);
    let mut second = chat_client(address, "new second", false);
    let first_call = upstream.call(CHAT_PATH);
    let second_call = upstream.call(CHAT_PATH);
    let mut markers = [first_call.marker(), second_call.marker()];
    markers.sort_unstable();
    assert_eq!(markers, ["new first", "new second"]);
    first_call.success();
    second_call.success();
    assert_eq!(response(&mut first).0, 200);
    assert_eq!(response(&mut second).0, 200);
    upstream.assert_quiet();
}

#[test]
fn a_request_error_and_a_cancelled_trial_each_release_only_one_next_trial() {
    let directory = TempDir::new("um-upstream-unsuccessful-trial-");
    let upstream = Fixture::start();
    let worker = upstream.start_worker(&directory, WAIT);
    let address = worker.addr();

    report_confirmed_outage(&upstream, address);
    let catalog = upstream.call(CATALOG_PATH);
    let mut rejected = chat_client(address, "rejected trial", false);
    catalog.catalog();
    let rejected_trial = upstream.call(CHAT_PATH);
    assert_eq!(rejected_trial.marker(), "rejected trial");

    let mut cancelled = chat_client(address, "cancelled trial", true);
    upstream.assert_quiet();
    rejected_trial.json(400, json!({"error": {"message": "invalid fixture input"}}));
    assert_eq!(response(&mut rejected).0, 400);

    let cancelled_trial = upstream.call(CHAT_PATH);
    assert_eq!(cancelled_trial.marker(), "cancelled trial");
    let stream = cancelled_trial.begin_stream();
    assert!(!cancelled
        .read_until(b"partial", WAIT)
        .expect("read trial output")
        .is_empty());

    let mut first = chat_client(address, "next trial", false);
    let mut second = chat_client(address, "remaining after trial", false);
    upstream.assert_quiet();
    drop(cancelled);

    let next_trial = upstream.call(CHAT_PATH);
    upstream.assert_quiet();
    let next_marker = next_trial.marker().to_owned();
    next_trial.success();
    drop(stream);
    let remaining = upstream.call(CHAT_PATH);
    let mut markers = [next_marker.as_str(), remaining.marker()];
    markers.sort_unstable();
    assert_eq!(markers, ["next trial", "remaining after trial"]);
    remaining.success();
    assert_eq!(response(&mut first).0, 200);
    assert_eq!(response(&mut second).0, 200);
}

#[test]
fn a_waiter_times_out_with_503_without_sending_its_request_to_um() {
    let directory = TempDir::new("um-upstream-recovery-timeout-");
    let upstream = Fixture::start();
    let budget = Duration::from_millis(1250);
    let worker = upstream.start_worker(&directory, budget);
    let address = worker.addr();

    report_confirmed_outage(&upstream, address);
    let probe = upstream.call(CATALOG_PATH);
    let started = Instant::now();
    let mut waiting = chat_client(address, "must not be sent", false);
    let (status, body) = response(&mut waiting);
    assert_eq!(status, 503);
    assert_eq!(error_code(&body), json!("upstream_recovery_timeout"));
    let elapsed = started.elapsed();
    assert!(
        elapsed >= budget.saturating_sub(QUIET),
        "returned early: {elapsed:?}"
    );
    assert!(
        elapsed < budget + Duration::from_secs(1),
        "exceeded original budget: {elapsed:?}"
    );
    upstream.assert_quiet();
    probe.catalog();
    upstream.assert_quiet();
}

#[test]
fn a_failed_trial_waits_for_another_catalog_and_skips_a_cancelled_waiter() {
    let directory = TempDir::new("um-upstream-failed-trial-");
    let upstream = Fixture::start();
    let worker = upstream.start_worker(&directory, WAIT);
    let address = worker.addr();

    report_confirmed_outage(&upstream, address);
    let catalog = upstream.call(CATALOG_PATH);
    let mut failed = chat_client(address, "failed trial", true);
    catalog.catalog();
    let failed_trial = upstream.call(CHAT_PATH);
    assert_eq!(failed_trial.marker(), "failed trial");
    let stream = failed_trial.begin_stream();
    assert!(!failed
        .read_until(b"partial", WAIT)
        .expect("read failed trial output")
        .is_empty());

    let cancelled = chat_client(address, "cancelled in queue", false);
    upstream.assert_quiet();
    drop(cancelled);
    let mut next = chat_client(address, "next trial", false);
    let mut last = chat_client(address, "remaining after recovery", false);
    upstream.assert_quiet();
    drop(stream);
    let terminal = failed.read_all(WAIT).expect("read trial stream failure");
    assert!(String::from_utf8_lossy(&terminal).contains("upstream_eof_without_done"));

    let catalog = upstream.call(CATALOG_PATH);
    upstream.assert_quiet();
    catalog.catalog();
    let trial = upstream.call(CHAT_PATH);
    upstream.assert_quiet();
    let marker = trial.marker().to_owned();
    trial.success();
    let remaining = upstream.call(CHAT_PATH);
    let mut markers = [marker.as_str(), remaining.marker()];
    markers.sort_unstable();
    assert_eq!(markers, ["next trial", "remaining after recovery"]);
    remaining.success();
    assert_eq!(response(&mut next).0, 200);
    assert_eq!(response(&mut last).0, 200);
    upstream.assert_quiet();
}

#[test]
fn an_upstream_401_preserves_the_existing_authentication_response() {
    let directory = TempDir::new("um-upstream-auth-rejection-");
    let upstream = Fixture::start();
    let worker = upstream.start_worker(&directory, WAIT);
    let address = worker.addr();

    let mut rejected = chat_client(address, "authentication rejected", false);
    upstream
        .call(CHAT_PATH)
        .json(401, json!({"detail": "Not authenticated"}));
    let (status, body) = response(&mut rejected);
    assert_eq!(status, 503);
    assert_eq!(error_code(&body), json!("reauthentication_required"));
    upstream.assert_quiet();
}

#[test]
fn ingress_propagates_waiter_cancellation_and_limits_the_first_release_batch() {
    let directory = TempDir::new("um-upstream-ingress-wait-");
    let upstream = Fixture::start();
    let _worker = upstream.start_worker(&directory, Duration::from_secs(15));
    let desired = directory.write("desired.json", r#"{"state":"running","boot_id":"fixture"}"#);
    let ingress = IngressServer::start(IngressConfig {
        worker_port_file: directory.path().join("worker.port"),
        desired_state_file: Some(desired),
        boot_id: Some("fixture".to_owned()),
        ..IngressConfig::default()
    })
    .unwrap();
    let address = ingress.addr();

    report_confirmed_outage(&upstream, address);
    let catalog = upstream.call(CATALOG_PATH);
    let cancelled = chat_client(address, "cancelled before forwarding", false);
    upstream.assert_quiet();
    drop(cancelled);
    upstream.assert_quiet();

    let mut trial = chat_client(address, "release trial", false);
    catalog.catalog();
    let trial_call = upstream.call(CHAT_PATH);
    assert_eq!(trial_call.marker(), "release trial");

    let markers = ["alpha", "bravo", "charlie", "delta"];
    let mut waiting = markers.map(|marker| chat_client(address, marker, false));
    upstream.assert_quiet();
    assert_eq!(
        client_request(address, &ClientRequest::get("/health"))
            .unwrap()
            .status,
        200
    );
    trial_call.success();

    let first = upstream.call(CHAT_PATH);
    let second = upstream.call(CHAT_PATH);
    let mut forwarded = vec![first.marker().to_owned(), second.marker().to_owned()];
    upstream.assert_quiet();
    first.success();

    let third = upstream.call(CHAT_PATH);
    forwarded.push(third.marker().to_owned());
    upstream.assert_quiet();
    second.success();

    let last = upstream.call(CHAT_PATH);
    forwarded.push(last.marker().to_owned());
    third.success();
    last.success();

    forwarded.sort();
    assert_eq!(forwarded, markers);
    assert_eq!(response(&mut trial).0, 200);
    for client in &mut waiting {
        assert_eq!(response(client).0, 200);
    }
    upstream.assert_quiet();
}

#[test]
fn the_shared_wait_queue_rejects_overflow_before_any_request_is_sent() {
    let directory = TempDir::new("um-upstream-wait-capacity-");
    let upstream = Fixture::start();
    let worker = upstream.start_worker(&directory, Duration::from_secs(20));
    let address = worker.addr();
    report_confirmed_outage(&upstream, address);
    let catalog = upstream.call(CATALOG_PATH);

    let mut waiting: Vec<_> = (0..=64)
        .map(|_| chat_client(address, "waiting for capacity", false))
        .collect();
    let deadline = Instant::now() + WAIT;
    let overflow = 'response: loop {
        for client in &mut waiting {
            if client
                .read_some(Duration::from_millis(2))
                .unwrap()
                .is_some()
            {
                break 'response client
                    .read_until(b"upstream_recovery_queue_full", WAIT)
                    .unwrap();
            }
        }
        assert!(
            Instant::now() < deadline,
            "no waiter received the queue limit error"
        );
    };
    assert!(overflow.starts_with(b"HTTP/1.1 503 "));
    assert!(String::from_utf8_lossy(&overflow).contains("upstream_recovery_queue_full"));
    upstream.assert_quiet();
    drop(waiting);
    upstream.assert_quiet();
    catalog.catalog();
}
