//! #82: `um-console --role local` assembles the local 8001 console.
//!
//! The local role is the production `usagesrv.py` local assembly: the console
//! read faces are credential-free behind the loopback Host/Origin gate, and the
//! `usage_session` cookie only authenticates the share-management faces. These
//! tests start the real `um-console-candidate` binary or an in-process candidate
//! console; no test reads a real credential or touches a production data dir.

mod support;

use std::io::{BufRead, BufReader};
use std::net::SocketAddr;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use serde_json::json;
use um_console::config::{ConsoleConfig, Role};

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
        .env_remove("USAGESRV_PORT")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

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

fn read_listen_addr(child: &mut Child) -> SocketAddr {
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
fn local_role_starts_in_candidate_mode_with_the_local_report_port() {
    let temp = temp();
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = Role::Local;
        config.port = 8001;
    });
    let health = client_request(console.addr, &ClientRequest::get("/health")).expect("health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["port"], json!(8001));
    assert_eq!(health.json()["service"], json!("usagesrv"));
}

#[test]
fn production_local_binds_8001_on_loopback() {
    let temp = temp();
    // 127.0.0.1:8001 is the production console on this host; 127.0.0.2 is
    // the same loopback interface without colliding with the live console.
    let mut child = console_command(&temp)
        .arg("--mode")
        .arg("production")
        .arg("--role")
        .arg("local")
        .arg("--host")
        .arg("127.0.0.2")
        .arg("--bind-port")
        .arg("8001")
        .spawn()
        .expect("spawn production local console");
    let addr = read_listen_addr(&mut child);
    let _guard = ChildGuard { child };
    assert_eq!(addr, SocketAddr::from(([127, 0, 0, 2], 8001)));
    let health = client_request(
        addr,
        &ClientRequest::get("/health").header("Host", "127.0.0.1:8001"),
    )
    .expect("health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["port"], json!(8001));
}

#[test]
fn production_local_refuses_cloud_port_and_non_loopback_host() {
    let temp = temp();
    for port in ["0", "8002", "8003", "8096"] {
        let mut command = console_command(&temp);
        command
            .arg("--mode")
            .arg("production")
            .arg("--role")
            .arg("local")
            .arg("--bind-port")
            .arg(port);
        let output = run_rejected(command);
        assert!(!output.status.success(), "production local must reject {port}");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("8001"), "port {port} stderr: {stderr}");
    }

    let mut command = console_command(&temp);
    command
        .arg("--mode")
        .arg("production")
        .arg("--role")
        .arg("local")
        .arg("--bind-port")
        .arg("8001")
        .arg("--host")
        .arg("0.0.0.0");
    let output = run_rejected(command);
    assert!(!output.status.success());
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(stderr.contains("loopback"), "stderr: {stderr}");
}

#[test]
fn production_cloud_still_binds_8002_and_refuses_8001() {
    let temp = temp();
    let mut config = base_config(&temp);
    config.candidate = false;
    config.bind_port = 8002;
    config.role = Role::Cloud;
    config.validate_bind_policy().expect("cloud production 8002");

    let mut config = base_config(&temp);
    config.candidate = false;
    config.bind_port = 8001;
    config.role = Role::Cloud;
    let error = config
        .validate_bind_policy()
        .expect_err("cloud production must refuse 8001");
    assert!(error.to_string().contains("8002"), "{error}");
}

#[test]
fn role_selects_its_default_report_port() {
    assert_eq!(ConsoleConfig::default().port, 8002);
    let mut local = ConsoleConfig::default();
    local.role = Role::Local;
    assert_eq!(local.role.default_port(), 8001);
    assert_eq!(local.role.default_bind_port(), 8001);
    assert_eq!(Role::Cloud.default_bind_port(), 8002);
}
