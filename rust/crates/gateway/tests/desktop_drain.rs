mod support;

use std::net::{SocketAddr, TcpListener};
use std::process::{Child, Command, Stdio};
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
    let free = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let addr = free.local_addr().unwrap();
    drop(free);
    let mut command = Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"));
    command
        .args(["--mode", "production", "--desktop-instance", "--no-browser"])
        .args(["--port", &addr.port().to_string()])
        .args(["--upstream-base-url", &stub.url()])
        .arg("--control-token-file")
        .arg(temp.write("control.token", CONTROL_TOKEN))
        .current_dir(temp.path())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut process = ChildGuard(command.spawn().expect("start isolated desktop gateway"));
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
            process.0.try_wait().unwrap().is_none(),
            "gateway exited before listening"
        );
        assert!(
            Instant::now() < deadline,
            "gateway did not listen on the custom port"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    let reply = control(addr, "wait", LEASE, CONTROL_TOKEN);
    assert_eq!((reply.status, reply.json()), (200, json!({"in_flight": 0})));
    assert_eq!(control(addr, "commit", LEASE, CONTROL_TOKEN).status, 202);
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = process.0.try_wait().unwrap() {
            assert!(status.success(), "unexpected exit: {status}");
            break;
        }
        assert!(Instant::now() < deadline, "gateway did not stop");
        std::thread::sleep(Duration::from_millis(50));
    }
}
