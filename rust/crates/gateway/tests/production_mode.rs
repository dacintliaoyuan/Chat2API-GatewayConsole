//! #68: production mode binds 8003/8002 on loopback; candidate stays the
//! default and only binds an ephemeral loopback port.
//!
//! The tests start the real `um-gateway-candidate` binary. Production mode is
//! explicit (`--mode production`); no test opens a browser, reads a real
//! credential or touches a production data directory.

mod support;

use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::json;

use um_gateway::config::{BindMode, GatewayConfig};

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

fn gateway_command(temp: &TempDir, stub: &StubUpstream) -> Command {
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

fn read_listen_addr(child: &mut Child) -> SocketAddr {
    let stdout = child.stdout.take().expect("child stdout");
    let mut reader = BufReader::new(stdout);
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut line = String::new();
    while Instant::now() < deadline {
        line.clear();
        let read = reader.read_line(&mut line).expect("read listen line");
        if read == 0 {
            panic!("gateway exited before printing its listen address");
        }
        if let Some(address) = line.trim().strip_prefix("um-gateway listening on http://") {
            return address.parse().expect("listen address");
        }
    }
    panic!("gateway did not print its listen address");
}

#[test]
fn production_mode_binds_8003_and_8002_on_loopback() {
    for port in [8003u16, 8002u16] {
        let temp = TempDir::new(unique_temp_prefix());
        let stub = StubUpstream::start();
        let mut child = gateway_command(&temp, &stub)
            .arg("--mode")
            .arg("production")
            .arg("--port")
            .arg(port.to_string())
            .spawn()
            .expect("spawn production gateway");
        let addr = read_listen_addr(&mut child);
        let _guard = ChildGuard { child };
        assert_eq!(
            addr,
            SocketAddr::from(([127, 0, 0, 1], port)),
            "production must bind loopback {port}"
        );

        let health = client_request(addr, &ClientRequest::get("/health")).expect("health");
        assert_eq!(health.status, 200);
        assert_eq!(health.json()["status"], json!("ok"));

        let ready = client_request(addr, &ClientRequest::get("/ready")).expect("ready");
        assert_eq!(
            ready.status,
            200,
            "production /ready: {}",
            String::from_utf8_lossy(&ready.body)
        );
        assert_eq!(ready.json()["status"], json!("ready"));

        let models = client_request(addr, &ClientRequest::get("/v1/models").bearer(SHARE_SECRET))
            .expect("shared models");
        assert_eq!(models.status, 200);
        assert!(models.json()["data"].is_array());

        let hidden = client_request(
            addr,
            &ClientRequest::get("/v1/version").bearer(SHARE_SECRET),
        )
        .expect("hidden version route");
        assert_eq!(hidden.status, 404);
        assert_eq!(
            body_field(&hidden.json(), "code"),
            &json!("route_not_shared")
        );
    }
}

#[test]
fn production_mode_rejects_non_loopback_before_bind() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let output = gateway_command(&temp, &stub)
        .arg("--mode")
        .arg("production")
        .arg("--host")
        .arg("0.0.0.0")
        .arg("--port")
        .arg("8003")
        .output()
        .expect("run rejected production gateway");
    assert!(
        !output.status.success(),
        "production must reject a non-loopback host"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("loopback"), "stderr: {stderr}");
}

#[test]
fn production_mode_rejects_ports_outside_8003_and_8002() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    for port in ["0", "8000", "8001", "8100", "8096"] {
        let output = gateway_command(&temp, &stub)
            .arg("--mode")
            .arg("production")
            .arg("--port")
            .arg(port)
            .output()
            .expect("run rejected production gateway");
        assert!(
            !output.status.success(),
            "production must reject port {port}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("8002 or 8003"), "{port} stderr: {stderr}");
    }
}

#[test]
fn candidate_remains_the_default_and_rejects_production_ports() {
    assert_eq!(GatewayConfig::default().bind_mode(), BindMode::Candidate);
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    for port in ["8003", "8002", "8100"] {
        let output = gateway_command(&temp, &stub)
            .arg("--port")
            .arg(port)
            .output()
            .expect("run rejected candidate gateway");
        assert!(
            !output.status.success(),
            "default candidate must reject port {port}"
        );
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains(port), "{port} stderr: {stderr}");
    }
}

#[test]
fn candidate_config_rejects_production_ports_without_binding() {
    for port in [8003u16, 8002, 8100] {
        let config = GatewayConfig {
            bind_port: port,
            ..GatewayConfig::default()
        };
        let error = config
            .validate_bind_policy()
            .expect_err("candidate must reject a production port");
        assert!(
            error.to_string().contains(&port.to_string()),
            "port {port}: {error}"
        );
    }
}

#[test]
fn production_config_rejects_non_loopback_and_other_ports() {
    let config = GatewayConfig {
        bind_host: "0.0.0.0".to_string(),
        bind_port: 8003,
        candidate: false,
        ..GatewayConfig::default()
    };
    let error = config
        .validate_bind_policy()
        .expect_err("production must reject a non-loopback host");
    assert!(error.to_string().contains("loopback"), "{error}");

    for port in [0u16, 8000, 8001, 8100, 8096] {
        let config = GatewayConfig {
            bind_port: port,
            candidate: false,
            ..GatewayConfig::default()
        };
        let error = config
            .validate_bind_policy()
            .expect_err("production must reject a non-production port");
        assert!(error.to_string().contains("8002 or 8003"), "{error}");
    }
}

#[test]
fn help_documents_candidate_and_production_modes() {
    let output = Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"))
        .arg("--help")
        .output()
        .expect("run help");
    assert!(output.status.success());
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        stdout.contains("--mode candidate|production"),
        "help must document --mode: {stdout}"
    );
    assert!(
        stdout.contains("8003 or 8002"),
        "help must document production ports: {stdout}"
    );
    assert!(
        stdout.contains("candidate (default)"),
        "help must document the default mode: {stdout}"
    );
}
