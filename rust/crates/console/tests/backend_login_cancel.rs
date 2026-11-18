//! #106: the cloud console's cancel-login write face.
//!
//! `POST /api/console/share-management/backend-login-cancel` proxies the
//! gateway's control-token-gated `POST /admin/auth/cancel` through the same
//! `AdminClient` the UM auth actions use. A success, "no active round", an
//! unreachable gateway, an unreadable control token and a malformed gateway
//! answer are each observable; no failure fabricates a cancelled round.

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
const CANCEL_PATH: &str = "/api/console/share-management/backend-login-cancel";

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

/// A loopback gateway stand-in for `POST /admin/auth/cancel`.
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
            response: (
                200,
                json!({"cancelled": true, "state": "cancelled", "detail": "已取消"}),
            ),
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

fn cancel_request(console: &Console) -> ClientResponse {
    let mut request = ClientRequest::post(CANCEL_PATH, "{}".to_string());
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

#[test]
fn a_gateway_200_cancel_is_relayed_and_carries_the_control_token() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(
        200,
        json!({
            "cancelled": true,
            "state": "cancelled",
            "detail": "已取消本轮登录；服务继续运行，普通请求不会自动重开",
        }),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = cancel_request(&console);

    assert_eq!(response.status, 200);
    assert_eq!(
        response.json(),
        json!({
            "cancelled": true,
            "state": "cancelled",
            "detail": "已取消本轮登录；服务继续运行，普通请求不会自动重开",
        })
    );
    let requests = gateway.requests();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].method, "POST");
    assert_eq!(requests[0].path, "/admin/auth/cancel");
    assert_eq!(requests[0].header("X-Admin-Control"), Some(CONTROL_TOKEN));
    assert!(requests[0].body.is_empty());
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_gateway_202_acceptance_is_relayed_as_success() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(
        202,
        json!({"cancelled": true, "state": "cancelled", "detail": null}),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = cancel_request(&console);

    assert_eq!(response.status, 200);
    assert_eq!(
        response.json(),
        json!({"cancelled": true, "state": "cancelled", "detail": null})
    );
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn no_active_round_is_relayed_truthfully_not_as_a_cancel() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(
        200,
        json!({
            "cancelled": false,
            "state": "no_login_in_progress",
            "detail": "当前没有进行中的登录",
        }),
    );
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = cancel_request(&console);

    assert_eq!(response.status, 200);
    let body = response.json();
    assert_eq!(body["cancelled"], json!(false));
    assert_eq!(body["state"], json!("no_login_in_progress"));
    assert_eq!(body["detail"], json!("当前没有进行中的登录"));
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

    let response = cancel_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["cancelled"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("login_cancel_unreachable"));
    assert!(body["detail"].as_str().unwrap().contains("不可达"));
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_closed_gateway_connection_without_an_answer_fails_closed() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_close_without_response();
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = cancel_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["error"], json!("login_cancel_unreachable"));
    assert_eq!(body["cancelled"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(gateway.requests().len(), 1);
}

#[test]
fn an_unreadable_control_token_fails_closed_without_contacting_the_gateway() {
    let temp = temp();
    let gateway = FakeGateway::start();
    let console = start_console_for(&temp, &gateway, Some(&[0xffu8, 0xfe][..]));

    let response = cancel_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["cancelled"], json!(false));
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

    let response = cancel_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["error"], json!("control_token_absent"));
    assert_eq!(body["cancelled"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert!(gateway.requests().is_empty());
}

#[test]
fn a_malformed_gateway_answer_fails_closed() {
    for payload in [
        json!({"cancelled": true}),
        json!({"cancelled": "yes", "state": "cancelled"}),
        json!({"cancelled": false, "state": 7}),
        json!(["cancelled"]),
    ] {
        let temp = temp();
        let gateway = FakeGateway::start();
        gateway.set_response(200, payload);
        let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

        let response = cancel_request(&console);

        assert_eq!(response.status, 503);
        let body = response.json();
        assert_eq!(body["cancelled"], json!(false));
        assert_eq!(body["state"], Value::Null);
        assert_eq!(body["error"], json!("login_cancel_schema"));
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

    let response = cancel_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["cancelled"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("login_cancel_failed"));
    assert!(body["detail"].as_str().unwrap().contains("HTTP 500"));
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn a_gateway_control_denial_is_distinguishable() {
    let temp = temp();
    let gateway = FakeGateway::start();
    gateway.set_response(403, json!({"error": "control denied"}));
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let response = cancel_request(&console);

    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["cancelled"], json!(false));
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], json!("control_denied"));
    assert!(!contains(&response.body, CONTROL_TOKEN));
}

#[test]
fn the_cancel_route_keeps_the_console_write_boundary() {
    let temp = temp();
    let gateway = FakeGateway::start();
    let console = start_console_for(&temp, &gateway, Some(CONTROL_TOKEN.as_bytes()));

    let request = ClientRequest::post(CANCEL_PATH, "{}".to_string());
    let response = client_request(console.addr, &request).expect("request");

    assert_eq!(response.status, 403);
    assert_eq!(
        response.json(),
        json!({"error": "same-origin marker or control token required"})
    );
    assert!(gateway.requests().is_empty());
}
