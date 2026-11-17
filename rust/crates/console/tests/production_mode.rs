//! #76: `um-console` gains the same `--mode candidate|production` switch as
//! `um-gateway`.
//!
//! Candidate stays the default and only binds an ephemeral loopback port.
//! Production must be selected explicitly, binds only `127.0.0.1:8002` and still
//! assembles only the cloud role. The tests start the real
//! `um-console-candidate` binary; no test reads a real credential or touches a
//! production data directory.

mod support;

use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::json;
use um_console::config::{BindMode, ConsoleConfig};

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

fn console_command(temp: &um_test_harness::TempDataDir) -> Command {
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
        .env_remove("UM_SHARE_ROLE")
        .env_remove("UM_SHARE_DEPLOYMENT")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

/// Run a console that must fail before binding. A console that starts serving is
/// killed and the test fails instead of hanging forever.
fn run_rejected(mut command: Command) -> std::process::Output {
    let mut child = command.spawn().expect("spawn rejected console");
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if child.try_wait().expect("wait rejected console").is_some() {
            return child
                .wait_with_output()
                .expect("collect rejected console output");
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("console accepted a binding it should have rejected");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
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
            panic!("console exited before printing its listen address");
        }
        if let Some(address) = line.trim().strip_prefix("um-console listening on http://") {
            return address.parse().expect("listen address");
        }
    }
    panic!("console did not print its listen address");
}

#[test]
fn production_mode_binds_8002_on_loopback() {
    let temp = temp();
    let mut child = console_command(&temp)
        .arg("--mode")
        .arg("production")
        .arg("--bind-port")
        .arg("8002")
        .spawn()
        .expect("spawn production console");
    let addr = read_listen_addr(&mut child);
    let _guard = ChildGuard { child };
    assert_eq!(
        addr,
        SocketAddr::from(([127, 0, 0, 1], 8002)),
        "production must bind loopback 8002"
    );

    let health = client_request(addr, &ClientRequest::get("/health")).expect("production health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["status"], json!("ok"));
    assert_eq!(health.json()["port"], json!(8002));
}

#[test]
fn candidate_remains_the_default_and_rejects_8002() {
    assert_eq!(ConsoleConfig::default().bind_mode(), BindMode::Candidate);

    let temp = temp();
    let mut config = base_config(&temp);
    config.bind_port = 8002;
    let error = config
        .validate_bind_policy()
        .expect_err("candidate must reject 8002");
    assert!(error.to_string().contains("8002"), "{error}");

    let mut command = console_command(&temp);
    command.arg("--bind-port").arg("8002");
    let output = run_rejected(command);
    assert!(
        !output.status.success(),
        "the default candidate must reject 8002"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("8002"), "stderr: {stderr}");
}

#[test]
fn production_config_rejects_non_loopback_and_ports_other_than_8002() {
    let temp = temp();
    let mut config = base_config(&temp);
    config.candidate = false;
    config.bind_port = 8002;
    config
        .validate_bind_policy()
        .expect("production must accept loopback 8002");

    for port in [0u16, 8001, 8003, 8096, 8100] {
        let mut config = base_config(&temp);
        config.candidate = false;
        config.bind_port = port;
        let error = config
            .validate_bind_policy()
            .expect_err("production must reject a port other than 8002");
        assert!(
            error.to_string().contains("8002"),
            "port {port}: {error}"
        );
    }

    let mut config = base_config(&temp);
    config.candidate = false;
    config.bind_port = 8002;
    config.bind_host = "0.0.0.0".to_string();
    let error = config
        .validate_bind_policy()
        .expect_err("production must reject a non-loopback host");
    assert!(error.to_string().contains("loopback"), "{error}");
}

#[test]
fn production_mode_rejects_non_8002_ports_and_non_loopback_before_bind() {
    let temp = temp();
    for port in ["0", "8001", "8003", "8096", "8100"] {
        let mut command = console_command(&temp);
        command
            .arg("--mode")
            .arg("production")
            .arg("--bind-port")
            .arg(port);
        let output = run_rejected(command);
        assert!(
            !output.status.success(),
            "production must reject port {port}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("8002"), "port {port} stderr: {stderr}");
    }

    for host in ["0.0.0.0", "192.0.2.10"] {
        let mut command = console_command(&temp);
        command
            .arg("--mode")
            .arg("production")
            .arg("--bind-port")
            .arg("8002")
            .arg("--host")
            .arg(host);
        let output = run_rejected(command);
        assert!(
            !output.status.success(),
            "production must reject host {host}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("loopback"), "host {host} stderr: {stderr}");
    }
}

#[test]
fn an_unknown_mode_is_a_usage_error() {
    let temp = temp();
    let output = console_command(&temp)
        .arg("--mode")
        .arg("staging")
        .output()
        .expect("run invalid mode");
    assert_eq!(
        output.status.code(),
        Some(64),
        "an unknown mode must be a usage error"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("candidate or production"),
        "stderr: {stderr}"
    );
}
