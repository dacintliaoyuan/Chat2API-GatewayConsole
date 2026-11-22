use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use slint::ComponentHandle;
use um_desktop::account::{
    apply_verify, verify_reason_label, verify_view, AuthAdmin, HttpAuthAdmin, VerifyOutcome,
    VerifyReport, VerifySurface, VerifyVerdict,
};
use um_desktop::gateway::ControlToken;
use um_desktop::overview::Tone;

slint::include_modules!();

impl VerifySurface for MainWindow {
    fn set_verify_at_label(&self, value: &str) {
        self.set_verify_at(value.into());
    }

    fn set_verify_result_label(&self, value: &str) {
        self.set_verify_result(value.into());
    }

    fn set_verify_detail_text(&self, value: &str) {
        self.set_verify_detail(value.into());
    }

    fn set_verify_tone_index(&self, tone: Tone) {
        self.set_verify_tone(match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        });
    }

    fn set_verify_action_text(&self, value: &str) {
        self.set_verify_action_label(value.into());
    }
}

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn init_backend() {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
}

fn window() -> MainWindow {
    init_backend();
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window
        .window()
        .set_size(slint::LogicalSize::new(1060.0, 720.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    window
}

fn report(verdict: VerifyVerdict, reason: Option<&str>, status: Option<u16>) -> VerifyReport {
    VerifyReport {
        verdict,
        reason: reason.map(str::to_string),
        upstream_status: status,
        checked_epoch: 1_765_000_000,
        note: None,
    }
}

fn serve_once(
    status: &str,
    body: &str,
) -> (
    std::net::SocketAddr,
    std::sync::mpsc::Receiver<String>,
    std::thread::JoinHandle<()>,
) {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc::channel;

    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake verify server");
    let address = listener.local_addr().unwrap();
    let (sender, receiver) = channel();
    let status = status.to_string();
    let body = body.to_string();
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("verify request");
        let mut buffer = Vec::new();
        let mut chunk = [0u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            let text = String::from_utf8_lossy(&buffer);
            if let Some(head_end) = text.find("\r\n\r\n") {
                let head = &text[..head_end];
                let length = head
                    .lines()
                    .find_map(|line| {
                        line.strip_prefix("Content-Length:")
                            .or_else(|| line.strip_prefix("content-length:"))
                    })
                    .and_then(|value| value.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                if buffer.len() >= head_end + 4 + length {
                    break;
                }
            }
        }
        sender
            .send(String::from_utf8_lossy(&buffer).into_owned())
            .expect("send request");
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
    });
    (address, receiver, handle)
}

#[test]
fn the_verify_report_parses_the_gateway_payload() {
    let body = serde_json::json!({
        "verify": "rejected",
        "reason": "upstream_rejected",
        "upstream_status": 401,
        "checked_epoch": 1_765_000_000u64,
        "note": "single-shot upstream /api/models probe; auth state and model cache untouched",
    });
    let report = VerifyReport::parse(&body).expect("verify report");
    assert_eq!(report.verdict, VerifyVerdict::Rejected);
    assert_eq!(report.reason.as_deref(), Some("upstream_rejected"));
    assert_eq!(report.upstream_status, Some(401));
    assert_eq!(report.checked_epoch, 1_765_000_000);
    assert!(report
        .note
        .as_deref()
        .is_some_and(|note| note.contains("single-shot")));
}

#[test]
fn a_verify_report_without_a_verdict_or_a_check_time_is_rejected() {
    for body in [
        serde_json::json!({"checked_epoch": 1u64}),
        serde_json::json!({"verify": ""}),
        serde_json::json!({"verify": "valid"}),
    ] {
        let error = VerifyReport::parse(&body).expect_err("malformed report");
        assert!(!error.is_empty(), "{body}");
    }
}

#[test]
fn verify_reasons_are_explained_without_echoing_credentials() {
    assert_eq!(
        verify_reason_label("upstream_rejected"),
        "上游拒绝了当前凭据"
    );
    assert!(verify_reason_label("no_local_credential").contains("凭据"));
    assert!(verify_reason_label("timeout").contains("超时"));
    let other = verify_reason_label("future_reason");
    assert!(other.contains("future_reason"), "{other}");
    assert!(!other.contains("password"), "{other}");
}

#[test]
fn http_auth_admin_verifies_with_the_control_token() {
    let (address, request, server) = serve_once(
        "200 OK",
        r#"{"verify":"valid","reason":null,"upstream_status":200,"checked_epoch":1765000000,"note":"single-shot upstream /api/models probe; auth state and model cache untouched"}"#,
    );
    let admin = HttpAuthAdmin::new();
    let token = ControlToken::parse(&"9a".repeat(32)).unwrap();
    let report = admin
        .verify(&format!("http://{address}"), &token)
        .expect("verify");
    assert_eq!(report.verdict, VerifyVerdict::Valid);
    assert_eq!(report.reason, None);
    assert_eq!(report.upstream_status, Some(200));
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("GET /admin/auth/verify HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token.expose())),
        "{request}"
    );
    server.join().unwrap();
}

#[test]
fn http_auth_admin_keeps_the_verify_failure_without_relaying_the_body() {
    let (address, _request, server) = serve_once(
        "503 Service Unavailable",
        r#"{"error":"control_denied","password":"super-secret"}"#,
    );
    let admin = HttpAuthAdmin::new();
    let token = ControlToken::parse(&"9b".repeat(32)).unwrap();
    let error = admin
        .verify(&format!("http://{address}"), &token)
        .expect_err("503 must fail");
    assert!(error.contains("503"), "{error}");
    assert!(error.contains("control_denied"), "{error}");
    assert!(!error.contains("super-secret"), "{error}");
    assert!(!error.contains("password"), "{error}");
    server.join().unwrap();
}

#[test]
fn the_overview_starts_with_no_verification_and_offers_the_first_probe() {
    let window = window();
    let view = verify_view(None);
    apply_verify(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert!(window.get_verify_card_visible());
    assert_eq!(window.get_verify_at(), "");
    assert_eq!(window.get_verify_result(), "从未核验");
    assert!(window.get_verify_detail().contains("还没有"));
    assert_eq!(window.get_verify_action_label(), "立即核验");
    assert!(window.get_verify_button_enabled());
}

#[test]
fn a_passed_verification_shows_the_time_result_and_reverify_action() {
    let window = window();
    let view = verify_view(Some(&VerifyOutcome::Report(report(
        VerifyVerdict::Valid,
        None,
        Some(200),
    ))));
    apply_verify(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert_eq!(window.get_verify_result(), "通过");
    assert!(
        window.get_verify_detail().contains("未调用模型"),
        "{}",
        window.get_verify_detail()
    );
    assert!(
        window.get_verify_at().starts_with("核验于 "),
        "{}",
        window.get_verify_at()
    );
    assert_eq!(window.get_verify_action_label(), "重新核验");
    assert_eq!(window.get_verify_tone(), 0);
}

#[test]
fn a_rejected_verification_keeps_its_reason_and_is_not_folded_into_logged_in() {
    let window = window();
    window.set_account_label("尚未登录".into());
    let view = verify_view(Some(&VerifyOutcome::Report(report(
        VerifyVerdict::Rejected,
        Some("upstream_rejected"),
        Some(401),
    ))));
    apply_verify(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert_eq!(window.get_verify_result(), "未通过");
    assert!(
        window.get_verify_detail().contains("上游拒绝"),
        "{}",
        window.get_verify_detail()
    );
    assert!(
        window.get_verify_detail().contains("401"),
        "{}",
        window.get_verify_detail()
    );
    assert_eq!(window.get_verify_tone(), 2);
    assert_eq!(
        window.get_account_label(),
        "尚未登录",
        "核验失败不能折成已登录"
    );
    assert!(window.get_verify_at().starts_with("核验于 "));
    assert_eq!(window.get_verify_action_label(), "重新核验");
}

#[test]
fn an_inconclusive_verification_keeps_the_timeout_reason() {
    let window = window();
    let view = verify_view(Some(&VerifyOutcome::Report(report(
        VerifyVerdict::Unknown,
        Some("timeout"),
        None,
    ))));
    apply_verify(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert_eq!(window.get_verify_result(), "结果未知");
    assert!(
        window.get_verify_detail().contains("超时"),
        "{}",
        window.get_verify_detail()
    );
    assert_eq!(window.get_verify_tone(), 1);
}

#[test]
fn the_verify_action_is_disabled_while_a_probe_is_in_flight() {
    let window = window();
    assert!(window.get_verify_button_enabled());
    window.set_verify_busy(true);
    assert!(!window.get_verify_button_enabled());
    window.set_verify_busy(false);
    assert!(window.get_verify_button_enabled());
}

#[test]
fn the_verify_card_runs_the_verify_login_callback() {
    let window = window();
    let clicks = Rc::new(Cell::new(0usize));
    window.on_verify_login({
        let clicks = Rc::clone(&clicks);
        move || clicks.set(clicks.get() + 1)
    });
    window.invoke_verify_login();
    assert_eq!(clicks.get(), 1);
}

#[test]
fn the_verify_view_never_reports_a_failed_probe_as_passed() {
    for outcome in [
        VerifyOutcome::Report(report(
            VerifyVerdict::Rejected,
            Some("upstream_rejected"),
            Some(401),
        )),
        VerifyOutcome::Report(report(
            VerifyVerdict::NeedsLogin,
            Some("no_local_credential"),
            None,
        )),
        VerifyOutcome::Report(report(VerifyVerdict::Unknown, Some("timeout"), None)),
        VerifyOutcome::Unavailable("连接被拒绝".to_string()),
    ] {
        let view = verify_view(Some(&outcome));
        assert_ne!(view.result, "通过", "{outcome:?}");
        assert_ne!(view.tone, Tone::Good, "{outcome:?}");
        assert!(!view.detail.is_empty(), "{outcome:?}");
    }
}

#[test]
fn the_login_action_reads_on_demand_and_only_a_manual_takeover_says_continue() {
    let window = window();
    window.set_account_can_continue(true);
    window.set_account_manual_takeover(false);
    assert_eq!(window.get_continue_login_label(), "按需登录");
    window.set_account_manual_takeover(true);
    assert_eq!(window.get_continue_login_label(), "继续登录");
    window.set_account_can_continue(false);
    assert_eq!(window.get_continue_login_label(), "配置账号");
}

use std::sync::atomic::{AtomicUsize, Ordering};

use um_desktop::account::{
    account_view, on_demand_login, on_demand_message, AuthRead, AuthState, AuthStatus, Credentials,
    LoadOutcome, OnDemandLogin,
};

#[test]
fn only_a_manual_takeover_marks_the_login_action_as_continue() {
    let saved = LoadOutcome::Loaded(Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    });
    let takeover = auth_status(AuthState::Failed, Some("umpass_prefill_missing"));
    let view = account_view(&saved, Some(&AuthRead::Status(takeover)));
    assert!(view.manual_takeover);

    let mut duo = auth_status(AuthState::Failed, Some("duo_prompt_waiting"));
    duo.phase = Some("awaiting_duo".to_string());
    let view = account_view(&saved, Some(&AuthRead::Status(duo)));
    assert!(
        !view.manual_takeover,
        "Duo progress is the same round, not a manual takeover"
    );

    let valid = account_view(
        &saved,
        Some(&AuthRead::Status(auth_status(AuthState::Valid, None))),
    );
    assert!(!valid.manual_takeover);
}

struct FakeAuthAdmin {
    verify: Result<VerifyReport, String>,
    status: AuthStatus,
    login: Result<AuthStatus, String>,
    verify_calls: AtomicUsize,
    status_calls: AtomicUsize,
    started: AtomicUsize,
}

impl FakeAuthAdmin {
    fn new(verify: Result<VerifyReport, String>) -> Self {
        Self {
            verify,
            status: auth_status(AuthState::Valid, None),
            login: Ok(auth_status(AuthState::LoggingIn, None)),
            verify_calls: AtomicUsize::new(0),
            status_calls: AtomicUsize::new(0),
            started: AtomicUsize::new(0),
        }
    }

    fn verify_calls(&self) -> usize {
        self.verify_calls.load(Ordering::SeqCst)
    }

    fn status_calls(&self) -> usize {
        self.status_calls.load(Ordering::SeqCst)
    }

    fn started(&self) -> usize {
        self.started.load(Ordering::SeqCst)
    }
}

impl AuthAdmin for FakeAuthAdmin {
    fn read_status(&self, _base_url: &str, _token: &ControlToken) -> Result<AuthStatus, String> {
        self.status_calls.fetch_add(1, Ordering::SeqCst);
        Ok(self.status.clone())
    }

    fn verify(&self, _base_url: &str, _token: &ControlToken) -> Result<VerifyReport, String> {
        self.verify_calls.fetch_add(1, Ordering::SeqCst);
        self.verify.clone()
    }

    fn start_visible_login(
        &self,
        _base_url: &str,
        _token: &ControlToken,
    ) -> Result<AuthStatus, String> {
        self.started.fetch_add(1, Ordering::SeqCst);
        self.login.clone()
    }
}

fn auth_status(state: AuthState, manual_reason: Option<&str>) -> AuthStatus {
    AuthStatus {
        state,
        label: state.label().to_string(),
        error: None,
        manual_reason: manual_reason.map(str::to_string),
        phase: None,
        detail: None,
    }
}

#[test]
fn on_demand_login_with_valid_credentials_does_not_open_a_window() {
    let mut admin = FakeAuthAdmin::new(Ok(report(VerifyVerdict::Valid, None, Some(200))));
    admin.status = auth_status(AuthState::Valid, None);
    let token = ControlToken::parse(&"a1".repeat(32)).unwrap();
    let outcome = on_demand_login(&admin, "http://127.0.0.1:7999", &token, None);
    let OnDemandLogin::AlreadyValid { report, status } = outcome else {
        panic!("valid credentials must not open a window, got {outcome:?}");
    };
    assert_eq!(report.verdict, VerifyVerdict::Valid);
    assert_eq!(status.expect("a fresh status read").state, AuthState::Valid);
    assert_eq!(admin.started(), 0, "no visible window");
    assert_eq!(admin.verify_calls(), 1, "verify runs first");
    assert_eq!(admin.status_calls(), 1, "only the state is refreshed");
    let (message, tone) = on_demand_message(&OnDemandLogin::AlreadyValid {
        report,
        status: Some(auth_status(AuthState::Valid, None)),
    });
    assert!(message.contains("无需登录"), "{message}");
    assert_eq!(tone, Tone::Good);
}

#[test]
fn on_demand_login_with_rejected_credentials_shows_the_reason_and_opens_one_window() {
    let admin = FakeAuthAdmin::new(Ok(report(
        VerifyVerdict::Rejected,
        Some("upstream_rejected"),
        Some(401),
    )));
    let token = ControlToken::parse(&"a2".repeat(32)).unwrap();
    let outcome = on_demand_login(&admin, "http://127.0.0.1:7999", &token, None);
    let OnDemandLogin::LoginStarted { report, status } = &outcome else {
        panic!("rejected credentials must open the visible login, got {outcome:?}");
    };
    assert_eq!(
        report.as_ref().expect("verify report").verdict,
        VerifyVerdict::Rejected
    );
    assert_eq!(status.state, AuthState::LoggingIn);
    assert_eq!(admin.started(), 1, "exactly one visible login");
    let (message, tone) = on_demand_message(&outcome);
    assert!(message.contains("上游拒绝"), "{message}");
    assert!(message.contains("已打开可见登录窗口"), "{message}");
    assert!(!message.contains("已登录"), "{message}");
    assert_eq!(tone, Tone::Warn);
}

#[test]
fn on_demand_login_with_an_unknown_verify_keeps_the_reason_without_a_window() {
    let admin = FakeAuthAdmin::new(Ok(report(VerifyVerdict::Unknown, Some("timeout"), None)));
    let token = ControlToken::parse(&"a3".repeat(32)).unwrap();
    let outcome = on_demand_login(&admin, "http://127.0.0.1:7999", &token, None);
    let OnDemandLogin::Inconclusive(report) = &outcome else {
        panic!("an unknown verify must not open a window, got {outcome:?}");
    };
    assert_eq!(report.reason.as_deref(), Some("timeout"));
    assert_eq!(admin.started(), 0, "no visible window");
    let (message, tone) = on_demand_message(&outcome);
    assert!(message.contains("暂时无法核验"), "{message}");
    assert!(message.contains("超时"), "{message}");
    assert!(message.contains("未打开登录窗口"), "{message}");
    assert_eq!(tone, Tone::Warn);
}

#[test]
fn on_demand_login_with_an_unreachable_verify_keeps_the_reason_without_a_window() {
    let admin = FakeAuthAdmin::new(Err("连接被拒绝".to_string()));
    let token = ControlToken::parse(&"a4".repeat(32)).unwrap();
    let outcome = on_demand_login(&admin, "http://127.0.0.1:7999", &token, None);
    let OnDemandLogin::VerifyUnavailable(error) = &outcome else {
        panic!("a failed verify must not open a window, got {outcome:?}");
    };
    assert!(error.contains("连接被拒绝"), "{error}");
    assert_eq!(admin.started(), 0);
    let (message, tone) = on_demand_message(&outcome);
    assert!(message.contains("无法核验"), "{message}");
    assert!(message.contains("连接被拒绝"), "{message}");
    assert_eq!(tone, Tone::Bad);
}

#[test]
fn a_manual_takeover_opens_the_visible_window_without_verifying_first() {
    let admin = FakeAuthAdmin::new(Ok(report(VerifyVerdict::Valid, None, Some(200))));
    let status = auth_status(AuthState::Failed, Some("umpass_prefill_missing"));
    let token = ControlToken::parse(&"a5".repeat(32)).unwrap();
    let outcome = on_demand_login(&admin, "http://127.0.0.1:7999", &token, Some(&status));
    let OnDemandLogin::LoginStarted { report, status } = &outcome else {
        panic!("a manual takeover must open the visible login, got {outcome:?}");
    };
    assert!(report.is_none(), "manual takeover skips the verify");
    assert_eq!(status.state, AuthState::LoggingIn);
    assert_eq!(admin.verify_calls(), 0, "no verify before a takeover");
    assert_eq!(admin.started(), 1, "exactly one visible login");
    let (message, _) = on_demand_message(&outcome);
    assert!(message.contains("正在打开可见登录窗口"), "{message}");
}

#[test]
fn on_demand_login_with_missing_credentials_opens_the_visible_login() {
    let admin = FakeAuthAdmin::new(Ok(report(
        VerifyVerdict::NeedsLogin,
        Some("no_local_credential"),
        None,
    )));
    let token = ControlToken::parse(&"a6".repeat(32)).unwrap();
    let outcome = on_demand_login(&admin, "http://127.0.0.1:7999", &token, None);
    assert!(matches!(outcome, OnDemandLogin::LoginStarted { .. }));
    assert_eq!(admin.started(), 1);
    let (message, _) = on_demand_message(&outcome);
    assert!(message.contains("凭据"), "{message}");
    assert!(message.contains("已打开可见登录窗口"), "{message}");
}

#[test]
fn a_visible_login_that_cannot_start_keeps_the_verify_reason() {
    let mut admin = FakeAuthAdmin::new(Ok(report(
        VerifyVerdict::Rejected,
        Some("upstream_rejected"),
        Some(401),
    )));
    admin.login = Err("没有可用的显示".to_string());
    let token = ControlToken::parse(&"a7".repeat(32)).unwrap();
    let outcome = on_demand_login(&admin, "http://127.0.0.1:7999", &token, None);
    let OnDemandLogin::LoginUnavailable { report, error } = &outcome else {
        panic!("a failed login must keep the verify reason, got {outcome:?}");
    };
    assert_eq!(
        report.as_ref().expect("verify report").verdict,
        VerifyVerdict::Rejected
    );
    assert!(error.contains("显示"), "{error}");
    let (message, tone) = on_demand_message(&outcome);
    assert!(message.contains("上游拒绝"), "{message}");
    assert!(message.contains("显示"), "{message}");
    assert_eq!(tone, Tone::Bad);
}
