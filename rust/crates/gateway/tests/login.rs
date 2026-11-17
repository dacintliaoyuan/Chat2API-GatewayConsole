//! #69: the real login executor's state machine, profile lock, token write,
//! admin surface and BrowserDisabled behavior. Every test injects a fake browser,
//! a fake profile and a fake credential probe; no real browser opens.

mod support;

use std::collections::VecDeque;
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use um_gateway::config::GatewayConfig;
use um_gateway::login::{
    BrowserUnavailable, CredentialProbe, CredentialSink, CredentialVerdict, HttpCredentialProbe, LaunchRequest,
    LoginBrowser, LoginBrowserFactory, LoginConfig, LoginFailure, LoginPage, LoginService,
    VisibleTarget,
};
use um_gateway::reauth::{
    AuthWaitBudget, ReauthLimits, ReauthProviderResult, ReauthResult, ReauthenticationProvider,
    ReauthenticationService,
};
use um_gateway::GatewayServer;

use support::{
    client_request, legacy_share_config, ClientRequest, StubUpstream, TempDir, CONTROL_TOKEN,
    SHARE_SECRET, STUB_TOKEN,
};

struct FakePage {
    url: Mutex<String>,
    token: Mutex<Option<String>>,
    reload_token: Mutex<Option<String>>,
    closed: AtomicBool,
    wait: Duration,
    drop_calls: AtomicUsize,
    reload_calls: AtomicUsize,
    unfilled_polls: AtomicUsize,
    submit_visible: AtomicBool,
    click_steps: Mutex<Vec<(String, Option<String>)>>,
    fill_calls: AtomicUsize,
    filled_values: Mutex<Vec<String>>,
    account_prefilled: AtomicBool,
    password_cleared: AtomicBool,
    reject_submit: AtomicBool,
    credential_error: AtomicBool,
    submit_calls: AtomicUsize,
}

impl FakePage {
    fn new(url: &str, token: Option<&str>) -> Arc<Self> {
        Arc::new(Self {
            url: Mutex::new(url.to_string()),
            token: Mutex::new(token.map(str::to_string)),
            reload_token: Mutex::new(None),
            closed: AtomicBool::new(false),
            wait: Duration::from_millis(5),
            drop_calls: AtomicUsize::new(0),
            reload_calls: AtomicUsize::new(0),
            unfilled_polls: AtomicUsize::new(0),
            submit_visible: AtomicBool::new(false),
            click_steps: Mutex::new(Vec::new()),
            fill_calls: AtomicUsize::new(0),
            filled_values: Mutex::new(Vec::new()),
            account_prefilled: AtomicBool::new(false),
            password_cleared: AtomicBool::new(false),
            reject_submit: AtomicBool::new(false),
            credential_error: AtomicBool::new(false),
            submit_calls: AtomicUsize::new(0),
        })
    }

    fn with_reload_token(self: &Arc<Self>, token: Option<&str>) -> Arc<Self> {
        *self.reload_token.lock().expect("reload token") = token.map(str::to_string);
        Arc::clone(self)
    }

    fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
    }

    fn drop_calls(&self) -> usize {
        self.drop_calls.load(Ordering::SeqCst)
    }

    fn reload_calls(&self) -> usize {
        self.reload_calls.load(Ordering::SeqCst)
    }

    fn fill_calls(&self) -> usize {
        self.fill_calls.load(Ordering::SeqCst)
    }
}

impl LoginPage for FakePage {
    fn url(&self) -> String {
        self.url.lock().expect("fake url").clone()
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }

    fn read_token(&self) -> Option<String> {
        self.token.lock().expect("fake token").clone()
    }

    fn drop_token(&self) -> Result<(), String> {
        self.drop_calls.fetch_add(1, Ordering::SeqCst);
        *self.token.lock().expect("fake token") = None;
        Ok(())
    }

    fn reload(&self) -> Result<(), String> {
        self.reload_calls.fetch_add(1, Ordering::SeqCst);
        let token = self.reload_token.lock().expect("reload token").clone();
        *self.token.lock().expect("fake token") = token;
        Ok(())
    }

    fn wait(&self, duration: Duration) -> Result<(), String> {
        thread::sleep(duration.min(self.wait));
        Ok(())
    }

    fn bring_to_front(&self) -> Result<(), String> {
        Ok(())
    }

    fn goto(&self, url: &str) -> Result<(), String> {
        *self.url.lock().expect("fake url") = url.to_string();
        Ok(())
    }

    fn any_visible(&self, selectors: &[&str]) -> bool {
        selectors.contains(&"#errorText") && self.credential_error.load(Ordering::SeqCst)
    }

    fn any_visible_text(&self, selectors: &[&str]) -> bool {
        self.any_visible(selectors)
    }

    fn field_filled(&self, selectors: &[&str]) -> bool {
        if selectors.contains(&"input[name=\"UserName\"]")
            && self.account_prefilled.load(Ordering::SeqCst)
        {
            return true;
        }
        if selectors.contains(&"input[name=\"Password\"]")
            && self.password_cleared.load(Ordering::SeqCst)
        {
            return false;
        }
        if self.unfilled_polls.load(Ordering::SeqCst) > 0 {
            self.unfilled_polls.fetch_sub(1, Ordering::SeqCst);
            return false;
        }
        self.fill_calls.load(Ordering::SeqCst) >= 2
    }

    fn one_visible(&self, _selectors: &[&str]) -> Option<VisibleTarget> {
        if !self.submit_visible.load(Ordering::SeqCst) {
            return None;
        }
        Some(VisibleTarget {
            selector: "input[type=\"submit\"]".to_string(),
            index: 0,
        })
    }

    fn click(&self, _target: &VisibleTarget) -> Result<(), String> {
        if self.reject_submit.load(Ordering::SeqCst)
            && self.url().starts_with("https://websso.um.edu.mo/")
        {
            self.submit_calls.fetch_add(1, Ordering::SeqCst);
            self.credential_error.store(true, Ordering::SeqCst);
            self.password_cleared.store(true, Ordering::SeqCst);
            return Ok(());
        }
        let step = {
            let mut steps = self.click_steps.lock().expect("click steps");
            if steps.is_empty() {
                None
            } else {
                Some(steps.remove(0))
            }
        };
        if let Some((url, token)) = step {
            *self.url.lock().expect("fake url") = url;
            *self.token.lock().expect("fake token") = token;
        }
        Ok(())
    }

    fn fill(&self, selectors: &[&str], value: &str) -> Result<(), String> {
        if selectors.contains(&"input[name=\"Password\"]") {
            self.password_cleared.store(false, Ordering::SeqCst);
        }
        self.fill_calls.fetch_add(1, Ordering::SeqCst);
        self.filled_values.lock().unwrap().push(value.to_string());
        Ok(())
    }
}

struct FakeFactoryState {
    page: Arc<FakePage>,
    requests: Mutex<Vec<LaunchRequest>>,
    cleanup_calls: AtomicUsize,
    closes: AtomicUsize,
}

#[derive(Clone)]
struct FakeFactory {
    state: Arc<FakeFactoryState>,
}

struct FakeBrowser {
    page: Arc<FakePage>,
    state: Arc<FakeFactoryState>,
}

impl FakeFactory {
    fn new(page: Arc<FakePage>) -> Arc<Self> {
        Arc::new(Self {
            state: Arc::new(FakeFactoryState {
                page,
                requests: Mutex::new(Vec::new()),
                cleanup_calls: AtomicUsize::new(0),
                closes: AtomicUsize::new(0),
            }),
        })
    }

    fn requests(&self) -> Vec<LaunchRequest> {
        self.state.requests.lock().expect("fake requests").clone()
    }

    fn cleanup_calls(&self) -> usize {
        self.state.cleanup_calls.load(Ordering::SeqCst)
    }

    fn closes(&self) -> usize {
        self.state.closes.load(Ordering::SeqCst)
    }
}

impl LoginBrowserFactory for FakeFactory {
    fn resolve_browser(&self) -> Result<std::path::PathBuf, BrowserUnavailable> {
        Ok(std::path::PathBuf::from("fake-browser"))
    }

    fn launch(&self, request: &LaunchRequest) -> Result<Box<dyn LoginBrowser>, LoginFailure> {
        self.state
            .requests
            .lock()
            .expect("fake requests")
            .push(request.clone());
        Ok(Box::new(FakeBrowser {
            page: Arc::clone(&self.state.page),
            state: Arc::clone(&self.state),
        }))
    }

    fn cleanup_profile(&self, _profile_dir: &Path) {
        self.state.cleanup_calls.fetch_add(1, Ordering::SeqCst);
    }
}

impl LoginBrowser for FakeBrowser {
    fn page(&self) -> &dyn LoginPage {
        &*self.page
    }

    fn close(&mut self) -> Result<(), String> {
        self.state.closes.fetch_add(1, Ordering::SeqCst);
        self.page.close();
        Ok(())
    }
}

struct ScriptedProbe {
    verdicts: Mutex<VecDeque<CredentialVerdict>>,
}

impl ScriptedProbe {
    fn new(verdicts: Vec<CredentialVerdict>) -> Arc<Self> {
        Arc::new(Self {
            verdicts: Mutex::new(verdicts.into()),
        })
    }

    fn valid() -> Arc<Self> {
        Self::new(vec![valid_verdict()])
    }

    fn rejected_then_valid() -> Arc<Self> {
        Self::new(vec![rejected_verdict(), valid_verdict()])
    }
}

impl CredentialProbe for ScriptedProbe {
    fn classify(&self, _bearer: &str) -> CredentialVerdict {
        self.verdicts
            .lock()
            .expect("probe verdicts")
            .pop_front()
            .unwrap_or_else(valid_verdict)
    }
}

fn valid_verdict() -> CredentialVerdict {
    CredentialVerdict {
        verdict: "valid",
        reason: None,
        upstream_status: Some(200),
    }
}

fn rejected_verdict() -> CredentialVerdict {
    CredentialVerdict {
        verdict: "rejected",
        reason: Some("upstream_rejected"),
        upstream_status: Some(401),
    }
}

#[derive(Default)]
struct FakeSink {
    published: Mutex<Vec<(String, Option<String>)>>,
}

impl FakeSink {
    fn published(&self) -> Vec<(String, Option<String>)> {
        self.published
            .lock()
            .expect("published credentials")
            .clone()
    }
}

impl CredentialSink for FakeSink {
    fn publish_credential(&self, token: &str, api_key: Option<&str>) {
        self.published
            .lock()
            .expect("published credentials")
            .push((token.to_string(), api_key.map(str::to_string)));
    }
}

fn login_config(temp: &TempDir) -> LoginConfig {
    LoginConfig {
        base_url: "http://127.0.0.1:9".to_string(),
        token_file: temp.path().join("token.json"),
        profile_dir: temp.path().join("profile"),
        display_file: temp.path().join("login-display.env"),
        login_timeout: Duration::from_secs(2),
        idle_timeout: Duration::from_secs(1),
        poll_interval: Duration::from_millis(5),
        click_observe_timeout: Duration::from_millis(20),
        stage_settle_timeout: Duration::from_millis(20),
        max_clicks_per_stage: 2,
        navigation_attempts: 1,
        attempt_window: Duration::from_secs(900),
        max_attempts_per_window: 3,
        max_sessions: 6,
        browser_disabled: false,
        um_account: String::new(),
        um_password: String::new(),
    }
}

fn login_service(
    temp: &TempDir,
    page: Arc<FakePage>,
    probe: Arc<ScriptedProbe>,
    sink: Arc<FakeSink>,
) -> (LoginService, Arc<FakeFactory>) {
    login_service_with(temp, page, probe, sink, |_| {})
}

fn login_service_with(
    temp: &TempDir,
    page: Arc<FakePage>,
    probe: Arc<ScriptedProbe>,
    sink: Arc<FakeSink>,
    configure: impl FnOnce(&mut LoginConfig),
) -> (LoginService, Arc<FakeFactory>) {
    let factory = FakeFactory::new(page);
    let mut config = login_config(temp);
    configure(&mut config);
    (
        LoginService::new(config, factory.clone(), probe, sink),
        factory,
    )
}

fn wait_for(service: &LoginService, predicate: impl Fn(&Value) -> bool) -> Value {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let status = service.status();
        if predicate(&status) {
            return status;
        }
        assert!(
            Instant::now() < deadline,
            "timed out waiting for status: {status}"
        );
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn a_browser_credential_probe_timeout_is_not_manual_takeover() {
    let temp = TempDir::new("um-rust-login-probe-unknown-");
    let page = FakePage::new("http://127.0.0.1:9", Some("pending-token"));
    let probe = ScriptedProbe::new(vec![CredentialVerdict::unknown("Timeout", None); 1000]);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service(&temp, page, probe, sink.clone());
    service.start_explicit(false);
    let status = wait_for(&service, |status| {
        status["state"] == json!("unknown")
            || status["login"]["manual_reason"] == json!("credential_probe_inconclusive")
    });
    assert_eq!(
        status["state"],
        json!("unknown"),
        "网络核验失败不能一直显示登录中或需人工：{status}"
    );
    assert_eq!(status["login"], Value::Null);
    assert_eq!(status["error"], Value::Null);
    assert_eq!(status["evidence"]["verdict"], json!("unknown"));
    assert_eq!(status["recovery"]["state"], json!("idle"));
    assert_eq!(factory.requests().len(), 1);
    assert_eq!(factory.closes(), 1);
    assert!(sink.published().is_empty(), "未核验成功的凭据不得发布");
}

#[test]
fn an_unknown_watch_probe_keeps_the_last_known_valid_state() {
    let temp = TempDir::new("um-rust-login-probe-recover-");
    let page = FakePage::new("http://127.0.0.1:9", Some("valid-token"));
    let (service, factory) = login_service(
        &temp,
        page,
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
    );
    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("valid"));
    let last_valid = service.status()["evidence"]["last_valid_epoch"].clone();
    service.note_probe("unknown", Some("timeout"), None);
    let status = service.status();
    assert_eq!(
        status["state"],
        json!("valid"),
        "未知核验只能记录证据，不能降级已确认有效的凭据"
    );
    assert_eq!(status["evidence"]["verdict"], json!("unknown"));
    assert_eq!(status["evidence"]["last_valid_epoch"], last_valid);
    assert!(!status["label"].as_str().unwrap().contains("需人工"));
    service.note_probe("valid", None, Some(200));
    assert_eq!(service.status()["state"], json!("valid"));
    assert_eq!(factory.requests().len(), 1);
}

#[test]
fn an_unknown_watch_probe_keeps_needs_login_and_opens_no_browser() {
    let temp = TempDir::new("um-rust-login-probe-needs-login-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let (service, factory) = login_service(
        &temp,
        page,
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
    );
    assert_eq!(service.status()["state"], json!("needs_login"));
    service.note_probe("unknown", Some("timeout"), None);
    let status = service.status();
    assert_eq!(
        status["state"],
        json!("needs_login"),
        "未知核验不能把已知的需登录状态降级为 unknown"
    );
    assert_eq!(status["evidence"]["verdict"], json!("unknown"));
    assert_eq!(
        factory.requests().len(),
        0,
        "未知核验绝不能打开登录窗口"
    );
}

#[test]
fn a_rejected_watch_probe_marks_needs_login_without_opening_a_browser() {
    let temp = TempDir::new("um-rust-login-probe-rejected-");
    let page = FakePage::new("http://127.0.0.1:9", Some("valid-token"));
    let (service, factory) = login_service(
        &temp,
        page,
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
    );
    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("valid"));
    service.note_probe("rejected", Some("upstream_rejected"), Some(401));
    let status = service.status();
    assert_eq!(status["state"], json!("needs_login"));
    assert_eq!(status["evidence"]["verdict"], json!("rejected"));
    assert_eq!(
        factory.requests().len(),
        1,
        "拒绝只记录证据并改状态，巡检本身不得打开第二个登录窗口"
    );
}

#[test]
fn status_carries_the_real_watch_rounds() {
    let temp = TempDir::new("um-rust-login-watch-status-");
    let page = FakePage::new("http://127.0.0.1:9", Some("valid-token"));
    let (service, _) = login_service(
        &temp,
        page,
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
    );
    assert_eq!(service.status()["watch"]["thread_alive"], json!(false));
    let watch = service.watch();
    watch.mark_started();
    watch.record_round("valid", Duration::from_secs(300));
    let status = service.status();
    assert_eq!(status["watch"]["thread_alive"], json!(true));
    assert_eq!(status["watch"]["rounds"], json!(1));
    assert_eq!(status["watch"]["last_round_outcome"], json!("valid"));
    let due = status["watch"]["next_round_due"]
        .as_f64()
        .expect("next round due");
    let epoch = status["watch"]["last_round_epoch"]
        .as_f64()
        .expect("last round epoch");
    assert!(
        (due - epoch - 300.0).abs() < 1e-9,
        "next_round_due 必须等于 last_round_epoch 加门控间隔: {status}"
    );
    watch.mark_stopped();
    assert_eq!(service.status()["watch"]["thread_alive"], json!(false));
}

#[test]
fn a_watch_probe_does_not_erase_an_operator_cancellation() {
    let temp = TempDir::new("um-rust-login-cancel-probe-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let (service, _) = login_service(&temp, page, ScriptedProbe::valid(), Arc::new(FakeSink::default()));
    service.start_explicit(false);
    service.cancel();
    wait_for(&service, |status| status["state"] == json!("cancelled"));
    service.note_probe("unknown", Some("timeout"), None);
    assert_eq!(service.status()["state"], json!("cancelled"));
    service.note_probe("valid", None, Some(200));
    assert_eq!(service.status()["state"], json!("cancelled"));
    assert_eq!(service.status()["error"], json!("cancelled"));
}

#[test]
fn a_visible_login_keeps_the_empty_umpass_form_open_for_the_operator() {
    let temp = TempDir::new("um-rust-visible-prefill-wait-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().unwrap() = vec![
        ("https://websso.um.edu.mo/adfs/ls/".to_string(), None),
        ("http://127.0.0.1:9".to_string(), Some("operator-token".to_string())),
    ];
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(&temp, page.clone(), ScriptedProbe::valid(), sink.clone(), |config| {
        config.display_file = marker;
    });
    service.start_explicit(true);
    let status = wait_for(&service, |s| s["login"]["manual_reason"] == json!("umpass_prefill_missing"));
    assert_eq!(status["state"], json!("logging_in"), "显式可见登录必须留出人工填写表单的机会：{status}");
    assert_eq!(factory.closes(), 0);
    // 模拟本人完成表单输入，随后沿正常提交路径继续认证。
    page.fill_calls.store(2, Ordering::SeqCst);
    wait_for(&service, |s| s["state"] == json!("valid"));
    assert_eq!(factory.requests().len(), 1);
    assert_eq!(factory.closes(), 1);
    assert_eq!(sink.published()[0].0, "operator-token");
}

#[test]
fn headless_login_waits_for_saved_browser_autofill_without_env_credentials() {
    let temp = TempDir::new("um-rust-headless-autofill-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().unwrap() = vec![
        ("https://websso.um.edu.mo/adfs/ls/".to_string(), None),
        ("http://127.0.0.1:9".to_string(), Some("autofilled-token".to_string())),
    ];
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(&temp, page.clone(), ScriptedProbe::valid(), sink.clone(), |config| {
        config.stage_settle_timeout = Duration::from_millis(250);
    });
    let autofill = thread::spawn(move || {
        thread::sleep(Duration::from_millis(80));
        // 浏览器在首次渲染后才补入已保存的字段；网关没有拿到明文配置。
        page.fill_calls.store(2, Ordering::SeqCst);
    });
    service.start_explicit(false);
    let status = wait_for(&service, |s| s["state"] == json!("valid") || s["state"] == json!("failed"));
    autofill.join().unwrap();
    assert_eq!(status["state"], json!("valid"), "不能在浏览器自动填充到达前终止后台登录：{status}");
    assert_eq!(factory.requests().len(), 1);
    assert!(!factory.requests()[0].headful);
    assert_eq!(sink.published()[0].0, "autofilled-token");
}

#[test]
fn login_probe_uses_the_authentication_budget_for_a_slow_catalog() {
    let stub = StubUpstream::start();
    stub.set(support::StubBehaviour { delay: Duration::from_millis(150), ..support::StubBehaviour::default() });
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_timeout: Duration::from_millis(50),
        ..GatewayConfig::default()
    };
    let probe = HttpCredentialProbe::from_config(&config).expect("credential probe");
    assert_eq!(probe.classify(STUB_TOKEN).verdict, "valid", "自动登录核验不应使用较短的普通请求预算");
}

fn write_display_marker(temp: &TempDir) -> std::path::PathBuf {
    let auth = temp.path().join("Xauthority");
    fs::write(&auth, "cookie").expect("auth file");
    let socket = temp.path().join("X11-unix-X99");
    fs::write(&socket, "").expect("socket file");
    let marker = temp.path().join("login-display.env");
    fs::write(
        &marker,
        format!(
            "UM_LOGIN_DISPLAY=:99\nUM_LOGIN_XAUTHORITY={}\n\
             UM_LOGIN_DISPLAY_SOCKET={}\nUM_LOGIN_DISPLAY_PID={}\n",
            auth.display(),
            socket.display(),
            std::process::id()
        ),
    )
    .expect("display marker");
    marker
}

#[test]
fn a_successful_headless_login_writes_the_token_and_publishes_it() {
    let temp = TempDir::new("um-rust-login-headless-");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service(&temp, page, probe, sink.clone());

    let started = service.start_explicit(false);
    assert_eq!(started["state"], json!("logging_in"));
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["label"], json!("认证有效"));
    assert_eq!(status["error"], Value::Null);

    let requests = factory.requests();
    assert_eq!(requests.len(), 1);
    assert!(!requests[0].headful);
    assert_eq!(factory.cleanup_calls(), 1);
    assert_eq!(factory.closes(), 1);

    let raw = fs::read_to_string(temp.path().join("token.json")).expect("token file");
    let document: Value = serde_json::from_str(&raw).expect("token json");
    assert_eq!(document["url"], json!("http://127.0.0.1:9"));
    assert_eq!(document["token"], json!("fresh-token"));
    assert_eq!(document["api_key"], Value::Null);
    assert!(document["saved_at"]
        .as_str()
        .is_some_and(|value| value.len() == 19));

    assert_eq!(sink.published(), vec![("fresh-token".to_string(), None)]);
}

#[test]
fn a_slow_umpass_form_is_not_reported_as_prefill_missing() {
    let temp = TempDir::new("um-rust-login-slow-umpass-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.unfilled_polls.store(3, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") = vec![
        (
            "https://websso.um.edu.mo/idp/profile/SAML2/Redirect/SSO".to_string(),
            None,
        ),
        ("http://127.0.0.1:9".to_string(), Some("fresh-token".to_string())),
    ];
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _factory) = login_service_with(&temp, page.clone(), probe, sink, |config| {
        config.um_account = "acct".to_string();
        config.um_password = "secret".to_string();
        config.stage_settle_timeout = Duration::from_millis(200);
        config.login_timeout = Duration::from_secs(5);
    });

    let started = service.start_explicit(false);
    assert_eq!(started["state"], json!("logging_in"));
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["error"], Value::Null);
    assert_eq!(page.fill_calls(), 2);
}

fn empty_form_page() -> Arc<FakePage> {
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().unwrap() = vec![
        ("https://websso.um.edu.mo/adfs/ls/".into(), None),
        ("http://127.0.0.1:9".into(), Some("fresh-token".into())),
    ];
    page
}

fn configure_form_pair(config: &mut LoginConfig) {
    config.um_account = "fixture-account".into();
    config.um_password = " fixture-password ".into();
}

#[test]
fn configured_pair_replaces_a_preexisting_browser_account_instead_of_mixing_sources() {
    let temp = TempDir::new("um-rust-login-configured-pair-");
    let page = empty_form_page();
    page.account_prefilled.store(true, Ordering::SeqCst);
    let (service, _) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        configure_form_pair,
    );
    service.start_explicit(false);
    let status = wait_for(&service, |s| s["state"] != json!("logging_in"));
    assert_eq!(status["state"], json!("valid"));
    assert_eq!(
        *page.filled_values.lock().unwrap(),
        vec!["fixture-account", " fixture-password "]
    );
}

#[test]
fn umpass_rejection_that_clears_password_stops_after_one_submission() {
    let temp = TempDir::new("um-rust-login-cleared-password-");
    let page = empty_form_page();
    page.reject_submit.store(true, Ordering::SeqCst);
    let (service, _) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        configure_form_pair,
    );
    service.start_explicit(false);
    let status = wait_for(&service, |s| s["state"] == json!("failed"));
    assert_eq!(status["error"], json!("upstream_credential_rejected"));
    assert_eq!(page.submit_calls.load(Ordering::SeqCst), 1);
    assert_eq!(page.fill_calls(), 2);
}

#[test]
fn configured_credentials_are_not_typed_on_untrusted_sso_origins() {
    for url in [
        "http://websso.um.edu.mo/adfs/",
        "https://other.websso.um.edu.mo/adfs/",
        "https://websso.um.edu.mo:8443/adfs/",
    ] {
        let temp = TempDir::new("um-rust-login-untrusted-form-");
        let page = empty_form_page();
        page.click_steps.lock().unwrap()[0].0 = url.to_string();
        let (service, _) = login_service_with(
            &temp,
            page.clone(),
            ScriptedProbe::valid(),
            Arc::new(FakeSink::default()),
            configure_form_pair,
        );
        service.start_explicit(false);
        let status = wait_for(&service, |s| s["state"] == json!("failed"));
        assert_eq!(
            status["error"],
            json!("umpass_credentials_untrusted_origin")
        );
        assert_eq!(page.fill_calls(), 0);
    }
}

#[test]
fn login_configuration_debug_does_not_reveal_form_credentials() {
    let temp = TempDir::new("um-rust-login-config-debug-");
    let mut config = login_config(&temp);
    config.um_account = "private-fixture-account".into();
    config.um_password = "private-fixture-password".into();
    let debug = format!("{config:?}");
    assert!(!debug.contains("private-fixture"));
}

#[test]
fn an_explicit_visible_login_uses_a_headful_window() {
    let temp = TempDir::new("um-rust-login-visible-");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(&temp, page, probe, sink, |config| {
        config.display_file = marker;
    });

    let started = service.start_explicit(true);
    assert_eq!(started["state"], json!("logging_in"));
    wait_for(&service, |status| status["state"] == json!("valid"));
    let requests = factory.requests();
    assert_eq!(requests.len(), 1);
    assert!(requests[0].headful);
    assert_eq!(
        requests[0].display.as_ref().expect("display").display,
        ":99"
    );
}

#[test]
fn a_manual_display_marker_makes_the_default_round_headful() {
    let temp = TempDir::new("um-rust-login-manual-display-");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(&temp, page, probe, sink, |config| {
        config.display_file = marker;
    });

    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("valid"));
    let requests = factory.requests();
    assert_eq!(requests.len(), 1);
    assert!(requests[0].headful);
}

#[test]
fn a_closed_visible_window_cancels_the_round() {
    let temp = TempDir::new("um-rust-login-window-closed-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.close();
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, _) = login_service_with(&temp, page, probe, sink, |config| {
        config.display_file = marker;
    });

    service.start_explicit(true);
    let status = wait_for(&service, |status| status["state"] == json!("cancelled"));
    assert_eq!(status["error"], json!("window_closed"));
    assert_eq!(status["label"], json!("已取消登录"));
}

#[test]
fn an_operator_cancel_ends_the_round_and_keeps_the_service_running() {
    let temp = TempDir::new("um-rust-login-cancel-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _factory) = login_service(&temp, page, probe, sink.clone());

    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("logging_in"));
    let cancelled = service.cancel();
    assert_eq!(cancelled["cancelled"], json!(true));
    assert_eq!(cancelled["state"], json!("cancelled"));
    let status = wait_for(&service, |status| status["state"] == json!("cancelled"));
    assert_eq!(status["error"], json!("cancelled"));
    assert_eq!(status["recovery"]["pause_reason"], json!("cancelled"));
    assert!(sink.published().is_empty());

    let second = service.cancel();
    assert_eq!(second["cancelled"], json!(false));
    assert_eq!(second["state"], json!("cancelled"));
}

#[test]
fn browser_disabled_reports_the_contract_reason() {
    let temp = TempDir::new("um-rust-login-browser-disabled-");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service_with(&temp, page, probe, sink, |config| {
        config.browser_disabled = true;
    });

    service.start_explicit(false);
    let status = service.status();
    assert_eq!(status["state"], json!("failed"));
    assert_eq!(status["error"], json!("BrowserDisabled"));
    assert_eq!(status["label"], json!("登录失败"));

    let result = service.reauthenticate(Instant::now() + Duration::from_millis(50));
    assert_eq!(
        result,
        ReauthProviderResult::Unavailable {
            reason: "BrowserDisabled".to_string(),
            retry_after: None,
        }
    );
}

#[test]
fn the_auth_wait_budget_times_out_without_stopping_the_round() {
    let temp = TempDir::new("um-rust-login-auth-wait-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service_with(&temp, page, probe, sink, |config| {
        config.login_timeout = Duration::from_secs(5);
    });
    let service = Arc::new(service);
    let limits = ReauthLimits {
        auth_wait_budget: Duration::from_millis(50),
        auth_wait_max_concurrent: 8,
        auth_wait_recheck: Duration::from_millis(10),
        max_attempts_per_window: usize::MAX,
        ..ReauthLimits::default()
    };
    let reauth = ReauthenticationService::new(service.clone(), limits);
    let mut budget = AuthWaitBudget::new(Duration::from_millis(50));
    let result = reauth.trigger(&mut budget, Instant::now() + Duration::from_secs(2));
    assert_eq!(
        result,
        ReauthResult::Unavailable {
            reason: "auth_wait_timeout".to_string(),
            retry_after: None,
        }
    );
    assert_eq!(service.status()["state"], json!("logging_in"));
    service.cancel();
    wait_for(&service, |status| status["state"] == json!("cancelled"));
}

#[test]
fn the_global_start_limit_refuses_a_second_explicit_round() {
    let temp = TempDir::new("um-rust-login-global-cap-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service_with(&temp, page, probe, sink, |config| {
        config.max_attempts_per_window = 1;
    });

    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("logging_in"));
    service.cancel();
    wait_for(&service, |status| status["state"] == json!("cancelled"));

    service.start_explicit(false);
    let status = service.status();
    assert_eq!(status["state"], json!("needs_login"));
    assert_eq!(status["error"], json!("reauth_refused:global_attempt_cap"));
    assert_eq!(status["recovery"]["state"], json!("waiting"));
    assert!(status["recovery"]["next_attempt_epoch"].is_number());
}

#[test]
fn a_busy_profile_is_a_throttle_and_refunds_the_start() {
    let temp = TempDir::new("um-rust-login-profile-busy-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let first_probe = ScriptedProbe::valid();
    let second_probe = ScriptedProbe::valid();
    let first_sink = Arc::new(FakeSink::default());
    let second_sink = Arc::new(FakeSink::default());
    let (first, first_factory) =
        login_service_with(&temp, page.clone(), first_probe, first_sink, |config| {
            config.login_timeout = Duration::from_secs(5);
            config.idle_timeout = Duration::from_secs(5);
        });
    let (second, second_factory) =
        login_service_with(&temp, page, second_probe, second_sink, |config| {
            config.login_timeout = Duration::from_secs(5);
            config.idle_timeout = Duration::from_secs(5);
        });

    first.start_explicit(false);
    wait_for(&first, |status| status["state"] == json!("logging_in"));
    let deadline = Instant::now() + Duration::from_secs(3);
    while first_factory.requests().is_empty() {
        assert!(Instant::now() < deadline, "first login never launched");
        thread::sleep(Duration::from_millis(10));
    }
    second.start_explicit(false);
    let status = wait_for(&second, |status| {
        status["error"]
            .as_str()
            .is_some_and(|error| error == "login_already_running")
    });
    assert_eq!(status["state"], json!("needs_login"));
    assert_eq!(status["recovery"]["state"], json!("waiting"));
    assert_eq!(status["recovery"]["session_starts"], json!(0));
    assert_eq!(first_factory.requests().len(), 1);
    assert_eq!(second_factory.requests().len(), 0);

    second.cancel();
    first.cancel();
    wait_for(&second, |status| status["state"] == json!("cancelled"));
    wait_for(&first, |status| status["state"] == json!("cancelled"));
}

#[test]
fn a_rejected_token_is_dropped_and_reloaded_before_the_next_probe() {
    let temp = TempDir::new("um-rust-login-rejected-token-");
    let page = FakePage::new("http://127.0.0.1:9", Some("bad-token"))
        .with_reload_token(Some("good-token"));
    let probe = ScriptedProbe::rejected_then_valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service(&temp, page.clone(), probe, sink);

    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(page.drop_calls(), 1);
    assert_eq!(page.reload_calls(), 1);
    let document: Value = serde_json::from_str(
        &fs::read_to_string(temp.path().join("token.json")).expect("token file"),
    )
    .expect("token json");
    assert_eq!(document["token"], json!("good-token"));
}

#[test]
fn a_token_save_failure_is_terminal_and_publishes_nothing() {
    let temp = TempDir::new("um-rust-login-token-save-");
    fs::create_dir_all(temp.path().join("token.json")).expect("blocking dir");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service(&temp, page, probe, sink.clone());

    service.start_explicit(false);
    let status = wait_for(&service, |status| status["state"] == json!("failed"));
    assert_eq!(status["error"], json!("token_save_failed"));
    assert!(sink.published().is_empty());
}

fn gateway_config(temp: &TempDir, stub: &StubUpstream) -> GatewayConfig {
    let mut config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        share_config: Some(temp.write("share.json", &legacy_share_config())),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        token_file: Some(temp.path().join("token.json")),
        profile_dir: Some(temp.path().join("profile")),
        login_display_file: Some(temp.path().join("login-display.env")),
        login_timeout: Duration::from_secs(2),
        login_idle_timeout: Duration::from_secs(1),
        login_poll_interval: Duration::from_millis(5),
        login_click_observe_timeout: Duration::from_millis(20),
        login_stage_settle_timeout: Duration::from_millis(20),
        login_navigation_attempts: 1,
        ..GatewayConfig::default()
    };
    config.candidate = true;
    config
}

#[test]
fn the_admin_auth_surface_reports_triggers_and_verifies() {
    let temp = TempDir::new("um-rust-login-admin-");
    let stub = StubUpstream::start();
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let probe = ScriptedProbe::valid();
    let config = gateway_config(&temp, &stub);
    let server = GatewayServer::start_with_login(config, FakeFactory::new(page), probe)
        .expect("start login gateway");

    let status = client_request(
        server.addr(),
        &ClientRequest::get("/admin/auth/status").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("status");
    assert_eq!(status.status, 200);
    assert_eq!(status.json()["state"], json!("needs_login"));

    let verify = client_request(
        server.addr(),
        &ClientRequest::get("/admin/auth/verify").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("verify");
    assert_eq!(verify.status, 200);
    assert_eq!(verify.json()["verify"], json!("valid"));
    assert_eq!(verify.json()["upstream_status"], json!(200));

    let login = client_request(
        server.addr(),
        &ClientRequest::post("/admin/auth/login", &json!({"visible": false}).to_string())
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(&json!({"visible": false}).to_string()),
    )
    .expect("login");
    assert_eq!(login.status, 202);
    assert_eq!(login.json()["state"], json!("logging_in"));

    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let status = client_request(
            server.addr(),
            &ClientRequest::get("/admin/auth/status").header("X-Admin-Control", CONTROL_TOKEN),
        )
        .expect("status");
        if status.json()["state"] == json!("valid") {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "login did not finish: {}",
            status.json()
        );
        thread::sleep(Duration::from_millis(10));
    }

    let models = client_request(
        server.addr(),
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("models");
    assert_eq!(models.status, 200);
    let seen = stub.seen();
    assert!(seen.iter().any(|request| {
        request.path == "/api/models"
            && request.authorization.as_deref() == Some("Bearer fresh-token")
    }));

    let cancel = client_request(
        server.addr(),
        &ClientRequest::post("/admin/auth/cancel", "").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("cancel");
    assert_eq!(cancel.status, 200);
    assert_eq!(cancel.json()["cancelled"], json!(false));
    assert_eq!(cancel.json()["state"], json!("no_login_in_progress"));

    let wrong_method = client_request(
        server.addr(),
        &ClientRequest::get("/admin/auth/login").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("wrong method");
    assert_eq!(wrong_method.status, 405);
    assert_eq!(wrong_method.header("allow"), Some("POST"));
}

#[test]
fn the_default_assembly_serves_auth_status_without_opening_a_browser() {
    let temp = TempDir::new("um-rust-login-default-assembly-");
    let stub = StubUpstream::start();
    let mut config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        token_file: Some(temp.path().join("token.json")),
        profile_dir: Some(temp.path().join("profile")),
        login_display_file: Some(temp.path().join("login-display.env")),
        ..GatewayConfig::default()
    };
    config.candidate = true;
    let server = GatewayServer::start(config).expect("default gateway");
    let status = client_request(
        server.addr(),
        &ClientRequest::get("/admin/auth/status").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("status");
    assert_eq!(status.status, 200);
    assert_eq!(status.json()["state"], json!("needs_login"));
    assert_eq!(status.json()["recovery"]["state"], json!("idle"));
}

#[test]
fn a_saved_token_is_loaded_and_used_by_the_default_assembly() {
    let temp = TempDir::new("um-rust-login-saved-token-");
    let stub = StubUpstream::start();
    let saved = json!({
        "url": stub.url(),
        "token": "saved-token",
        "api_key": Value::Null,
        "saved_at": "2026-11-16 12:00:00",
    });
    temp.write("token.json", &saved.to_string());
    let mut config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        share_config: Some(temp.write("share.json", &legacy_share_config())),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        token_file: Some(temp.path().join("token.json")),
        profile_dir: Some(temp.path().join("profile")),
        login_display_file: Some(temp.path().join("login-display.env")),
        ..GatewayConfig::default()
    };
    config.candidate = true;
    let server = GatewayServer::start_with_reauth(config, None).expect("default gateway");

    let status = client_request(
        server.addr(),
        &ClientRequest::get("/admin/auth/status").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("status");
    assert_eq!(status.json()["state"], json!("valid"));
    assert_eq!(status.json()["label"], json!("认证有效"));

    let models = client_request(
        server.addr(),
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("models");
    assert_eq!(models.status, 200);
    let seen = stub.seen();
    assert!(seen.iter().any(|request| {
        request.path == "/api/models"
            && request.authorization.as_deref() == Some("Bearer saved-token")
    }));
}
