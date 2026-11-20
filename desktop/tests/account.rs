use um_desktop::account::{
    account_view, clear, credentials_path, first_login, first_login_needed, load_from,
    manual_reason_label, save_to, tray_tooltip, AccountForm, AuthAdmin, AuthRead, AuthState,
    AuthStatus, Credentials, HttpAuthAdmin, LoadOutcome, PasswordInput,
};
use um_desktop::gateway::ControlToken;
use um_desktop::overview::Tone;

mod common;
use common::TempDir;

#[test]
fn missing_credentials_file_is_a_legal_first_open_state() {
    let dir = TempDir::new("account-missing");
    let path = dir.path().join("credentials.json");
    assert_eq!(load_from(&path), LoadOutcome::Missing);
}

#[test]
fn a_corrupt_credentials_file_is_unusable_not_an_empty_account() {
    let dir = TempDir::new("account-corrupt");
    let path = dir.path().join("credentials.json");
    std::fs::write(&path, "{not json").unwrap();
    match load_from(&path) {
        LoadOutcome::Unusable(reason) => assert!(reason.contains("JSON"), "{reason}"),
        other => panic!("a corrupt file must be unusable, got {other:?}"),
    }
}

#[test]
fn half_credentials_are_unusable_instead_of_configured() {
    let dir = TempDir::new("account-half");
    let path = dir.path().join("credentials.json");
    for body in [
        r#"{"version":1,"account":"someone@um.edu.mo","password":""}"#,
        r#"{"version":1,"account":"","password":"secret"}"#,
        r#"{"version":1,"account":"someone@um.edu.mo"}"#,
        r#"{"version":2,"account":"someone@um.edu.mo","password":"secret"}"#,
        r#"{"version":1,"account":42,"password":"secret"}"#,
    ] {
        std::fs::write(&path, body).unwrap();
        match load_from(&path) {
            LoadOutcome::Unusable(reason) => assert!(!reason.is_empty(), "{body}"),
            other => panic!("{body} must be unusable, got {other:?}"),
        }
    }
}

#[test]
fn saving_credentials_writes_plaintext_version_1_and_round_trips() {
    let dir = TempDir::new("account-save");
    let path = credentials_path(dir.path());
    let credentials = Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "p@ss word".to_string(),
    };
    save_to(&path, &credentials).expect("save credentials");
    let raw = std::fs::read_to_string(&path).expect("read credentials");
    let body: serde_json::Value = serde_json::from_str(&raw).expect("credentials json");
    assert_eq!(body["version"], 1);
    assert_eq!(body["account"], "someone@um.edu.mo");
    assert_eq!(body["password"], "p@ss word");
    assert_eq!(load_from(&path), LoadOutcome::Loaded(credentials));

    let names: Vec<String> = std::fs::read_dir(dir.path())
        .unwrap()
        .filter_map(Result::ok)
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(
        names,
        ["credentials.json"],
        "no temp file may survive the save"
    );

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
    }
    #[cfg(windows)]
    {
        let (aces, only_current_user) = common::acl_ace_count(&path);
        assert!(aces >= 1);
        assert!(only_current_user, "credentials must be current-user only");
    }
}

#[test]
fn saving_empty_fields_is_rejected_and_keeps_the_old_file() {
    let dir = TempDir::new("account-save-invalid");
    let path = credentials_path(dir.path());
    let saved = Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "secret".to_string(),
    };
    save_to(&path, &saved).unwrap();
    for credentials in [
        Credentials {
            account: " ".to_string(),
            password: "secret".to_string(),
        },
        Credentials {
            account: "someone@um.edu.mo".to_string(),
            password: String::new(),
        },
    ] {
        let error = save_to(&path, &credentials).expect_err("empty field must be rejected");
        assert!(!error.is_empty());
        assert!(!error.contains("secret"), "{error}");
    }
    assert_eq!(load_from(&path), LoadOutcome::Loaded(saved));
}

#[test]
fn clearing_removes_the_file_and_is_idempotent() {
    let dir = TempDir::new("account-clear");
    let path = credentials_path(dir.path());
    save_to(
        &path,
        &Credentials {
            account: "someone@um.edu.mo".to_string(),
            password: "secret".to_string(),
        },
    )
    .unwrap();
    assert!(clear(&path).expect("clear credentials"));
    assert!(!path.exists());
    assert_eq!(load_from(&path), LoadOutcome::Missing);
    assert!(!clear(&path).expect("clear missing credentials"));
}

#[test]
fn credentials_debug_never_prints_the_account_or_password() {
    let credentials = Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    };
    let rendered = format!("{credentials:?}");
    assert!(!rendered.contains("someone@um.edu.mo"), "{rendered}");
    assert!(!rendered.contains("super-secret"), "{rendered}");
}

#[test]
fn the_account_form_prefills_the_saved_account_and_masks_the_password() {
    let form = AccountForm::new(Some("someone@um.edu.mo"));
    assert_eq!(form.account(), "someone@um.edu.mo");
    assert_eq!(form.password(), "");
    assert!(!form.password_visible());
    assert_eq!(form.password_input_type(), PasswordInput::Masked);
}

#[test]
fn toggling_the_password_visibility_flips_the_input_type() {
    let mut form = AccountForm::new(None);
    form.set_password("secret");
    form.toggle_password_visible();
    assert!(form.password_visible());
    assert_eq!(form.password_input_type(), PasswordInput::Visible);
    form.toggle_password_visible();
    assert!(!form.password_visible());
    assert_eq!(form.password_input_type(), PasswordInput::Masked);
}

#[test]
fn the_account_form_rejects_empty_fields_and_trims_the_account() {
    let mut form = AccountForm::new(None);
    assert!(form.validate().is_err());
    form.set_account("  someone@um.edu.mo  ");
    assert!(form.validate().is_err(), "password is still empty");
    form.set_password(" secret ");
    let credentials = form.validate().expect("valid form");
    assert_eq!(credentials.account, "someone@um.edu.mo");
    assert_eq!(
        credentials.password, " secret ",
        "password is stored verbatim"
    );
}

#[test]
fn auth_status_parses_the_gateway_states_and_manual_reason() {
    let body: serde_json::Value = serde_json::from_str(
        r#"{
            "state": "failed",
            "label": "登录失败（需人工：duo_prompt_waiting）",
            "error": "manual_takeover_required:duo_prompt_waiting",
            "login": {
                "phase": "manual_takeover",
                "manual_reason": "duo_prompt_waiting",
                "detail": null
            }
        }"#,
    )
    .unwrap();
    let status = AuthStatus::parse(&body).expect("status");
    assert_eq!(status.state, AuthState::Failed);
    assert_eq!(status.phase.as_deref(), Some("manual_takeover"));
    assert_eq!(status.manual_reason.as_deref(), Some("duo_prompt_waiting"));
    assert_eq!(
        status.manual_reason_label().unwrap(),
        "等待本人确认 Duo 推送"
    );

    let valid: serde_json::Value =
        serde_json::from_str(r#"{"state":"valid","label":"认证有效","error":null,"login":null}"#)
            .unwrap();
    let status = AuthStatus::parse(&valid).expect("valid status");
    assert_eq!(status.state, AuthState::Valid);
    assert_eq!(status.manual_reason_label(), None);
}

#[test]
fn auth_status_without_a_state_is_rejected_and_unknown_states_stay_visible() {
    let missing = serde_json::json!({"label": "x"});
    assert!(AuthStatus::parse(&missing).is_err());

    let unknown = serde_json::json!({"state": "something_new", "label": "新状态"});
    let status = AuthStatus::parse(&unknown).expect("unknown state is still a status");
    assert_eq!(status.state, AuthState::Unknown);
    assert_eq!(status.label, "新状态");
}

#[test]
fn first_login_is_requested_only_when_the_gateway_is_not_valid_or_already_logging_in() {
    for (state, expected) in [
        (AuthState::Valid, false),
        (AuthState::LoggingIn, false),
        (AuthState::NeedsLogin, true),
        (AuthState::Failed, true),
        (AuthState::Cancelled, true),
        (AuthState::Unknown, true),
    ] {
        let status = AuthStatus {
            state,
            label: String::new(),
            error: None,
            manual_reason: None,
            phase: None,
            detail: None,
        };
        assert_eq!(first_login_needed(&status), expected, "{state:?}");
    }
}

#[test]
fn manual_reasons_are_explained_without_leaking_sensitive_values() {
    assert!(manual_reason_label("duo_prompt_waiting").contains("Duo"));
    assert!(manual_reason_label("umpass_prefill_missing").contains("UMPASS"));
    assert!(manual_reason_label("umpass_account_mismatch").contains("账号"));
    let other = manual_reason_label("some_future_reason");
    assert!(other.contains("some_future_reason"), "{other}");
    assert!(!other.contains("password"));
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

    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake auth server");
    let address = listener.local_addr().unwrap();
    let (sender, receiver) = channel();
    let status = status.to_string();
    let body = body.to_string();
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("auth request");
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
fn http_auth_admin_reads_status_with_the_control_token() {
    let (address, request, server) = serve_once(
        "200 OK",
        r#"{"state":"valid","label":"认证有效","error":null,"login":null}"#,
    );
    let admin = HttpAuthAdmin::new();
    let token = ControlToken::parse(&"ab".repeat(32)).unwrap();
    let status = admin
        .read_status(&format!("http://{address}"), &token)
        .expect("status");
    assert_eq!(status.state, AuthState::Valid);
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("GET /admin/auth/status HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token.expose())),
        "{request}"
    );
    server.join().unwrap();
}

#[test]
fn http_auth_admin_posts_one_visible_login() {
    let (address, request, server) = serve_once(
        "202 Accepted",
        r#"{"state":"logging_in","label":"登录中","login":{"phase":"awaiting_umpass"}}"#,
    );
    let admin = HttpAuthAdmin::new();
    let token = ControlToken::parse(&"cd".repeat(32)).unwrap();
    let status = admin
        .start_visible_login(&format!("http://{address}"), &token)
        .expect("visible login");
    assert_eq!(status.state, AuthState::LoggingIn);
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("POST /admin/auth/login HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token.expose())),
        "{request}"
    );
    let body = request.split("\r\n\r\n").nth(1).unwrap_or_default();
    let payload: serde_json::Value = serde_json::from_str(body).expect("login body");
    assert_eq!(payload, serde_json::json!({"visible": true}));
    server.join().unwrap();
}

#[test]
fn http_auth_admin_never_relays_the_response_body_on_failure() {
    let (address, _request, server) = serve_once(
        "403 Forbidden",
        r#"{"error":"control_denied","password":"super-secret"}"#,
    );
    let admin = HttpAuthAdmin::new();
    let token = ControlToken::parse(&"ef".repeat(32)).unwrap();
    let error = admin
        .read_status(&format!("http://{address}"), &token)
        .expect_err("403 must fail");
    assert!(error.contains("403"), "{error}");
    assert!(error.contains("control_denied"), "{error}");
    assert!(!error.contains("super-secret"), "{error}");
    assert!(!error.contains("password"), "{error}");
    server.join().unwrap();
}

#[test]
fn http_auth_admin_rejects_a_status_without_a_state() {
    let (address, _request, server) = serve_once("200 OK", r#"{"label":"x"}"#);
    let admin = HttpAuthAdmin::new();
    let token = ControlToken::parse(&"12".repeat(32)).unwrap();
    let error = admin
        .read_status(&format!("http://{address}"), &token)
        .expect_err("missing state");
    assert!(error.contains("state"), "{error}");
    server.join().unwrap();
}

fn status(state: AuthState) -> AuthStatus {
    AuthStatus {
        state,
        label: state.label().to_string(),
        error: None,
        manual_reason: None,
        phase: None,
        detail: None,
    }
}

#[test]
fn the_account_card_shows_the_saved_account_and_the_gateway_state() {
    let saved = LoadOutcome::Loaded(Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    });
    let view = account_view(&saved, Some(&AuthRead::Status(status(AuthState::Valid))));
    assert_eq!(view.saved_account, "someone@um.edu.mo");
    assert_eq!(view.state_value, "已登录");
    assert_eq!(view.state_tone, Tone::Good);
    assert_eq!(view.overview_label, "已登录");
    assert_eq!(view.overview_tone, Tone::Good);
    assert!(view.can_clear);
    assert!(!view.can_continue_login);
    assert!(!format!("{view:?}").contains("super-secret"));
}

#[test]
fn not_logged_in_stays_not_logged_in_in_the_overview_and_offers_login() {
    let saved = LoadOutcome::Loaded(Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    });
    let mut needs_login = status(AuthState::NeedsLogin);
    needs_login.manual_reason = Some("umpass_prefill_missing".to_string());
    let view = account_view(&saved, Some(&AuthRead::Status(needs_login)));
    assert_eq!(view.state_value, "尚未登录");
    assert_eq!(view.overview_label, "尚未登录");
    assert_eq!(view.state_tone, Tone::Warn);
    assert!(
        view.state_detail.contains("UMPASS"),
        "{}",
        view.state_detail
    );
    assert!(view.can_continue_login);
    assert!(view.can_clear);

    let logging_in = account_view(
        &saved,
        Some(&AuthRead::Status(status(AuthState::LoggingIn))),
    );
    assert_eq!(logging_in.state_value, "登录中");
    assert_eq!(logging_in.overview_label, "登录中");
    assert!(logging_in.can_continue_login);

    let failed = account_view(&saved, Some(&AuthRead::Status(status(AuthState::Failed))));
    assert_eq!(failed.state_value, "登录失败");
    assert_eq!(failed.state_tone, Tone::Bad);
    assert_eq!(failed.overview_label, "尚未登录");
    assert!(failed.can_continue_login);
}

#[test]
fn unusable_credentials_are_reported_with_a_reentry_path() {
    let saved = LoadOutcome::Unusable("凭据缺少密码".to_string());
    let view = account_view(
        &saved,
        Some(&AuthRead::Status(status(AuthState::NeedsLogin))),
    );
    assert_eq!(view.saved_account, "凭据不可用");
    assert!(
        view.saved_detail.contains("凭据缺少密码"),
        "{}",
        view.saved_detail
    );
    assert!(view.can_clear);
    assert!(!view.can_continue_login, "re-entry must come first");
    assert_eq!(view.overview_label, "尚未登录");
}

#[test]
fn missing_credentials_are_not_shown_as_configured() {
    let view = account_view(&LoadOutcome::Missing, None);
    assert_eq!(view.saved_account, "凭据不可用");
    assert_eq!(view.state_value, "认证状态读取中");
    assert!(!view.can_clear);
    assert!(!view.can_continue_login);
}

#[test]
fn an_unreachable_gateway_keeps_its_reason_and_does_not_claim_login() {
    let saved = LoadOutcome::Loaded(Credentials {
        account: "someone@um.edu.mo".to_string(),
        password: "super-secret".to_string(),
    });
    let view = account_view(
        &saved,
        Some(&AuthRead::Unavailable("连接被拒绝".to_string())),
    );
    assert_eq!(view.state_value, "认证未知");
    assert_eq!(view.state_tone, Tone::Warn);
    assert!(view.state_detail.contains("连接被拒绝"));
    assert_eq!(view.overview_label, "认证未知");
    assert!(!view.can_continue_login);
    assert!(!format!("{view:?}").contains("super-secret"));
}

#[test]
fn the_tray_tooltip_reports_the_login_state_without_sensitive_values() {
    let valid = AuthRead::Status(status(AuthState::Valid));
    assert!(tray_tooltip(Some(&valid), false).contains("已登录"));
    let logging_in = AuthRead::Status(status(AuthState::LoggingIn));
    assert!(tray_tooltip(Some(&logging_in), false).contains("登录中"));
    assert!(tray_tooltip(None, false).contains("尚未登录"));
    let unreachable = AuthRead::Unavailable("连接被拒绝".to_string());
    assert!(!tray_tooltip(Some(&unreachable), false).contains("已登录"));
    assert!(tray_tooltip(None, true).contains("日志已暂停"));
    assert!(!tray_tooltip(None, true).contains("已登录"));
}

struct RecordingAuthAdmin {
    status: AuthStatus,
    started: std::sync::atomic::AtomicUsize,
}

impl AuthAdmin for RecordingAuthAdmin {
    fn read_status(&self, _base_url: &str, _token: &ControlToken) -> Result<AuthStatus, String> {
        Ok(self.status.clone())
    }

    fn start_visible_login(
        &self,
        _base_url: &str,
        _token: &ControlToken,
    ) -> Result<AuthStatus, String> {
        self.started
            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Ok(AuthStatus {
            state: AuthState::LoggingIn,
            label: "登录中".to_string(),
            error: None,
            manual_reason: None,
            phase: None,
            detail: None,
        })
    }
}

#[test]
fn a_first_login_posts_exactly_one_visible_login() {
    let token = ControlToken::parse(&"34".repeat(32)).unwrap();
    let admin = RecordingAuthAdmin {
        status: status(AuthState::NeedsLogin),
        started: std::sync::atomic::AtomicUsize::new(0),
    };
    let auth = first_login(&admin, "http://127.0.0.1:7999", &token, &admin.status);
    assert!(matches!(
        auth,
        AuthRead::Status(AuthStatus {
            state: AuthState::LoggingIn,
            ..
        })
    ));
    assert_eq!(admin.started.load(std::sync::atomic::Ordering::SeqCst), 1);
}

#[test]
fn a_running_auto_relogin_is_not_triggered_again() {
    let token = ControlToken::parse(&"56".repeat(32)).unwrap();
    let admin = RecordingAuthAdmin {
        status: status(AuthState::LoggingIn),
        started: std::sync::atomic::AtomicUsize::new(0),
    };
    let auth = first_login(&admin, "http://127.0.0.1:7999", &token, &admin.status);
    assert!(matches!(
        auth,
        AuthRead::Status(AuthStatus {
            state: AuthState::LoggingIn,
            ..
        })
    ));
    assert_eq!(admin.started.load(std::sync::atomic::Ordering::SeqCst), 0);
}

#[test]
fn a_valid_login_does_not_open_the_visible_edge() {
    let token = ControlToken::parse(&"78".repeat(32)).unwrap();
    let admin = RecordingAuthAdmin {
        status: status(AuthState::Valid),
        started: std::sync::atomic::AtomicUsize::new(0),
    };
    let auth = first_login(&admin, "http://127.0.0.1:7999", &token, &admin.status);
    assert!(matches!(
        auth,
        AuthRead::Status(AuthStatus {
            state: AuthState::Valid,
            ..
        })
    ));
    assert_eq!(admin.started.load(std::sync::atomic::Ordering::SeqCst), 0);
}
