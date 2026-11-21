//! #133 A: the local 8000 ingress survives a worker restart.
//!
//! The ingress is a separate process from the model worker. It keeps
//! `127.0.0.1:8000` listening while the worker is down, queues only requests
//! that were never forwarded, and forwards each one exactly once after the worker
//! becomes connectable. These tests use real TCP sockets and, for at least one
//! case, the real `GatewayServer` as the worker.

mod support;

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::json;

use support::*;
use um_gateway::config::{GatewayConfig, Role, RunMode};
use um_gateway::ingress::{start_worker, IngressConfig, IngressHandle, IngressServer};

const BOOT_ID: &str = "boot-fixture";

struct Ingress {
    handle: IngressHandle,
    addr: SocketAddr,
}

impl Drop for Ingress {
    fn drop(&mut self) {
        self.handle.shutdown();
    }
}

fn running_state_json() -> String {
    format!(r#"{{"state":"running","boot_id":"{BOOT_ID}"}}"#)
}

fn start_ingress_with(
    temp: &TempDir,
    worker_port_file: &Path,
    desired_state_file: Option<&Path>,
    configure: impl FnOnce(&mut IngressConfig),
) -> Ingress {
    let control = temp.write("ingress-control.token", CONTROL_TOKEN);
    let mut config = IngressConfig {
        worker_port_file: worker_port_file.to_path_buf(),
        desired_state_file: desired_state_file.map(Path::to_path_buf),
        boot_id: Some(BOOT_ID.to_string()),
        recovery_wait: Duration::from_secs(1),
        control_token_file: Some(control),
        ..IngressConfig::default()
    };
    configure(&mut config);
    let handle = IngressServer::start(config).expect("start ingress");
    Ingress {
        addr: handle.addr(),
        handle,
    }
}

fn start_ingress_with_state(
    temp: &TempDir,
    worker_port: u16,
    desired_state: &str,
    configure: impl FnOnce(&mut IngressConfig),
) -> Ingress {
    let port_file = temp.path().join("worker.port");
    std::fs::write(&port_file, format!("{worker_port}\n")).expect("write worker port file");
    let desired = temp.write("desired-state.json", desired_state);
    start_ingress_with(temp, &port_file, Some(&desired), configure)
}

fn start_ingress_without_state(temp: &TempDir, worker_port: u16) -> Ingress {
    let port_file = temp.path().join("worker.port");
    std::fs::write(&port_file, format!("{worker_port}\n")).expect("write worker port file");
    start_ingress_with(temp, &port_file, None, |_| {})
}

fn reserve_port() -> u16 {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("reserve worker port");
    listener.local_addr().expect("reserve worker addr").port()
}

fn chat_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        r#"{"model":"fixture-model-a","messages":[{"role":"user","content":"hi"}]}"#,
    )
    .json_body(r#"{"model":"fixture-model-a","messages":[{"role":"user","content":"hi"}]}"#)
}

#[derive(Clone)]
struct SeenFakeRequest {
    method: String,
    target: String,
    body: Vec<u8>,
}

#[derive(Clone)]
enum FakeBehaviour {
    Json { status: u16, body: Vec<u8> },
    CloseAfterRequest,
    ChunkedSse { chunks: Vec<(Duration, Vec<u8>)> },
}

struct FakeWorker {
    port: u16,
    shutdown: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    seen: Arc<Mutex<Vec<SeenFakeRequest>>>,
}

impl FakeWorker {
    fn start(port: u16, behaviour: FakeBehaviour) -> Self {
        Self::start_after(port, Duration::ZERO, behaviour)
    }

    fn start_after(port: u16, delay: Duration, behaviour: FakeBehaviour) -> Self {
        let shutdown = Arc::new(AtomicBool::new(false));
        let seen = Arc::new(Mutex::new(Vec::new()));
        let worker_shutdown = Arc::clone(&shutdown);
        let worker_seen = Arc::clone(&seen);
        let handle = thread::spawn(move || {
            if !delay.is_zero() {
                thread::sleep(delay);
            }
            if worker_shutdown.load(Ordering::SeqCst) {
                return;
            }
            let listener = match TcpListener::bind(("127.0.0.1", port)) {
                Ok(listener) => listener,
                Err(_) => return,
            };
            listener
                .set_nonblocking(true)
                .expect("fake worker nonblocking");
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let seen = Arc::clone(&worker_seen);
                        let behaviour = behaviour.clone();
                        thread::spawn(move || {
                            let _ = serve_fake_worker(stream, &behaviour, &seen);
                        });
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            port,
            shutdown,
            handle: Some(handle),
            seen,
        }
    }

    fn seen(&self) -> Vec<SeenFakeRequest> {
        self.seen.lock().expect("fake worker seen").clone()
    }
}

impl Drop for FakeWorker {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            self.shutdown.store(true, Ordering::SeqCst);
            let addr = SocketAddr::from(([127, 0, 0, 1], self.port));
            let _ = TcpStream::connect_timeout(&addr, Duration::from_millis(200));
            let _ = handle.join();
        }
    }
}

fn serve_fake_worker(
    mut stream: TcpStream,
    behaviour: &FakeBehaviour,
    seen: &Mutex<Vec<SeenFakeRequest>>,
) -> std::io::Result<()> {
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    let request = match read_fake_request(&mut stream) {
        Ok(request) => request,
        Err(_) => return Ok(()),
    };
    seen.lock().expect("fake worker seen").push(request.clone());
    match behaviour {
        FakeBehaviour::Json { status, body } => {
            let head = format!(
                "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n\
                 Content-Length: {}\r\nConnection: keep-alive\r\n\r\n",
                body.len()
            );
            stream.write_all(head.as_bytes())?;
            stream.write_all(body)?;
            stream.flush()?;
        }
        FakeBehaviour::CloseAfterRequest => {}
        FakeBehaviour::ChunkedSse { chunks } => {
            stream.write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\
                  Transfer-Encoding: chunked\r\n\r\n",
            )?;
            stream.flush()?;
            for (delay, bytes) in chunks {
                thread::sleep(*delay);
                let frame = format!("{:X}\r\n", bytes.len());
                stream.write_all(frame.as_bytes())?;
                stream.write_all(bytes)?;
                stream.write_all(b"\r\n")?;
                stream.flush()?;
            }
            stream.write_all(b"0\r\n\r\n")?;
            stream.flush()?;
        }
    }
    Ok(())
}

fn read_fake_request(stream: &mut TcpStream) -> std::io::Result<SeenFakeRequest> {
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                "fake worker request ended",
            ));
        }
        raw.extend_from_slice(&buffer[..read]);
    };
    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let target = parts.next().unwrap_or_default().to_string();
    let mut length = 0usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            if name.trim().eq_ignore_ascii_case("Content-Length") {
                length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = raw[header_end..].to_vec();
    while body.len() < length {
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        body.extend_from_slice(&buffer[..read]);
    }
    body.truncate(length);
    Ok(SeenFakeRequest {
        method,
        target,
        body,
    })
}

fn write_request(stream: &mut TcpStream, addr: SocketAddr, target: &str, body: &str) {
    let raw = format!(
        "POST {target} HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\n\r\n{}",
        body.len(),
        body
    );
    stream.write_all(raw.as_bytes()).expect("write request");
    stream.flush().expect("flush request");
}

fn error_code(response: &ClientResponse) -> String {
    response.json()["error"]["code"]
        .as_str()
        .unwrap_or_default()
        .to_string()
}

fn error_message(response: &ClientResponse) -> String {
    response.json()["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .to_string()
}

#[test]
fn worker_mode_writes_the_port_file_and_forwards_through_the_real_gateway() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({"id": "chatcmpl-fixture", "choices": []}),
    });
    let control = temp.write("worker-control.token", CONTROL_TOKEN);
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        control_token_file: Some(control),
        role: Role::Local,
        run_mode: RunMode::Worker,
        candidate: true,
        ..GatewayConfig::default()
    };
    let port_file = temp.path().join("worker.port");
    let worker = start_worker(config, &port_file).expect("start worker");
    let written = std::fs::read_to_string(&port_file).expect("worker port file");
    assert_eq!(
        written,
        format!("{}\n{}\n", worker.addr().port(), std::process::id())
    );
    let leftovers: Vec<String> = std::fs::read_dir(temp.path())
        .expect("worker temp dir")
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.contains(".tmp-"))
        .collect();
    assert!(leftovers.is_empty(), "atomic write left {leftovers:?}");

    let ingress =
        start_ingress_with_state(&temp, worker.addr().port(), &running_state_json(), |_| {});
    let response = client_request(ingress.addr, &chat_request()).expect("chat through ingress");
    assert_eq!(response.status, 200);
    assert_eq!(stub.attempts(), 1);
    drop(worker);
}

#[test]
fn two_concurrent_requests_wait_and_forward_once_after_worker_recovers() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let addr = ingress.addr;
    let first = thread::spawn(move || {
        client_request_with_timeout(addr, &chat_request(), Duration::from_secs(5))
    });
    let second = thread::spawn(move || {
        client_request_with_timeout(addr, &chat_request(), Duration::from_secs(5))
    });
    wait_until(Duration::from_secs(2), || {
        ingress.handle.state().queued_requests() == 2
    });
    let worker = FakeWorker::start_after(
        port,
        Duration::from_millis(500),
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"ok":true}"#.to_vec(),
        },
    );
    let first_response = first.join().expect("first client").expect("first response");
    let second_response = second
        .join()
        .expect("second client")
        .expect("second response");
    assert_eq!(first_response.status, 200);
    assert_eq!(second_response.status, 200);
    assert_eq!(worker.seen().len(), 2);
    assert_eq!(worker.seen()[0].method, "POST");
    assert_eq!(worker.seen()[0].target, "/v1/chat/completions");
    assert!(String::from_utf8_lossy(&worker.seen()[0].body).contains("fixture-model-a"));
}

#[test]
fn recovery_wait_timeout_releases_a_retryable_503() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(1);
    });
    let started = Instant::now();
    let response = client_request(ingress.addr, &chat_request()).expect("timed out request");
    assert!(started.elapsed() >= Duration::from_millis(900));
    assert_eq!(response.status, 503);
    assert_eq!(error_code(&response), "gateway_recovery_timeout");
    assert!(error_message(&response).contains("gateway_recovery_timeout"));
    assert_eq!(response.header("Retry-After"), None);
    assert_eq!(response.header("X-Should-Retry"), Some("false"));
    assert_eq!(response.header("Cache-Control"), Some("no-store"));
    assert!(response.header("X-Request-Id").is_some());
    assert_eq!(
        response.json()["error"]["detail"]["forwarded"],
        json!(false)
    );
}

#[test]
fn cancelled_request_is_removed_and_never_forwarded() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let addr = ingress.addr;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2)).expect("connect");
    write_request(&mut stream, addr, "/v1/chat/completions", "{}");
    wait_until(Duration::from_secs(2), || {
        ingress.handle.state().enqueued_total() == 1
    });
    drop(stream);
    wait_until(Duration::from_secs(2), || {
        ingress.handle.state().queued_requests() == 0
    });
    let worker = FakeWorker::start(
        port,
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"ok":true}"#.to_vec(),
        },
    );
    thread::sleep(Duration::from_millis(300));
    assert!(worker.seen().is_empty());
}

#[test]
fn explicit_stopped_intent_does_not_forward_or_queue() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let stopped = format!(r#"{{"state":"stopped","boot_id":"{BOOT_ID}"}}"#);
    let ingress = start_ingress_with_state(&temp, port, &stopped, |_| {});
    let started = Instant::now();
    let response = client_request(ingress.addr, &chat_request()).expect("stopped request");
    assert!(started.elapsed() < Duration::from_millis(900));
    assert_eq!(response.status, 503);
    assert_eq!(error_code(&response), "gateway_stopped");
    assert!(error_message(&response).contains("gateway_stopped"));
    assert_eq!(response.header("X-Should-Retry"), Some("false"));
    assert_eq!(
        response.json()["error"]["detail"]["forwarded"],
        json!(false)
    );
    let worker = FakeWorker::start(
        port,
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"ok":true}"#.to_vec(),
        },
    );
    thread::sleep(Duration::from_millis(200));
    assert!(worker.seen().is_empty());
}

#[test]
fn missing_or_mismatched_desired_state_returns_gateway_not_running() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_without_state(&temp, port);
    let response = client_request(ingress.addr, &chat_request()).expect("missing state request");
    assert_eq!(response.status, 503);
    assert_eq!(error_code(&response), "gateway_not_running");
    assert!(error_message(&response).contains("gateway_not_running"));
    assert_eq!(
        response.json()["error"]["detail"]["forwarded"],
        json!(false)
    );

    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let mismatched = format!(r#"{{"state":"running","boot_id":"other-boot"}}"#);
    let ingress = start_ingress_with_state(&temp, port, &mismatched, |_| {});
    let response = client_request(ingress.addr, &chat_request()).expect("mismatched boot request");
    assert_eq!(response.status, 503);
    assert_eq!(error_code(&response), "gateway_not_running");
    assert!(error_message(&response).contains("gateway_not_running"));
}

#[test]
fn admin_reads_do_not_wait_for_the_worker() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let started = Instant::now();
    let response = client_request(
        ingress.addr,
        &ClientRequest::get("/admin/auth/status").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("admin read without worker");
    assert!(started.elapsed() < Duration::from_millis(900));
    assert_eq!(response.status, 503);
    assert_eq!(error_code(&response), "gateway_not_running");
    assert!(error_message(&response).contains("gateway_not_running"));
    assert_eq!(response.header("X-Should-Retry"), Some("false"));
    assert_eq!(
        response.json()["error"]["detail"]["forwarded"],
        json!(false)
    );

    let worker = FakeWorker::start(
        port,
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"state":"fixture"}"#.to_vec(),
        },
    );
    thread::sleep(Duration::from_millis(300));
    assert!(worker.seen().is_empty());
}

#[test]
fn queue_count_limit_returns_a_retryable_queue_full() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
        config.queue_max = 1;
    });
    let addr = ingress.addr;
    let first = thread::spawn(move || {
        client_request_with_timeout(addr, &chat_request(), Duration::from_secs(5))
    });
    wait_until(Duration::from_secs(2), || {
        ingress.handle.state().queued_requests() == 1
    });
    let response = client_request(ingress.addr, &chat_request()).expect("second request");
    assert_eq!(response.status, 503);
    assert_eq!(error_code(&response), "gateway_recovery_queue_full");
    assert!(error_message(&response).contains("gateway_recovery_queue_full"));
    assert_eq!(response.header("Retry-After"), Some("5"));
    assert_eq!(response.header("X-Should-Retry"), Some("true"));
    assert_eq!(
        response.json()["error"]["detail"]["forwarded"],
        json!(false)
    );

    let worker = FakeWorker::start(
        port,
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"ok":true}"#.to_vec(),
        },
    );
    let first_response = first.join().expect("first client").expect("first response");
    assert_eq!(first_response.status, 200);
    assert_eq!(worker.seen().len(), 1);
}

#[test]
fn queue_byte_limit_returns_a_retryable_queue_full() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
        config.queue_max = 8;
        config.queue_max_bytes = 16;
        config.max_request_bytes = 1024;
    });
    let body = r#"{"model":"fixture-model-a","messages":[{"role":"user","content":"hi"}]}"#;
    let response = client_request(
        ingress.addr,
        &ClientRequest::post("/v1/chat/completions", body).json_body(body),
    )
    .expect("oversized queued request");
    assert_eq!(response.status, 503);
    assert_eq!(error_code(&response), "gateway_recovery_queue_full");
    assert!(error_message(&response).contains("gateway_recovery_queue_full"));
    assert_eq!(response.header("Retry-After"), Some("5"));
    assert_eq!(response.header("X-Should-Retry"), Some("true"));
    assert_eq!(
        response.json()["error"]["detail"]["forwarded"],
        json!(false)
    );
}

#[test]
fn a_forwarded_request_is_not_resent_when_the_worker_disconnects() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let worker = FakeWorker::start(port, FakeBehaviour::CloseAfterRequest);
    let response = client_request(ingress.addr, &chat_request()).expect("worker lost request");
    assert_eq!(response.status, 502);
    assert_eq!(error_code(&response), "gateway_worker_lost");
    assert!(error_message(&response).contains("gateway_worker_lost"));
    assert_eq!(response.header("X-Should-Retry"), Some("false"));
    assert_eq!(response.json()["error"]["detail"]["forwarded"], json!(true));
    assert_eq!(worker.seen().len(), 1);
    thread::sleep(Duration::from_millis(300));
    assert_eq!(worker.seen().len(), 1);
}

#[test]
fn sse_response_is_streamed_before_the_worker_finishes() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let worker = FakeWorker::start(
        port,
        FakeBehaviour::ChunkedSse {
            chunks: vec![
                (Duration::ZERO, b"data: first\n\n".to_vec()),
                (Duration::from_millis(400), b"data: second\n\n".to_vec()),
            ],
        },
    );
    let mut client =
        StreamingClient::send(ingress.addr, &chat_request()).expect("streaming client");
    let started = Instant::now();
    let first = client
        .read_until(b"data: first\n\n", Duration::from_secs(1))
        .expect("first SSE event");
    assert!(
        first.windows(13).any(|window| window == b"data: first\n\n"),
        "first event: {}",
        String::from_utf8_lossy(&first)
    );
    assert!(
        started.elapsed() < Duration::from_millis(300),
        "the first event waited for the whole response: {:?}",
        started.elapsed()
    );
    let rest = client
        .read_until(b"data: second", Duration::from_secs(2))
        .expect("second SSE event");
    assert!(
        rest.windows(12).any(|window| window == b"data: second"),
        "second event: {}",
        String::from_utf8_lossy(&rest)
    );
    assert_eq!(worker.seen().len(), 1);
}

#[test]
fn keep_alive_serves_two_requests_on_one_client_connection() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let worker = FakeWorker::start(
        port,
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"ok":true}"#.to_vec(),
        },
    );
    let addr = ingress.addr;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2)).expect("connect");
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    write_request(&mut stream, addr, "/v1/chat/completions", "{}");
    let first = read_client_response(&mut stream).expect("first response");
    assert_eq!(first.status, 200);
    write_request(&mut stream, addr, "/v1/chat/completions", "{}");
    let second = read_client_response(&mut stream).expect("second response");
    assert_eq!(second.status, 200);
    assert_eq!(worker.seen().len(), 2);
}

#[test]
fn idle_keep_alive_timeout_closes_without_a_synthetic_400() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
        config.head_read_timeout = Duration::from_millis(200);
    });
    let worker = FakeWorker::start(
        port,
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"ok":true}"#.to_vec(),
        },
    );
    let addr = ingress.addr;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2)).expect("connect");
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    write_request(&mut stream, addr, "/v1/chat/completions", "{}");
    let first = read_client_response(&mut stream).expect("first response");
    assert_eq!(first.status, 200);

    // Stay idle past the head read timeout. The ingress must close the
    // keep-alive connection without inventing a 400 for a request that was
    // never sent.
    thread::sleep(Duration::from_millis(400));
    let mut buffer = Vec::new();
    let read = stream.read_to_end(&mut buffer);
    assert!(
        read.is_ok(),
        "an idle keep-alive connection was not closed cleanly: {read:?}"
    );
    assert!(
        buffer.is_empty(),
        "an idle keep-alive timeout received a response: {}",
        String::from_utf8_lossy(&buffer)
    );
    assert_eq!(worker.seen().len(), 1);
}

#[test]
fn health_and_ready_proxy_the_worker() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let health = client_request(ingress.addr, &ClientRequest::get("/health")).expect("health down");
    assert_eq!(health.status, 503);
    let ready = client_request(ingress.addr, &ClientRequest::get("/ready")).expect("ready down");
    assert_eq!(ready.status, 503);

    let worker = FakeWorker::start(
        port,
        FakeBehaviour::Json {
            status: 200,
            body: br#"{"status":"ok"}"#.to_vec(),
        },
    );
    let health = client_request(ingress.addr, &ClientRequest::get("/health")).expect("health up");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["status"], json!("ok"));
    let ready = client_request(ingress.addr, &ClientRequest::get("/ready")).expect("ready up");
    assert_eq!(ready.status, 200);
    assert_eq!(worker.seen().len(), 2);
}

#[test]
fn admin_shutdown_releases_queued_requests() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let addr = ingress.addr;
    let client = thread::spawn(move || {
        client_request_with_timeout(addr, &chat_request(), Duration::from_secs(5))
    });
    wait_until(Duration::from_secs(2), || {
        ingress.handle.state().queued_requests() == 1
    });
    let shutdown = client_request(
        ingress.addr,
        &ClientRequest::post("/admin/shutdown", "{}")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body("{}"),
    )
    .expect("admin shutdown");
    assert_eq!(shutdown.status, 202);
    let queued = client
        .join()
        .expect("queued client")
        .expect("queued response");
    assert_eq!(queued.status, 503);
    assert_eq!(error_code(&queued), "gateway_stopped");
    assert!(error_message(&queued).contains("gateway_stopped"));
    assert!(ingress.handle.shutdown_request().load(Ordering::SeqCst));
}

#[test]
fn messages_errors_use_the_anthropic_shape() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(1);
    });
    let body =
        r#"{"model":"fixture-model-a","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}"#;
    let response = client_request(
        ingress.addr,
        &ClientRequest::post("/v1/messages", body).json_body(body),
    )
    .expect("messages timeout");
    assert_eq!(response.status, 503);
    assert_eq!(response.json()["type"], json!("error"));
    assert_eq!(
        response.json()["error"]["code"],
        json!("gateway_recovery_timeout")
    );
    assert!(error_message(&response).contains("gateway_recovery_timeout"));
    assert_eq!(response.header("Retry-After"), None);
    assert_eq!(response.header("X-Should-Retry"), Some("false"));
    assert_eq!(response.header("Cache-Control"), Some("no-store"));
    assert!(response.header("X-Request-Id").is_some());
}

#[test]
fn worker_config_allows_ephemeral_and_non_governed_high_ports() {
    for port in [0u16, 49152, 50000] {
        let config = GatewayConfig {
            bind_port: port,
            run_mode: RunMode::Worker,
            candidate: false,
            upstream_base_url: "http://127.0.0.1:8000".to_string(),
            ..GatewayConfig::default()
        };
        config
            .validate_bind_policy()
            .unwrap_or_else(|error| panic!("worker must allow {port}: {error}"));
    }
    for port in [8000u16, 8001, 1023] {
        let config = GatewayConfig {
            bind_port: port,
            run_mode: RunMode::Worker,
            candidate: false,
            upstream_base_url: "http://127.0.0.1:8000".to_string(),
            ..GatewayConfig::default()
        };
        let error = config
            .validate_bind_policy()
            .expect_err("worker must reject a governed or privileged port");
        assert!(
            error.to_string().contains(&port.to_string()),
            "{port}: {error}"
        );
    }
}

#[test]
fn ingress_config_only_binds_8000_or_an_ephemeral_loopback_port() {
    IngressConfig::default()
        .validate()
        .expect("ephemeral ingress");
    IngressConfig {
        bind_port: 8000,
        ..IngressConfig::default()
    }
    .validate()
    .expect("8000 ingress");
    let error = IngressConfig {
        bind_port: 8001,
        ..IngressConfig::default()
    }
    .validate()
    .expect_err("ingress must reject 8001");
    assert!(error.to_string().contains("8001"), "{error}");
    let error = IngressConfig {
        bind_host: "0.0.0.0".to_string(),
        ..IngressConfig::default()
    }
    .validate()
    .expect_err("ingress must reject a non-loopback host");
    assert!(error.to_string().contains("loopback"), "{error}");
}

#[test]
fn admin_shutdown_rejects_a_bad_token_and_keeps_running() {
    let temp = TempDir::new(unique_temp_prefix());
    let port = reserve_port();
    let ingress = start_ingress_with_state(&temp, port, &running_state_json(), |config| {
        config.recovery_wait = Duration::from_secs(5);
    });
    let response = client_request(
        ingress.addr,
        &ClientRequest::post("/admin/shutdown", "{}")
            .header("X-Admin-Control", "wrong-token")
            .json_body("{}"),
    )
    .expect("admin shutdown with a bad token");
    assert_eq!(response.status, 403);
    assert_eq!(error_code(&response), "control_denied_token");
    assert!(!ingress.handle.state().is_stopping());
    assert!(!ingress.handle.shutdown_request().load(Ordering::SeqCst));
}
