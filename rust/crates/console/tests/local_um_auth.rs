//! #93: the local 8001 UM authentication actions run in Rust.
//!
//! `account.um_status`, `account.um_verify` and `account.login` talk to the
//! local gateway's `/admin/auth/*` control face directly; no Python sidecar is
//! involved. A fake loopback gateway serves scripted status/verify/login answers,
//! and a scripted runtime plus service control pin the login orchestration
//! without a real browser, service or upstream.

mod support;

use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

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

#[derive(Debug)]
struct FakeState {
    status: Vec<(u16, Value)>,
    verify: (u16, Value),
    login: (u16, Value),
    requests: Vec<RecordedRequest>,
    close: bool,
}

/// A loopback gateway stand-in for `/admin/auth/status|verify|login`.
struct FakeAdmin {
    addr: SocketAddr,
    state: Arc<Mutex<FakeState>>,
    shutdown: Arc<AtomicBool>,
}

impl FakeAdmin {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake gateway bind");
        listener
            .set_nonblocking(true)
            .expect("fake gateway nonblocking");
        let addr = listener.local_addr().expect("fake gateway addr");
        let state = Arc::new(Mutex::new(FakeState {
            status: Vec::new(),
            verify: (
                200,
                json!({"verify": "valid", "reason": null,
                       "upstream_status": 200, "checked_epoch": 1}),
            ),
            login: (202, json!({"state": "logging_in"})),
            requests: Vec::new(),
            close: false,
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
                            handle_admin_connection(connection_state, stream);
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

    fn push_status(&self, status: Value) {
        self.state
            .lock()
            .expect("fake gateway state")
            .status
            .push((200, status));
    }

    fn set_status(&self, statuses: Vec<Value>) {
        self.state.lock().expect("fake gateway state").status =
            statuses.into_iter().map(|status| (200, status)).collect();
    }

    fn request_count(&self) -> usize {
        self.state
            .lock()
            .expect("fake gateway state")
            .requests
            .len()
    }

    fn set_verify(&self, code: u16, payload: Value) {
        self.state.lock().expect("fake gateway state").verify = (code, payload);
    }

    fn set_login(&self, code: u16, payload: Value) {
        self.state.lock().expect("fake gateway state").login = (code, payload);
    }

    fn set_close(&self) {
        self.state.lock().expect("fake gateway state").close = true;
    }

    fn requests(&self) -> Vec<RecordedRequest> {
        self.state
            .lock()
            .expect("fake gateway state")
            .requests
            .clone()
    }
}

impl Drop for FakeAdmin {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
    }
}

fn handle_admin_connection(state: Arc<Mutex<FakeState>>, mut stream: TcpStream) {
    let request = match um_gateway::http::read_request(&mut stream, IpAddr::V4(Ipv4Addr::LOCALHOST))
    {
        Ok(Some(request)) => request,
        _ => return,
    };
    let method = request.method.clone();
    let path = request.path().to_string();
    let mut headers = Vec::new();
    for header in request.headers.iter() {
        headers.push((header.name.clone(), header.value.clone()));
    }
    let body = match &request.body {
        um_gateway::http::BodyRead::Complete(bytes) => bytes.clone(),
        _ => Vec::new(),
    };
    let (response, close) = {
        let mut state = state.lock().expect("fake gateway state");
        state.requests.push(RecordedRequest {
            method: method.clone(),
            path: path.clone(),
            headers,
            body,
        });
        let close = state.close;
        let response = match path.as_str() {
            "/admin/auth/status" => {
                let response = if state.status.len() > 1 {
                    state.status.remove(0)
                } else {
                    state
                        .status
                        .first()
                        .cloned()
                        .unwrap_or((200, json!({"state": "valid"})))
                };
                Some(response)
            }
            "/admin/auth/verify" => Some(state.verify.clone()),
            "/admin/auth/login" => Some(state.login.clone()),
            _ => Some((404, json!({"error": "unknown endpoint"}))),
        };
        (response, close)
    };
    if close {
        return;
    }
    let Some((status, payload)) = response else {
        return;
    };
    let response = um_gateway::http::Response::json(status, &payload);
    let _ = um_gateway::http::write_response(&mut stream, &response);
}

fn fake_admin_config(temp: &TempDataDir, gateway: &FakeAdmin) -> um_console::config::ConsoleConfig {
    let mut config = base_config(temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = gateway.port();
    let control = temp.path().join("admin-control-token");
    std::fs::write(&control, format!("{CONTROL_TOKEN}\n")).expect("control token");
    config.control_token_file = Some(control);
    // Any accidental sidecar launch must fail: this executable does not exist.
    config.python_exe = temp.path().join("python-must-not-run");
    config.repo_root = temp.path().to_path_buf();
    config
}

fn start_local(
    temp: &TempDataDir,
    gateway: &FakeAdmin,
    runtime: Arc<dyn um_console::runtime::RuntimeFacts>,
) -> Console {
    let config = fake_admin_config(temp, gateway);
    Console::from_server(
        ConsoleServer::start_with_runtime(config, runtime).expect("start local console"),
    )
}

fn request(console: &Console, method: &str, target: &str, body: Option<Value>) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    if let Some(body) = body {
        request = request.json_body(body.to_string());
    }
    client_request(console.addr, &request).expect("request")
}

fn submit(console: &Console, action: &str, target: &str, params: Value) -> ClientResponse {
    request(
        console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": action, "target": target, "params": params})),
    )
}

fn wait_terminal(console: &Console, operation: &Value) -> Value {
    let op_id = operation["id"].as_str().expect("operation id").to_string();
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let fetched = request(
            console,
            "GET",
            &format!("/api/console/operations/{op_id}"),
            None,
        );
        assert_eq!(fetched.status, 200);
        let operation = fetched.json()["operation"].clone();
        if matches!(
            operation["state"].as_str(),
            Some("succeeded" | "failed" | "cancelled" | "interrupted")
        ) {
            return operation;
        }
        assert!(
            Instant::now() < deadline,
            "operation never finished: {operation}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn submit_and_wait(console: &Console, action: &str, params: Value) -> Value {
    let created = submit(console, action, "chat2api", params);
    assert_eq!(created.status, 201);
    wait_terminal(console, &created.json()["operation"])
}

#[test]
fn um_status_reads_the_gateway_round_and_reports_the_memory_state() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.push_status(json!({
        "state": "logging_in",
        "label": "登录中（需人工：duo_prompt_waiting）",
        "error": null,
        "primary": null,
        "recovery": {
            "state": "running", "round": 2, "session_starts": 1,
            "max_sessions": 6, "next_attempt_epoch": null,
            "pause_reason": null,
        },
        "login": {
            "phase": "awaiting_duo",
            "origin": "https://api-1234.duosecurity.com/prompt/xyz",
            "manual_reason": "duo_prompt_waiting",
        },
        "login_window_deadline_epoch": 2800,
        "login_window_s": 1800,
    }));
    let console = start_local(&temp, &gateway, stopped_runtime());

    let operation = submit_and_wait(&console, "account.um_status", json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["state"], json!("logging_in"));
    assert_eq!(
        result["label"],
        json!("登录中（需人工：duo_prompt_waiting）")
    );
    assert_eq!(result["source"], json!("memory-state"));
    assert_eq!(result["verified"], json!(false));
    assert_eq!(result["state_known"], json!(true));
    assert!(result["checked_epoch"].is_i64());
    assert_eq!(result["login_window_deadline_epoch"], json!(2800.0));
    assert_eq!(result["login_window_s"], json!(1800.0));
    assert_eq!(result["recovery"]["round"], json!(2));
    assert_eq!(result["login"]["phase"], json!("awaiting_duo"));
    assert_eq!(
        result["login"]["manual_reason"],
        json!("duo_prompt_waiting")
    );
    assert!(result["note"]
        .as_str()
        .unwrap_or_default()
        .contains("内存态"));

    let requests = gateway.requests();
    assert_eq!(requests.len(), 1, "{requests:?}");
    assert_eq!(requests[0].method, "GET");
    assert_eq!(requests[0].path, "/admin/auth/status");
    assert_eq!(requests[0].header("X-Admin-Control"), Some(CONTROL_TOKEN));
    assert_eq!(requests[0].header("Origin"), None);
}

#[test]
fn um_verify_reports_the_restricted_gateway_verdicts() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    let console = start_local(&temp, &gateway, stopped_runtime());

    for (verify, reason, upstream_status, checked_epoch) in [
        ("valid", None, Some(200), 1_726_000_000i64),
        ("rejected", Some("upstream_401"), Some(401), 1_726_000_001),
        ("needs_login", None, None, 1_726_000_002),
        ("unknown", Some("upstream_5xx"), Some(503), 1_726_000_003),
    ] {
        gateway.set_verify(
            200,
            json!({
                "verify": verify,
                "reason": reason,
                "upstream_status": upstream_status,
                "checked_epoch": checked_epoch,
                "note": "endpoint note",
            }),
        );
        let operation = submit_and_wait(&console, "account.um_verify", json!({}));
        assert_eq!(operation["state"], json!("succeeded"), "{operation}");
        let result = &operation["result"];
        assert_eq!(result["verify"], json!(verify), "{verify}");
        assert_eq!(result["reason"], json!(reason), "{verify}");
        assert_eq!(
            result["upstream_status"],
            json!(upstream_status),
            "{verify}"
        );
        assert_eq!(result["checked_epoch"], json!(checked_epoch), "{verify}");
        assert!(result["note"]
            .as_str()
            .unwrap_or_default()
            .contains("受限核验"));
    }

    let requests = gateway.requests();
    assert_eq!(requests.len(), 4, "{requests:?}");
    for request in requests {
        assert_eq!(request.method, "GET");
        assert_eq!(request.path, "/admin/auth/verify");
        assert_eq!(request.header("X-Admin-Control"), Some(CONTROL_TOKEN));
        assert_eq!(request.header("Origin"), None);
    }
}

#[test]
fn um_verify_maps_transport_and_schema_failures() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    let console = start_local(&temp, &gateway, stopped_runtime());

    gateway.set_verify(403, json!({"error": "denied"}));
    let denied = submit_and_wait(&console, "account.um_verify", json!({}));
    assert_eq!(denied["state"], json!("failed"));
    assert_eq!(denied["error"]["code"], json!("control_denied"));

    gateway.set_verify(503, json!({"error": "boom"}));
    let failed = submit_and_wait(&console, "account.um_verify", json!({}));
    assert_eq!(failed["state"], json!("failed"));
    assert_eq!(failed["error"]["code"], json!("verify_query_failed"));
    assert!(failed["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("HTTP 503"));
    assert!(failed["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("未知 ≠ 失效"));

    gateway.set_verify(200, json!({"verify": "unrecognized"}));
    let schema = submit_and_wait(&console, "account.um_verify", json!({}));
    assert_eq!(schema["state"], json!("failed"));
    assert_eq!(schema["error"]["code"], json!("verify_schema"));
    assert_eq!(
        schema["error"]["message"],
        json!("受限核验响应形状与预期不符")
    );

    gateway.set_verify(
        200,
        json!({"verify": "valid", "reason": null, "upstream_status": 200}),
    );
    let before = epoch_seconds();
    let missing_clock = submit_and_wait(&console, "account.um_verify", json!({}));
    assert_eq!(missing_clock["state"], json!("succeeded"));
    assert!(
        missing_clock["result"]["checked_epoch"]
            .as_i64()
            .unwrap_or_default()
            >= before
    );
    assert!(missing_clock["result"]["note"]
        .as_str()
        .unwrap_or_default()
        .contains("端点未带时间"));

    gateway.set_close();
    let closed = submit_and_wait(&console, "account.um_verify", json!({}));
    assert_eq!(closed["state"], json!("failed"));
    assert_eq!(closed["error"]["code"], json!("status_unreachable"));
    assert!(closed["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("未知 ≠ 失效"));
}

fn epoch_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock after epoch")
        .as_secs() as i64
}

fn running_runtime() -> Arc<dyn um_console::runtime::RuntimeFacts> {
    Arc::new(um_console::runtime::StaticRuntimeFacts::new(
        um_console::runtime::RuntimeSnapshot::running(Vec::new()),
    ))
}

#[test]
fn login_skips_the_round_when_the_gateway_reports_a_valid_credential() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.set_verify(
        200,
        json!({"verify": "valid", "reason": null,
               "upstream_status": 200, "checked_epoch": 1}),
    );
    let console = start_local(&temp, &gateway, running_runtime());

    let operation = submit_and_wait(&console, "account.login", json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["was_running"], json!(true));
    assert_eq!(result["restored"], json!(false));
    assert_eq!(result["final_state"], json!("valid"));
    assert_eq!(result["login_skipped"], json!(true));
    assert_eq!(result["login_waited_s"], json!(0.0));
    assert!(result["note"]
        .as_str()
        .unwrap_or_default()
        .contains("无需重新登录"));

    let requests = gateway.requests();
    assert_eq!(requests.len(), 1, "{requests:?}");
    assert_eq!(requests[0].method, "GET");
    assert_eq!(requests[0].path, "/admin/auth/verify");
}

#[test]
fn login_follows_a_rejected_verification_round_to_valid() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.set_verify(
        200,
        json!({"verify": "rejected", "reason": "upstream_401",
               "upstream_status": 401, "checked_epoch": 1}),
    );
    gateway.set_status(vec![
        json!({"state": "logging_in", "error": null, "login": null,
               "recovery": {"state": "running"}}),
        json!({"state": "valid", "error": null, "login": null,
               "recovery": {"state": "idle"}}),
    ]);
    let console = start_local(&temp, &gateway, running_runtime());

    let operation = submit_and_wait(&console, "account.login", json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["final_state"], json!("valid"));
    assert_eq!(result["login_skipped"], json!(false));
    assert!(result["login_waited_s"].as_f64().unwrap_or_default() >= 0.0);
    assert!(result["note"]
        .as_str()
        .unwrap_or_default()
        .contains("登录完成（state=valid）"));

    let requests = gateway.requests();
    let calls: Vec<(&str, &str)> = requests
        .iter()
        .map(|request| (request.method.as_str(), request.path.as_str()))
        .collect();
    assert_eq!(
        calls,
        vec![
            ("GET", "/admin/auth/verify"),
            ("POST", "/admin/auth/login"),
            ("GET", "/admin/auth/status"),
            ("GET", "/admin/auth/status"),
        ]
    );
    let post = requests
        .iter()
        .find(|request| request.method == "POST")
        .expect("login POST");
    assert_eq!(post.header("X-Admin-Control"), Some(CONTROL_TOKEN));
    assert_eq!(post.header("Content-Type"), Some("application/json"));
    assert_eq!(post.header("Origin"), None);
    assert_eq!(post.body, br#"{"visible":false}"#.to_vec());
}

#[test]
fn login_refuses_an_unknown_verification_without_opening_a_round() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.set_verify(
        200,
        json!({"verify": "unknown", "reason": "upstream_5xx",
               "upstream_status": 503, "checked_epoch": 1}),
    );
    let console = start_local(&temp, &gateway, running_runtime());

    let operation = submit_and_wait(&console, "account.login", json!({}));

    assert_eq!(operation["state"], json!("failed"));
    assert_eq!(
        operation["error"]["code"],
        json!("login_verification_unknown")
    );
    let message = operation["error"]["message"].as_str().unwrap_or_default();
    assert!(message.contains("无法确认当前认证，暂不打开登录"));
    assert!(message.contains("受限核验结果为 unknown"));
    assert!(message.contains("服务保持运行"));
    let requests = gateway.requests();
    assert_eq!(requests.len(), 1, "{requests:?}");
    assert_eq!(requests[0].path, "/admin/auth/verify");
}

#[test]
fn login_maps_duo_and_terminal_manual_rounds() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    let console = start_local(&temp, &gateway, running_runtime());

    gateway.set_verify(
        200,
        json!({"verify": "needs_login", "reason": null,
               "upstream_status": null, "checked_epoch": 1}),
    );
    gateway.set_status(vec![json!({
        "state": "logging_in", "error": null,
        "login": {
            "phase": "awaiting_duo",
            "origin": "https://api-1234.duosecurity.com/prompt/xyz",
            "manual_reason": "duo_prompt_waiting",
        },
        "recovery": {"state": "running"},
    })]);
    let duo = submit_and_wait(&console, "account.login", json!({}));
    assert_eq!(duo["state"], json!("failed"));
    assert_eq!(
        duo["error"]["code"],
        json!("login_manual_takeover_required")
    );
    assert_eq!(
        duo["error"]["message"],
        json!("登录需要本人确认 Duo 推送；本轮仍在等待，可打开可见窗口")
    );

    gateway.set_status(vec![json!({
        "state": "failed",
        "error": "manual_takeover_required:umpass_mfa_required",
        "primary": null,
        "recovery": {
            "state": "exhausted",
            "pause_reason": "manual_takeover_required:umpass_mfa_required",
        },
    })]);
    let manual = submit_and_wait(&console, "account.login", json!({}));
    assert_eq!(manual["state"], json!("failed"));
    assert_eq!(
        manual["error"]["code"],
        json!("login_manual_takeover_required")
    );
    let message = manual["error"]["message"].as_str().unwrap_or_default();
    assert!(message.contains("manual_takeover_required:umpass_mfa_required"));
    assert!(message.contains("浏览器已关闭"));
}

#[test]
fn login_maps_cancelled_and_timeout_rounds_honestly() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    let console = start_local(&temp, &gateway, running_runtime());

    gateway.set_verify(
        200,
        json!({"verify": "rejected", "reason": null,
               "upstream_status": 401, "checked_epoch": 1}),
    );
    gateway.set_status(vec![json!({
        "state": "cancelled",
        "error": "cancelled",
        "primary": null,
        "recovery": {"state": "exhausted", "pause_reason": "cancelled"},
    })]);
    let cancelled = submit_and_wait(&console, "account.login", json!({}));
    assert_eq!(cancelled["state"], json!("cancelled"), "{cancelled}");
    assert_eq!(cancelled["error"]["code"], json!("cancelled"));

    gateway.set_status(vec![json!({
        "state": "logging_in",
        "error": null,
        "login": null,
        "recovery": {"state": "running"},
    })]);
    let timeout = submit_and_wait(
        &console,
        "account.login",
        json!({"login_budget_s": 1, "poll_interval_s": 1}),
    );
    assert_eq!(timeout["state"], json!("failed"), "{timeout}");
    assert_eq!(timeout["error"]["code"], json!("login_timeout_not_settled"));
    let message = timeout["error"]["message"].as_str().unwrap_or_default();
    assert!(message.contains("登录在控制台等待上限内未确认完成"));
    assert!(message.contains("复查状态=logging_in"));
    assert!(message.contains("服务保持运行"));
}

#[test]
fn login_reports_the_gateway_round_errors_without_a_second_round() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    let console = start_local(&temp, &gateway, running_runtime());

    for (body, expected_code) in [
        (
            json!({"error": {"code": "login_already_running"}}),
            "login_busy",
        ),
        (
            json!({"error": {"code": "visible_login_unavailable"}}),
            "login_unavailable",
        ),
    ] {
        gateway.set_verify(
            200,
            json!({"verify": "needs_login", "reason": null,
                   "upstream_status": null, "checked_epoch": 1}),
        );
        gateway.set_login(409, body);
        let operation = submit_and_wait(&console, "account.login", json!({}));
        assert_eq!(operation["state"], json!("failed"));
        assert_eq!(operation["error"]["code"], json!(expected_code));
    }

    gateway.set_login(500, json!({"error": "boom"}));
    let failed = submit_and_wait(&console, "account.login", json!({}));
    assert_eq!(failed["state"], json!("failed"));
    assert_eq!(failed["error"]["code"], json!("login_unreachable"));
    assert!(failed["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("HTTP 500"));
}

#[test]
fn cancelling_a_running_login_records_cancelled() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.set_verify(
        200,
        json!({"verify": "needs_login", "reason": null,
               "upstream_status": null, "checked_epoch": 1}),
    );
    gateway.set_status(vec![json!({
        "state": "logging_in",
        "error": null,
        "login": null,
        "recovery": {"state": "running"},
    })]);
    let console = start_local(&temp, &gateway, running_runtime());

    let created = submit(
        &console,
        "account.login",
        "chat2api",
        json!({"login_budget_s": 120, "poll_interval_s": 1}),
    );
    assert_eq!(created.status, 201);
    let op_id = created.json()["operation"]["id"]
        .as_str()
        .expect("operation id")
        .to_string();
    wait_for_requests(&gateway, 3);

    let cancelled = request(
        &console,
        "POST",
        &format!("/api/console/operations/{op_id}/cancel"),
        None,
    );
    assert_eq!(cancelled.status, 200);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("cancelled"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("cancelled"));
}

fn wait_for_requests(gateway: &FakeAdmin, count: usize) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while gateway.request_count() < count {
        assert!(
            Instant::now() < deadline,
            "gateway never saw {count} request(s)"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

struct ScriptedRuntime {
    states: Mutex<std::collections::VecDeque<um_console::runtime::RuntimeSnapshot>>,
}

impl ScriptedRuntime {
    fn new(states: Vec<um_console::runtime::RuntimeSnapshot>) -> Self {
        Self {
            states: Mutex::new(states.into()),
        }
    }
}

impl um_console::runtime::RuntimeFacts for ScriptedRuntime {
    fn snapshot(
        &self,
    ) -> Result<um_console::runtime::RuntimeSnapshot, um_console::runtime::RuntimeError> {
        let mut states = self.states.lock().expect("scripted runtime");
        if states.len() > 1 {
            Ok(states.pop_front().expect("scripted runtime state"))
        } else {
            Ok(states
                .front()
                .cloned()
                .unwrap_or_else(|| um_console::runtime::RuntimeSnapshot::stopped("none")))
        }
    }
}

struct FakeServiceControl {
    calls: Mutex<Vec<&'static str>>,
}

impl FakeServiceControl {
    fn new() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
        }
    }

    fn calls(&self) -> Vec<&'static str> {
        self.calls.lock().expect("service calls").clone()
    }
}

impl um_console::local_um_auth::ServiceControl for FakeServiceControl {
    fn start(
        &self,
        _ctx: &um_console::operation_manager::OperationContext,
    ) -> Result<(), um_console::operation_manager::ActionFailure> {
        self.calls.lock().expect("service calls").push("start");
        Ok(())
    }

    fn stop(
        &self,
        _ctx: &um_console::operation_manager::OperationContext,
    ) -> Result<(), um_console::operation_manager::ActionFailure> {
        self.calls.lock().expect("service calls").push("stop");
        Ok(())
    }
}

fn start_local_with_service(
    temp: &TempDataDir,
    gateway: &FakeAdmin,
    runtime: Arc<dyn um_console::runtime::RuntimeFacts>,
    service: Arc<dyn um_console::local_um_auth::ServiceControl>,
) -> Console {
    let config = fake_admin_config(temp, gateway);
    let retained = Arc::new(um_console::RetainedActionExecutor::from_config(&config));
    let auth = um_console::UmAuthExecutor::new(&config, Arc::clone(&runtime), service);
    let executor = Arc::new(um_console::LocalActionExecutor::new(auth, retained));
    Console::from_server(
        um_console::ConsoleServer::start_with_wiring(
            config,
            runtime,
            Arc::new(um_console::share_read::UnwiredSeams),
            executor,
            Arc::new(um_console::share_write::UnwiredWriteSeams),
        )
        .expect("start local console"),
    )
}

#[test]
fn login_starts_and_restores_a_stopped_service() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.set_verify(
        200,
        json!({"verify": "valid", "reason": null,
               "upstream_status": 200, "checked_epoch": 1}),
    );
    let runtime = Arc::new(ScriptedRuntime::new(vec![
        um_console::runtime::RuntimeSnapshot::stopped("stopped"),
        um_console::runtime::RuntimeSnapshot::running(Vec::new()),
        um_console::runtime::RuntimeSnapshot::stopped("stopped"),
    ]));
    let service = Arc::new(FakeServiceControl::new());
    let console = start_local_with_service(
        &temp,
        &gateway,
        Arc::clone(&runtime) as Arc<dyn um_console::runtime::RuntimeFacts>,
        Arc::clone(&service) as Arc<dyn um_console::local_um_auth::ServiceControl>,
    );

    let operation = submit_and_wait(&console, "account.login", json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["was_running"], json!(false));
    assert_eq!(result["restored"], json!(true));
    assert_eq!(result["final_state"], json!("valid"));
    assert_eq!(result["login_skipped"], json!(true));
    assert!(result["note"]
        .as_str()
        .unwrap_or_default()
        .contains("服务已停止并复验 Stopped"));
    assert_eq!(service.calls(), vec!["start", "stop"]);
    let requests = gateway.requests();
    assert_eq!(requests.len(), 1, "{requests:?}");
    assert_eq!(requests[0].path, "/admin/auth/verify");
}

#[test]
fn login_reports_a_failed_start_without_verifying_or_stopping() {
    struct FailingStart;
    impl um_console::local_um_auth::ServiceControl for FailingStart {
        fn start(
            &self,
            _ctx: &um_console::operation_manager::OperationContext,
        ) -> Result<(), um_console::operation_manager::ActionFailure> {
            Err(um_console::operation_manager::ActionFailure::new(
                "action_failed",
                "wrapper failed",
            ))
        }

        fn stop(
            &self,
            _ctx: &um_console::operation_manager::OperationContext,
        ) -> Result<(), um_console::operation_manager::ActionFailure> {
            panic!("a failed start must not issue a stop");
        }
    }

    let temp = temp();
    let gateway = FakeAdmin::start();
    let runtime = Arc::new(ScriptedRuntime::new(vec![
        um_console::runtime::RuntimeSnapshot::stopped("stopped"),
    ]));
    let console = start_local_with_service(
        &temp,
        &gateway,
        Arc::clone(&runtime) as Arc<dyn um_console::runtime::RuntimeFacts>,
        Arc::new(FailingStart) as Arc<dyn um_console::local_um_auth::ServiceControl>,
    );

    let operation = submit_and_wait(&console, "account.login", json!({}));
    assert_eq!(operation["state"], json!("failed"));
    assert_eq!(operation["error"]["code"], json!("start_failed"));
    assert!(operation["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("未发送登录请求"));
    assert!(gateway.requests().is_empty(), "{:?}", gateway.requests());
}

#[test]
fn login_rejects_visible_without_force() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    let console = start_local(&temp, &gateway, running_runtime());

    let operation = submit_and_wait(
        &console,
        "account.login",
        json!({"visible": true, "force": false}),
    );

    assert_eq!(operation["state"], json!("failed"));
    assert_eq!(operation["error"]["code"], json!("invalid_params"));
    assert!(operation["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("visible=true"));
    assert!(gateway.requests().is_empty(), "{:?}", gateway.requests());
}

#[test]
fn login_reports_an_unconfirmed_restore_as_stop_failed() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.set_verify(
        200,
        json!({"verify": "valid", "reason": null,
               "upstream_status": 200, "checked_epoch": 1}),
    );
    let runtime = Arc::new(ScriptedRuntime::new(vec![
        um_console::runtime::RuntimeSnapshot::stopped("stopped"),
        um_console::runtime::RuntimeSnapshot::running(Vec::new()),
        um_console::runtime::RuntimeSnapshot::running(Vec::new()),
    ]));
    let service = Arc::new(FakeServiceControl::new());
    let console = start_local_with_service(
        &temp,
        &gateway,
        Arc::clone(&runtime) as Arc<dyn um_console::runtime::RuntimeFacts>,
        Arc::clone(&service) as Arc<dyn um_console::local_um_auth::ServiceControl>,
    );

    let operation = submit_and_wait(&console, "account.login", json!({}));

    assert_eq!(operation["state"], json!("failed"));
    assert_eq!(operation["error"]["code"], json!("stop_failed"));
    let message = operation["error"]["message"].as_str().unwrap_or_default();
    assert!(message.contains("无需重新登录"));
    assert!(message.contains("残留"));
    assert!(!message.contains("登录已完成"));
    assert_eq!(service.calls(), vec!["start", "stop"]);
}

#[test]
fn um_status_reports_the_three_control_token_file_failures() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.push_status(json!({"state": "valid", "label": "有效", "error": null}));
    let mut config = fake_admin_config(&temp, &gateway);
    let control = temp.path().join("admin-control-token");
    config.control_token_file = Some(control.clone());
    let console = Console::from_server(
        ConsoleServer::start_with_runtime(config, running_runtime()).expect("start console"),
    );

    std::fs::remove_file(&control).expect("remove token file");
    let absent = submit_and_wait(&console, "account.um_status", json!({}));
    assert_eq!(absent["state"], json!("failed"));
    assert_eq!(absent["error"]["code"], json!("control_token_absent"));
    assert!(absent["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("管理控制令牌文件缺席"));

    std::fs::create_dir(&control).expect("token path is a directory");
    let unreadable = submit_and_wait(&console, "account.um_status", json!({}));
    assert_eq!(unreadable["state"], json!("failed"));
    assert_eq!(
        unreadable["error"]["code"],
        json!("control_token_unreadable")
    );
    assert!(unreadable["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("状态未知"));
    std::fs::remove_dir(&control).expect("remove token directory");

    std::fs::write(&control, "  \n\t\n").expect("blank token file");
    let empty = submit_and_wait(&console, "account.um_status", json!({}));
    assert_eq!(empty["state"], json!("failed"));
    assert_eq!(empty["error"]["code"], json!("control_token_empty"));
    assert!(empty["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("管理控制令牌文件为空"));
}

#[test]
fn um_status_maps_gateway_failures_without_folding_unknown_into_invalid() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    let console = start_local(&temp, &gateway, running_runtime());

    gateway.set_status(vec![json!({"label": "x"})]);
    let schema = submit_and_wait(&console, "account.um_status", json!({}));
    assert_eq!(schema["state"], json!("failed"));
    assert_eq!(schema["error"]["code"], json!("status_schema"));

    let denied = FakeAdmin::start();
    denied.set_status(vec![json!({"state": "valid"})]);
    let mut config = fake_admin_config(&temp, &gateway);
    let control = temp.path().join("denied-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    config.control_token_file = Some(control);
    config.gateway_port = 9;
    let unreachable = Console::from_server(
        ConsoleServer::start_with_runtime(config, running_runtime()).expect("start console"),
    );
    let failed = submit_and_wait(&unreachable, "account.um_status", json!({}));
    assert_eq!(failed["state"], json!("failed"));
    assert_eq!(failed["error"]["code"], json!("status_unreachable"));
    assert!(failed["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("未知 ≠ 失效"));
}

#[test]
fn um_status_reads_the_active_login_round_without_the_login_lock() {
    let temp = temp();
    let gateway = FakeAdmin::start();
    gateway.set_verify(
        200,
        json!({"verify": "needs_login", "reason": null,
               "upstream_status": null, "checked_epoch": 1}),
    );
    gateway.set_status(vec![json!({
        "state": "logging_in",
        "error": null,
        "primary": null,
        "login": {
            "phase": "awaiting_duo",
            "origin": "https://api-1234.duosecurity.com/prompt/xyz",
            "manual_reason": null,
        },
        "recovery": {"state": "running"},
    })]);
    let console = start_local(&temp, &gateway, running_runtime());

    let created = submit(
        &console,
        "account.login",
        "chat2api",
        json!({"login_budget_s": 120, "poll_interval_s": 1}),
    );
    assert_eq!(created.status, 201);
    let login_id = created.json()["operation"]["id"]
        .as_str()
        .expect("login operation id")
        .to_string();
    wait_for_requests(&gateway, 3);

    let started = Instant::now();
    let status = submit_and_wait(&console, "account.um_status", json!({}));
    assert!(
        started.elapsed() < Duration::from_secs(5),
        "a read-only status refresh must not wait for the login lock"
    );
    assert_eq!(status["state"], json!("succeeded"), "{status}");
    assert_eq!(status["result"]["state"], json!("logging_in"));
    assert_eq!(status["result"]["login"]["phase"], json!("awaiting_duo"));

    let live = request(
        &console,
        "GET",
        &format!("/api/console/operations/{login_id}"),
        None,
    );
    assert_eq!(live.status, 200);
    assert_eq!(live.json()["operation"]["state"], json!("running"));

    let cancelled = request(
        &console,
        "POST",
        &format!("/api/console/operations/{login_id}/cancel"),
        None,
    );
    assert_eq!(cancelled.status, 200);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("cancelled"));
}
