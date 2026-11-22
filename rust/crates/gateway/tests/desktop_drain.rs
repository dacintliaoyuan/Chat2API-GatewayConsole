mod support;

use std::net::{SocketAddr, TcpListener};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use serde_json::json;
use support::{
    client_request, start_chat_gateway, ChatAttempt, ChatChunk, ChatStub, ClientRequest,
    StreamingClient, TempDir, CONTROL_TOKEN,
};
use um_gateway::config::{GatewayConfig, Role};

const PATH: &str = "/admin/desktop/port-switch";
const LEASE: &str = "5b7d9984f5014395973579ffe91ae2ab";
const OTHER_LEASE: &str = "d6392d4978e54d12a75f3f5e239042aa";

struct ChildGuard(Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn free_addr() -> SocketAddr {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("free port");
    listener.local_addr().expect("free addr")
}

fn control(addr: SocketAddr, action: &str, lease: &str, token: &str) -> support::ClientResponse {
    client_request(
        addr,
        &ClientRequest::post(PATH, "")
            .header("X-Admin-Control", token)
            .header("X-Desktop-Port-Switch", lease)
            .header("X-Desktop-Port-Switch-Action", action),
    )
    .expect("desktop port switch reply")
}

fn shutdown(addr: SocketAddr) -> support::ClientResponse {
    client_request(
        addr,
        &ClientRequest::post("/admin/shutdown", "").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("desktop shutdown reply")
}

fn chat_request(stream: bool) -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        })
        .to_string(),
    )
    .header("Content-Type", "application/json")
}

fn success_body() -> serde_json::Value {
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

/// A real production desktop gateway process on one isolated loopback port.
struct DesktopGateway {
    process: ChildGuard,
    addr: SocketAddr,
    _temp: TempDir,
}

impl DesktopGateway {
    fn start(temp: TempDir, stub: &ChatStub, addr: SocketAddr) -> Self {
        let token = temp.write("control.token", CONTROL_TOKEN);
        let stderr = std::fs::File::create(temp.path().join("gateway-stderr.log"))
            .expect("gateway stderr file");
        let mut command = Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"));
        command
            .args(["--mode", "production", "--desktop-instance", "--no-browser"])
            .args(["--port", &addr.port().to_string()])
            .args(["--upstream-base-url", &stub.url()])
            .arg("--control-token-file")
            .arg(token)
            .current_dir(temp.path())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::from(stderr));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000);
        }
        let mut gateway = Self {
            process: ChildGuard(command.spawn().expect("start isolated desktop gateway")),
            addr,
            _temp: temp,
        };
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            if let Ok(health) = client_request(addr, &ClientRequest::get("/health")) {
                assert_eq!(
                    (health.status, health.json()["status"].clone()),
                    (200, json!("ok"))
                );
                break;
            }
            assert!(
                gateway
                    .process
                    .0
                    .try_wait()
                    .expect("wait gateway")
                    .is_none(),
                "gateway exited before listening"
            );
            assert!(
                Instant::now() < deadline,
                "gateway did not listen on the isolated port"
            );
            std::thread::sleep(Duration::from_millis(50));
        }
        gateway
    }

    fn wait_for_exit(&mut self, timeout: Duration) -> ExitStatus {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = self.process.0.try_wait().expect("wait gateway") {
                return status;
            }
            assert!(Instant::now() < deadline, "gateway did not stop in time");
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

#[test]
fn desktop_port_switch_finishes_the_admitted_sse_before_stopping() {
    let temp = TempDir::new("um-desktop-drain-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"first-token\"},\"finish_reason\":null}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::after(
                Duration::from_secs(2),
                b"data: {\"choices\":[{\"delta\":{\"content\":\"last-token\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
                    .to_vec(),
            ),
        ],
        hold: Duration::ZERO,
    });
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.role = Role::Local;
        config.desktop_instance = true;
        config.socket_silence_timeout = Duration::from_secs(5);
    });
    let request = ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": true,
        })
        .to_string(),
    )
    .header("Content-Type", "application/json");
    let mut stream = StreamingClient::send(gateway.addr, &request).unwrap();
    let head = stream
        .read_until(b"first-token", Duration::from_secs(5))
        .unwrap();
    assert!(String::from_utf8_lossy(&head).contains("first-token"));

    let reply = control(gateway.addr, "wait", LEASE, "");
    assert_eq!(reply.status, 403);
    let reply = control(gateway.addr, "wait", LEASE, CONTROL_TOKEN);
    assert_eq!((reply.status, reply.json()), (200, json!({"in_flight": 1})));
    let reply = control(gateway.addr, "cancel", OTHER_LEASE, CONTROL_TOKEN);
    assert_eq!(reply.status, 409);
    let reply = control(gateway.addr, "commit", LEASE, CONTROL_TOKEN);
    assert_eq!(reply.status, 409);
    assert!(!gateway
        .server
        .state()
        .shutdown_request
        .load(Ordering::SeqCst));

    let refused = client_request(gateway.addr, &request).unwrap();
    assert_eq!(refused.status, 503);
    assert_eq!(
        refused.json()["error"]["code"],
        "desktop_port_switch_in_progress"
    );
    let health = client_request(gateway.addr, &ClientRequest::get("/health")).unwrap();
    assert_eq!(
        (health.status, health.json()["status"].clone()),
        (200, json!("ok"))
    );
    assert_eq!(stub.attempts(), 1);

    let tail = stream
        .read_until(b"[DONE]", Duration::from_secs(5))
        .unwrap();
    assert!(String::from_utf8_lossy(&tail).contains("last-token"));
    assert!(String::from_utf8_lossy(&tail).contains("[DONE]"));
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    loop {
        let reply = control(gateway.addr, "wait", LEASE, CONTROL_TOKEN);
        if reply.json()["in_flight"] == 0 {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "finished stream still counted"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let reply = control(gateway.addr, "commit", LEASE, CONTROL_TOKEN);
    assert_eq!(
        (reply.status, reply.json()),
        (202, json!({"status": "stopping"}))
    );
}

#[test]
fn desktop_mode_accepts_custom_loopback_ports_and_keeps_shared_ports_reserved() {
    let mut config = GatewayConfig {
        candidate: false,
        desktop_instance: true,
        role: Role::Local,
        bind_port: 7995,
        upstream_base_url: "http://127.0.0.1:9999".into(),
        control_token_file: Some("fixture-token".into()),
        ..GatewayConfig::default()
    };
    config.validate_bind_policy().unwrap();
    for port in [0, 5173, 5901, 8000, 8009, 8090, 8119, 8312, 18096] {
        config.bind_port = port;
        assert!(
            config.validate_bind_policy().is_err(),
            "reserved port {port}"
        );
    }
    config.bind_port = 7995;
    config.bind_host = "0.0.0.0".into();
    assert!(config.validate_bind_policy().is_err());
    config.bind_host = "127.0.0.1".into();
    config.desktop_instance = false;
    assert!(config.validate_bind_policy().is_err());
}

#[test]
fn production_desktop_binary_starts_and_drains_on_an_isolated_custom_port() {
    let temp = TempDir::new("um-desktop-custom-port-");
    let stub = ChatStub::start();
    let mut gateway = DesktopGateway::start(temp, &stub, free_addr());
    let reply = control(gateway.addr, "wait", LEASE, CONTROL_TOKEN);
    assert_eq!((reply.status, reply.json()), (200, json!({"in_flight": 0})));
    assert_eq!(
        control(gateway.addr, "commit", LEASE, CONTROL_TOKEN).status,
        202
    );
    let status = gateway.wait_for_exit(Duration::from_secs(5));
    assert!(status.success(), "unexpected exit: {status}");
}

#[test]
fn production_desktop_binary_finishes_a_real_sse_then_restarts_on_the_new_port() {
    let temp = TempDir::new("um-desktop-real-switch-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"first-token\"},\"finish_reason\":null}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::after(
                Duration::from_secs(2),
                b"data: {\"choices\":[{\"delta\":{\"content\":\"last-token\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
                    .to_vec(),
            ),
        ],
        hold: Duration::ZERO,
    });
    let mut old = DesktopGateway::start(temp, &stub, free_addr());
    let mut stream = StreamingClient::send(old.addr, &chat_request(true)).unwrap();
    let head = stream
        .read_until(b"first-token", Duration::from_secs(5))
        .unwrap();
    assert!(String::from_utf8_lossy(&head).contains("first-token"));

    let reply = control(old.addr, "wait", LEASE, CONTROL_TOKEN);
    assert_eq!((reply.status, reply.json()), (200, json!({"in_flight": 1})));
    let refused = client_request(old.addr, &chat_request(false)).unwrap();
    assert_eq!(refused.status, 503);
    assert_eq!(
        refused.json()["error"]["code"],
        "desktop_port_switch_in_progress"
    );
    assert_eq!(stub.attempts(), 1);

    let tail = stream
        .read_until(b"[DONE]", Duration::from_secs(5))
        .unwrap();
    assert!(String::from_utf8_lossy(&tail).contains("last-token"));
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let reply = control(old.addr, "wait", LEASE, CONTROL_TOKEN);
        if reply.json()["in_flight"] == 0 {
            break;
        }
        assert!(Instant::now() < deadline, "finished stream still counted");
        std::thread::sleep(Duration::from_millis(10));
    }
    let reply = control(old.addr, "commit", LEASE, CONTROL_TOKEN);
    assert_eq!(
        (reply.status, reply.json()),
        (202, json!({"status": "stopping"}))
    );
    let status = old.wait_for_exit(Duration::from_secs(5));
    assert!(status.success(), "old gateway exit: {status}");
    assert!(client_request(old.addr, &ClientRequest::get("/health")).is_err());

    let new_addr = {
        let mut addr = free_addr();
        while addr == old.addr {
            addr = free_addr();
        }
        addr
    };
    let new_temp = TempDir::new("um-desktop-real-new-port-");
    let mut new_gateway = DesktopGateway::start(new_temp, &stub, new_addr);
    let health = client_request(new_gateway.addr, &ClientRequest::get("/health")).unwrap();
    assert_eq!(
        (health.status, health.json()["status"].clone()),
        (200, json!("ok"))
    );
    assert_eq!(shutdown(new_gateway.addr).status, 202);
    assert!(new_gateway.wait_for_exit(Duration::from_secs(5)).success());
}

#[test]
fn production_desktop_binary_shutdown_interrupts_an_active_sse_stream() {
    let temp = TempDir::new("um-desktop-real-shutdown-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"first-token\"},\"finish_reason\":null}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::after(
                Duration::from_secs(10),
                b"data: {\"choices\":[{\"delta\":{\"content\":\"last-token\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
                    .to_vec(),
            ),
        ],
        hold: Duration::ZERO,
    });
    let mut gateway = DesktopGateway::start(temp, &stub, free_addr());
    let mut stream = StreamingClient::send(gateway.addr, &chat_request(true)).unwrap();
    let head = stream
        .read_until(b"first-token", Duration::from_secs(5))
        .unwrap();
    assert!(String::from_utf8_lossy(&head).contains("first-token"));

    let started = Instant::now();
    assert_eq!(shutdown(gateway.addr).status, 202);
    let status = gateway.wait_for_exit(Duration::from_secs(3));
    assert!(status.success(), "unexpected exit: {status}");
    assert!(
        started.elapsed() < Duration::from_secs(3),
        "shutdown must interrupt the in-flight stream"
    );
    assert!(client_request(gateway.addr, &ClientRequest::get("/health")).is_err());

    let tail = stream.read_all(Duration::from_secs(1)).unwrap_or_default();
    let tail = String::from_utf8_lossy(&tail);
    assert!(!tail.contains("last-token"), "{tail}");
    assert!(!tail.contains("[DONE]"), "{tail}");
}

#[test]
fn production_desktop_binary_cancel_resumes_admission_on_the_old_port() {
    let temp = TempDir::new("um-desktop-real-cancel-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"first-token\"},\"finish_reason\":null}]}\n\n"
                    .to_vec(),
            ),
            ChatChunk::after(
                Duration::from_secs(2),
                b"data: {\"choices\":[{\"delta\":{\"content\":\"last-token\"},\"finish_reason\":\"stop\"}]}\n\ndata: [DONE]\n\n"
                    .to_vec(),
            ),
        ],
        hold: Duration::ZERO,
    });
    stub.push(ChatAttempt::Json {
        status: 200,
        body: success_body(),
    });
    let mut gateway = DesktopGateway::start(temp, &stub, free_addr());
    let mut stream = StreamingClient::send(gateway.addr, &chat_request(true)).unwrap();
    let head = stream
        .read_until(b"first-token", Duration::from_secs(5))
        .unwrap();
    assert!(String::from_utf8_lossy(&head).contains("first-token"));

    let reply = control(gateway.addr, "wait", LEASE, CONTROL_TOKEN);
    assert_eq!((reply.status, reply.json()), (200, json!({"in_flight": 1})));
    let refused = client_request(gateway.addr, &chat_request(false)).unwrap();
    assert_eq!(refused.status, 503);
    let reply = control(gateway.addr, "cancel", LEASE, CONTROL_TOKEN);
    assert_eq!(
        (reply.status, reply.json()),
        (200, json!({"status": "accepting"}))
    );

    let admitted = client_request(gateway.addr, &chat_request(false)).unwrap();
    assert_eq!(
        (admitted.status, admitted.json()["object"].clone()),
        (200, json!("chat.completion"))
    );
    assert_eq!(stub.attempts(), 2);
    let tail = stream
        .read_until(b"[DONE]", Duration::from_secs(5))
        .unwrap();
    assert!(String::from_utf8_lossy(&tail).contains("last-token"));

    assert_eq!(shutdown(gateway.addr).status, 202);
    assert!(gateway.wait_for_exit(Duration::from_secs(5)).success());
}

#[test]
fn production_desktop_binary_reports_an_occupied_port_and_keeps_the_old_listener() {
    let temp = TempDir::new("um-desktop-real-occupied-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::Json {
        status: 200,
        body: success_body(),
    });
    let mut gateway = DesktopGateway::start(temp, &stub, free_addr());

    let second_temp = TempDir::new("um-desktop-real-occupied-second-");
    let second_token = second_temp.write("control.token", CONTROL_TOKEN);
    let stderr_path = second_temp.path().join("gateway-stderr.log");
    let stderr = std::fs::File::create(&stderr_path).expect("second stderr file");
    let mut command = Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"));
    command
        .args(["--mode", "production", "--desktop-instance", "--no-browser"])
        .args(["--port", &gateway.addr.port().to_string()])
        .args(["--upstream-base-url", &stub.url()])
        .arg("--control-token-file")
        .arg(second_token)
        .current_dir(second_temp.path())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::from(stderr));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut second = ChildGuard(command.spawn().expect("second gateway process"));
    let deadline = Instant::now() + Duration::from_secs(5);
    let status = loop {
        if let Some(status) = second.0.try_wait().expect("wait second gateway") {
            break status;
        }
        assert!(
            Instant::now() < deadline,
            "a second gateway on the occupied port must fail"
        );
        std::thread::sleep(Duration::from_millis(20));
    };
    assert!(!status.success(), "unexpected second exit: {status}");
    let stderr = std::fs::read_to_string(&stderr_path).expect("second stderr");
    assert!(stderr.contains("um-gateway-candidate:"), "{stderr}");

    let health = client_request(gateway.addr, &ClientRequest::get("/health")).unwrap();
    assert_eq!(
        (health.status, health.json()["status"].clone()),
        (200, json!("ok"))
    );
    let chat = client_request(gateway.addr, &chat_request(false)).unwrap();
    assert_eq!(chat.status, 200);
    assert_eq!(shutdown(gateway.addr).status, 202);
    assert!(gateway.wait_for_exit(Duration::from_secs(5)).success());
}

#[test]
fn desktop_shutdown_closes_the_listener_before_the_auth_watch_stops() {
    let temp = TempDir::new("um-desktop-shutdown-order-");
    let stub = ChatStub::start();
    let mut gateway = DesktopGateway::start(temp, &stub, free_addr());
    assert_eq!(shutdown(gateway.addr).status, 202);
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let refused = std::net::TcpStream::connect_timeout(
            &gateway.addr,
            Duration::from_millis(100),
        )
        .is_err();
        if refused {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "the listener stayed up after shutdown"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    let status = gateway.wait_for_exit(Duration::from_secs(5));
    assert!(status.success(), "unexpected exit: {status}");
}
