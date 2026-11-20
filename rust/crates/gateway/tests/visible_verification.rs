//! Explicit real-Edge regression: a visible login keeps the same window while
//! the pending token cannot be verified, then closes and saves it on success.
#![cfg(windows)]

mod support;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::json;

use um_gateway::login::profile::acquire_profile_lock;
use um_gateway::login::{
    BrowserUnavailable, CredentialProbe, CredentialSink, CredentialVerdict, LaunchRequest,
    LoginBrowser, LoginBrowserFactory, LoginConfig, LoginFailure, LoginPage, LoginService,
    SystemBrowserFactory,
};

struct CountingFactory {
    inner: SystemBrowserFactory,
    launches: AtomicUsize,
    closes: Arc<AtomicUsize>,
}

impl CountingFactory {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            inner: SystemBrowserFactory,
            launches: AtomicUsize::new(0),
            closes: Arc::new(AtomicUsize::new(0)),
        })
    }

    fn launches(&self) -> usize {
        self.launches.load(Ordering::SeqCst)
    }

    fn closes(&self) -> usize {
        self.closes.load(Ordering::SeqCst)
    }
}

struct CountingBrowser {
    inner: Box<dyn LoginBrowser>,
    closes: Arc<AtomicUsize>,
}

impl LoginBrowser for CountingBrowser {
    fn page(&self) -> &dyn LoginPage {
        self.inner.page()
    }

    fn close(&mut self) -> Result<(), String> {
        self.closes.fetch_add(1, Ordering::SeqCst);
        self.inner.close()
    }
}

impl LoginBrowserFactory for CountingFactory {
    fn resolve_browser(&self) -> Result<std::path::PathBuf, BrowserUnavailable> {
        self.inner.resolve_browser()
    }

    fn launch(&self, request: &LaunchRequest) -> Result<Box<dyn LoginBrowser>, LoginFailure> {
        self.launches.fetch_add(1, Ordering::SeqCst);
        let inner = self.inner.launch(request)?;
        Ok(Box::new(CountingBrowser {
            inner,
            closes: Arc::clone(&self.closes),
        }))
    }

    fn cleanup_profile(&self, profile_dir: &std::path::Path) {
        self.inner.cleanup_profile(profile_dir);
    }
}

struct ShortOutageProbe {
    calls: AtomicUsize,
    first_call: Mutex<Option<Instant>>,
}

impl ShortOutageProbe {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            calls: AtomicUsize::new(0),
            first_call: Mutex::new(None),
        })
    }

    fn calls(&self) -> usize {
        self.calls.load(Ordering::SeqCst)
    }
}

impl CredentialProbe for ShortOutageProbe {
    fn classify(&self, _bearer: &str) -> CredentialVerdict {
        self.calls.fetch_add(1, Ordering::SeqCst);
        let mut first_call = self.first_call.lock().expect("probe first call");
        let first = *first_call.get_or_insert_with(Instant::now);
        if first.elapsed() < Duration::from_secs(2) {
            CredentialVerdict::unknown("network_error", None)
        } else {
            CredentialVerdict::valid(200)
        }
    }
}

#[derive(Default)]
struct RecordingSink {
    tokens: Mutex<Vec<String>>,
}

impl RecordingSink {
    fn tokens(&self) -> Vec<String> {
        self.tokens.lock().expect("sink tokens").clone()
    }
}

impl CredentialSink for RecordingSink {
    fn publish_credential(&self, token: &str, _api_key: Option<&str>) {
        self.tokens
            .lock()
            .expect("sink tokens")
            .push(token.to_string());
    }
}

fn serve_token_page() -> (String, Arc<AtomicBool>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("loopback fixture");
    listener.set_nonblocking(true).expect("nonblocking fixture");
    let base = format!("http://{}", listener.local_addr().expect("fixture addr"));
    let stopped = Arc::new(AtomicBool::new(false));
    let stop_server = Arc::clone(&stopped);
    thread::spawn(move || {
        while !stop_server.load(Ordering::SeqCst) {
            let (mut stream, _) = match listener.accept() {
                Ok(connection) => connection,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10));
                    continue;
                }
                Err(error) => panic!("loopback fixture: {error}"),
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .expect("fixture read timeout");
            let mut raw = Vec::new();
            let mut buffer = [0u8; 1024];
            while raw.len() < 16384 {
                match stream.read(&mut buffer) {
                    Ok(0) | Err(_) => break,
                    Ok(length) => raw.extend_from_slice(&buffer[..length]),
                }
                if raw.windows(4).any(|part| part == b"\r\n\r\n") {
                    break;
                }
            }
            let body = "<!doctype html><script>\
                        localStorage.setItem('token','pending-token')\
                        </script>";
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(response.as_bytes());
        }
    });
    (base, stopped)
}

fn login_config(base_url: String, temp: &support::TempDir) -> LoginConfig {
    LoginConfig {
        base_url,
        token_file: temp.path().join("token.json"),
        profile_dir: temp.path().join("profile"),
        display_file: temp.path().join("login-display.env"),
        login_timeout: Duration::from_secs(30),
        idle_timeout: Duration::from_secs(5),
        poll_interval: Duration::from_millis(100),
        click_observe_timeout: Duration::from_millis(500),
        stage_settle_timeout: Duration::from_millis(500),
        max_clicks_per_stage: 2,
        navigation_attempts: 1,
        attempt_window: Duration::from_secs(900),
        max_attempts_per_window: 3,
        max_sessions: 6,
        browser_disabled: false,
        recovery_retry_interval: Duration::from_secs(1800),
        recovery_retry_max_per_day: 3,
        auth_state_file: None,
        login_history_file: None,
        um_account: String::new(),
        um_password: String::new(),
    }
}

#[test]
#[ignore = "opens a real visible Edge window; only uses an isolated profile and loopback fixture"]
fn a_visible_edge_window_survives_a_short_verification_outage() {
    let temp = support::TempDir::new("um-rust-visible-window-");
    let (base, stopped) = serve_token_page();
    let factory = CountingFactory::new();
    let probe = ShortOutageProbe::new();
    let sink = Arc::new(RecordingSink::default());
    let service = LoginService::new(
        login_config(base, &temp),
        factory.clone(),
        probe.clone(),
        sink.clone(),
    );

    service.start_explicit(true);
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let status = service.status();
        if status["login"]["phase"] == json!("verifying_login") && probe.calls() >= 2 {
            assert_eq!(status["state"], json!("logging_in"));
            assert_eq!(factory.launches(), 1, "核验复查期间不得重开窗口");
            assert!(
                !temp.path().join("token.json").exists(),
                "未核验的凭据不得落盘"
            );
            assert!(
                acquire_profile_lock(&temp.path().join("profile")).is_err(),
                "窗口保留期间 profile 锁必须仍被持有"
            );
            break;
        }
        assert!(
            Instant::now() < deadline,
            "real Edge did not enter verification: {status}"
        );
        thread::sleep(Duration::from_millis(200));
    }

    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        let status = service.status();
        if status["state"] == json!("valid") {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "real Edge login did not finish: {status}"
        );
        thread::sleep(Duration::from_millis(200));
    }
    assert_eq!(factory.launches(), 1, "全程只启动一个 Edge 窗口");
    assert_eq!(factory.closes(), 1, "核验通过后才正常关闭窗口");
    assert_eq!(sink.tokens(), vec!["pending-token".to_string()]);
    let lock =
        acquire_profile_lock(&temp.path().join("profile")).expect("本轮结束后必须释放 profile 锁");
    drop(lock);
    stopped.store(true, Ordering::SeqCst);
}
