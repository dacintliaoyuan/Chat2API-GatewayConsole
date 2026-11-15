//! Acceptance 9: the candidate process only listens on an ephemeral loopback port.
//!
//! The real `um-gateway-candidate` binary is started with `--port 0`; its
//! printed address must be a loopback ephemeral port outside the governed
//! production set, it must answer `/health`, and it must never switch 8003.

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

fn candidate_command(temp: &TempDir, stub: &StubUpstream) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"));
    command
        .arg("--upstream-base-url")
        .arg(stub.url())
        .arg("--token")
        .arg(STUB_TOKEN)
        .arg("--share-config")
        .arg(temp.write("share.json", &legacy_share_config()))
        .arg("--control-token-file")
        .arg(temp.write("control.token", CONTROL_TOKEN))
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
        if let Some(address) = line.trim().strip_prefix("um-gateway listening on http://") {
            return address.parse().expect("listen address");
        }
    }
    panic!("candidate did not print its listen address");
}

#[test]
fn the_candidate_process_binds_only_an_ephemeral_loopback_port() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let mut child = candidate_command(&temp, &stub)
        .spawn()
        .expect("spawn candidate");
    let addr = read_listen_addr(&mut child);
    let mut guard = ChildGuard { child };
    assert!(addr.ip().is_loopback(), "candidate must bind loopback");
    assert!(addr.port() >= 1024, "candidate must bind an ephemeral port");
    assert_ne!(addr.port(), 8003, "candidate must not bind 8003");
    assert!(
        !production_ports().contains(&addr.port()),
        "candidate bound a governed production port: {}",
        addr.port()
    );

    let health = client_request(addr, &ClientRequest::get("/health")).expect("candidate health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["status"], json!("ok"));
    assert!(
        guard.child.try_wait().expect("candidate status").is_none(),
        "the candidate must still be serving after /health"
    );
}

#[test]
fn the_candidate_refuses_8003_and_non_loopback_before_bind() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();

    for (name, host, port) in [
        ("production-port", "127.0.0.1", "8003"),
        ("non-loopback", "0.0.0.0", "0"),
    ] {
        let mut command = candidate_command(&temp, &stub);
        command
            .arg("--host")
            .arg(host)
            .arg("--port")
            .arg(port);
        let output = command.output().expect("run rejected candidate");
        assert!(
            !output.status.success(),
            "{name} candidate must fail before bind"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            stderr.contains("8003") || stderr.contains("loopback"),
            "{name} stderr: {stderr}"
        );
    }
}

#[test]
fn the_candidate_requires_an_upstream_url() {
    let output = Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"))
        .arg("--port")
        .arg("0")
        .output()
        .expect("run candidate without upstream");
    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("upstream-base-url"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
