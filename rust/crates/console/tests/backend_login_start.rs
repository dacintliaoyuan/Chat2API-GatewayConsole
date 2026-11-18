//! #107: the cloud console's explicit visible login write face.
//!
//! `POST /api/console/share-management/backend-login` proxies the gateway's
//! control-token-gated `POST /admin/auth/login` with `visible=true` through the
//! same `AdminClient` the UM auth actions use. A started/joined round, the
//! gateway's own `global_attempt_cap` refusal, an unreachable gateway, an
//! unreadable control token and a malformed gateway answer are each observable;
//! no failure fabricates a started round.

mod support;

use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use um_console::config::Role;
use um_test_harness::TempDataDir;

use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";
const START_PATH: &str = "/api/console/share-management/backend-login";

#[derive(Debug, Clone)]
struct RecordedRequest {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}

impl RecordedRequest {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case(name))
            .map(|(_, value)| value.as_str())
    }
}

struct FakeState {
    response: (u16, Value),
    close_without_response: bool,
    requests: Vec<RecordedRequest>,
}

/// A loopback gateway stand-in for `POST /admin/auth/login`.
struct FakeGateway {
    addr: SocketAddr,
    state: Arc<Mutex<FakeState>>,
    shutdown: Arc<AtomicBool>,
}

impl FakeGateway {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake gateway bind");
        listener
            .set_nonblocking(true)
            .expect("fake gateway nonblocking");
        let addr = listener.local_addr().expect("fake gateway addr");
        let state = Arc::new(Mutex::new(FakeState {
            response: (202, json!({"state": "logging_in", "label": "登录中"})),
            close_without_response: false,
            requests: Vec::new(),
        }));
        let shutdown = Arc::new(AtomicBool::new(false));
        let worker_state = Arc::clone(&state);
        let worker_shutdown = Arc::clone(&shutdown);
        std::thread::spawn(move || {
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let connection_state = Arc::clone(&worker_state);
                        std::thread::spawn(move || {
                            handle_gateway_connection(connection_state, stream);
                        });
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
            state,
            shutdown,
        }
    }

    fn port(&self) -> u16 {
        self.addr.port()
    }

    fn set_response(&self, status: u16, payload: Value) {
        self.state.lock().expect("fake gateway state").response = (status, payload);
    }

    fn set_close_without_response(&self) {
        self.state
            .lock()
            .expect("fake gateway state")
            .close_without_response = true;
    }

    fn requests(&self) -> Vec<RecordedRequest> {
        self.state
            .lock()
            .expect("fake gateway state")
            .requests
            .clone()
    }
}

impl Drop for FakeGateway {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
    }
}

fn handle_gateway_connection(state: Arc<Mutex<FakeState>>, mut stream: TcpStream) {
    let _ = stream.set_nonblocking(false);
    let request = match um_gateway::http::read_request(&mut stream, IpAddr::V4(Ipv4Addr::LOCALHOST))
    {
        Ok(Some(request)) => request,
        _ => return,
    };
    let mut headers = Vec::new();
    for header in request.headers.iter() {
        headers.push((header.name.clone(), header.value.clone()));
    }
    let body = match &request.body {
        um_gateway::http::BodyRead::Complete(bytes) => bytes.clone(),
        _ => Vec::new(),
    };
    let response = {
        let mut state = state.lock().expect("fake gateway state");
        state.requests.push(RecordedRequest {
            method: request.method.clone(),
            path: request.path().to_string(),
            headers,
            body,
        });
        if state.close_without_response {
            None
        } else {
            Some(state.response.clone())
        }
    };
    let Some((status, payload)) = response else {
        return;
    };
    let response = um_gateway::http::Response::json(status, &payload);
    let _ = um_gateway::http::write_response(&mut stream, &response);
}

fn start_console_for(temp: &TempDataDir, gateway: &FakeGateway, token: Option<&[u8]>) -> Console {
    let control = temp.path().join("admin-control-token");
    if let Some(token) = token {
        std::fs::write(&control, token).expect("control token");
    }
    start_console_with(temp, stopped_runtime(), |config| {
        config.role = Role::Cloud;
        config.gateway_port = gateway.port();
        config.control_token_file = token.map(|_| control.clone());
    })
}

fn start_request(console: &Console) -> ClientResponse {
    let mut request = ClientRequest::post(START_PATH, "{}".to_string());
    request = request.header("X-Requested-With", "XMLHttpRequest");
    client_request(console.addr, &request).expect("request")
}

fn contains(body: &[u8], needle: &str) -> bool {
    !needle.is_empty()
        && body
            .windows(needle.len())
            .any(|window| window == needle.as_bytes())
}

fn closed_port() -> u16 {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind");
    let port = listener.local_addr().expect("addr").port();
    drop(listener);
    port
}

fn full_status(state: &str, label: &str, error: Value, recovery: Value) -> Value {
    json!({
        "state": state,
        "label": label,
        "error": error,
        "primary": null,
        "evidence": {
            "verdict": null,
            "reason": null,
            "upstream_status": null,
            "checked_epoch": null,
            "last_valid_epoch": null,
            "source": null,
        },
        "login": null,
        "gate": null,
        "watch": {},
        "recovery": recovery,
        "login_window_deadline_epoch": 1763540000.0,
        "login_window_s": 190.0,
    })
}

#[test]
fn a_gateway_202_start_is_relayed_as_a_running_round() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(
        202,
        full_status(
            "logging_in",
            "登录中",
            Value::Null,
            json!({
                "state": "running",
                "round": 1,
                "session_starts": 1,
                "max_sessions": 3,
                "next_attempt_epoch": null,
                "last_failure_epoch": null,
                "last_failure_reason": null,
                "pause_reason": null,
                "visible": true,
            }),
        ),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = start_request(&console);

    assert_eq!(response.status, 200);
    assert_eq!(
        response.json(),
        json!({
            "started": true,
            "state": "logging_in",
            "label": "登录中",
            "error": null,
            "primary": null,
            "login": null,
            "recovery": {
                "state": "running",
                "round": 1,
                "session_starts": 1,
                "max_sessions": 3,
                "next_attempt_epoch": null,
                "pause_reason": null,
                "primary": null,
            },
            "login_window_deadline_epoch": 1763540000.0,
            "login_window_s": 190.0,
        })
    );
    let requests = gateway.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].method, "POST");
    assert_eq!(requests[0].path, "/admin/auth/login");
    assert_eq!(requests[0].header("X-Admin-Control"), Some(CONTROL_TOKEN));
    assert_eq!(
        serde_json::from_slice::<Value>(&requests[0].body).expect("body"),
        json!({"visible": true})
    );
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_gateway_200_start_is_relayed_too() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(
        200,
        full_status(
            "logging_in",
            "登录中",
            Value::Null,
            json!({
                "state": "running",
                "round": 1,
                "session_starts": 1,
                "max_sessions": 3,
                "next_attempt_epoch": null,
                "pause_reason": null,
            }),
        ),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = start_request(&console);

    assert_eq!(response.status, 200);
    let body = response.json();
    assert_eq!(body["started"], json!(true));
    assert_eq!(body["state"], json!("logging_in"));
    assert_eq!(body["recovery"]["state"], json!("running"));
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_click_while_a_round_is_active_joins_the_relayed_round() {
    let temp = temp();
    let gateway = FakeGateway::start();
    // The gateway's `start_explicit` joins the active round instead of opening a
    // second browser; the console relays that one round's state unchanged.
    gateway.set_response(
        202,
        full_status(
            "logging_in",
            "登录中",
            Value::Null,
            json!({
                "state": "running",
                "round": 2,
                "session_starts": 1,
                "max_sessions": 3,
                "next_attempt_epoch": null,
                "pause_reason": null,
            }),
        ),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let first = start_request(&console);
    let second = start_request(&console);

    for response in [first, second] {
        assert_eq!(response.status, 200);
        let body = response.json();
        assert_eq!(body["started"], json!(true));
        assert_eq!(body["state"], json!("logging_in"));
        assert_eq!(body["recovery"]["round"], json!(2));
    }
    // Each click is one gateway POST; the single-round constraint itself lives
    // in the gateway's `start_explicit`, not in a second console-side browser.
    assert_eq!(gateway.requests().len(), 2);
}

#[test]
fn a_global_attempt_cap_is_relayed_truthfully_with_the_retry_time() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(
        202,
        full_status(
            "needs_login",
            "需要登录",
            json!("reauth_refused:global_attempt_cap"),
            json!({
                "state": "waiting",
                "round": 1,
                "session_starts": 0,
                "max_sessions": 3,
                "next_attempt_epoch": 1763540300.0,
                "pause_reason": null,
            }),
        ),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = start_request(&console);

    assert_eq!(response.status, 200);
    let body = response.json();
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["state"], json!("needs_login"));
    assert_eq!(body["error"], json!("reauth_refused:global_attempt_cap"));
    assert_eq!(body["recovery"]["state"], json!("waiting"));
    assert_eq!(
        body["recovery"]["next_attempt_epoch"],
        json!(1763540300.0)
    );
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn an_unreachable_gateway_fails_closed() {
    let temp = temp();
    let port = closed_port();
    let control = temp.path().join("admin-control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = Role::Cloud;
        config.gateway_port = port;
        config.control_token_file = Some(control);
    });

    let response = start_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("login_start_unreachable"));
    assert!(body["detail"].as_str().unwrap().contains("不可达"));
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_closed_gateway_connection_without_an_answer_fails_closed() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_close_without_response();
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = start_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("login_start_unreachable"));
    assert_eq!(gateway.requests().len(), 1);
}

#[test]
fn an_unreadable_control_token_fails_closed_without_contacting_the_gateway() {
    let temp = temp();
    let gateway = FakeGateway::start();
    let console = start_console_for(&temp, &gateway, Some(&[0xffu8, 0xfe][..]));

    let response = start_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("control_token_unreadable"));
    assert!(body["detail"].as_str().unwrap().contains("不可读"));
    assert!(gateway.requests().is_empty());
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_missing_control_token_fails_closed_without_contacting_the_gateway() {
    let temp = temp();
    let gateway = FakeGateway::start();
    let console = start_console_for(&temp, &gateway, None);

    let response = start_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("control_token_absent"));
    assert!(gateway.requests().is_empty());
}

#[test]
fn a_malformed_gateway_answer_fails_closed() {
    for payload in [
        json!({}),
        json!({"state": 7}),
        json!({"state": ""}),
        json!({"label": "登录中"}),
        json!(["logging_in"]),
    ] {
        let temp = temp();
        let gateway = FakeGateway::start();
        gateway.set_response(202, payload);
        let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

        let response = start_request(&console);

        assert_eq!(response.status, 503);
        let body = response.json();
        assert_eq!(body["started"], json!(false));
        assert_eq!(body["state"], Value::Null);
        assert_eq!(body["error"], json!("login_start_schema"));
        assert!(body["detail"].as_str().unwrap().contains("不符合契约"));
        assert!(!contains(&response.body, CONTROL_TOKEN));
    }
}

#[test]
fn a_gateway_refusal_fails_closed_with_the_http_status() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(500, json!({"error": "login_unassembled"}));
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = start_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("login_start_failed"));
    assert!(body["detail"].as_str().unwrap().contains("HTTP 500"));
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_gateway_control_denial_is_distinguishable() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(403, json!({"error": "control denied"}));
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = start_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("control_denied"));
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn the_login_start_route_keeps_the_console_write_boundary() {
    let temp = temp();
    let gateway = FakeGateway::start();
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let request = ClientRequest::post(START_PATH, "{}".to_string());
    let response = client_request(console.addr, &request).expect("request");

    assert_eq!(response.status, 403);
    assert_eq!(
        response.json(),
        json!({"error": "same-origin marker or control token required"})
    );
    assert!(gateway.requests().is_empty());
}

#[test]
fn the_operations_api_relogin_reaches_the_visible_gateway_round() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(
        202,
        full_status(
            "logging_in",
            "登录中",
            Value::Null,
            json!({
                "state": "running",
                "round": 1,
                "session_starts": 1,
                "max_sessions": 3,
                "next_attempt_epoch": null,
                "last_failure_epoch": null,
                "last_failure_reason": null,
                "pause_reason": null,
                "visible": true,
            }),
        ),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let catalogue =
        client_request(console.addr, &ClientRequest::get("/api/console/actions")).expect("catalogue");
    assert_eq!(catalogue.status, 200);
    assert!(contains(&catalogue.body, "\"account.login\""));

    let body = json!({
        "action": "account.login",
        "target": "share",
        "params": {},
        "idempotency_key": "relogin-1",
    })
    .to_string();
    let created = client_request(
        console.addr,
        &ClientRequest::post("/api/console/operations", body.clone())
            .header("X-Requested-With", "XMLHttpRequest")
            .json_body(body),
    )
    .expect("submit");
    assert_eq!(created.status, 201);
    let op_id = created.json()["operation"]["id"]
        .as_str()
        .expect("operation id")
        .to_string();

    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    let mut terminal = None;
    while std::time::Instant::now() < deadline {
        let read = client_request(
            console.addr,
            &ClientRequest::get(&format!("/api/console/operations/{op_id}")),
        )
        .expect("poll");
        let operation = read.json()["operation"].clone();
        let state = operation["state"].as_str().unwrap_or_default();
        if matches!(state, "succeeded" | "failed" | "cancelled" | "interrupted") {
            terminal = Some(operation);
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    let operation = terminal.expect("terminal operation");
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["started"], json!(true));

    let requests = gateway.requests();
    let start = requests
        .iter()
        .find(|request| request.path == "/admin/auth/login")
        .expect("gateway login request");
    assert_eq!(start.method, "POST");
    assert_eq!(start.header("X-Admin-Control"), Some(CONTROL_TOKEN));
    let payload: Value = serde_json::from_slice(&start.body).expect("visible body");
    assert_eq!(payload, json!({"visible": true}));
}
