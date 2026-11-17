//! Acceptance 6: the candidate process only listens on an ephemeral loopback port.
//!
//! The real `um-console-candidate` binary is started with no production
//! arguments; its printed address must be a loopback ephemeral port outside the
//! governed production set, it must answer `/health`, and it must never switch
//! 8002.

mod support;

use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::json;

use support::*;

struct ChildGuard {
    child: Child,
}

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn candidate_command(temp: &um_test_harness::TempDataDir) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_um-console-candidate"));
    command
        .arg("--gateway-pid-file")
        .arg(temp.path().join("share-gateway.pid"))
        .arg("--gateway-state-dir")
        .arg(temp.path().join("run"))
        .arg("--proc-root")
        .arg(temp.path().join("proc"))
        .arg("--state-file")
        .arg(temp.path().join("console.sqlite3"))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

fn read_listen_addr(child: &mut Child) -> std::net::SocketAddr {
    let stdout = child.stdout.take().expect("child stdout");
    let mut reader = BufReader::new(stdout);
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut line = String::new();
    while Instant::now() < deadline {
        line.clear();
        let read = reader.read_line(&mut line).expect("read listen line");
        if read == 0 {
            panic!("candidate exited before printing its listen address");
        }
        if let Some(address) = line.trim().strip_prefix("um-console listening on http://") {
            return address.parse().expect("listen address");
        }
    }
    panic!("candidate did not print its listen address");
}

#[test]
fn the_candidate_process_binds_only_an_ephemeral_loopback_port() {
    let temp = temp();
    let mut child = candidate_command(&temp).spawn().expect("spawn candidate");
    let addr = read_listen_addr(&mut child);
    let mut guard = ChildGuard { child };
    assert!(addr.ip().is_loopback(), "candidate must bind loopback");
    assert!(addr.port() >= 1024, "candidate must bind an ephemeral port");
    assert_ne!(addr.port(), 8002, "candidate must not bind 8002");
    assert_ne!(addr.port(), 8003, "candidate must not bind 8003");
    assert!(
        !production_ports().contains(&addr.port()),
        "candidate bound a governed production port: {}",
        addr.port()
    );

    let health = client_request(addr, &ClientRequest::get("/health")).expect("candidate health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["status"], json!("ok"));
    assert_eq!(health.json()["service"], json!("usagesrv"));
    assert!(
        guard.child.try_wait().expect("candidate status").is_none(),
        "the candidate must still be serving after /health"
    );
}

#[test]
fn the_candidate_refuses_8002_and_non_loopback_before_bind() {
    let temp = temp();
    for (name, host, port) in [
        ("production-port", "127.0.0.1", "8002"),
        ("non-loopback", "0.0.0.0", "0"),
    ] {
        let mut command = candidate_command(&temp);
        command.arg("--host").arg(host).arg("--bind-port").arg(port);
        let output = command.output().expect("run rejected candidate");
        assert!(
            !output.status.success(),
            "{name} candidate must fail before bind"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("8002") || stderr.contains("loopback"),
            "{name} stderr: {stderr}"
        );
    }
}

#[test]
fn the_candidate_starts_the_local_role_on_an_ephemeral_port() {
    let temp = temp();
    let mut child = candidate_command(&temp)
        .arg("--role")
        .arg("local")
        .spawn()
        .expect("spawn local-role candidate");
    let addr = read_listen_addr(&mut child);
    let _guard = ChildGuard { child };
    assert!(addr.ip().is_loopback());
    assert!(addr.port() >= 1024);
    assert!(!production_ports().contains(&addr.port()));
    let health = client_request(addr, &ClientRequest::get("/health")).expect("health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["port"], json!(8001));
}

#[test]
fn the_candidate_serves_the_share_read_faces_without_binding_8002() {
    let temp = temp();
    let store = temp.path().join("share.db");
    let mut command = candidate_command(&temp);
    command
        .env("USAGESRV_PORT", "8002")
        .env("SHARE_STORE_FILE", &store)
        .env("CONSOLE_PUBLIC_ORIGIN", "https://share.example.test");
    let mut child = command.spawn().expect("spawn candidate");
    let addr = read_listen_addr(&mut child);
    let mut guard = ChildGuard { child };
    assert!(addr.ip().is_loopback());
    assert!(addr.port() >= 1024);
    assert_ne!(addr.port(), 8002);
    assert_ne!(addr.port(), 8003);
    assert!(!production_ports().contains(&addr.port()));

    let health = client_request(addr, &ClientRequest::get("/health")).expect("candidate health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["port"], json!(8002));
    let status = client_request(
        addr,
        &ClientRequest::get("/api/console/share-management/status"),
    )
    .expect("candidate share status");
    assert_eq!(status.status, 500);
    assert_eq!(status.json(), json!({"error": "share management error"}));
    assert!(
        !store.exists(),
        "the read face must not create the share store"
    );
    assert!(
        guard.child.try_wait().expect("candidate status").is_none(),
        "the candidate must still be serving after the read faces"
    );
}
