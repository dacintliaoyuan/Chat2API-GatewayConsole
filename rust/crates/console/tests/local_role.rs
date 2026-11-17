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
use um_console::RuntimeFacts;

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

/// Start from a temp working directory without `--state-file`/`--dist-dir` so the
/// CLI's own role defaults are the only source of those paths.
fn console_command_without_paths(temp: &um_test_harness::TempDataDir) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_um-console-candidate"));
    command
        .current_dir(temp.path())
        .arg("--gateway-pid-file")
        .arg(temp.path().join("share-gateway.pid"))
        .arg("--gateway-state-dir")
        .arg(temp.path().join("run"))
        .arg("--proc-root")
        .arg(temp.path().join("proc"))
        .env_remove("UM_SHARE_ROLE")
        .env_remove("UM_SHARE_DEPLOYMENT")
        .env_remove("USAGESRV_PORT")
        .env_remove("CONSOLE_STATE_FILE")
        .env_remove("CONSOLE_LOCK_DIR")
        .env_remove("SHARE_STORE_FILE")
        .env_remove("CHAT2API_SHARE_SECRET_KEY_FILE")
        .env_remove("USAGE_CONTROL_TOKEN_FILE")
        .env_remove("CONSOLE_SHARE_BACKEND_DIR")
        .env_remove("USAGE_SESSION_CODE_FILE")
        .env_remove("SELFUSEAI_GATEWAY_WATCH_DIR")
        .env_remove("CONSOLE_DIST_DIR")
        .env_remove("CONSOLE_PYTHON_EXE")
        .env_remove("CONSOLE_REPO_ROOT")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command
}

fn seed_operations(path: &std::path::Path, operation_id: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create state dir");
    }
    let connection = rusqlite::Connection::open(path).expect("open operations db");
    connection
        .execute_batch(
            "CREATE TABLE console_meta(key TEXT PRIMARY KEY, value TEXT);
             CREATE TABLE operations(
                id TEXT PRIMARY KEY, action TEXT, target TEXT, state TEXT,
                resource TEXT, generation INTEGER, created_at REAL,
                started_at REAL, finished_at REAL, cancel_requested INTEGER,
                steps_json TEXT, result_json TEXT, error_json TEXT,
                idempotency_key TEXT);
             INSERT INTO console_meta(key, value) VALUES('schema_version', '1');",
        )
        .expect("create operations schema");
    connection
        .execute(
            "INSERT INTO operations(id, action, target, state, resource, generation,
                created_at, started_at, finished_at, cancel_requested, steps_json,
                result_json, error_json, idempotency_key)
             VALUES(?1, 'account.um_status', 'chat2api', 'succeeded',
                'chat2api', 0, 1700000000, 1700000000, NULL, 0, '[]',
                '{\"status\":\"valid\"}', NULL, NULL)",
            [operation_id],
        )
        .expect("insert operation");
}

fn seed_local_dist(temp: &um_test_harness::TempDataDir) {
    let dist = temp.path().join("web").join("dist");
    std::fs::create_dir_all(&dist).expect("create dist dir");
    std::fs::write(
        dist.join("candidate-build.json"),
        r#"{"role":"local","line":"production","sourceHash":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}"#,
    )
    .expect("write build identity");
    std::fs::write(dist.join("index.html"), "<html>local console</html>").expect("write index");
}

fn operations_rows(addr: SocketAddr) -> Vec<serde_json::Value> {
    let response = client_request(
        addr,
        &ClientRequest::get("/api/console/operations").header("Host", "127.0.0.1:8001"),
    )
    .expect("operations request");
    assert_eq!(response.status, 200);
    response.json()["operations"]
        .as_array()
        .cloned()
        .expect("operations array")
}

fn assert_local_defaults_observable(addr: SocketAddr, operation_id: &str) {
    let rows = operations_rows(addr);
    assert_eq!(rows.len(), 1, "default state file rows: {rows:?}");
    assert_eq!(rows[0]["id"], json!(operation_id));

    let index = client_request(
        addr,
        &ClientRequest::get("/").header("Host", "127.0.0.1:8001"),
    )
    .expect("index request");
    assert_eq!(index.status, 200);
    assert_eq!(
        String::from_utf8_lossy(&index.body),
        "<html>local console</html>"
    );
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
fn local_role_defaults_match_pythons_paths() {
    let mut config = ConsoleConfig::default();
    config.role = Role::Local;
    um_console::config::apply_local_defaults(&mut config);
    assert_eq!(
        config.console_state_file.as_deref(),
        Some(std::path::Path::new("data/console-state.sqlite3"))
    );
    assert_eq!(
        config.console_lock_dir.as_deref(),
        Some(std::path::Path::new("data/console-locks"))
    );
    assert_eq!(
        config.share_store_file.as_deref(),
        Some(std::path::Path::new("data/share-store.db"))
    );
    assert_eq!(
        config.control_token_file.as_deref(),
        Some(std::path::Path::new("data/admin-control-token"))
    );
    assert_eq!(
        config.usage_session_code_file.as_deref(),
        Some(std::path::Path::new("data/usage-session-code"))
    );
    assert_eq!(config.usage_log, std::path::PathBuf::from("data/usage-chat2api.jsonl"));
    assert_eq!(config.dsh_snapshot, std::path::PathBuf::from("data/dsh-ledger-20260902.json"));
    assert_eq!(config.service_log, std::path::PathBuf::from("chat2api/chat2api.log"));
}

#[test]
fn cli_role_local_applies_the_same_defaults_as_the_role_environment() {
    let temp = temp();
    seed_operations(
        &temp.path().join("data").join("console-state.sqlite3"),
        "op-local-default",
    );
    seed_local_dist(&temp);

    let mut cli = console_command_without_paths(&temp);
    cli.arg("--mode")
        .arg("production")
        .arg("--role")
        .arg("local")
        .arg("--host")
        .arg("127.0.0.4")
        .arg("--bind-port")
        .arg("8001");
    let mut cli_child = cli.spawn().expect("spawn cli local console");
    let cli_addr = read_listen_addr(&mut cli_child);
    let _cli_guard = ChildGuard { child: cli_child };
    assert_local_defaults_observable(cli_addr, "op-local-default");

    let mut environment = console_command_without_paths(&temp);
    environment
        .env("UM_SHARE_ROLE", "local")
        .arg("--mode")
        .arg("production")
        .arg("--host")
        .arg("127.0.0.5")
        .arg("--bind-port")
        .arg("8001");
    let mut environment_child = environment.spawn().expect("spawn env local console");
    let environment_addr = read_listen_addr(&mut environment_child);
    let _environment_guard = ChildGuard {
        child: environment_child,
    };
    assert_local_defaults_observable(environment_addr, "op-local-default");
}

#[test]
fn explicit_state_file_overrides_the_local_default() {
    let temp = temp();
    seed_operations(
        &temp.path().join("data").join("console-state.sqlite3"),
        "op-local-default",
    );
    let explicit = temp.path().join("custom").join("console-state.sqlite3");
    seed_operations(&explicit, "op-explicit");

    let mut command = console_command_without_paths(&temp);
    command
        .arg("--mode")
        .arg("production")
        .arg("--role")
        .arg("local")
        .arg("--host")
        .arg("127.0.0.6")
        .arg("--bind-port")
        .arg("8001")
        .arg("--state-file")
        .arg(&explicit);
    let mut child = command.spawn().expect("spawn explicit state console");
    let addr = read_listen_addr(&mut child);
    let _guard = ChildGuard { child };

    let rows = operations_rows(addr);
    assert_eq!(rows.len(), 1, "explicit state file rows: {rows:?}");
    assert_eq!(rows[0]["id"], json!("op-explicit"));
}

#[test]
fn cloud_role_does_not_apply_the_local_defaults() {
    let temp = temp();
    seed_operations(
        &temp.path().join("data").join("console-state.sqlite3"),
        "op-local-default",
    );

    let mut command = console_command_without_paths(&temp);
    command
        .arg("--mode")
        .arg("production")
        .arg("--role")
        .arg("cloud")
        .arg("--host")
        .arg("127.0.0.7")
        .arg("--bind-port")
        .arg("8002");
    let mut child = command.spawn().expect("spawn cloud console");
    let addr = read_listen_addr(&mut child);
    let _guard = ChildGuard { child };

    let rows = operations_rows(addr);
    assert!(
        rows.is_empty(),
        "cloud role must not read the local default state file: {rows:?}"
    );
}

#[test]
fn local_runtime_facts_probe_the_chat2api_loopback_port() {
    use std::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let port = listener.local_addr().expect("listener addr").port();
    let facts = um_console::LocalTcpFacts::new(port);
    let snapshot = facts.snapshot().expect("running snapshot");
    assert_eq!(snapshot.state, "running");
    assert!(snapshot.healthy);
    assert_eq!(snapshot.display_state(), "Running");

    drop(listener);
    let snapshot = um_console::LocalTcpFacts::new(port)
        .snapshot()
        .expect("closed snapshot");
    // A just-closed loopback port answers refused on Linux and a connect
    // timeout on Windows; both are non-running and the timeout stays unknown.
    assert!(matches!(snapshot.state.as_str(), "stopped" | "unknown"));
    assert!(!snapshot.healthy);
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
