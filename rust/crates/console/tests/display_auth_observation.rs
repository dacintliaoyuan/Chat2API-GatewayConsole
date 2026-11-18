//! #104: the cloud display-state face reads the gateway auth round.
//!
//! The production entry wires `DisplaySeams::auth_observation` to the SAME
//! `/admin/auth/status` control face the UM auth actions use. A fake loopback
//! gateway scripts the answer; the tests observe only the public display-state
//! route, so the seam stays replaceable.

mod support;

use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use um_console::config::Role;
use um_console::ConsoleServer;
use um_test_harness::TempDataDir;

use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

#[derive(Debug, Clone)]
struct RecordedRequest {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
}

impl RecordedRequest {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

#[derive(Clone)]
enum Reply {
    Json(u16, Value),
    Raw(u16, Vec<u8>),
}

struct FakeGateway {
    addr: SocketAddr,
    requests: Arc<Mutex<Vec<RecordedRequest>>>,
    shutdown: Arc<AtomicBool>,
}

impl FakeGateway {
    fn start(reply: Reply) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake gateway bind");
        listener
            .set_nonblocking(true)
            .expect("fake gateway nonblocking");
        let addr = listener.local_addr().expect("fake gateway addr");
        let reply = Arc::new(Mutex::new(reply));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let shutdown = Arc::new(AtomicBool::new(false));
        let worker_reply = Arc::clone(&reply);
        let worker_requests = Arc::clone(&requests);
        let worker_shutdown = Arc::clone(&shutdown);
        std::thread::spawn(move || {
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        handle_gateway_connection(
                            Arc::clone(&worker_reply),
                            Arc::clone(&worker_requests),
                            stream,
                        );
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            addr,
            requests,
            shutdown,
        }
    }

    fn port(&self) -> u16 {
        self.addr.port()
    }

    fn requests(&self) -> Vec<RecordedRequest> {
        self.requests.lock().expect("fake gateway requests").clone()
    }
}

impl Drop for FakeGateway {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
    }
}

fn handle_gateway_connection(
    reply: Arc<Mutex<Reply>>,
    requests: Arc<Mutex<Vec<RecordedRequest>>>,
    mut stream: TcpStream,
) {
    let _ = stream.set_nonblocking(false);
    let request = match um_gateway::http::read_request(&mut stream, IpAddr::V4(Ipv4Addr::LOCALHOST))
    {
        Ok(Some(request)) => request,
        _ => return,
    };
    let headers = request
        .headers
        .iter()
        .map(|header| (header.name.clone(), header.value.clone()))
        .collect();
    requests.lock().expect("fake gateway requests").push(RecordedRequest {
        method: request.method.clone(),
        path: request.path().to_string(),
        headers,
    });
    let reply = reply.lock().expect("fake gateway reply").clone();
    let response = match reply {
        Reply::Json(status, payload) => um_gateway::http::Response::json(status, &payload),
        Reply::Raw(status, body) => {
            um_gateway::http::Response::new(status, body).with_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
        }
    };
    let _ = um_gateway::http::write_response(&mut stream, &response);
}

fn control_token_file(temp: &TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("admin-control-token");
    std::fs::write(&path, format!("{CONTROL_TOKEN}\n")).expect("control token");
    path
}

fn start_console(
    temp: &TempDataDir,
    role: Role,
    gateway_port: u16,
    token_file: Option<&Path>,
) -> Console {
    let mut config = base_config(temp);
    config.role = role;
    config.port = role.default_port();
    config.gateway_port = gateway_port;
    config.control_token_file = token_file.map(Path::to_path_buf);
    Console::from_server(
        ConsoleServer::start_with_runtime(config, stopped_runtime())
            .expect("start production console"),
    )
}

fn display_state(console: &Console) -> Value {
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-management/display-state"),
    )
    .expect("display-state request");
    assert_eq!(response.status, 200);
    response.json()
}

fn valid_status() -> Value {
    json!({
        "state": "valid",
        "label": "认证有效",
        "error": null,
        "primary": null,
        "login_window_deadline_epoch": 1700000600,
    })
}

#[test]
fn display_state_reads_a_valid_gateway_auth_round_in_both_roles() {
    for role in [Role::Cloud, Role::Local] {
        let temp = temp();
        let gateway = FakeGateway::start(Reply::Json(200, valid_status()));
        let console = start_console(
            &temp,
            role,
            gateway.port(),
            Some(&control_token_file(&temp)),
        );

        let payload = display_state(&console);

        assert_eq!(payload["state"], json!("valid"), "{role:?}");
        assert_eq!(payload["label"], json!("登录有效"), "{role:?}");
        assert_eq!(payload["detail"], Value::Null, "{role:?}");
        assert_eq!(payload["auth_error"], Value::Null, "{role:?}");
        assert_eq!(
            payload["login_window_deadline_epoch"],
            json!(1700000600.0),
            "{role:?}"
        );
        assert!(
            payload["observed_at"].as_i64().unwrap_or_default() > 0,
            "{role:?}"
        );
        let requests = gateway.requests();
        assert_eq!(requests.len(), 1, "{role:?}");
        assert_eq!(requests[0].method, "GET", "{role:?}");
        assert_eq!(requests[0].path, "/admin/auth/status", "{role:?}");
        assert_eq!(
            requests[0].header("X-Admin-Control"),
            Some(CONTROL_TOKEN),
            "{role:?}"
        );
    }
}

fn auth_status(state: &str, label: &str, error: Option<&str>) -> Value {
    json!({
        "state": state,
        "label": label,
        "error": error,
        "primary": null,
        "login_window_deadline_epoch": 1700000600,
    })
}

#[test]
fn display_state_shows_the_gateway_manual_takeover_wait() {
    let temp = temp();
    let gateway = FakeGateway::start(Reply::Json(
        200,
        auth_status(
            "logging_in",
            "登录中（需人工：umpass_autofill_protected）",
            Some("manual_takeover_required:umpass_autofill_protected"),
        ),
    ));
    let console = start_console(
        &temp,
        Role::Cloud,
        gateway.port(),
        Some(&control_token_file(&temp)),
    );

    let payload = display_state(&console);

    assert_eq!(payload["state"], json!("waiting_mfa"));
    assert_eq!(payload["label"], json!("等待 Duo 登录（本人操作）"));
    assert_eq!(
        payload["detail"],
        json!("manual_takeover_required:umpass_autofill_protected")
    );
    assert_eq!(
        payload["login_window_deadline_epoch"],
        json!(1700000600.0)
    );
    assert_eq!(payload["auth_error"], Value::Null);
}

#[test]
fn display_state_shows_the_gateway_duo_prompt_wait() {
    let temp = temp();
    let gateway = FakeGateway::start(Reply::Json(
        200,
        auth_status("logging_in", "登录中（需人工：duo_prompt_waiting）", Some("duo_prompt_waiting")),
    ));
    let console = start_console(
        &temp,
        Role::Cloud,
        gateway.port(),
        Some(&control_token_file(&temp)),
    );

    let payload = display_state(&console);

    assert_eq!(payload["state"], json!("waiting_duo"));
    assert_eq!(payload["label"], json!("等待本人确认 Duo 推送"));
    assert_eq!(payload["detail"], json!("duo_prompt_waiting"));
}

#[test]
fn display_state_keeps_a_plain_logging_in_round_in_progress() {
    let temp = temp();
    let gateway = FakeGateway::start(Reply::Json(
        200,
        auth_status("logging_in", "登录中", None),
    ));
    let console = start_console(
        &temp,
        Role::Cloud,
        gateway.port(),
        Some(&control_token_file(&temp)),
    );

    let payload = display_state(&console);

    assert_eq!(payload["state"], json!("logging_in"));
    assert_eq!(payload["label"], json!("正在登录"));
    assert_eq!(payload["detail"], Value::Null);
}

#[test]
fn display_state_reports_needs_login_without_a_manual_reason_as_not_started() {
    let temp = temp();
    let gateway = FakeGateway::start(Reply::Json(
        200,
        auth_status("needs_login", "需要登录", None),
    ));
    let console = start_console(
        &temp,
        Role::Cloud,
        gateway.port(),
        Some(&control_token_file(&temp)),
    );

    let payload = display_state(&console);

    assert_eq!(payload["state"], json!("not_started"));
    assert_eq!(payload["label"], json!("未开始登录"));
    assert_eq!(payload["detail"], Value::Null);
}

#[test]
fn display_state_reports_a_gateway_manual_failure() {
    let temp = temp();
    let gateway = FakeGateway::start(Reply::Json(
        200,
        auth_status(
            "failed",
            "认证失败（需人工：umpass_mfa_required）",
            Some("manual_takeover_required:umpass_mfa_required"),
        ),
    ));
    let console = start_console(
        &temp,
        Role::Local,
        gateway.port(),
        Some(&control_token_file(&temp)),
    );

    let payload = display_state(&console);

    assert_eq!(payload["state"], json!("failed"));
    assert_eq!(payload["label"], json!("需要人工接管"));
    assert_eq!(
        payload["detail"],
        json!("manual_takeover_required:umpass_mfa_required")
    );
}

fn unused_port() -> u16 {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("reserve unused port");
    let port = listener.local_addr().expect("unused port addr").port();
    drop(listener);
    port
}

fn assert_read_failure(payload: &Value, code: &str, message_fragment: &str) {
    assert_eq!(payload["state"], json!("display_disconnected"));
    assert_eq!(payload["label"], json!("显示连接中断"));
    let detail = payload["detail"].as_str().unwrap_or_default();
    assert!(
        detail.contains(&format!("认证状态读取失败（{code}）")),
        "detail {detail:?} does not name {code}"
    );
    assert!(
        detail.contains(message_fragment),
        "detail {detail:?} does not contain {message_fragment:?}"
    );
    assert_eq!(payload["auth_error"]["code"], json!(code));
    assert!(!payload["auth_error"]["message"]
        .as_str()
        .unwrap_or_default()
        .is_empty());
    assert_eq!(payload["observed_at"], Value::Null);
    assert_eq!(payload["login_window_deadline_epoch"], Value::Null);
}

#[test]
fn display_state_fails_closed_when_the_gateway_is_unreachable() {
    let temp = temp();
    let console = start_console(
        &temp,
        Role::Cloud,
        unused_port(),
        Some(&control_token_file(&temp)),
    );

    let payload = display_state(&console);

    assert_read_failure(&payload, "status_unreachable", "不可达");
}

#[test]
fn display_state_fails_closed_when_the_control_token_is_unreadable() {
    let temp = temp();
    let gateway = FakeGateway::start(Reply::Json(200, valid_status()));
    let token = temp.path().join("broken-admin-control-token");
    std::fs::write(&token, [0xff, 0xfe]).expect("unreadable token file");
    let console = start_console(&temp, Role::Cloud, gateway.port(), Some(&token));

    let payload = display_state(&console);

    assert_read_failure(&payload, "control_token_unreadable", "不可读");
    assert!(
        gateway.requests().is_empty(),
        "an unreadable token must not reach the gateway"
    );
}

#[test]
fn display_state_fails_closed_on_a_gateway_response_schema_mismatch() {
    let temp = temp();
    let gateway = FakeGateway::start(Reply::Raw(200, br#"{"state":42}"#.to_vec()));
    let console = start_console(
        &temp,
        Role::Cloud,
        gateway.port(),
        Some(&control_token_file(&temp)),
    );

    let payload = display_state(&console);

    assert_read_failure(&payload, "status_schema", "响应形状与预期不符");
    assert_eq!(gateway.requests().len(), 1);
}
