//! #69: the real login executor's state machine, profile lock, token write,
//! admin surface and BrowserDisabled behavior. Every test injects a fake browser,
//! a fake profile and a fake credential probe; no real browser opens.

mod support;

use std::collections::VecDeque;
use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use um_gateway::config::{GatewayConfig, Role};
use um_gateway::login::profile::acquire_profile_lock;
use um_gateway::login::{
    BrowserUnavailable, CredentialEvidence, CredentialField, CredentialProbe, CredentialSink,
    CredentialVerdict, HttpCredentialProbe, LaunchRequest, LoginBrowser, LoginBrowserFactory,
    LoginConfig, LoginFailure, LoginPage, LoginService, VisibleTarget,
};
use um_gateway::reauth::{
    AuthWaitBudget, ReauthLimits, ReauthProviderResult, ReauthResult, ReauthTrigger,
    ReauthenticationProvider, ReauthenticationService,
};
use um_gateway::{GatewayServer, ServerHandle};

use um_gateway::login::history::read_records;

use support::{
    client_request, legacy_share_config, ChatAttempt, ChatStub, ClientRequest, StubUpstream,
    TempDir, CONTROL_TOKEN, SHARE_SECRET, STUB_TOKEN,
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
    submit_ambiguous: AtomicBool,
    click_steps: Mutex<Vec<(String, Option<String>)>>,
    fill_calls: AtomicUsize,
    filled_values: Mutex<Vec<String>>,
    account_prefilled: AtomicBool,
    account_value: Mutex<Option<String>>,
    password_prefilled: AtomicBool,
    autofill_protected: AtomicBool,
    password_cleared: AtomicBool,
    reject_submit: AtomicBool,
    credential_error: AtomicBool,
    submit_calls: AtomicUsize,
    duo_manual: AtomicBool,
    catalog_ready: bool,
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
            submit_ambiguous: AtomicBool::new(false),
            click_steps: Mutex::new(Vec::new()),
            fill_calls: AtomicUsize::new(0),
            filled_values: Mutex::new(Vec::new()),
            account_prefilled: AtomicBool::new(false),
            account_value: Mutex::new(None),
            password_prefilled: AtomicBool::new(false),
            autofill_protected: AtomicBool::new(false),
            password_cleared: AtomicBool::new(false),
            reject_submit: AtomicBool::new(false),
            credential_error: AtomicBool::new(false),
            submit_calls: AtomicUsize::new(0),
            duo_manual: AtomicBool::new(false),
            catalog_ready: false,
        })
    }

    fn with_reload_token(self: &Arc<Self>, token: Option<&str>) -> Arc<Self> {
        *self.reload_token.lock().expect("reload token") = token.map(str::to_string);
        Arc::clone(self)
    }

    fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
    }

    fn set_token(&self, token: Option<&str>) {
        *self.token.lock().expect("fake token") = token.map(str::to_string);
    }

    fn set_url(&self, url: &str) {
        *self.url.lock().expect("fake url") = url.to_string();
    }

    fn set_duo_manual(&self, manual: bool) {
        self.duo_manual.store(manual, Ordering::SeqCst);
    }

    fn set_account_value(&self, value: &str) {
        *self.account_value.lock().expect("account value") = Some(value.to_string());
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

    fn submit_clicks(&self) -> usize {
        self.submit_calls.load(Ordering::SeqCst)
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

    fn catalog_access(&self, _base_url: &str, candidate: &str, _budget: Duration) -> bool {
        self.catalog_ready && self.read_token().as_deref() == Some(candidate)
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
        if selectors.contains(&"#errorText") && self.credential_error.load(Ordering::SeqCst) {
            return true;
        }
        selectors.contains(&"input[type=\"submit\"]")
            && (self.submit_visible.load(Ordering::SeqCst)
                || (self.submit_ambiguous.load(Ordering::SeqCst)
                    && self.url().starts_with("https://websso.um.edu.mo/")))
    }

    fn any_visible_text(&self, selectors: &[&str]) -> bool {
        self.any_visible(selectors)
    }

    fn credential_field(&self, selectors: &[&str]) -> CredentialField {
        if selectors.contains(&"input[name=\"UserName\"]")
            && self.account_prefilled.load(Ordering::SeqCst)
        {
            return CredentialField::Filled;
        }
        if selectors.contains(&"input[name=\"Password\"]")
            && self.password_cleared.load(Ordering::SeqCst)
        {
            return CredentialField::Missing;
        }
        if selectors.contains(&"input[name=\"Password\"]")
            && self.password_prefilled.load(Ordering::SeqCst)
        {
            return CredentialField::Filled;
        }
        if self.unfilled_polls.load(Ordering::SeqCst) > 0 {
            self.unfilled_polls.fetch_sub(1, Ordering::SeqCst);
            return CredentialField::Missing;
        }
        if self.fill_calls.load(Ordering::SeqCst) >= 2 {
            CredentialField::Filled
        } else if self.autofill_protected.load(Ordering::SeqCst) {
            CredentialField::ProtectedAutofill
        } else {
            CredentialField::Missing
        }
    }

    fn credential_value(&self, selectors: &[&str]) -> Option<String> {
        if selectors.contains(&"input[name=\"UserName\"]") {
            return self.account_value.lock().expect("account value").clone();
        }
        None
    }

    fn one_visible(&self, _selectors: &[&str]) -> Option<VisibleTarget> {
        if !self.submit_visible.load(Ordering::SeqCst) {
            return None;
        }
        if self.submit_ambiguous.load(Ordering::SeqCst)
            && self.url().starts_with("https://websso.um.edu.mo/")
        {
            return None;
        }
        let selector = if self.url().starts_with("https://websso.um.edu.mo/") {
            "input[type=\"submit\"]"
        } else {
            "button:has-text(\"使用 UMPASS 继续\")"
        };
        Some(VisibleTarget {
            selector: selector.to_string(),
            index: 0,
        })
    }

    fn click(&self, _target: &VisibleTarget) -> Result<(), String> {
        if self.url().starts_with("https://websso.um.edu.mo/") {
            self.submit_calls.fetch_add(1, Ordering::SeqCst);
        }
        if self.reject_submit.load(Ordering::SeqCst)
            && self.url().starts_with("https://websso.um.edu.mo/")
        {
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

    fn duo_requires_confirmation(&self) -> bool {
        self.duo_manual.load(Ordering::SeqCst)
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

/// A probe the test drives by hand: scripted verdicts first, then every call
/// blocks until `release`, which answers `valid`. It records the bearer of every
/// classification so the test can prove the SAME token is rechecked serially.
struct ManualProbe {
    calls: AtomicUsize,
    bearers: Mutex<Vec<String>>,
    scripted: Mutex<VecDeque<CredentialVerdict>>,
    released: Mutex<bool>,
    condvar: Condvar,
}

impl ManualProbe {
    fn new(scripted: Vec<CredentialVerdict>) -> Arc<Self> {
        Arc::new(Self {
            calls: AtomicUsize::new(0),
            bearers: Mutex::new(Vec::new()),
            scripted: Mutex::new(scripted.into()),
            released: Mutex::new(false),
            condvar: Condvar::new(),
        })
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }

    fn bearers(&self) -> Vec<String> {
        self.bearers.lock().expect("probe bearers").clone()
    }

    fn release(&self) {
        *self.released.lock().expect("probe release") = true;
        self.condvar.notify_all();
    }
}

impl CredentialProbe for ManualProbe {
    fn classify(&self, bearer: &str) -> CredentialVerdict {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.bearers
            .lock()
            .expect("probe bearers")
            .push(bearer.to_string());
        if let Some(verdict) = self
            .scripted
            .lock()
            .expect("probe script")
            .pop_front()
        {
            return verdict;
        }
        let mut released = self.released.lock().expect("probe release");
        while !*released {
            released = self
                .condvar
                .wait(released)
                .expect("probe release poisoned");
        }
        valid_verdict()
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

fn unknown_verdict() -> CredentialVerdict {
    CredentialVerdict::unknown("Timeout", None)
}

#[derive(Default)]
struct FakeSink {
    published: Mutex<Vec<(String, Option<String>)>>,
    evidence: Mutex<CredentialEvidence>,
}

impl FakeSink {
    fn published(&self) -> Vec<(String, Option<String>)> {
        self.published
            .lock()
            .expect("published credentials")
            .clone()
    }

    fn set_evidence(&self, evidence: CredentialEvidence) {
        *self.evidence.lock().expect("sink evidence") = evidence;
    }
}

impl CredentialSink for FakeSink {
    fn publish_credential(&self, token: &str, api_key: Option<&str>) {
        self.published
            .lock()
            .expect("published credentials")
            .push((token.to_string(), api_key.map(str::to_string)));
    }

    fn credential_evidence(&self) -> CredentialEvidence {
        self.evidence.lock().expect("sink evidence").clone()
    }
}

fn login_config(temp: &TempDir) -> LoginConfig {
    LoginConfig {
        role: Role::Local,
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
        recovery_retry_interval: Duration::from_secs(1800),
        recovery_retry_max_per_day: 3,
        auth_state_file: None,
        login_history_file: Some(temp.path().join("login-history.jsonl")),
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
    login_service_with_probe(temp, page, probe, sink, configure)
}

fn login_service_with_probe(
    temp: &TempDir,
    page: Arc<FakePage>,
    probe: Arc<dyn CredentialProbe>,
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

fn wait_until(mut condition: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(3);
    while !condition() {
        assert!(
            Instant::now() < deadline,
            "timed out waiting for condition"
        );
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn a_headless_round_rechecks_the_same_candidate_and_publishes_it_once() {
    let temp = TempDir::new("um-rust-login-headless-verify-");
    let mut page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    Arc::get_mut(&mut page).unwrap().catalog_ready = true;
    let probe = ManualProbe::new(vec![unknown_verdict()]);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with_probe(&temp, page, probe.clone(), sink.clone(), |_| {});

    let started = service.start_explicit(false);
    let pending = wait_for(&service, |status| {
        status["browser_catalog_valid"] == true || status["state"] == "unknown"
    });
    probe.release();
    assert_eq!(pending["label"], "正在核验登录结果");
    assert_eq!(pending["evidence"]["verdict"], "unknown");
    let completed = wait_for(&service, |status| {
        status["state"] == "valid" || status["state"] == "unknown"
    });
    assert_eq!(completed["state"], "valid");
    assert_eq!(completed["recovery"]["round"], started["recovery"]["round"]);
    assert_eq!(factory.requests().len(), 1);
    assert_eq!(probe.bearers(), vec!["fresh-token", "fresh-token"]);
    assert_eq!(sink.published(), vec![("fresh-token".to_string(), None)]);
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
    assert_eq!(status["recovery"]["last_outcome"], "verification_unknown:verification_timeout");
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
fn protected_autofill_requires_the_operator_then_visible_login_can_continue() {
    let temp = TempDir::new("um-rust-protected-autofill-");
    let url = "https://websso.um.edu.mo/adfs/ls/";
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.autofill_protected.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            config.stage_settle_timeout = Duration::from_millis(50);
        },
    );

    *page.click_steps.lock().unwrap() = vec![
        (url.to_string(), None),
        ("http://127.0.0.1:9".to_string(), Some("unexpected-token".to_string())),
    ];
    service.reauthenticate(Instant::now() + Duration::from_secs(2));
    let status = service.status();
    assert_eq!(status["error"], json!("manual_takeover_required:umpass_autofill_protected"));
    assert_eq!(status["login"]["manual_reason"], json!("umpass_autofill_protected"));
    assert_eq!(page.fill_calls(), 0);
    assert_eq!(page.click_steps.lock().unwrap().len(), 1);
    assert!(!factory.requests()[0].headful);
    assert_eq!(factory.closes(), 1);

    *page.click_steps.lock().unwrap() = vec![
        (url.to_string(), None),
        ("http://127.0.0.1:9".to_string(), Some("operator-token".to_string())),
    ];
    write_display_marker(&temp);
    page.closed.store(false, Ordering::SeqCst);
    service.start_explicit(true);
    let status = wait_for(&service, |s| s["login"]["phase"] == json!("waiting_for_credentials"));
    assert_eq!(status["login"]["manual_reason"], json!("umpass_autofill_protected"));
    assert!(factory.requests()[1].headful);
    page.fill_calls.store(2, Ordering::SeqCst);
    wait_for(&service, |s| s["state"] == json!("valid"));
    assert_eq!(sink.published()[0].0, "operator-token");
}

#[test]
fn protected_autofill_with_env_credentials_fills_instead_of_stopping() {
    let temp = TempDir::new("um-rust-protected-autofill-env-");
    let url = "https://websso.um.edu.mo/adfs/ls/";
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.autofill_protected.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            config.um_account = "operator".to_string();
            config.um_password = "operator-secret".to_string();
            config.stage_settle_timeout = Duration::from_millis(50);
        },
    );
    *page.click_steps.lock().unwrap() = vec![
        (url.to_string(), None),
        ("http://127.0.0.1:9".to_string(), Some("env-filled-token".to_string())),
    ];

    service.reauthenticate(Instant::now() + Duration::from_secs(2));
    let status = service.status();
    assert_eq!(status["state"], json!("valid"), "{status}");
    assert_eq!(page.fill_calls(), 2);
    assert_eq!(
        page.filled_values.lock().unwrap().clone(),
        vec!["operator".to_string(), "operator-secret".to_string()]
    );
    assert_eq!(sink.published()[0].0, "env-filled-token");
    assert_eq!(factory.requests().len(), 1);
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
fn a_background_round_gets_a_fresh_deadline_after_an_expired_explicit_window() {
    let temp = TempDir::new("um-rust-login-round-deadline-");
    let page = FakePage::new("http://127.0.0.1:9", Some("first-token"));
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        |config| config.login_timeout = Duration::from_millis(700),
    );

    let first = service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("valid"));
    let old_deadline = first["login_window_deadline_epoch"].as_f64().unwrap();
    thread::sleep(Duration::from_millis(750));

    page.closed.store(false, Ordering::SeqCst);
    page.set_token(Some("second-token"));
    let _ = service.reauthenticate_for(
        Instant::now() + Duration::from_secs(2),
        ReauthTrigger::BusinessRequest,
    );
    let next = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(next["recovery"]["round"], json!(2));
    assert!(next["login_window_deadline_epoch"].as_f64().unwrap() > old_deadline);
    assert_eq!(factory.requests().len(), 2);
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
fn a_prefilled_umpass_account_that_differs_from_the_saved_account_stops_for_manual_confirmation() {
    let temp = TempDir::new("um-rust-login-account-mismatch-");
    let page = empty_form_page();
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.password_prefilled.store(true, Ordering::SeqCst);
    page.set_account_value("someone-else@um.edu.mo");
    let (service, _) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        configure_form_pair,
    );

    service.start_explicit(false);
    let status = wait_for(&service, |s| {
        s["state"] == json!("failed")
            && s["login"]["manual_reason"] == json!("umpass_account_mismatch")
    });
    assert_eq!(status["error"], json!("umpass_account_mismatch"));
    assert_eq!(page.submit_clicks(), 0, "账号不一致时不得提交旧凭据");
    assert_eq!(page.fill_calls(), 0, "账号不一致时不得覆盖页面账号");
}

#[test]
fn a_prefilled_same_account_with_a_missing_password_is_filled_and_submitted_once() {
    let temp = TempDir::new("um-rust-login-same-account-missing-password-");
    let page = empty_form_page();
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.set_account_value("FIXTURE-ACCOUNT");
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
    assert_eq!(page.submit_clicks(), 1, "补填后只自动提交一次");
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

// ---------------------------------------------------------------------------
// #101: 点击「继续登录」后，只有可信 UMPASS 页面上账号、密码栏已就绪且提交
// 按钮唯一时才自动提交一次；受保护预填保留窗口并给出明确提示；字段未就绪、
// 提交按钮不唯一、不可信页面和上游明确拒绝分别处理并记录状态。
// ---------------------------------------------------------------------------

fn trusted_umpass_url() -> &'static str {
    "https://websso.um.edu.mo/adfs/ls/"
}

#[test]
fn a_ready_trusted_umpass_form_is_submitted_once_and_recorded() {
    let temp = TempDir::new("um-rust-umpass-submit-once-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.password_prefilled.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![(url.to_string(), None)];
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(500);
            config.login_timeout = Duration::from_secs(3);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| {
        s["login"]["phase"] == json!("umpass_submitted")
    });
    assert_eq!(status["login"]["form"], json!("ready"));
    assert_eq!(status["login"]["submit_count"], json!(1));
    assert_eq!(page.submit_clicks(), 1, "已就绪的可信表单只自动提交一次");

    // 模拟本人在同一窗口完成登录；同一轮继续。
    page.set_token(Some("operator-token"));
    let status = wait_for(&service, |s| s["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1));
    assert_eq!(status["recovery"]["last_form"], json!("ready"));
    assert_eq!(status["recovery"]["last_submit_count"], json!(1));
    assert_eq!(page.submit_clicks(), 1);
    assert_eq!(factory.requests().len(), 1);
    assert_eq!(sink.published()[0].0, "operator-token");
}

#[test]
fn a_swallowed_umpass_submit_is_not_retried_with_the_same_credentials() {
    let temp = TempDir::new("um-rust-umpass-submit-swallowed-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.password_prefilled.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    // 第一次提交被页面吞掉：URL 不变，提交按钮仍在。
    *page.click_steps.lock().expect("click steps") =
        vec![(url.to_string(), None)];
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        |config| {
            config.display_file = marker;
            config.click_observe_timeout = Duration::from_millis(50);
            config.login_timeout = Duration::from_millis(400);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| {
        s["login"]["manual_reason"] == json!("umpass_click_swallowed")
    });
    assert_eq!(status["login"]["form"], json!("submitted"));
    assert_eq!(status["login"]["submit_count"], json!(1));
    assert_eq!(page.submit_clicks(), 1, "提交被吞后不得再次自动提交旧凭据");
    assert_eq!(factory.closes(), 0, "可见窗口保持到本轮结束");
    let status = wait_for(&service, |s| s["state"] == json!("failed"));
    assert_eq!(status["recovery"]["last_form"], json!("submitted"));
    assert_eq!(status["recovery"]["last_submit_count"], json!(1));
}

#[test]
fn protected_autofill_keeps_the_window_open_with_the_clear_hint() {
    let temp = TempDir::new("um-rust-umpass-protected-hint-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.autofill_protected.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![(url.to_string(), None)];
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(50);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| {
        s["login"]["manual_reason"] == json!("umpass_autofill_protected")
    });
    assert_eq!(status["state"], json!("logging_in"));
    assert_eq!(status["login"]["phase"], json!("waiting_for_credentials"));
    assert_eq!(status["login"]["form"], json!("protected_autofill"));
    assert_eq!(
        status["login"]["detail"],
        json!("Edge 已预填，请在 UMPASS 页面点击登录")
    );
    assert_eq!(status["login"]["submit_count"], json!(0));
    assert_eq!(page.submit_clicks(), 0, "预填保护时不得自动提交");
    assert_eq!(factory.closes(), 0, "窗口必须保持打开");

    // 本人点击 UMPASS 登录后，同一轮继续。
    page.set_token(Some("operator-token"));
    let status = wait_for(&service, |s| s["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1));
    assert_eq!(status["recovery"]["last_form"], json!("protected_autofill"));
    assert_eq!(status["recovery"]["last_submit_count"], json!(0));
    assert_eq!(page.submit_clicks(), 0);
    assert_eq!(factory.requests().len(), 1);
    assert_eq!(sink.published()[0].0, "operator-token");
}

#[test]
fn a_filled_form_on_an_untrusted_umpass_origin_is_not_submitted() {
    let temp = TempDir::new("um-rust-umpass-untrusted-filled-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.password_prefilled.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") = vec![
        ("http://websso.um.edu.mo/adfs/ls/".to_string(), None),
        (
            "http://127.0.0.1:9".to_string(),
            Some("unexpected-token".to_string()),
        ),
    ];
    let marker = write_display_marker(&temp);
    let (service, _) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(20);
            config.login_timeout = Duration::from_millis(300);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| s["state"] == json!("failed"));
    assert_eq!(
        status["error"],
        json!("umpass_credentials_untrusted_origin")
    );
    assert_eq!(page.submit_clicks(), 0, "不可信页面不得自动提交");
    assert_eq!(status["recovery"]["last_form"], json!("untrusted_origin"));
    assert_eq!(status["recovery"]["last_submit_count"], json!(0));
}

#[test]
fn a_missing_umpass_form_waits_with_a_clear_state() {
    let temp = TempDir::new("um-rust-umpass-missing-form-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![(url.to_string(), None)];
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(20);
            config.login_timeout = Duration::from_secs(3);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| {
        s["login"]["manual_reason"] == json!("umpass_prefill_missing")
    });
    assert_eq!(status["state"], json!("logging_in"));
    assert_eq!(status["login"]["phase"], json!("waiting_for_credentials"));
    assert_eq!(status["login"]["form"], json!("missing_fields"));
    assert_eq!(
        status["login"]["detail"],
        json!("UMPASS 表单未就绪，请在 Edge 窗口中完成登录")
    );
    assert_eq!(status["login"]["submit_count"], json!(0));
    assert_eq!(page.submit_clicks(), 0, "字段未就绪时不得自动提交");
    assert_eq!(factory.closes(), 0, "窗口必须保持打开");

    page.set_token(Some("operator-token"));
    let status = wait_for(&service, |s| s["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1));
    assert_eq!(page.submit_clicks(), 0);
    assert_eq!(sink.published()[0].0, "operator-token");
}

#[test]
fn an_ambiguous_umpass_submit_is_not_clicked() {
    let temp = TempDir::new("um-rust-umpass-ambiguous-submit-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.password_prefilled.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    page.submit_ambiguous.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![(url.to_string(), None)];
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(20);
            config.login_timeout = Duration::from_secs(3);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| {
        s["login"]["manual_reason"] == json!("umpass_submit_unrecognized")
    });
    assert_eq!(status["login"]["form"], json!("submit_ambiguous"));
    assert_eq!(
        status["login"]["detail"],
        json!("UMPASS 页面有多个提交按钮，请在 Edge 窗口中手动登录")
    );
    assert_eq!(status["login"]["submit_count"], json!(0));
    assert_eq!(page.submit_clicks(), 0, "提交按钮不唯一时不得自动提交");
    assert_eq!(factory.closes(), 0, "窗口必须保持打开");

    page.set_token(Some("operator-token"));
    let status = wait_for(&service, |s| s["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1));
    assert_eq!(page.submit_clicks(), 0);
}

#[test]
fn an_upstream_rejection_after_the_automatic_umpass_submit_stops_old_credentials() {
    let temp = TempDir::new("um-rust-umpass-manual-rejected-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.autofill_protected.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    page.reject_submit.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![(url.to_string(), None)];
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(50);
            config.login_timeout = Duration::from_secs(3);
            configure_form_pair(config);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| s["state"] == json!("failed"));
    assert_eq!(status["error"], json!("upstream_credential_rejected"));
    assert_eq!(
        status["recovery"]["last_form"],
        json!("rejected")
    );
    assert_eq!(status["recovery"]["last_submit_count"], json!(1));
    assert_eq!(
        page.fill_calls(),
        2,
        "受保护但为空的字段用环境凭据成对填一次；明确拒绝后不得再填"
    );
    assert_eq!(page.submit_clicks(), 1, "只自动提交一次");
    assert_eq!(factory.closes(), 1);
}

#[test]
fn an_upstream_rejection_that_keeps_the_account_stops_old_credentials() {
    let temp = TempDir::new("um-rust-umpass-manual-rejected-kept-account-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.autofill_protected.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    page.reject_submit.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") = vec![(url.to_string(), None)];
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(50);
            config.login_timeout = Duration::from_secs(3);
            configure_form_pair(config);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |s| s["state"] == json!("failed"));
    assert_eq!(status["error"], json!("upstream_credential_rejected"));
    assert_eq!(status["recovery"]["last_form"], json!("rejected"));
    assert_eq!(status["recovery"]["last_submit_count"], json!(1));
    assert_eq!(
        page.fill_calls(),
        2,
        "环境凭据成对填一次；明确拒绝后不得再填"
    );
    assert_eq!(page.submit_clicks(), 1, "只自动提交一次");
    assert_eq!(factory.closes(), 1);
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
fn a_visible_login_rechecks_the_same_pending_token_inside_one_window() {
    let temp = TempDir::new("um-rust-visible-verification-retry-");
    let page = FakePage::new("http://127.0.0.1:9", Some("pending-token"));
    let probe = ManualProbe::new(vec![unknown_verdict(), unknown_verdict()]);
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with_probe(
        &temp,
        page.clone(),
        probe.clone(),
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.login_timeout = Duration::from_secs(2);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |status| {
        status["login"]["phase"] == json!("verifying_login")
    });
    assert_eq!(status["state"], json!("logging_in"));
    assert_eq!(status["label"], json!("正在核验登录结果"));
    assert_eq!(status["recovery"]["round"], json!(1));
    assert_eq!(
        factory.requests().len(),
        1,
        "暂时无法核验不得关闭并重开可见窗口"
    );
    assert_eq!(factory.closes(), 0);
    assert_eq!(status["evidence"]["verdict"], json!("unknown"));

    wait_until(|| probe.calls() >= 2);
    assert!(
        probe.bearers().iter().all(|bearer| bearer == "pending-token"),
        "复查必须针对同一个待核验 token：{:?}",
        probe.bearers()
    );
    let verifying = service.status();
    assert_eq!(
        verifying["login_window_deadline_epoch"], status["login_window_deadline_epoch"],
        "核验复查不得延长本轮截止时间"
    );
    assert_eq!(verifying["recovery"]["round"], json!(1));
    assert_eq!(factory.requests().len(), 1);

    probe.release();
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "核验成功仍属于同一轮");
    assert_eq!(factory.requests().len(), 1, "全程只启动一个 Edge 窗口");
    assert_eq!(factory.closes(), 1, "核验通过后才正常关闭窗口");
    assert_eq!(sink.published(), vec![("pending-token".to_string(), None)]);
    let document: Value = serde_json::from_str(
        &fs::read_to_string(temp.path().join("token.json")).expect("token file"),
    )
    .expect("token json");
    assert_eq!(document["token"], json!("pending-token"));
}

#[test]
fn a_visible_login_ends_at_the_round_deadline_when_verification_stays_unknown() {
    let temp = TempDir::new("um-rust-visible-verification-deadline-");
    let page = FakePage::new("http://127.0.0.1:9", Some("pending-token"));
    let probe = ScriptedProbe::new(vec![unknown_verdict(); 1000]);
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with_probe(
        &temp,
        page.clone(),
        probe,
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.login_timeout = Duration::from_millis(400);
            config.idle_timeout = Duration::from_millis(60);
        },
    );

    let started = Instant::now();
    service.start_explicit(true);
    let status = wait_for(&service, |status| {
        status["login"]["phase"] == json!("verifying_login")
    });
    let deadline = status["login_window_deadline_epoch"]
        .as_f64()
        .expect("login window deadline");
    // 状态轮询与后台巡检的未知证据都不得续期本轮截止时间。
    for _ in 0..5 {
        let polled = service.status();
        assert_eq!(
            polled["login_window_deadline_epoch"].as_f64(),
            Some(deadline)
        );
        assert_eq!(polled["state"], json!("logging_in"));
        service.note_probe("unknown", Some("timeout"), None);
        assert_eq!(
            service.status()["login_window_deadline_epoch"].as_f64(),
            Some(deadline)
        );
    }

    let status = wait_for(&service, |status| status["state"] == json!("unknown"));
    let elapsed = started.elapsed();
    assert!(
        elapsed < Duration::from_millis(1500),
        "核验复查不得延长本轮截止时间：{elapsed:?}"
    );
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("verification_unknown:verification_timeout")
    );
    assert_eq!(status["error"], Value::Null);
    assert_eq!(status["login"], Value::Null);
    assert_eq!(status["evidence"]["verdict"], json!("unknown"));
    assert_eq!(factory.requests().len(), 1, "截止时由网关关闭同一个窗口");
    assert_eq!(factory.closes(), 1);
    assert!(sink.published().is_empty(), "未核验的凭据不得发布");
    assert!(
        !temp.path().join("token.json").exists(),
        "未核验的凭据不得落盘"
    );
    let lock = acquire_profile_lock(&temp.path().join("profile"))
        .expect("本轮截止后必须释放 profile 锁");
    drop(lock);
}

#[test]
fn a_visible_login_cancels_when_the_operator_closes_the_window_while_verifying() {
    let temp = TempDir::new("um-rust-visible-verification-close-");
    let page = FakePage::new("http://127.0.0.1:9", Some("pending-token"));
    let probe = ManualProbe::new(vec![unknown_verdict(); 1000]);
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with_probe(
        &temp,
        page.clone(),
        probe.clone(),
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.login_timeout = Duration::from_secs(5);
            config.idle_timeout = Duration::from_millis(60);
        },
    );

    service.start_explicit(true);
    wait_for(&service, |status| {
        status["login"]["phase"] == json!("verifying_login")
    });
    wait_until(|| probe.calls() >= 1);
    page.close();

    let status = wait_for(&service, |status| status["state"] == json!("cancelled"));
    assert_eq!(status["error"], json!("window_closed"));
    assert_eq!(status["label"], json!("已取消登录"));
    assert_eq!(factory.requests().len(), 1, "本人关闭的是同一个可见窗口");
    assert_eq!(factory.closes(), 1);
    assert!(sink.published().is_empty(), "未核验的凭据不得发布");
    assert!(
        !temp.path().join("token.json").exists(),
        "未核验的凭据不得落盘"
    );
    let lock = acquire_profile_lock(&temp.path().join("profile"))
        .expect("本人关闭窗口后必须释放 profile 锁");
    drop(lock);
}

#[test]
fn a_rejected_pending_token_returns_to_the_login_page_in_the_same_window() {
    let temp = TempDir::new("um-rust-visible-verification-rejected-");
    let page = FakePage::new("http://127.0.0.1:9", Some("stale-token"));
    page.submit_visible.store(true, Ordering::SeqCst);
    page.fill_calls.store(2, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") = vec![
        (
            "https://websso.um.edu.mo/adfs/ls/".to_string(),
            None,
        ),
        (
            "http://127.0.0.1:9".to_string(),
            Some("fresh-token".to_string()),
        ),
    ];
    let probe = ManualProbe::new(vec![rejected_verdict()]);
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with_probe(
        &temp,
        page.clone(),
        probe.clone(),
        sink.clone(),
        |config| {
            config.display_file = marker;
            config.login_timeout = Duration::from_secs(5);
        },
    );

    service.start_explicit(true);
    wait_until(|| probe.calls() >= 2);
    let status = service.status();
    assert_eq!(status["state"], json!("logging_in"), "明确拒绝不得结束本轮");
    assert_eq!(status["error"], Value::Null);
    assert_eq!(status["evidence"]["verdict"], json!("rejected"));
    assert_eq!(status["evidence"]["reason"], json!("upstream_rejected"));
    assert_eq!(status["recovery"]["round"], json!(1));
    assert_eq!(page.drop_calls(), 1, "被拒的 token 必须从页面丢弃");
    assert_eq!(page.reload_calls(), 1, "被拒后必须回到可继续登录的页面");
    assert_eq!(factory.requests().len(), 1, "拒绝后仍在同一个可见窗口继续登录");
    assert_eq!(factory.closes(), 0);
    assert!(sink.published().is_empty());
    assert!(!temp.path().join("token.json").exists());

    probe.release();
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1));
    assert_eq!(factory.requests().len(), 1);
    assert_eq!(factory.closes(), 1);
    assert_eq!(sink.published(), vec![("fresh-token".to_string(), None)]);
    let document: Value = serde_json::from_str(
        &fs::read_to_string(temp.path().join("token.json")).expect("token file"),
    )
    .expect("token json");
    assert_eq!(document["token"], json!("fresh-token"));
    let lock = acquire_profile_lock(&temp.path().join("profile"))
        .expect("本轮结束后必须释放 profile 锁");
    drop(lock);
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
    let result = reauth.trigger(
        ReauthTrigger::BusinessRequest,
        &mut budget,
        Instant::now() + Duration::from_secs(2),
    );
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
fn a_released_attempt_window_accepts_the_next_explicit_visible_round_directly() {
    let temp = TempDir::new("um-rust-login-cap-release-");
    let page = FakePage::new("http://127.0.0.1:9", Some("valid-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let marker = write_display_marker(&temp);
    let (service, factory) = login_service_with(&temp, page.clone(), probe, sink, |config| {
        config.attempt_window = Duration::from_secs(1);
        config.max_attempts_per_window = 1;
        config.display_file = marker;
    });

    // One completed round uses the whole rolling start budget.
    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("valid"));
    let first_round = service.status()["recovery"]["round"]
        .as_u64()
        .expect("first round");
    assert_eq!(factory.requests().len(), 1);

    // No login task is running, so the cap refuses the request, publishes the
    // retry time and opens no browser or round.
    let refused = service.start_explicit(true);
    assert_eq!(refused["state"], json!("needs_login"), "{refused}");
    assert_eq!(
        refused["error"],
        json!("reauth_refused:global_attempt_cap"),
        "{refused}"
    );
    assert_eq!(refused["recovery"]["state"], json!("waiting"), "{refused}");
    assert!(
        refused["recovery"]["next_attempt_epoch"].is_number(),
        "拒绝必须带可重试时间: {refused}"
    );
    assert_eq!(
        refused["recovery"]["round"],
        json!(first_round),
        "拒绝不得新增登录轮次: {refused}"
    );
    assert_eq!(
        factory.requests().len(),
        1,
        "拒绝不得打开浏览器: {refused}"
    );

    // Once the rolling window releases, the same explicit visible request starts
    // a new round directly: no cancel, no restart, one visible window.
    page.closed.store(false, Ordering::SeqCst);
    page.set_token(None);
    thread::sleep(Duration::from_millis(1100));
    let started = service.start_explicit(true);
    assert_eq!(started["state"], json!("logging_in"), "{started}");
    assert_eq!(started["recovery"]["state"], json!("running"), "{started}");
    assert_eq!(
        started["error"],
        Value::Null,
        "旧限流原因必须清除: {started}"
    );
    assert_eq!(
        started["recovery"]["next_attempt_epoch"],
        Value::Null,
        "旧等待时间必须清除: {started}"
    );
    assert_eq!(
        started["recovery"]["round"],
        json!(first_round + 1),
        "额度释放后必须进入新一轮: {started}"
    );

    let deadline = Instant::now() + Duration::from_secs(3);
    while factory.requests().len() < 2 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        factory.requests().len(),
        2,
        "额度释放后只允许一个新的可见窗口: {started}"
    );
    assert!(
        factory.requests()[1].headful,
        "显式可见登录必须打开可见 Edge"
    );

    page.set_token(Some("valid-token"));
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(
        status["recovery"]["round"],
        json!(first_round + 1),
        "{status}"
    );
    assert_eq!(factory.requests().len(), 2, "不得打开额外窗口: {status}");
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

#[test]
fn a_login_commit_preserves_and_publishes_the_saved_api_key() {
    let temp = TempDir::new("um-rust-login-api-key-");
    let stub = StubUpstream::start();
    let saved = json!({
        "url": stub.url(),
        "token": "stale-token",
        "api_key": "fixture-api-key",
        "saved_at": "2026-11-19 17:02:42",
    });
    temp.write("token.json", &saved.to_string());
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let config = gateway_config(&temp, &stub);
    let server = GatewayServer::start_with_login(config, FakeFactory::new(page), ScriptedProbe::valid())
        .expect("start login gateway");

    let login = client_request(
        server.addr(),
        &ClientRequest::post("/admin/auth/login", &json!({"visible": false}).to_string())
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(&json!({"visible": false}).to_string()),
    )
    .expect("login");
    assert_eq!(login.status, 202);

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

    let document: Value = serde_json::from_str(
        &fs::read_to_string(temp.path().join("token.json")).expect("token file"),
    )
    .expect("token json");
    assert_eq!(document["token"], json!("fresh-token"));
    assert_eq!(
        document["api_key"],
        json!("fixture-api-key"),
        "登录提交新 token 时不得清掉已保存的 API key"
    );

    let models = client_request(
        server.addr(),
        &ClientRequest::get("/v1/models").bearer(SHARE_SECRET),
    )
    .expect("models");
    assert_eq!(models.status, 200);
    let seen = stub.seen();
    assert!(seen.iter().any(|request| {
        request.path == "/api/models"
            && request.authorization.as_deref() == Some("Bearer fixture-api-key")
    }));
}

#[test]
fn the_auth_state_file_records_the_probe_evidence_and_api_key_flags() {
    let temp = TempDir::new("um-rust-auth-state-probe-");
    let page = FakePage::new("http://127.0.0.1:9", Some("stored-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let token = "header.eyJpZCI6InVzZXItMSIsImp0aSI6Imp0aS1zZWNyZXQiLCJpYXQiOjE3MDAwMDAwMDAsImV4cCI6MTcwMDAzMjQwMH0.signature";
    temp.write(
        "token.json",
        &json!({
            "url": "http://127.0.0.1:9",
            "token": token,
            "api_key": "fixture-api-key",
            "saved_at": "2026-11-19 17:02:42",
        })
        .to_string(),
    );
    sink.set_evidence(CredentialEvidence {
        checked_epoch: Some(100.0),
        verdict: Some("rejected".to_string()),
        reason: Some("upstream_rejected".to_string()),
        upstream_status: Some(401),
        last_valid_epoch: Some(90.0),
        upstream_401_summary: Some("401 Unauthorized".to_string()),
        api_key_present: true,
        api_key_rejected: false,
    });
    let state_file = temp.path().join("auth-state.json");
    let (service, _) = login_service_with(&temp, page, probe, sink.clone(), |config| {
        config.auth_state_file = Some(state_file.clone());
    });

    let startup: Value = serde_json::from_str(
        &fs::read_to_string(&state_file).expect("startup auth state"),
    )
    .expect("startup auth state json");
    assert_eq!(startup["recovery"]["state"], json!("idle"));
    assert_eq!(startup["saved_at"], json!("2026-11-19 17:02:42"));
    assert_eq!(startup["token_iat"], json!(1700000000));
    assert_eq!(startup["token_exp"], json!(1700032400));
    assert_eq!(
        startup["token_jti_sha12"].as_str().map(str::len),
        Some(12)
    );
    assert_eq!(
        startup["token_id_sha12"].as_str().map(str::len),
        Some(12)
    );
    let text = fs::read_to_string(&state_file).expect("startup auth state");
    assert!(!text.contains("stored-token"));
    assert!(!text.contains("fixture-api-key"));
    assert!(!text.contains("jti-secret"));
    assert!(!text.contains("user-1"));

    service.note_probe("rejected", Some("upstream_rejected"), Some(401));
    let state: Value =
        serde_json::from_str(&fs::read_to_string(&state_file).expect("auth state"))
            .expect("auth state json");
    assert_eq!(state["verdict"], json!("rejected"));
    assert_eq!(state["reason"], json!("upstream_rejected"));
    assert_eq!(state["upstream_status"], json!(401));
    assert_eq!(state["source"], json!("auth_watch"));
    assert!(state["checked_epoch"].is_number());
    assert_eq!(state["last_valid_epoch"], Value::Null);
    assert_eq!(state["upstream_401_summary"], json!("401 Unauthorized"));
    assert_eq!(state["api_key_present"], json!(true));
    assert_eq!(state["api_key_rejected"], json!(false));
}

#[test]
fn the_auth_state_file_records_the_round_outcome_and_form() {
    let temp = TempDir::new("um-rust-auth-state-round-");
    let url = trusted_umpass_url();
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.password_prefilled.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") = vec![(url.to_string(), None)];
    let marker = write_display_marker(&temp);
    let state_file = temp.path().join("auth-state.json");
    let (service, _) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
        |config| {
            config.display_file = marker;
            config.stage_settle_timeout = Duration::from_millis(500);
            config.login_timeout = Duration::from_secs(3);
            config.auth_state_file = Some(state_file.clone());
        },
    );

    service.start_explicit(true);
    wait_for(&service, |status| {
        status["login"]["phase"] == json!("umpass_submitted")
    });
    page.set_token(Some("operator-token"));
    wait_for(&service, |status| status["state"] == json!("valid"));

    let state: Value =
        serde_json::from_str(&fs::read_to_string(&state_file).expect("auth state"))
            .expect("auth state json");
    assert_eq!(state["round"], json!(1));
    assert_eq!(state["trigger_source"], json!("explicit"));
    assert_eq!(state["recovery"]["state"], json!("idle"));
    assert_eq!(state["recovery"]["last_outcome"], json!("restored"));
    assert_eq!(state["recovery"]["last_form"], json!("ready"));
    assert_eq!(state["recovery"]["last_submit_count"], json!(1));
    assert_eq!(state["recovery"]["session_starts"], json!(1));
    assert_eq!(state["verdict"], json!("valid"));
}

#[test]
fn an_auth_state_file_write_failure_does_not_fail_the_round() {
    let temp = TempDir::new("um-rust-auth-state-write-failure-");
    let blocker = temp.write("blocked", "not a directory");
    let state_file = blocker.join("auth-state.json");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service_with(&temp, page, probe, sink, |config| {
        config.auth_state_file = Some(state_file);
    });

    service.start_explicit(false);
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["last_outcome"], json!("restored"));
}

#[test]
fn a_timeout_round_schedules_a_bounded_automatic_retry() {
    let temp = TempDir::new("um-rust-recovery-retry-timeout-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let state_file = temp.path().join("auth-state.json");
    let (service, factory) = login_service_with(&temp, page.clone(), probe, sink, |config| {
        config.login_timeout = Duration::from_millis(500);
        config.stage_settle_timeout = Duration::from_secs(10);
        config.recovery_retry_interval = Duration::from_millis(50);
        config.recovery_retry_max_per_day = 3;
        config.auth_state_file = Some(state_file.clone());
    });
    let service = Arc::new(service);

    service.start_explicit(false);
    let status = wait_for(&service, |status| status["state"] == json!("failed"));
    assert_eq!(status["error"], json!("timeout"));
    assert_eq!(status["recovery"]["state"], json!("exhausted"));
    assert_eq!(status["recovery"]["pause_reason"], json!("timeout"));
    assert_eq!(status["recovery"]["retry_scheduled"], json!(true));
    assert_eq!(status["recovery"]["retry_count"], json!(1));
    assert!(
        status["recovery"]["next_attempt_epoch"].is_number(),
        "{status}"
    );
    let file: Value =
        serde_json::from_str(&fs::read_to_string(&state_file).expect("auth state"))
            .expect("auth state json");
    assert_eq!(file["recovery"]["retry_scheduled"], json!(true));
    assert_eq!(file["recovery"]["pause_reason"], json!("timeout"));

    // The auth watch or a business request asks the provider again after the
    // interval; the due retry clears `Failed` and opens a NEW round.
    page.closed.store(false, Ordering::SeqCst);
    thread::sleep(Duration::from_millis(80));
    let reauth_service = Arc::clone(&service);
    let reauth = thread::spawn(move || {
        reauth_service.reauthenticate(Instant::now() + Duration::from_secs(2))
    });
    let started = wait_for(&service, |status| status["recovery"]["round"] == json!(2));
    assert_eq!(started["state"], json!("logging_in"), "{started}");
    assert_eq!(started["recovery"]["retry_scheduled"], json!(false));
    page.set_token(Some("fresh-token"));
    assert_eq!(
        reauth.join().expect("reauth result"),
        ReauthProviderResult::Restored
    );
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(2));
    assert_eq!(status["recovery"]["retry_scheduled"], json!(false));
    assert_eq!(status["recovery"]["last_outcome"], json!("restored"));
    assert_eq!(factory.requests().len(), 2, "只允许一次自动重开轮次");
}

#[test]
fn an_attempts_exhausted_round_schedules_a_bounded_automatic_retry() {
    let temp = TempDir::new("um-rust-recovery-retry-attempts-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(&temp, page.clone(), probe, sink, |config| {
        config.login_timeout = Duration::from_secs(5);
        config.max_sessions = 1;
        config.recovery_retry_interval = Duration::from_millis(50);
        config.recovery_retry_max_per_day = 3;
    });

    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("logging_in"));
    page.close();
    let status = wait_for(&service, |status| status["state"] == json!("failed"));
    assert_eq!(status["error"], json!("attempts_exhausted"), "{status}");
    assert_eq!(status["recovery"]["state"], json!("exhausted"));
    assert_eq!(
        status["recovery"]["pause_reason"],
        json!("attempts_exhausted")
    );
    assert_eq!(status["recovery"]["retry_scheduled"], json!(true));
    assert_eq!(status["recovery"]["retry_count"], json!(1));
    assert_eq!(factory.requests().len(), 1);
}

#[test]
fn a_retryable_timeout_stops_at_the_daily_retry_cap() {
    let temp = TempDir::new("um-rust-recovery-retry-cap-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(&temp, page.clone(), probe, sink, |config| {
        config.login_timeout = Duration::from_millis(100);
        config.stage_settle_timeout = Duration::from_secs(10);
        config.recovery_retry_interval = Duration::from_millis(20);
        config.recovery_retry_max_per_day = 1;
    });

    service.start_explicit(false);
    let status = wait_for(&service, |status| status["state"] == json!("failed"));
    assert_eq!(status["recovery"]["retry_scheduled"], json!(true));
    assert_eq!(status["recovery"]["retry_count"], json!(1));

    page.closed.store(false, Ordering::SeqCst);
    thread::sleep(Duration::from_millis(40));
    let result = service.reauthenticate(Instant::now() + Duration::from_secs(2));
    assert_eq!(
        result,
        ReauthProviderResult::Failed {
            reason: "timeout".to_string(),
        }
    );
    let status = wait_for(&service, |status| {
        status["recovery"]["pause_reason"] == json!("retry_exhausted")
    });
    assert_eq!(status["state"], json!("failed"), "{status}");
    assert_eq!(status["recovery"]["retry_scheduled"], json!(false));
    assert_eq!(status["recovery"]["retry_count"], json!(1));
    assert_eq!(status["recovery"]["next_attempt_epoch"], Value::Null);
    assert_eq!(status["recovery"]["last_outcome"], json!("retry_exhausted"));
    assert_eq!(factory.requests().len(), 2);
}

#[test]
fn a_manual_takeover_round_is_not_retried() {
    let temp = TempDir::new("um-rust-recovery-no-retry-manual-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![("https://unexpected.example/login".to_string(), None)];
    let probe = ScriptedProbe::valid();
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(&temp, page, probe, sink, |config| {
        config.login_timeout = Duration::from_secs(5);
        config.recovery_retry_interval = Duration::from_millis(50);
        config.recovery_retry_max_per_day = 3;
    });

    service.start_explicit(false);
    let status = wait_for(&service, |status| status["state"] == json!("failed"));
    assert_eq!(
        status["recovery"]["pause_reason"],
        json!("manual_takeover_required:unrecognized_page")
    );
    assert_eq!(status["recovery"]["retry_scheduled"], json!(false));
    assert_eq!(status["recovery"]["next_attempt_epoch"], Value::Null);
    assert_eq!(status["recovery"]["retry_count"], json!(0));

    let result = service.reauthenticate(Instant::now() + Duration::from_millis(200));
    assert_eq!(
        result,
        ReauthProviderResult::Failed {
            reason: "manual_takeover_required:unrecognized_page".to_string(),
        }
    );
    assert_eq!(service.status()["recovery"]["round"], json!(1));
    assert_eq!(factory.requests().len(), 1, "人工接管不得自动重开轮次");
}

// ---------------------------------------------------------------------------
// #102: Duo auto-pass and a prompt that only the account holder can confirm.
// ---------------------------------------------------------------------------

const DUO_PROMPT_URL: &str = "https://api-1234.duosecurity.com/frame/v4/auth/prompt";

fn duo_round_page() -> Arc<FakePage> {
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.fill_calls.store(2, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") = vec![
        ("https://websso.um.edu.mo/adfs/ls/".to_string(), None),
        (
            "https://api-1234.duosecurity.com/frame/v4/auth/prompt".to_string(),
            None,
        ),
    ];
    page
}

fn duo_timing(config: &mut LoginConfig) {
    config.login_timeout = Duration::from_secs(10);
    config.idle_timeout = Duration::from_secs(5);
    config.stage_settle_timeout = Duration::from_secs(2);
    config.click_observe_timeout = Duration::from_millis(50);
    config.poll_interval = Duration::from_millis(5);
}

#[test]
fn a_duo_auto_pass_publishes_progress_and_finishes_the_same_round() {
    let temp = TempDir::new("um-rust-duo-auto-pass-");
    let page = duo_round_page();
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            duo_timing(config);
        },
    );

    service.start_explicit(false);
    let status = wait_for(&service, |status| {
        status["login"]["phase"] == json!("awaiting_duo")
    });
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    assert_eq!(status["label"], json!("正在完成 Duo 验证"), "{status}");
    assert_eq!(
        status["login"]["manual_reason"],
        Value::Null,
        "Duo 自动通过期间不得发布人工确认原因: {status}"
    );
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(factory.requests().len(), 1);
    assert!(!factory.requests()[0].headful);
    assert_eq!(factory.closes(), 0, "Duo 过渡期间必须保留同一个窗口");

    // Duo 自行通过：页面返回应用并带上凭据，同一轮继续核验。
    page.set_url("http://127.0.0.1:9");
    page.set_token(Some("duo-auto-token"));
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
    assert_eq!(status["login"], Value::Null, "{status}");
    assert_eq!(
        factory.requests().len(),
        1,
        "Duo 自动通过不得启动新一轮或第二个窗口"
    );
    assert_eq!(factory.closes(), 1);
    assert_eq!(sink.published()[0].0, "duo-auto-token");
}

#[test]
fn a_duo_manual_prompt_keeps_the_same_visible_round_until_approval() {
    let temp = TempDir::new("um-rust-duo-manual-approve-");
    let page = duo_round_page();
    page.set_duo_manual(true);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            duo_timing(config);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |status| {
        status["login"]["manual_reason"] == json!("duo_prompt_waiting")
    });
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    assert_eq!(
        status["label"],
        json!("登录中（需人工：duo_prompt_waiting）"),
        "{status}"
    );
    assert_eq!(status["login"]["phase"], json!("awaiting_duo"), "{status}");
    assert_eq!(
        status["login"]["detail"],
        json!("请在 Duo 页面确认本次登录"),
        "{status}"
    );
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(status["recovery"]["visible"], json!(true), "{status}");
    assert_eq!(factory.requests().len(), 1);
    assert!(factory.requests()[0].headful);
    assert_eq!(factory.closes(), 0, "本人确认前必须保留同一个可见窗口");

    // 批准后页面自行返回，同一轮继续核验并转为成功。
    page.set_duo_manual(false);
    page.set_url("http://127.0.0.1:9");
    page.set_token(Some("duo-approved-token"));
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
    assert_eq!(factory.requests().len(), 1, "批准后不得重开窗口或新一轮");
    assert_eq!(factory.closes(), 1);
    assert_eq!(sink.published()[0].0, "duo-approved-token");
}

#[test]
fn a_duo_manual_prompt_can_be_cancelled() {
    let temp = TempDir::new("um-rust-duo-manual-cancel-");
    let page = duo_round_page();
    page.set_duo_manual(true);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            duo_timing(config);
        },
    );

    service.start_explicit(true);
    wait_for(&service, |status| {
        status["login"]["manual_reason"] == json!("duo_prompt_waiting")
    });
    service.cancel();
    let status = wait_for(&service, |status| status["state"] == json!("cancelled"));
    assert_eq!(status["error"], json!("cancelled"), "{status}");
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("cancelled"),
        "{status}"
    );
    assert_eq!(factory.requests().len(), 1);
    let deadline = Instant::now() + Duration::from_secs(3);
    while factory.closes() < 1 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(factory.closes(), 1);
    assert!(sink.published().is_empty());
}

#[test]
fn a_duo_manual_prompt_ends_with_the_round_timeout() {
    let temp = TempDir::new("um-rust-duo-manual-timeout-");
    let page = duo_round_page();
    page.set_duo_manual(true);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            duo_timing(config);
            config.login_timeout = Duration::from_millis(150);
        },
    );

    service.start_explicit(true);
    let status = wait_for(&service, |status| {
        status["login"]["manual_reason"] == json!("duo_prompt_waiting")
    });
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    let status = wait_for(&service, |status| status["state"] == json!("failed"));
    assert_eq!(status["error"], json!("timeout"), "{status}");
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("timeout"),
        "{status}"
    );
    assert_eq!(status["label"], json!("登录窗口已超时"), "{status}");
    assert_eq!(factory.requests().len(), 1);
    assert_eq!(factory.closes(), 1);
    assert!(sink.published().is_empty());
}

#[test]
fn a_background_duo_manual_prompt_waits_for_the_visible_choice_in_the_same_round() {
    let temp = TempDir::new("um-rust-duo-background-choice-");
    let page = duo_round_page();
    page.set_duo_manual(true);
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with(
        &temp,
        page.clone(),
        ScriptedProbe::valid(),
        sink.clone(),
        |config| {
            duo_timing(config);
        },
    );

    service.start_explicit(false);
    let status = wait_for(&service, |status| {
        status["login"]["manual_reason"] == json!("duo_prompt_waiting")
            && status["recovery"]["state"] == json!("waiting")
    });
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(status["recovery"]["visible"], json!(false), "{status}");
    assert_eq!(factory.requests().len(), 1);
    assert!(!factory.requests()[0].headful);
    let deadline = Instant::now() + Duration::from_secs(3);
    while factory.closes() < 1 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(factory.closes(), 1, "后台 Duo 等待必须释放 headless 浏览器");
    thread::sleep(Duration::from_millis(100));
    assert_eq!(
        factory.requests().len(),
        1,
        "本人选择可见继续前不得自动打开 Edge"
    );

    // 本人选择可见继续：沿用同一轮次，才打开可见窗口。
    page.closed.store(false, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") = vec![
        ("https://websso.um.edu.mo/adfs/ls/".to_string(), None),
        (
            "https://api-1234.duosecurity.com/frame/v4/auth/prompt".to_string(),
            None,
        ),
    ];
    service.start_explicit(true);
    let deadline = Instant::now() + Duration::from_secs(3);
    while factory.requests().len() < 2 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(factory.requests().len(), 2);
    assert!(factory.requests()[1].headful, "可见继续必须打开可见 Edge");
    let status = service.status();
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(status["recovery"]["visible"], json!(true), "{status}");
    assert_eq!(status["state"], json!("logging_in"), "{status}");

    // 批准后同一轮继续核验并成功。
    page.set_duo_manual(false);
    page.set_url("http://127.0.0.1:9");
    page.set_token(Some("background-approved-token"));
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
    assert_eq!(factory.requests().len(), 2);
    assert_eq!(factory.closes(), 2);
    assert_eq!(sink.published()[0].0, "background-approved-token");
}

#[test]
fn a_request_auth_wait_budget_keeps_the_duo_round_and_its_later_result() {
    let temp = TempDir::new("um-rust-duo-wait-budget-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 401,
        body: "upstream rejected".to_string(),
        retry_after: None,
    });
    let page = duo_round_page();
    let factory = FakeFactory::new(page.clone());
    let mut config = reauth_gateway_config(&temp, &stub.url());
    config.login_timeout = Duration::from_secs(10);
    config.login_idle_timeout = Duration::from_secs(5);
    config.login_stage_settle_timeout = Duration::from_secs(2);
    config.login_poll_interval = Duration::from_millis(5);
    config.login_click_observe_timeout = Duration::from_millis(50);
    config.auth_wait_budget = Some(Duration::from_millis(200));
    config.auth_wait_recheck = Duration::from_millis(10);
    let server = GatewayServer::start_with_login(config, factory.clone(), ScriptedProbe::valid())
        .expect("start reauth gateway");

    start_background_round(&server);
    let status = wait_for_status(&server, |status| {
        status["login"]["phase"] == json!("awaiting_duo")
    });
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["login"]["manual_reason"],
        Value::Null,
        "Duo 自动通过期间不得发布人工确认原因: {status}"
    );

    let page_for_auto_pass = page.clone();
    let auto_pass = thread::spawn(move || {
        thread::sleep(Duration::from_millis(500));
        page_for_auto_pass.set_url("http://127.0.0.1:9");
        page_for_auto_pass.set_token(Some("duo-later-token"));
    });
    let addr = server.addr();
    let request = thread::spawn(move || client_request(addr, &chat_request()));

    let response = request
        .join()
        .expect("request thread")
        .expect("business 401");
    assert_eq!(response.status, 503);
    assert_eq!(
        response.json()["error"]["reason"],
        json!("auth_wait_timeout")
    );

    // 请求等待预算耗尽不结束在途轮次，也不发布人工确认。
    let status = auth_status(&server);
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(status["login"]["phase"], json!("awaiting_duo"), "{status}");
    assert_eq!(
        status["login"]["manual_reason"],
        Value::Null,
        "Duo 自动通过期间不得发布人工确认原因: {status}"
    );
    assert_eq!(factory.requests().len(), 1);
    assert!(!factory.requests()[0].headful);

    // 后续 Duo 自行通过时，同一轮继续核验并发布真实结果。
    auto_pass.join().expect("auto-pass thread");
    let status = wait_for_status(&server, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
    assert_eq!(factory.requests().len(), 1);
}

#[test]
fn a_request_auth_wait_budget_reports_the_duo_manual_wait() {
    let temp = TempDir::new("um-rust-duo-wait-budget-manual-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 401,
        body: "upstream rejected".to_string(),
        retry_after: None,
    });
    let page = duo_round_page();
    page.set_duo_manual(true);
    let factory = FakeFactory::new(page.clone());
    let mut config = reauth_gateway_config(&temp, &stub.url());
    config.login_timeout = Duration::from_secs(10);
    config.login_idle_timeout = Duration::from_secs(5);
    config.login_stage_settle_timeout = Duration::from_secs(2);
    config.login_poll_interval = Duration::from_millis(5);
    config.login_click_observe_timeout = Duration::from_millis(50);
    config.auth_wait_budget = Some(Duration::from_millis(200));
    config.auth_wait_recheck = Duration::from_millis(10);
    let server = GatewayServer::start_with_login(config, factory.clone(), ScriptedProbe::valid())
        .expect("start reauth gateway");

    start_background_round(&server);
    let status = wait_for_status(&server, |status| {
        status["login"]["manual_reason"] == json!("duo_prompt_waiting")
    });
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");

    let addr = server.addr();
    let request = thread::spawn(move || client_request(addr, &chat_request()));
    let response = request
        .join()
        .expect("request thread")
        .expect("business 401");
    assert_eq!(response.status, 503);
    assert_eq!(
        response.json()["error"]["reason"],
        json!("duo_prompt_waiting")
    );
    assert_eq!(response.header("retry-after"), Some("30"));

    // 请求等待结束只影响该请求；本轮仍保留，后续结果继续更新。
    let status = auth_status(&server);
    assert_eq!(status["state"], json!("logging_in"), "{status}");
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["login"]["manual_reason"],
        json!("duo_prompt_waiting"),
        "{status}"
    );

    page.set_duo_manual(false);
    page.closed.store(false, Ordering::SeqCst);
    page.set_url("http://127.0.0.1:9");
    page.set_token(Some("manual-later-token"));
    let visible = client_request(
        server.addr(),
        &ClientRequest::post("/admin/auth/login", &json!({"visible": true}).to_string())
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(&json!({"visible": true}).to_string()),
    )
    .expect("visible continue");
    assert_eq!(visible.status, 202, "{:?}", visible.json());
    let deadline = Instant::now() + Duration::from_secs(3);
    while factory.requests().len() < 2 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(factory.requests().len(), 2);
    assert!(factory.requests()[1].headful);
    let status = wait_for_status(&server, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
}

// ---------------------------------------------------------------------------
// #99: one shared background re-authentication round for business 401s and the
// periodic auth watch. The trigger source and round id are real observations;
// nothing here opens a visible Edge window.
// ---------------------------------------------------------------------------

fn chat_request() -> ClientRequest {
    ClientRequest::post(
        "/v1/chat/completions",
        &json!({
            "model": "fixture-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 16,
        })
        .to_string(),
    )
    .header("Content-Type", "application/json")
}

fn chat_success() -> Value {
    json!({
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
}

fn reauth_gateway_config(temp: &TempDir, upstream_url: &str) -> GatewayConfig {
    let mut config = GatewayConfig {
        role: Role::Local,
        upstream_base_url: upstream_url.to_string(),
        upstream_token: STUB_TOKEN.to_string(),
        control_token_file: Some(temp.write("control.token", CONTROL_TOKEN)),
        token_file: Some(temp.path().join("token.json")),
        profile_dir: Some(temp.path().join("profile")),
        login_display_file: Some(temp.path().join("login-display.env")),
        login_timeout: Duration::from_secs(5),
        login_idle_timeout: Duration::from_secs(1),
        login_poll_interval: Duration::from_millis(5),
        login_click_observe_timeout: Duration::from_millis(20),
        login_stage_settle_timeout: Duration::from_millis(20),
        login_navigation_attempts: 1,
        // Business-path tests must not be raced by the periodic watch.
        auth_watch_interval: Some(Duration::from_secs(300)),
        ..GatewayConfig::default()
    };
    config.candidate = true;
    config
}

fn auth_status(server: &ServerHandle) -> Value {
    client_request(
        server.addr(),
        &ClientRequest::get("/admin/auth/status").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("auth status")
    .json()
}

fn start_background_round(server: &ServerHandle) -> Value {
    let started = client_request(
        server.addr(),
        &ClientRequest::post("/admin/auth/login", &json!({"visible": false}).to_string())
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(&json!({"visible": false}).to_string()),
    )
    .expect("start background round");
    assert_eq!(started.status, 202, "{:?}", started.json());
    started.json()
}

fn wait_for_status(server: &ServerHandle, predicate: impl Fn(&Value) -> bool) -> Value {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let status = auth_status(server);
        if predicate(&status) {
            return status;
        }
        assert!(
            Instant::now() < deadline,
            "timed out waiting for auth status: {status}"
        );
        thread::sleep(Duration::from_millis(10));
    }
}

fn wait_for_chat_attempts(stub: &ChatStub, count: usize) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while stub.seen().len() < count {
        assert!(
            Instant::now() < deadline,
            "timed out waiting for {count} chat attempts"
        );
        thread::sleep(Duration::from_millis(10));
    }
}

fn wait_for_browser_launches(factory: &FakeFactory, count: usize) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while factory.requests().len() < count {
        assert!(
            Instant::now() < deadline,
            "timed out waiting for {count} browser launches"
        );
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        factory.requests().len(),
        count,
        "unexpected extra browser launches"
    );
}

#[test]
fn a_business_401_starts_one_background_round_and_later_requests_use_the_restored_credential() {
    let temp = TempDir::new("um-rust-reauth-round-business-");
    let stub = ChatStub::start();
    stub.push_all(vec![
        ChatAttempt::HttpStatus {
            status: 401,
            body: "upstream rejected".to_string(),
            retry_after: None,
        },
        ChatAttempt::Json {
            status: 200,
            body: chat_success(),
        },
        ChatAttempt::Json {
            status: 200,
            body: chat_success(),
        },
    ]);
    let old_token = "fixture.eyJleHAiOjQxMDI0NDQ4MDB9.fixture";
    let page = FakePage::new("http://127.0.0.1:9", Some(old_token))
        .with_reload_token(Some("fresh-token"));
    let factory = FakeFactory::new(page);
    let mut config = reauth_gateway_config(&temp, &stub.url());
    config.upstream_token = old_token.to_string();
    config.login_history_file = Some(temp.path().join("login-history.jsonl"));
    let probe = ManualProbe::new(vec![valid_verdict(), valid_verdict()]);
    let server = GatewayServer::start_with_login(config, factory.clone(), probe.clone())
        .expect("start reauth gateway");

    let status = auth_status(&server);
    assert_eq!(status["recovery"]["round"], json!(0), "{status}");
    assert_eq!(
        status["recovery"]["trigger_source"],
        Value::Null,
        "未触发时不得伪造触发来源: {status}"
    );
    assert_eq!(status["recovery"]["last_outcome"], Value::Null, "{status}");

    let first = client_request(server.addr(), &chat_request()).expect("business 401");
    assert_eq!(
        first.status,
        200,
        "{}",
        String::from_utf8_lossy(&first.body)
    );

    let status = auth_status(&server);
    assert_eq!(status["state"], json!("valid"), "{status}");
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("business_request"),
        "{status}"
    );
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
    assert_eq!(status["evidence"]["source"], json!("login"), "{status}");
    assert_eq!(factory.requests().len(), 1);
    assert!(!factory.requests()[0].headful, "自动恢复不得打开可见 Edge");
    let seen = stub.seen();
    assert_eq!(seen.len(), 2, "{seen:?}");
    let old_bearer = format!("Bearer {old_token}");
    assert_eq!(seen[0].authorization.as_deref(), Some(old_bearer.as_str()));
    assert_eq!(seen[1].authorization.as_deref(), Some("Bearer fresh-token"));
    assert_eq!(probe.bearers(), vec!["fresh-token"]);
    let records = read_records(&temp.path().join("login-history.jsonl")).unwrap();
    assert_eq!(records[0].rejected_before_expiry_status, Some(401));

    let later = client_request(server.addr(), &chat_request()).expect("later request");
    assert_eq!(later.status, 200);
    let seen = stub.seen();
    assert_eq!(seen.len(), 3);
    assert_eq!(seen[2].authorization.as_deref(), Some("Bearer fresh-token"));
    assert_eq!(factory.requests().len(), 1, "后续请求不得再启动浏览器");
    let status = auth_status(&server);
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("business_request"),
        "{status}"
    );
}

#[test]
fn a_late_401_for_the_previous_bearer_retries_with_the_already_published_bearer() {
    let temp = TempDir::new("um-rust-reauth-late-401-");
    let stub = ChatStub::start();
    stub.push_all(vec![
        ChatAttempt::DelayedHttpStatus {
            delay: Duration::from_millis(600),
            status: 401,
            body: "upstream rejected".to_string(),
        },
        ChatAttempt::Json {
            status: 200,
            body: chat_success(),
        },
    ]);
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let factory = FakeFactory::new(page);
    let config = reauth_gateway_config(&temp, &stub.url());
    let server = GatewayServer::start_with_login(config, factory.clone(), ScriptedProbe::valid())
        .expect("start reauth gateway");
    let address = server.addr();
    let request = thread::spawn(move || client_request(address, &chat_request()).unwrap());
    wait_for_chat_attempts(&stub, 1);
    start_background_round(&server);
    wait_for_status(&server, |status| status["state"] == "valid");
    assert_eq!(request.join().unwrap().status, 200);
    assert_eq!(factory.requests().len(), 1);
    let seen = stub.seen();
    let stale = format!("Bearer {STUB_TOKEN}");
    assert_eq!(seen[0].authorization.as_deref(), Some(stale.as_str()));
    assert_eq!(seen[1].authorization.as_deref(), Some("Bearer fresh-token"));
    assert_eq!(auth_status(&server)["recovery"]["round"], 1);
}

#[test]
fn a_watch_round_and_business_401s_share_one_background_round_and_one_browser() {
    let temp = TempDir::new("um-rust-reauth-round-overlap-");
    let stub = ChatStub::start();
    stub.push_all(vec![
        ChatAttempt::HttpStatus {
            status: 401,
            body: "upstream rejected".to_string(),
            retry_after: None,
        },
        ChatAttempt::HttpStatus {
            status: 401,
            body: "upstream rejected".to_string(),
            retry_after: None,
        },
        ChatAttempt::Json {
            status: 200,
            body: chat_success(),
        },
        ChatAttempt::Json {
            status: 200,
            body: chat_success(),
        },
    ]);
    let page = FakePage::new("http://127.0.0.1:9", None);
    let factory = FakeFactory::new(page.clone());
    let mut config = reauth_gateway_config(&temp, &stub.url());
    // The watch probe carries the same rejected credential, so the first watch
    // round starts the background round without a business request.
    config.upstream_token = "rejected-token".to_string();
    config.auth_watch_interval = Some(Duration::from_millis(100));
    let server = GatewayServer::start_with_login(config, factory.clone(), ScriptedProbe::valid())
        .expect("start reauth gateway");

    let status = wait_for_status(&server, |status| {
        status["recovery"]["round"] == json!(1) && status["state"] == json!("logging_in")
    });
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("auth_watch"),
        "{status}"
    );
    wait_for_browser_launches(&factory, 1);

    // Repeated status reads and an explicit join must not open another round.
    for _ in 0..5 {
        let queried = auth_status(&server);
        assert_eq!(queried["recovery"]["round"], json!(1), "{queried}");
    }
    let joined = client_request(
        server.addr(),
        &ClientRequest::post("/admin/auth/login", &json!({"visible": false}).to_string())
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(&json!({"visible": false}).to_string()),
    )
    .expect("join explicit login");
    assert_eq!(joined.status, 202);
    assert_eq!(joined.json()["recovery"]["round"], json!(1));
    assert_eq!(factory.requests().len(), 1);

    let addr = server.addr();
    let first = thread::spawn(move || client_request(addr, &chat_request()));
    let second = thread::spawn(move || client_request(addr, &chat_request()));
    // Both business 401s must be waiting on the watch round before it restores;
    // otherwise the first retry could finish and hide a duplicate round.
    wait_for_chat_attempts(&stub, 2);
    assert_eq!(factory.requests().len(), 1);
    page.set_token(Some("fresh-token"));

    let first = first
        .join()
        .expect("first request thread")
        .expect("first request");
    let second = second
        .join()
        .expect("second request thread")
        .expect("second request");
    assert_eq!(
        first.status,
        200,
        "{}",
        String::from_utf8_lossy(&first.body)
    );
    assert_eq!(
        second.status,
        200,
        "{}",
        String::from_utf8_lossy(&second.body)
    );

    let status = wait_for_status(&server, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("auth_watch"),
        "{status}"
    );
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
    assert_eq!(
        factory.requests().len(),
        1,
        "并发请求和巡检重叠时只能有一个浏览器实例"
    );
    assert!(!factory.requests()[0].headful);
    let seen = stub.seen();
    assert_eq!(seen.len(), 4, "{seen:?}");
    let rejected = "Bearer rejected-token".to_string();
    let restored = "Bearer fresh-token".to_string();
    assert_eq!(seen[0].authorization.as_deref(), Some(rejected.as_str()));
    assert_eq!(seen[1].authorization.as_deref(), Some(rejected.as_str()));
    assert_eq!(seen[2].authorization.as_deref(), Some(restored.as_str()));
    assert_eq!(seen[3].authorization.as_deref(), Some(restored.as_str()));
}

#[test]
fn a_rejected_watch_probe_starts_one_background_round_with_its_source() {
    let temp = TempDir::new("um-rust-reauth-round-watch-");
    let stub = ChatStub::start();
    let page = FakePage::new("http://127.0.0.1:9", None);
    let factory = FakeFactory::new(page.clone());
    let mut config = reauth_gateway_config(&temp, &stub.url());
    config.upstream_token = "rejected-token".to_string();
    config.auth_watch_interval = Some(Duration::from_millis(100));
    let server = GatewayServer::start_with_login(config, factory.clone(), ScriptedProbe::valid())
        .expect("start reauth gateway");

    let status = wait_for_status(&server, |status| {
        status["recovery"]["round"] == json!(1) && status["state"] == json!("logging_in")
    });
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("auth_watch"),
        "{status}"
    );
    assert_eq!(
        status["recovery"]["last_outcome"],
        Value::Null,
        "轮次尚未结束不得预写结果: {status}"
    );
    wait_for_browser_launches(&factory, 1);
    assert!(
        !factory.requests()[0].headful,
        "巡检触发的自动轮次不得打开可见 Edge"
    );

    page.set_token(Some("fresh-token"));
    let status = wait_for_status(&server, |status| status["state"] == json!("valid"));
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("auth_watch"),
        "{status}"
    );
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("restored"),
        "{status}"
    );
    assert_eq!(factory.requests().len(), 1);
}

#[test]
fn a_background_manual_takeover_keeps_the_reason_and_opens_no_edge_until_the_visible_action() {
    let temp = TempDir::new("um-rust-reauth-round-manual-");
    let stub = ChatStub::start();
    stub.push(ChatAttempt::HttpStatus {
        status: 401,
        body: "upstream rejected".to_string(),
        retry_after: None,
    });
    let page = FakePage::new("http://127.0.0.1:9", None);
    page.submit_visible.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![("https://unexpected.example/login".to_string(), None)];
    let factory = FakeFactory::new(page.clone());
    let config = reauth_gateway_config(&temp, &stub.url());
    let server = GatewayServer::start_with_login(config, factory.clone(), ScriptedProbe::valid())
        .expect("start reauth gateway");

    let response = client_request(server.addr(), &chat_request()).expect("business 401");
    assert_eq!(response.status, 503);
    assert_eq!(
        response.json()["error"]["reason"],
        json!("manual_takeover_required:unrecognized_page")
    );

    let status = auth_status(&server);
    assert_eq!(status["state"], json!("failed"), "{status}");
    assert_eq!(status["recovery"]["round"], json!(1), "{status}");
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("business_request"),
        "{status}"
    );
    assert_eq!(
        status["recovery"]["last_outcome"],
        json!("manual_takeover_required:unrecognized_page"),
        "{status}"
    );
    assert_eq!(
        status["recovery"]["pause_reason"],
        json!("manual_takeover_required:unrecognized_page"),
        "{status}"
    );
    assert_eq!(
        status["login"]["manual_reason"],
        json!("unrecognized_page"),
        "{status}"
    );
    assert_eq!(factory.requests().len(), 1);
    assert!(
        !factory.requests()[0].headful,
        "人工接管前不得打开可见 Edge"
    );

    // The explicit visible action is the only thing that opens Edge, and it
    // starts a NEW round instead of reusing the finished background one.
    page.closed.store(false, Ordering::SeqCst);
    let visible = client_request(
        server.addr(),
        &ClientRequest::post("/admin/auth/login", &json!({"visible": true}).to_string())
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body(&json!({"visible": true}).to_string()),
    )
    .expect("visible login");
    assert_eq!(visible.status, 202);
    let deadline = Instant::now() + Duration::from_secs(3);
    while factory.requests().len() < 2 && Instant::now() < deadline {
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(factory.requests().len(), 2);
    assert!(
        factory.requests()[1].headful,
        "显式可见登录必须打开可见 Edge"
    );
    let status = auth_status(&server);
    assert_eq!(status["recovery"]["round"], json!(2), "{status}");
    assert_eq!(
        status["recovery"]["trigger_source"],
        json!("explicit"),
        "{status}"
    );
}
#[test]
fn every_started_round_records_its_trigger_and_result() {
    let temp = TempDir::new("um-rust-login-history-triggers-");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service(&temp, page.clone(), ScriptedProbe::valid(), sink.clone());
    let path = temp.path().join("login-history.jsonl");

    assert_eq!(
        service.reauthenticate_for(
            Instant::now() + Duration::from_secs(3),
            ReauthTrigger::AuthWatch,
        ),
        ReauthProviderResult::Restored
    );
    // The fake factory reuses one page object; a real browser launch opens a
    // fresh page, so reset the fake's closed flag before the next round.
    page.closed.store(false, Ordering::SeqCst);
    assert_eq!(
        service.reauthenticate_for(
            Instant::now() + Duration::from_secs(3),
            ReauthTrigger::BusinessRequest,
        ),
        ReauthProviderResult::Restored
    );
    page.closed.store(false, Ordering::SeqCst);
    service.start_explicit(true);
    wait_for(&service, |status| status["state"] == json!("valid"));

    let records = read_records(&path).expect("login history");
    assert_eq!(records.len(), 3, "每轮实际启动的登录只记录一次");
    assert_eq!(records[0].round, 3);
    assert_eq!(records[0].trigger_source.as_deref(), Some("explicit"));
    assert!(records[0].visible);
    assert_eq!(
        records[1].trigger_source.as_deref(),
        Some("business_request")
    );
    assert!(!records[1].visible);
    assert_eq!(records[2].trigger_source.as_deref(), Some("auth_watch"));
    assert!(!records[2].visible);
    for record in &records {
        assert_eq!(record.state, "completed");
        assert_eq!(record.outcome.as_deref(), Some("restored"));
        assert_eq!(record.upstream_status, Some(200));
        assert_eq!(record.submit_count, 0);
    }
}

#[test]
fn concurrent_triggers_join_one_round_and_record_once() {
    let temp = TempDir::new("um-rust-login-history-concurrent-");
    let page = FakePage::new("http://127.0.0.1:9", Some("pending-token"));
    let probe = ManualProbe::new(Vec::new());
    let sink = Arc::new(FakeSink::default());
    let (service, factory) = login_service_with_probe(&temp, page, probe.clone(), sink, |_| {});

    let first = {
        let service = service.clone();
        thread::spawn(move || {
            service.reauthenticate_for(
                Instant::now() + Duration::from_secs(3),
                ReauthTrigger::AuthWatch,
            )
        })
    };
    wait_until(|| probe.calls() >= 1);
    let second = {
        let service = service.clone();
        thread::spawn(move || {
            service.reauthenticate_for(
                Instant::now() + Duration::from_secs(3),
                ReauthTrigger::BusinessRequest,
            )
        })
    };
    let third = {
        let service = service.clone();
        thread::spawn(move || {
            service.reauthenticate_for(
                Instant::now() + Duration::from_secs(3),
                ReauthTrigger::Explicit,
            )
        })
    };
    assert_eq!(
        factory.requests().len(),
        1,
        "同一轮的并发触发不得打开第二个窗口"
    );
    probe.release();
    assert_eq!(first.join().expect("first"), ReauthProviderResult::Restored);
    assert_eq!(
        second.join().expect("second"),
        ReauthProviderResult::Restored
    );
    assert_eq!(third.join().expect("third"), ReauthProviderResult::Restored);

    let records = read_records(&temp.path().join("login-history.jsonl")).expect("login history");
    assert_eq!(records.len(), 1, "并发触发与内部重试不得新增轮次");
    assert_eq!(records[0].trigger_source.as_deref(), Some("auth_watch"));
    assert_eq!(records[0].outcome.as_deref(), Some("restored"));
}

#[test]
fn a_cancelled_round_records_its_cancel_result() {
    let temp = TempDir::new("um-rust-login-history-cancel-");
    let page = FakePage::new("http://127.0.0.1:9", None);
    let (service, _) = login_service(
        &temp,
        page,
        ScriptedProbe::valid(),
        Arc::new(FakeSink::default()),
    );
    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("logging_in"));
    service.cancel();

    let records = read_records(&temp.path().join("login-history.jsonl")).expect("login history");
    assert_eq!(records.len(), 1);
    assert_eq!(records[0].state, "completed");
    assert_eq!(records[0].outcome.as_deref(), Some("cancelled"));
    assert_eq!(records[0].reason.as_deref(), Some("cancelled"));
    assert_eq!(records[0].ended_epoch.is_some(), true);
}

#[test]
fn a_network_unknown_round_and_an_upstream_401_round_are_distinguishable() {
    let temp = TempDir::new("um-rust-login-history-unknown-401-");
    let page = FakePage::new("http://127.0.0.1:9", Some("pending-token"));
    page.account_prefilled.store(true, Ordering::SeqCst);
    page.password_prefilled.store(true, Ordering::SeqCst);
    page.submit_visible.store(true, Ordering::SeqCst);
    page.reject_submit.store(true, Ordering::SeqCst);
    *page.click_steps.lock().expect("click steps") =
        vec![("https://websso.um.edu.mo/adfs/ls/".to_string(), None)];
    let probe = ScriptedProbe::new(vec![
        CredentialVerdict {
            verdict: "unknown",
            reason: Some("ConnectionError"),
            upstream_status: None,
        },
        CredentialVerdict::unknown("ConnectionError", None),
        rejected_verdict(),
    ]);
    let (service, _) = login_service_with(
        &temp,
        page.clone(),
        probe,
        Arc::new(FakeSink::default()),
        |config| {
            configure_form_pair(config);
            config.stage_settle_timeout = Duration::from_millis(20);
            config.login_timeout = Duration::from_secs(1);
        },
    );

    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("unknown"));
    // The fake factory reuses one page object; a real browser launch opens a
    // fresh page, so reset the fake's closed flag before the next round.
    page.closed.store(false, Ordering::SeqCst);
    page.set_token(Some("stale-token"));
    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("failed"));

    let records = read_records(&temp.path().join("login-history.jsonl")).expect("login history");
    assert_eq!(records.len(), 2);
    let rejected = &records[0];
    let unknown = &records[1];
    assert_eq!(unknown.outcome.as_deref(), Some("verification_unknown"));
    assert_eq!(unknown.reason.as_deref(), Some("ConnectionError"));
    assert_eq!(unknown.upstream_status, None);
    assert_eq!(unknown.evidence_verdict.as_deref(), Some("unknown"));
    assert_eq!(rejected.outcome.as_deref(), Some("rejected"));
    assert_eq!(
        rejected.reason.as_deref(),
        Some("upstream_credential_rejected")
    );
    assert_eq!(
        rejected.upstream_status,
        Some(401),
        "HTTP 401 必须与网络核验未知可区分"
    );
    assert_eq!(rejected.evidence_verdict.as_deref(), Some("rejected"));
    assert_eq!(rejected.submit_count, 1);
}

#[test]
fn credentials_and_upstream_evidence_never_reach_the_history_file() {
    let temp = TempDir::new("um-rust-login-history-redaction-");
    let page = FakePage::new("http://127.0.0.1:9", Some("private-fixture-token"));
    let probe = ScriptedProbe::new(vec![CredentialVerdict {
        verdict: "unknown",
        reason: Some("ConnectionError Bearer private-fixture-token"),
        upstream_status: None,
    }; 2]);
    let sink = Arc::new(FakeSink::default());
    let (service, _) = login_service_with(&temp, page, probe, sink, |config| {
        config.um_account = "private-fixture-account".into();
        config.um_password = "private-fixture-password".into();
    });
    service.start_explicit(false);
    wait_for(&service, |status| status["state"] == json!("unknown"));

    let text = fs::read_to_string(temp.path().join("login-history.jsonl")).expect("login history");
    for secret in [
        "private-fixture-account",
        "private-fixture-password",
        "private-fixture-token",
        "Bearer ",
    ] {
        assert!(!text.contains(secret), "历史不得包含敏感值：{secret}");
    }
    assert!(text.contains("ConnectionError"));
    assert!(text.contains("[redacted]"));
}

#[test]
fn a_history_write_failure_never_blocks_the_login() {
    let temp = TempDir::new("um-rust-login-history-write-failure-");
    let page = FakePage::new("http://127.0.0.1:9", Some("fresh-token"));
    let sink = Arc::new(FakeSink::default());
    let blocked = temp.path().join("history-is-a-directory");
    fs::create_dir(&blocked).expect("create blocker");
    let (service, _) = login_service_with(
        &temp,
        page,
        ScriptedProbe::valid(),
        sink.clone(),
        |config| config.login_history_file = Some(blocked.clone()),
    );
    service.start_explicit(false);
    let status = wait_for(&service, |status| status["state"] == json!("valid"));
    assert_eq!(status["error"], Value::Null);
    assert_eq!(sink.published(), vec![("fresh-token".to_string(), None)]);
    assert!(read_records(&blocked).is_err());
}
