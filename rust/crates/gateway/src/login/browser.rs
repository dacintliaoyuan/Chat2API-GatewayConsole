//! The browser seam and the real Chromium-family driver.
//!
//! The login state machine only touches [`LoginPage`]. Tests inject a fake
//! factory and a fake page; an explicit ignored test covers real Edge persistence. The production
//! factory launches a Chromium-family binary with `--remote-debugging-port=0`,
//! discovers the page target through the loopback DevTools endpoint, and drives it
//! over a minimal CDP WebSocket client.

use std::io::{self, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::profile::{cleanup_profile_browsers, DisplayEnvironment};

pub const BROWSER_PATH_ENV: &str = "CHAT2API_BROWSER_PATH";
pub const LOGIN_DISPLAY_FILE_ENV: &str = "CHAT2API_LOGIN_DISPLAY_FILE";
pub const LOGIN_DISPLAY_FILE: &str = "/run/um-share/login-display.env";
pub const BROWSER_STARTUP_BUDGET: Duration = Duration::from_secs(30);
pub const BROWSER_DISCOVERY_BUDGET: Duration = Duration::from_secs(30);
pub const CDP_READ_TIMEOUT: Duration = Duration::from_secs(30);
const BROWSER_CLOSE_BUDGET: Duration = Duration::from_secs(5);
const CLICK_ATTEMPTS: usize = 3;
const CLICK_BACKOFF: Duration = Duration::from_millis(100);
const CLICK_ACTIONABILITY_BUDGET: Duration = Duration::from_secs(2);
const CLICK_ACTIONABILITY_POLL: Duration = Duration::from_millis(50);
const LINUX_BROWSER_NAMES: [&str; 4] = [
    "chromium",
    "chromium-browser",
    "google-chrome",
    "microsoft-edge",
];
const WINDOWS_EDGE_ROOTS: [&str; 3] = ["PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"];

/// Visible Duo controls that only the account holder can use.
///
/// A trusted device may pass through the Duo page on its own. Only a control that
/// asks the person to approve a push, choose a method or enter a code is positive
/// evidence that the round must wait for the account holder.
pub const DUO_CONFIRMATION_SELECTORS: [&str; 10] = [
    "button:has-text(\"Send me a Push\")",
    "button:has-text(\"Call Me\")",
    "button:has-text(\"Yes, this is my device\")",
    "a:has-text(\"Enter a Passcode\")",
    "a:has-text(\"Other options\")",
    "body :has-text(\"Check for a Duo Push\")",
    "body :has-text(\"Waiting for you to approve\")",
    "body :has-text(\"Is this your device?\")",
    "body :has-text(\"Choose an authentication method\")",
    "body :has-text(\"Approve with Duo Mobile\")",
];

/// The browser binary could not be resolved at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BrowserUnavailable {
    pub reason: String,
    pub detail: String,
}

/// One launch failure that the login executor classifies.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoginFailure {
    pub reason: String,
    pub detail: String,
}

impl LoginFailure {
    pub fn unavailable(reason: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
            detail: detail.into(),
        }
    }
}

/// One visible control identified by a selector and an index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisibleTarget {
    pub selector: String,
    pub index: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialField {
    Missing,
    Empty,
    Ambiguous,
    ReadFailed,
    Filled,
    ProtectedAutofill,
}

impl CredentialField {
    pub(super) fn diagnostic_name(self) -> &'static str {
        match self {
            Self::Missing => "unresolved",
            Self::Empty => "empty",
            Self::Ambiguous => "ambiguous",
            Self::ReadFailed => "read_failed",
            Self::Filled => "filled",
            Self::ProtectedAutofill => "protected_autofill",
        }
    }
}

/// The narrow page facade the login state machine drives.
pub trait LoginPage: Send {
    fn url(&self) -> String;
    fn is_closed(&self) -> bool;
    fn read_token(&self) -> Option<String>;
    fn catalog_access(&self, _base_url: &str, _candidate: &str, _budget: Duration) -> bool {
        false
    }
    fn drop_token(&self) -> Result<(), String>;
    fn reload(&self) -> Result<(), String>;
    fn wait(&self, duration: Duration) -> Result<(), String>;
    fn bring_to_front(&self) -> Result<(), String>;
    fn goto(&self, url: &str) -> Result<(), String>;
    fn any_visible(&self, selectors: &[&str]) -> bool;
    fn any_visible_text(&self, selectors: &[&str]) -> bool;
    /// The first selector that currently shows text, if any; the login-round
    /// history records it as the rejection evidence.
    fn first_visible(&self, selectors: &[&'static str]) -> Option<&'static str> {
        selectors
            .iter()
            .copied()
            .find(|selector| self.any_visible_text(&[*selector]))
    }
    fn credential_field(&self, selectors: &[&str]) -> CredentialField;
    /// The unique visible field's trimmed text, or `None` when the field is
    /// absent, ambiguous, empty or unreadable.
    fn credential_value(&self, _selectors: &[&str]) -> Option<String> {
        None
    }
    fn one_visible(&self, selectors: &[&str]) -> Option<VisibleTarget>;
    fn click(&self, target: &VisibleTarget) -> Result<(), String>;
    fn fill(&self, selectors: &[&str], value: &str) -> Result<(), String>;
    /// Positive page evidence that Duo waits for the account holder.
    ///
    /// Absence of evidence is not a manual prompt: a trusted device may pass
    /// through Duo without any interaction. Implementations must only report
    /// `true` for controls or text that ask the person to act.
    fn duo_requires_confirmation(&self) -> bool {
        false
    }
}

/// One launched browser and its page.
pub trait LoginBrowser: Send {
    fn page(&self) -> &dyn LoginPage;
    fn close(&mut self) -> Result<(), String>;
}

/// One browser launch request selected by the login executor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchRequest {
    pub executable: PathBuf,
    pub profile_dir: PathBuf,
    pub headful: bool,
    pub display: Option<DisplayEnvironment>,
    pub initial_url: String,
}

/// Resolve, launch and clean up a login browser.
pub trait LoginBrowserFactory: Send + Sync {
    fn resolve_browser(&self) -> Result<PathBuf, BrowserUnavailable>;
    fn launch(&self, request: &LaunchRequest) -> Result<Box<dyn LoginBrowser>, LoginFailure>;
    fn cleanup_profile(&self, profile_dir: &Path);
    /// Which driver this factory runs; the login-round history records it.
    fn driver_name(&self) -> &'static str {
        "native"
    }
}

/// The production factory: Edge on Windows, the first Chromium-family binary
/// on PATH on POSIX, or an explicit `CHAT2API_BROWSER_PATH` override.
#[derive(Debug, Default)]
pub struct SystemBrowserFactory;

impl LoginBrowserFactory for SystemBrowserFactory {
    fn resolve_browser(&self) -> Result<PathBuf, BrowserUnavailable> {
        find_browser()
    }

    fn launch(&self, request: &LaunchRequest) -> Result<Box<dyn LoginBrowser>, LoginFailure> {
        launch_real_browser(request)
    }

    fn cleanup_profile(&self, profile_dir: &Path) {
        cleanup_profile_browsers(profile_dir);
    }
}

/// Resolve the browser executable exactly like Python `find_browser`.
pub fn find_browser() -> Result<PathBuf, BrowserUnavailable> {
    if let Ok(override_path) = std::env::var(BROWSER_PATH_ENV) {
        let override_path = override_path.trim();
        if !override_path.is_empty() {
            let path = PathBuf::from(override_path);
            if !path.exists() {
                return Err(BrowserUnavailable {
                    reason: "browser_config_error".to_string(),
                    detail: format!("browser_path_missing: {}", path.display()),
                });
            }
            if !path.is_file() || !is_executable(&path) {
                return Err(BrowserUnavailable {
                    reason: "browser_config_error".to_string(),
                    detail: format!("browser_path_not_executable: {}", path.display()),
                });
            }
            return Ok(path);
        }
    }
    if cfg!(windows) {
        for root in WINDOWS_EDGE_ROOTS {
            let Ok(root) = std::env::var(root) else {
                continue;
            };
            let path = PathBuf::from(root)
                .join("Microsoft")
                .join("Edge")
                .join("Application")
                .join("msedge.exe");
            if path.exists() {
                return Ok(path);
            }
        }
        return Err(BrowserUnavailable {
            reason: "edge_missing".to_string(),
            detail: "Microsoft Edge was not found".to_string(),
        });
    }
    for name in LINUX_BROWSER_NAMES {
        if let Some(path) = find_in_path(name) {
            return Ok(path);
        }
    }
    Err(BrowserUnavailable {
        reason: "browser_missing".to_string(),
        detail: "Microsoft Edge was not found on Windows; on Linux none of \
                 chromium, chromium-browser, google-chrome or microsoft-edge \
                 was found in PATH"
            .to_string(),
    })
}

fn find_in_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for directory in std::env::split_paths(&path) {
        let candidate = directory.join(name);
        if candidate.is_file() && is_executable(&candidate) {
            return Some(candidate);
        }
    }
    None
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .map(|metadata| metadata.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(path: &Path) -> bool {
    path.is_file()
}

fn launch_real_browser(request: &LaunchRequest) -> Result<Box<dyn LoginBrowser>, LoginFailure> {
    let _ = std::fs::remove_file(request.profile_dir.join("DevToolsActivePort"));
    let mut command = Command::new(&request.executable);
    command
        .arg(format!("--user-data-dir={}", request.profile_dir.display()))
        .arg("--remote-debugging-port=0")
        .arg("--remote-allow-origins=*")
        .arg("--no-first-run")
        .arg("--no-default-browser-check")
        .arg("--disable-background-networking")
        .arg("--disable-sync")
        .arg("--disable-extensions")
        .arg("--disable-features=Translate")
        // Ubuntu 23.10+ disables unprivileged user namespaces, so the bundled
        // Chromium refuses to start without --no-sandbox. Playwright's own default
        // (chromiumSandbox=false) passes the same flag; the login browser is a
        // short-lived, single-purpose process with an isolated profile.
        .arg("--no-sandbox")
        .arg("--disable-dev-shm-usage")
        .arg("--window-size=1280,900");
    if request.headful {
        command.arg("--start-maximized");
    } else {
        command.arg("--headless=new");
        command.arg("--disable-gpu");
    }
    if let Some(display) = &request.display {
        command.env("DISPLAY", &display.display);
        command.env("XAUTHORITY", &display.xauthority);
        command.env_remove("WAYLAND_DISPLAY");
    }
    // The service user's passwd HOME (/var/lib/um-share) does not exist, so
    // Chromium's crashpad handler aborts on launch (SIGTRAP) and takes the
    // gateway down with it. Give the short-lived login browser a writable HOME
    // under the console state directory instead.
    let home = request
        .profile_dir
        .parent()
        .and_then(|parent| parent.parent())
        .filter(|directory| directory.is_dir())
        .unwrap_or(&request.profile_dir);
    command.env("HOME", home);
    command.arg(&request.initial_url);
    command.stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    if request
        .executable
        .file_name()
        .and_then(std::ffi::OsStr::to_str)
        .is_some_and(|name| name.eq_ignore_ascii_case("msedge.exe"))
    {
        // Edge's compatibility relaunch can exit the child with code 0
        // before its replacement publishes DevToolsActivePort. Keep the
        // original child as the owner for startup, liveness and cleanup.
        command.arg("--edge-skip-compat-layer-relaunch");
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().map_err(|error| {
        LoginFailure::unavailable(
            "browser_launch_failed",
            format!("{:?}: {error}", error.kind()),
        )
    })?;
    let port = match wait_devtools_port(&mut child, &request.profile_dir) {
        Ok(port) => port,
        Err(error) => {
            eprintln!(
                "um-gateway: login browser failed: {}: {}",
                error.reason, error.detail
            );
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    let page_url = match discover_page_target(port, &request.initial_url) {
        Ok(url) => url,
        Err(error) => {
            eprintln!(
                "um-gateway: login page target failed: {}: {}",
                error.reason, error.detail
            );
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    let connection = CdpConnection::connect(&page_url).map_err(|error| {
        eprintln!("um-gateway: login devtools websocket failed: {error}");
        LoginFailure::unavailable(
            "browser_launch_failed",
            format!("devtools websocket: {error}"),
        )
    })?;
    let child = Arc::new(Mutex::new(child));
    let page = RealPage {
        connection: Mutex::new(connection),
        child: Arc::clone(&child),
        closed: AtomicBool::new(false),
    };
    Ok(Box::new(RealBrowser { page, child }))
}

fn wait_devtools_port(child: &mut Child, profile_dir: &Path) -> Result<u16, LoginFailure> {
    let path = profile_dir.join("DevToolsActivePort");
    let deadline = Instant::now() + BROWSER_STARTUP_BUDGET;
    loop {
        if let Ok(raw) = std::fs::read_to_string(&path) {
            if let Some(port) = raw
                .lines()
                .next()
                .and_then(|line| line.trim().parse::<u16>().ok())
            {
                if port > 0 {
                    return Ok(port);
                }
            }
        }
        if let Ok(Some(status)) = child.try_wait() {
            return Err(LoginFailure::unavailable(
                "browser_launch_failed",
                format!("browser exited before DevTools port: {status}"),
            ));
        }
        if Instant::now() >= deadline {
            return Err(LoginFailure::unavailable(
                "browser_launch_failed",
                "browser did not publish DevToolsActivePort",
            ));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn discover_page_target(port: u16, initial_url: &str) -> Result<String, LoginFailure> {
    let deadline = Instant::now() + BROWSER_DISCOVERY_BUDGET;
    loop {
        if let Ok(value) = http_get_json(port, "/json/list") {
            if let Some(url) = page_websocket_url(&value) {
                return Ok(url);
            }
        }
        if Instant::now() >= deadline {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    if let Ok(value) = http_get_json(port, "/json/version") {
        if let Some(browser_url) = value.get("webSocketDebuggerUrl").and_then(Value::as_str) {
            if let Ok(mut browser) = CdpConnection::connect(browser_url) {
                let _ = browser.call("Target.createTarget", json!({"url": initial_url}));
                let deadline = Instant::now() + BROWSER_DISCOVERY_BUDGET;
                while Instant::now() < deadline {
                    if let Ok(value) = http_get_json(port, "/json/list") {
                        if let Some(url) = page_websocket_url(&value) {
                            return Ok(url);
                        }
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
            }
        }
    }
    Err(LoginFailure::unavailable(
        "browser_launch_failed",
        "browser did not expose a page target",
    ))
}

fn page_websocket_url(value: &Value) -> Option<String> {
    value
        .as_array()?
        .iter()
        .find(|target| target.get("type").and_then(Value::as_str) == Some("page"))
        .and_then(|target| target.get("webSocketDebuggerUrl"))
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn http_get_json(port: u16, path: &str) -> io::Result<Value> {
    let address = format!("127.0.0.1:{port}")
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "devtools address"))?;
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(5))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\n\
         Connection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;
    stream.flush()?;
    // Read the head, then EXACTLY the declared Content-Length body. The
    // DevTools HTTP server keeps the connection open after the response, so
    // read_to_end would block until the timeout and the page target would never
    // be discovered.
    let mut raw = Vec::new();
    let mut buffer = [0u8; 4096];
    let header_end = loop {
        let read = stream.read(&mut buffer)?;
        if read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "devtools response head",
            ));
        }
        raw.extend_from_slice(&buffer[..read]);
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        if raw.len() > 64 * 1024 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "devtools response head",
            ));
        }
    };
    let head = String::from_utf8_lossy(&raw[..header_end]).to_string();
    let content_length = head.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        if name.trim().eq_ignore_ascii_case("content-length") {
            value.trim().parse::<usize>().ok()
        } else {
            None
        }
    });
    let mut body = raw[header_end..].to_vec();
    if let Some(content_length) = content_length {
        while body.len() < content_length {
            let read = stream.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            body.extend_from_slice(&buffer[..read]);
            if body.len() > 1024 * 1024 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "devtools response body",
                ));
            }
        }
    }
    serde_json::from_slice(&body).map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

struct RealBrowser {
    page: RealPage,
    child: Arc<Mutex<Child>>,
}

impl LoginBrowser for RealBrowser {
    fn page(&self) -> &dyn LoginPage {
        &self.page
    }

    fn close(&mut self) -> Result<(), String> {
        self.page.closed.store(true, Ordering::SeqCst);
        if browser_exited(&self.child)? {
            return Ok(());
        }
        // Match Playwright context.close(): let Chromium flush the persistent
        // profile before exit. taskkill /F loses newly written cookies.
        // Do not wait for a CDP reply: closing the browser can close its socket
        // first. The owned process exiting is the completion signal.
        let requested = self
            .page
            .connection
            .lock()
            .expect("cdp connection poisoned")
            .send_command("Browser.close", json!({}));
        let deadline = Instant::now() + BROWSER_CLOSE_BUDGET;
        while Instant::now() < deadline {
            if browser_exited(&self.child)? {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        // Only compensate for a broken/unresponsive DevTools close. Forced
        // cleanup cannot certify a flushed profile and must not commit a token.
        kill_child_tree(&self.child)?;
        Err(if requested.is_err() {
            "browser_graceful_close_failed"
        } else {
            "browser_graceful_close_timeout"
        }
        .to_string())
    }
}

impl Drop for RealBrowser {
    fn drop(&mut self) {
        if let Err(error) = self.close() {
            eprintln!("um-gateway: login browser cleanup failed: {error}");
        }
    }
}

fn browser_exited(child: &Arc<Mutex<Child>>) -> Result<bool, String> {
    child
        .lock()
        .expect("browser child poisoned")
        .try_wait()
        .map(|status| status.is_some())
        .map_err(|_| "browser_exit_check_failed".to_string())
}

struct RealPage {
    connection: Mutex<CdpConnection>,
    child: Arc<Mutex<Child>>,
    closed: AtomicBool,
}

impl LoginPage for RealPage {
    fn url(&self) -> String {
        match self.evaluate("location.href") {
            Ok(Value::String(value)) => value,
            _ => String::new(),
        }
    }

    fn is_closed(&self) -> bool {
        if self.closed.load(Ordering::SeqCst) {
            return true;
        }
        let mut child = self.child.lock().expect("browser child poisoned");
        matches!(child.try_wait(), Ok(Some(_)))
    }

    fn read_token(&self) -> Option<String> {
        match self.evaluate("localStorage.getItem('token')") {
            Ok(Value::String(value)) if !value.is_empty() => Some(value),
            _ => None,
        }
    }

    fn drop_token(&self) -> Result<(), String> {
        self.evaluate("localStorage.removeItem('token')")
            .map(|_| ())
    }

    fn catalog_access(&self, base_url: &str, candidate: &str, budget: Duration) -> bool {
        let parameters = json!([base_url, candidate, budget.as_millis()]);
        let expression = format!(
            r#"(async ([base, candidate, timeout]) => {{
                if (location.origin !== new URL(base).origin ||
                    localStorage.getItem('token') !== candidate) return false;
                try {{
                    const r = await fetch('/api/models', {{
                        headers: {{ Accept: 'application/json', Authorization: 'Bearer ' + candidate }},
                        credentials: 'same-origin', redirect: 'error', cache: 'no-store',
                        signal: AbortSignal.timeout(timeout),
                    }});
                    return r.status === 200 && Array.isArray((await r.json()).data);
                }} catch {{ return false; }}
            }})({parameters})"#
        );
        self.call(
            "Runtime.evaluate",
            json!({ "expression": expression, "returnByValue": true, "awaitPromise": true }),
        )
        .is_ok_and(|result| result["result"]["value"] == Value::Bool(true))
    }

    fn reload(&self) -> Result<(), String> {
        self.call("Page.reload", json!({"ignoreCache": false}))?;
        self.wait(Duration::from_millis(300))
    }

    fn wait(&self, duration: Duration) -> Result<(), String> {
        std::thread::sleep(duration);
        Ok(())
    }

    fn bring_to_front(&self) -> Result<(), String> {
        self.call("Page.bringToFront", json!({})).map(|_| ())
    }

    fn goto(&self, url: &str) -> Result<(), String> {
        let result = self.call("Page.navigate", json!({"url": url}))?;
        if let Some(error) = result.get("errorText").and_then(Value::as_str) {
            return Err(error.to_string());
        }
        let deadline = Instant::now() + Duration::from_secs(30);
        while Instant::now() < deadline {
            match self.evaluate("document.readyState") {
                Ok(Value::String(state)) if state == "complete" || state == "interactive" => {
                    return Ok(())
                }
                Ok(_) => {}
                Err(error) => return Err(error),
            }
            std::thread::sleep(Duration::from_millis(200));
        }
        Ok(())
    }

    fn any_visible(&self, selectors: &[&str]) -> bool {
        let expression = selector_count_expression(selectors);
        matches!(self.evaluate(&expression), Ok(Value::Number(value)) if value.as_u64().unwrap_or(0) > 0)
    }

    fn any_visible_text(&self, selectors: &[&str]) -> bool {
        let expression = selector_count_expression_with_text(selectors, true);
        matches!(self.evaluate(&expression), Ok(Value::Number(value)) if value.as_u64().unwrap_or(0) > 0)
    }

    fn credential_field(&self, selectors: &[&str]) -> CredentialField {
        let expression = credential_field_expression(selectors);
        // #101: Edge's autofill paints the field before the value commits, so the
        // first read can see the autofill marker with an empty `value` while the
        // Python `input_value()` read would see the committed text. Re-read briefly
        // before declaring the field protected; a field that stays empty with the
        // marker is the real autofill-protected case.
        for attempt in 0..5 {
            match self.evaluate(&expression) {
                Ok(Value::String(state)) if state == "filled" => return CredentialField::Filled,
                Ok(Value::String(state)) if state == "missing" => return CredentialField::Missing,
                Ok(Value::String(state)) if state == "empty" => return CredentialField::Empty,
                Ok(Value::String(state)) if state == "ambiguous" => return CredentialField::Ambiguous,
                Ok(Value::String(state)) if state == "protected" => {
                    if attempt == 4 {
                        return CredentialField::ProtectedAutofill;
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                _ => return CredentialField::ReadFailed,
            }
        }
        CredentialField::ProtectedAutofill
    }

    fn credential_value(&self, selectors: &[&str]) -> Option<String> {
        let expression = credential_value_expression(selectors);
        match self.evaluate(&expression) {
            Ok(Value::String(value)) if !value.is_empty() => Some(value),
            _ => None,
        }
    }

    fn one_visible(&self, selectors: &[&str]) -> Option<VisibleTarget> {
        let expression = one_visible_expression(selectors);
        let value = self.evaluate(&expression).ok()?;
        let selector = value.get("selector")?.as_str()?.to_string();
        let index = value.get("index")?.as_u64()? as usize;
        Some(VisibleTarget { selector, index })
    }

    fn click(&self, target: &VisibleTarget) -> Result<(), String> {
        let selector =
            serde_json::to_string(&target.selector).map_err(|error| error.to_string())?;
        // #101: Playwright's `locator.click()` scrolls the target into view and
        // waits for it to be visible, stable, enabled and hit-testable before it
        // dispatches the real mouse sequence; a synthetic `element.click()` does
        // not submit Keycloak's UMPASS form (the site keeps asking to fill in).
        // Match that behaviour: scroll, re-read the rect, wait for
        // actionability and drive `Input.dispatchMouseEvent` at the visible
        // center. A stale or swallowed click is retried within this click.
        retry_click(|| self.click_once(&selector))
    }

    fn fill(&self, selectors: &[&str], value: &str) -> Result<(), String> {
        // Replace the selection with one browser input. Clearing the DOM value
        // first would notify the page of an empty credential, and firing another
        // input/change afterwards can run validation again.
        let prepare = fill_prepare_expression(selectors);
        let prepared = self.evaluate(&prepare)?;
        let target = prepared["target"]
            .as_str()
            .ok_or_else(|| "login field is missing, ambiguous or not editable".to_string())?;
        let inserted = (|| {
            #[cfg(windows)]
            if prepared["protected"] == Value::Bool(true) {
                // Edge commits protected autofill on trusted input. DOM
                // select() before that can select an empty value, causing
                // insertText to append to the newly committed credential.
                self.call(
                    "Input.dispatchKeyEvent",
                    json!({"type": "rawKeyDown", "key": "a", "code": "KeyA",
                           "windowsVirtualKeyCode": 65, "modifiers": 2, "commands": ["SelectAll"]}),
                )?;
                self.call(
                    "Input.dispatchKeyEvent",
                    json!({"type": "keyUp", "key": "a", "code": "KeyA",
                           "windowsVirtualKeyCode": 65, "modifiers": 2}),
                )?;
                let focused = format!(
                    "document.activeElement?.getAttribute('data-um-login-fill-target') === {}",
                    json!(target)
                );
                if self.evaluate(&focused)? != Value::Bool(true) {
                    return Err("login field lost focus before entry".to_string());
                }
            }
            self.call("Input.insertText", json!({"text": value}))
        })();
        let settled = self.evaluate(&fill_settle_expression(
            target,
            inserted.as_ref().ok().map(|_| value),
        ));
        inserted?;
        if settled? == Value::Bool(true) {
            Ok(())
        } else {
            Err("login field did not retain the inserted value".to_string())
        }
    }

    fn duo_requires_confirmation(&self) -> bool {
        self.any_visible(&DUO_CONFIRMATION_SELECTORS)
    }
}

#[derive(Debug, PartialEq, Eq)]
enum ClickFailure {
    /// The target was not visible, stable, enabled or hit-testable in time.
    Actionability(String),
    /// The mouse sequence was dispatched but the page did not receive a click.
    NotReceived(String),
    /// A transport or protocol error: retrying cannot help.
    Fatal(String),
}

impl ClickFailure {
    fn message(self) -> String {
        match self {
            Self::Actionability(message) | Self::NotReceived(message) | Self::Fatal(message) => {
                message
            }
        }
    }
}

impl RealPage {
    fn click_once(&self, selector: &str) -> Result<(), ClickFailure> {
        let scrolled = self
            .evaluate(&click_scroll_expression(selector))
            .map_err(ClickFailure::Fatal)?;
        if scrolled != Value::Bool(true) {
            return Err(ClickFailure::Actionability(
                "login target disappeared".to_string(),
            ));
        }
        let (x, y) = self.wait_for_click_actionable(selector)?;
        // Arm a capture listener before dispatching so a click that the page
        // receives can be distinguished from one lost between the actionability
        // check and the mouse release (the element moved, re-rendered or became
        // covered). A page navigation also counts as received: it is the click's
        // result.
        let before_href = self
            .evaluate(&click_arm_expression())
            .map_err(ClickFailure::Fatal)?;
        let before_href = before_href.as_str().unwrap_or_default().to_string();
        for (event_type, button, click_count) in [
            ("mouseMoved", "none", 0),
            ("mousePressed", "left", 1),
            ("mouseReleased", "left", 1),
        ] {
            self.call(
                "Input.dispatchMouseEvent",
                serde_json::json!({
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": button,
                    "clickCount": click_count,
                }),
            )
            .map_err(ClickFailure::Fatal)?;
        }
        match self.evaluate(&click_receipt_expression()) {
            Ok(receipt) => {
                let received = receipt
                    .get("received")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                let href = receipt
                    .get("href")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if received || href != before_href {
                    Ok(())
                } else {
                    Err(ClickFailure::NotReceived(
                        "login click not received".to_string(),
                    ))
                }
            }
            // A destroyed execution context means the click started a navigation.
            Err(error) if error.contains("context") || error.contains("navigation") => Ok(()),
            Err(error) => Err(ClickFailure::Fatal(error)),
        }
    }

    fn wait_for_click_actionable(&self, selector: &str) -> Result<(f64, f64), ClickFailure> {
        let expression = click_actionability_expression(selector);
        let deadline = Instant::now() + CLICK_ACTIONABILITY_BUDGET;
        let mut previous_rect: Option<Vec<f64>> = None;
        let mut last_error = "login target not actionable".to_string();
        loop {
            match self.evaluate(&expression).map_err(ClickFailure::Fatal)? {
                Value::Object(state) => {
                    if state.get("ok").and_then(Value::as_bool) == Some(true) {
                        let rect = state
                            .get("rect")
                            .and_then(Value::as_array)
                            .map(|values| {
                                values
                                    .iter()
                                    .filter_map(Value::as_f64)
                                    .collect::<Vec<f64>>()
                            })
                            .unwrap_or_default();
                        let x = state.get("x").and_then(Value::as_f64);
                        let y = state.get("y").and_then(Value::as_f64);
                        if rect.len() == 4 {
                            // Stable means two consecutive polls read the same
                            // rect; the first poll only records the baseline.
                            if previous_rect.as_deref() == Some(rect.as_slice()) {
                                if let (Some(x), Some(y)) = (x, y) {
                                    return Ok((x, y));
                                }
                            }
                            previous_rect = Some(rect);
                        } else {
                            previous_rect = None;
                        }
                    } else {
                        previous_rect = None;
                        if let Some(reason) = state.get("reason").and_then(Value::as_str) {
                            last_error = format!("login target not actionable: {reason}");
                        }
                    }
                }
                _ => {
                    previous_rect = None;
                    last_error = "login target not actionable".to_string();
                }
            }
            if Instant::now() >= deadline {
                return Err(ClickFailure::Actionability(last_error));
            }
            std::thread::sleep(CLICK_ACTIONABILITY_POLL);
        }
    }

    fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        self.connection
            .lock()
            .expect("cdp connection poisoned")
            .call(method, params)
    }

    fn evaluate(&self, expression: &str) -> Result<Value, String> {
        let result = self
            .connection
            .lock()
            .expect("cdp connection poisoned")
            .evaluate(expression);
        if let Err(error) = &result {
            if error.contains("websocket closed") || error.contains("Target closed") {
                self.closed.store(true, Ordering::SeqCst);
            }
        }
        result
    }
}

fn selector_count_expression(selectors: &[&str]) -> String {
    selector_count_expression_with_text(selectors, false)
}

fn selector_count_expression_with_text(selectors: &[&str], require_text: bool) -> String {
    let selectors = serde_json::to_string(selectors).unwrap_or_else(|_| "[]".to_string());
    format!(
        "(function(selectors){{var visible=function(el){{var r=el.getBoundingClientRect();\
         var s=getComputedStyle(el);return !!(r.width||r.height||el.getClientRects().length)\
         && s.visibility!=='hidden'&&s.display!=='none'\
         && (!{require_text}||(el.textContent||'').trim().length>0);}};\
         var matches=function(sel){{if(sel.indexOf('text=')===0){{var text=sel.slice(5);\
         if(text[0]==='\"')text=text.slice(1,-1);\
         return Array.prototype.slice.call(document.querySelectorAll('*')).filter(function(el){{\
         return visible(el)&&(el.textContent||'').trim()===text;}});}}\
         var marker=sel.indexOf(':has-text(\"');if(marker>=0){{var base=sel.slice(0,marker);\
         var text=sel.slice(marker+11);if(text[text.length-1]===')')text=text.slice(0,-1);\
         if(text[text.length-1]==='\"')text=text.slice(0,-1);\
         try{{return Array.prototype.slice.call(document.querySelectorAll(base)).filter(function(el){{\
         return visible(el)&&(el.textContent||'').indexOf(text)>=0;}});}}catch(e){{return [];}}}}\
         try{{return Array.prototype.slice.call(document.querySelectorAll(sel)).filter(visible);}}\
         catch(e){{return [];}}}};var found=[];for(var i=0;i<selectors.length;i++){{\
         var items=matches(selectors[i]);for(var j=0;j<items.length;j++){{\
         if(found.indexOf(items[j])<0)found.push(items[j]);}}}}return found.length;}})({selectors})"
    )
}

fn credential_field_expression(selectors: &[&str]) -> String {
    let selectors = serde_json::to_string(selectors).unwrap_or_else(|_| "[]".to_string());
    format!(
        "(function(selectors){{var visible=function(el){{var r=el.getBoundingClientRect();\
         var s=getComputedStyle(el);return !!(r.width||r.height||el.getClientRects().length)\
         && s.visibility!=='hidden'&&s.display!=='none';}};\
         var matches=function(sel){{if(sel.indexOf('text=')===0)return [];\
         var marker=sel.indexOf(':has-text(\"');if(marker>=0)return [];\
         try{{return Array.prototype.slice.call(document.querySelectorAll(sel)).filter(visible);}}\
         catch(e){{return [];}}}};var found=[];for(var i=0;i<selectors.length;i++){{\
         var items=matches(selectors[i]);for(var j=0;j<items.length;j++){{\
         if(found.indexOf(items[j])<0)found.push(items[j]);}}}}if(!found.length)return 'missing';\
         if(found.length>1)return 'ambiguous';\
         var field=found[0];if((field.value||'').trim().length>0)return 'filled';\
         return field.matches(':-webkit-autofill')?'protected':'empty';}})({selectors})"
    )
}

fn credential_value_expression(selectors: &[&str]) -> String {
    let selectors = serde_json::to_string(selectors).unwrap_or_else(|_| "[]".to_string());
    format!(
        "(function(selectors){{var visible=function(el){{var r=el.getBoundingClientRect();\
         var s=getComputedStyle(el);return !!(r.width||r.height||el.getClientRects().length)\
         && s.visibility!=='hidden'&&s.display!=='none';}};\
         var matches=function(sel){{if(sel.indexOf('text=')===0)return [];\
         var marker=sel.indexOf(':has-text(\"');if(marker>=0)return [];\
         try{{return Array.prototype.slice.call(document.querySelectorAll(sel)).filter(visible);}}\
         catch(e){{return [];}}}};var found=[];for(var i=0;i<selectors.length;i++){{\
         var items=matches(selectors[i]);for(var j=0;j<items.length;j++){{\
         if(found.indexOf(items[j])<0)found.push(items[j]);}}}}if(found.length!==1)return null;\
         var value=(found[0].value||'').trim();return value.length?value:null;}})({selectors})"
    )
}

fn one_visible_expression(selectors: &[&str]) -> String {
    let selectors = serde_json::to_string(selectors).unwrap_or_else(|_| "[]".to_string());
    format!(
        "(function(selectors){{var visible=function(el){{var r=el.getBoundingClientRect();\
         var s=getComputedStyle(el);return !!(r.width||r.height||el.getClientRects().length)\
         && s.visibility!=='hidden'&&s.display!=='none';}};\
         var matches=function(sel){{if(sel.indexOf('text=')===0){{var text=sel.slice(5);\
         if(text[0]==='\"')text=text.slice(1,-1);\
         return Array.prototype.slice.call(document.querySelectorAll('*')).filter(function(el){{\
         return visible(el)&&(el.textContent||'').trim()===text;}});}}\
         var marker=sel.indexOf(':has-text(\"');if(marker>=0){{var base=sel.slice(0,marker);\
         var text=sel.slice(marker+11);if(text[text.length-1]===')')text=text.slice(0,-1);\
         if(text[text.length-1]==='\"')text=text.slice(0,-1);\
         try{{return Array.prototype.slice.call(document.querySelectorAll(base)).filter(function(el){{\
         return visible(el)&&(el.textContent||'').indexOf(text)>=0;}});}}catch(e){{return [];}}}}\
         try{{return Array.prototype.slice.call(document.querySelectorAll(sel)).filter(visible);}}\
         catch(e){{return [];}}}};var found=[];for(var i=0;i<selectors.length;i++){{\
         var items=matches(selectors[i]);for(var j=0;j<items.length;j++){{\
         if(found.indexOf(items[j])<0)found.push(items[j]);}}}}if(found.length!==1)return null;\
         var marker='um-login-'+Math.random().toString(36).slice(2);\
         found[0].setAttribute('data-um-login-target',marker);\
         return {{selector:'[data-um-login-target=\"'+marker+'\"]',index:0}};}})({selectors})"
    )
}

fn click_scroll_expression(selector: &str) -> String {
    format!(
        "(function(selector){{var el=document.querySelector(selector);\
         if(!el)return false;\
         el.scrollIntoView({{block:'center',inline:'center',behavior:'instant'}});\
         return true;}})({selector})"
    )
}

fn click_actionability_expression(selector: &str) -> String {
    format!(
        "(function(selector){{var el=document.querySelector(selector);\
         if(!el)return {{ok:false,reason:'missing'}};\
         var r=el.getBoundingClientRect();\
         var s=getComputedStyle(el);\
         if(!(r.width>0&&r.height>0)||s.visibility==='hidden'||s.display==='none')\
         return {{ok:false,reason:'not-visible'}};\
         if(el.disabled||el.getAttribute('aria-disabled')==='true')\
         return {{ok:false,reason:'disabled'}};\
         var vw=window.innerWidth||document.documentElement.clientWidth||0;\
         var vh=window.innerHeight||document.documentElement.clientHeight||0;\
         var left=Math.max(r.left,0),top=Math.max(r.top,0);\
         var right=Math.min(r.right,vw),bottom=Math.min(r.bottom,vh);\
         if(right<=left||bottom<=top)return {{ok:false,reason:'outside-viewport'}};\
         var x=(left+right)/2,y=(top+bottom)/2;\
         var hit=document.elementFromPoint(x,y);\
         if(!hit||!(hit===el||el.contains(hit)))\
         return {{ok:false,reason:'obscured'}};\
         return {{ok:true,x:x,y:y,rect:[r.left,r.top,r.width,r.height]}};\
         }})({selector})"
    )
}

fn click_arm_expression() -> String {
    "(function(){\
     if(window.__umLoginClickHandler){\
     document.removeEventListener('click',window.__umLoginClickHandler,true);}\
     window.__umLoginClickReceived=false;\
     window.__umLoginClickHandler=function(){window.__umLoginClickReceived=true;};\
     document.addEventListener('click',window.__umLoginClickHandler,true);\
     return location.href;})()"
        .to_string()
}

fn click_receipt_expression() -> String {
    "(function(){\
     if(window.__umLoginClickHandler){\
     document.removeEventListener('click',window.__umLoginClickHandler,true);\
     delete window.__umLoginClickHandler;}\
     var received=window.__umLoginClickReceived===true;\
     delete window.__umLoginClickReceived;\
     return {received:received,href:location.href};})()"
        .to_string()
}

fn retry_click(mut attempt: impl FnMut() -> Result<(), ClickFailure>) -> Result<(), String> {
    let mut last_error = String::new();
    for attempt_index in 0..CLICK_ATTEMPTS {
        match attempt() {
            Ok(()) => return Ok(()),
            Err(ClickFailure::Fatal(error)) => return Err(error),
            Err(error) => last_error = error.message(),
        }
        if attempt_index + 1 < CLICK_ATTEMPTS {
            std::thread::sleep(CLICK_BACKOFF);
        }
    }
    Err(last_error)
}

fn fill_prepare_expression(selectors: &[&str]) -> String {
    let selectors = serde_json::to_string(selectors).unwrap_or_else(|_| "[]".to_string());
    format!(
        r#"(function(selectors) {{
            const found = new Set();
            for (const selector of selectors) {{
                try {{
                    for (const el of document.querySelectorAll(selector)) {{
                        const style = getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        if (rect.width && rect.height && style.visibility === 'visible') {{
                            found.add(el);
                        }}
                    }}
                }} catch (_) {{}}
            }}
            if (found.size !== 1) return null;
            const el = found.values().next().value;
            if (!(el instanceof HTMLInputElement) || el.readOnly ||
                el.matches(':disabled') || el.closest('[aria-disabled="true"]')) return null;
            el.focus();
            if (!el.isConnected || document.activeElement !== el) return null;
            el.select();
            if (!el.isConnected || document.activeElement !== el) return null;
            const target = Math.random().toString(36).slice(2);
            el.setAttribute('data-um-login-fill-target', target);
            return {{target, protected: !el.value && el.matches(':-webkit-autofill')}};
        }})({selectors})"#
    )
}

fn fill_settle_expression(target: &str, value: Option<&str>) -> String {
    let target = json!(target);
    let value = json!(value);
    format!(
        r#"(function(target, expected) {{
            const marker = 'data-um-login-fill-target';
            const matches = Array.from(document.querySelectorAll('[' + marker + ']'))
                .filter(el => el.getAttribute(marker) === target);
            for (const el of matches) el.removeAttribute(marker);
            return expected !== null && matches.length === 1 && matches[0].value === expected;
        }})({target}, {value})"#
    )
}

fn kill_child_tree(child: &Arc<Mutex<Child>>) -> Result<(), String> {
    let mut child = child.lock().expect("browser child poisoned");
    if matches!(child.try_wait(), Ok(Some(_))) {
        return Ok(());
    }
    let pid = child.id();
    #[cfg(unix)]
    {
        unsafe {
            libc::kill(-(pid as libc::pid_t), libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        unsafe {
            libc::kill(-(pid as libc::pid_t), libc::SIGKILL);
        }
        let _ = child.wait();
    }
    #[cfg(windows)]
    {
        let _ = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        let _ = child.wait();
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = child.kill();
        let _ = child.wait();
    }
    Ok(())
}

/// A minimal CDP client over a loopback WebSocket.
struct CdpConnection {
    websocket: WebSocket,
    next_id: u64,
}

impl CdpConnection {
    fn connect(url: &str) -> Result<Self, String> {
        Ok(Self {
            websocket: WebSocket::connect(url)?,
            next_id: 1,
        })
    }

    fn send_command(&mut self, method: &str, params: Value) -> Result<u64, String> {
        let id = self.next_id;
        self.next_id += 1;
        let message = json!({"id": id, "method": method, "params": params});
        self.websocket.send_text(&message.to_string())?;
        Ok(id)
    }

    fn call(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.send_command(method, params)?;
        loop {
            let text = self.websocket.read_text()?;
            let value: Value =
                serde_json::from_str(&text).map_err(|error| format!("cdp json: {error}"))?;
            if value.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(error) = value.get("error") {
                    return Err(format!("cdp {method} failed: {error}"));
                }
                return Ok(value.get("result").cloned().unwrap_or(Value::Null));
            }
        }
    }

    fn evaluate(&mut self, expression: &str) -> Result<Value, String> {
        let result = self.call(
            "Runtime.evaluate",
            json!({
                "expression": expression,
                "returnByValue": true,
                "awaitPromise": false,
            }),
        )?;
        if let Some(exception) = result.get("exceptionDetails") {
            return Err(format!("javascript exception: {exception}"));
        }
        Ok(result
            .get("result")
            .and_then(|result| result.get("value"))
            .cloned()
            .unwrap_or(Value::Null))
    }
}

struct WebSocket {
    stream: TcpStream,
    buffered: Vec<u8>,
}

impl WebSocket {
    fn connect(url: &str) -> Result<Self, String> {
        let (host, port, path) = parse_websocket_url(url)?;
        let address = format!("{host}:{port}")
            .to_socket_addrs()
            .map_err(|error| format!("websocket address: {error}"))?
            .next()
            .ok_or_else(|| "websocket address missing".to_string())?;
        let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(5))
            .map_err(|error| format!("websocket connect: {error}"))?;
        let _ = stream.set_read_timeout(Some(CDP_READ_TIMEOUT));
        let _ = stream.set_write_timeout(Some(Duration::from_secs(5)));
        let key = websocket_key();
        let request = format!(
            "GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n\
             Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n\
             Sec-WebSocket-Version: 13\r\n\r\n"
        );
        stream
            .write_all(request.as_bytes())
            .map_err(|error| format!("websocket handshake write: {error}"))?;
        stream
            .flush()
            .map_err(|error| format!("websocket handshake flush: {error}"))?;
        let mut response = Vec::new();
        let mut buffer = [0u8; 1024];
        loop {
            if let Some(position) = response.windows(4).position(|window| window == b"\r\n\r\n") {
                let head = String::from_utf8_lossy(&response[..position]);
                if !head.starts_with("HTTP/1.1 101") {
                    return Err(format!(
                        "websocket handshake rejected: {}",
                        head.lines().next().unwrap_or("")
                    ));
                }
                let leftover = response[position + 4..].to_vec();
                return Ok(Self {
                    stream,
                    buffered: leftover,
                });
            }
            let read = stream
                .read(&mut buffer)
                .map_err(|error| format!("websocket handshake read: {error}"))?;
            if read == 0 {
                return Err("websocket handshake closed".to_string());
            }
            response.extend_from_slice(&buffer[..read]);
        }
    }

    fn send_text(&mut self, text: &str) -> Result<(), String> {
        self.send_frame(0x1, text.as_bytes())
    }

    fn send_frame(&mut self, opcode: u8, payload: &[u8]) -> Result<(), String> {
        let mut frame = Vec::new();
        frame.push(0x80 | opcode);
        let length = payload.len();
        if length < 126 {
            frame.push(0x80 | length as u8);
        } else if length <= u16::MAX as usize {
            frame.push(0x80 | 126);
            frame.extend_from_slice(&(length as u16).to_be_bytes());
        } else {
            frame.push(0x80 | 127);
            frame.extend_from_slice(&(length as u64).to_be_bytes());
        }
        let mask = websocket_mask();
        frame.extend_from_slice(&mask);
        for (index, byte) in payload.iter().enumerate() {
            frame.push(byte ^ mask[index % 4]);
        }
        self.stream
            .write_all(&frame)
            .map_err(|error| format!("websocket write: {error}"))?;
        self.stream
            .flush()
            .map_err(|error| format!("websocket flush: {error}"))
    }

    fn read_text(&mut self) -> Result<String, String> {
        let mut fragments = Vec::new();
        loop {
            let (fin, opcode, payload) = self.read_frame()?;
            match opcode {
                0x0 => {
                    fragments.extend_from_slice(&payload);
                    if fin {
                        return String::from_utf8(fragments)
                            .map_err(|error| format!("websocket text: {error}"));
                    }
                }
                0x1 => {
                    if fin {
                        return String::from_utf8(payload)
                            .map_err(|error| format!("websocket text: {error}"));
                    }
                    fragments = payload;
                }
                0x8 => return Err("websocket closed".to_string()),
                0x9 => {
                    self.send_frame(0xA, &payload)?;
                }
                0xA => {}
                _ => {}
            }
        }
    }

    fn read_frame(&mut self) -> Result<(bool, u8, Vec<u8>), String> {
        let first = self.read_exact(2)?;
        let fin = first[0] & 0x80 != 0;
        let opcode = first[0] & 0x0F;
        let masked = first[1] & 0x80 != 0;
        let mut length = (first[1] & 0x7F) as usize;
        if length == 126 {
            let extended = self.read_exact(2)?;
            length = u16::from_be_bytes([extended[0], extended[1]]) as usize;
        } else if length == 127 {
            let extended = self.read_exact(8)?;
            length = u64::from_be_bytes([
                extended[0],
                extended[1],
                extended[2],
                extended[3],
                extended[4],
                extended[5],
                extended[6],
                extended[7],
            ]) as usize;
        }
        if length > 16 * 1024 * 1024 {
            return Err("websocket frame too large".to_string());
        }
        let mask = if masked {
            Some(self.read_exact(4)?)
        } else {
            None
        };
        let mut payload = self.read_exact(length)?;
        if let Some(mask) = mask {
            for (index, byte) in payload.iter_mut().enumerate() {
                *byte ^= mask[index % 4];
            }
        }
        Ok((fin, opcode, payload))
    }

    fn read_exact(&mut self, length: usize) -> Result<Vec<u8>, String> {
        while self.buffered.len() < length {
            let mut buffer = [0u8; 4096];
            let read = self
                .stream
                .read(&mut buffer)
                .map_err(|error| format!("websocket read: {error}"))?;
            if read == 0 {
                return Err("websocket closed".to_string());
            }
            self.buffered.extend_from_slice(&buffer[..read]);
        }
        let rest = self.buffered.split_off(length);
        let bytes = std::mem::replace(&mut self.buffered, rest);
        Ok(bytes)
    }
}

fn parse_websocket_url(url: &str) -> Result<(String, u16, String), String> {
    let rest = url
        .strip_prefix("ws://")
        .ok_or_else(|| format!("unsupported websocket url: {url}"))?;
    let (authority, path) = match rest.split_once('/') {
        Some((authority, path)) => (authority, format!("/{path}")),
        None => (rest, "/".to_string()),
    };
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (
            host.to_string(),
            port.parse::<u16>()
                .map_err(|_| format!("invalid websocket port: {authority}"))?,
        ),
        None => (authority.to_string(), 80),
    };
    Ok((host, port, path))
}

fn websocket_key() -> String {
    let mut bytes = [0u8; 16];
    let seed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_nanos() as u64)
        .unwrap_or_default()
        ^ (std::process::id() as u64) << 32;
    let mut state = seed | 1;
    for byte in &mut bytes {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        *byte = state as u8;
    }
    base64_encode(&bytes)
}

fn websocket_mask() -> [u8; 4] {
    static COUNTER: AtomicU64 = AtomicU64::new(1);
    let mut state = COUNTER.fetch_add(0x9E37_79B9_7F4A_7C15, Ordering::SeqCst)
        ^ std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|value| value.as_nanos() as u64)
            .unwrap_or_default();
    let mut mask = [0u8; 4];
    for byte in &mut mask {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        *byte = state as u8;
    }
    mask
}

fn base64_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::new();
    for chunk in bytes.chunks(3) {
        let first = chunk[0] as u32;
        let second = chunk.get(1).copied().unwrap_or(0) as u32;
        let third = chunk.get(2).copied().unwrap_or(0) as u32;
        let triple = (first << 16) | (second << 8) | third;
        output.push(ALPHABET[((triple >> 18) & 0x3F) as usize] as char);
        output.push(ALPHABET[((triple >> 12) & 0x3F) as usize] as char);
        output.push(if chunk.len() > 1 {
            ALPHABET[((triple >> 6) & 0x3F) as usize] as char
        } else {
            '='
        });
        output.push(if chunk.len() > 2 {
            ALPHABET[(triple & 0x3F) as usize] as char
        } else {
            '='
        });
    }
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_websocket_url_is_split_into_host_port_and_path() {
        assert_eq!(
            parse_websocket_url("ws://127.0.0.1:9222/devtools/page/ABC").expect("url"),
            (
                "127.0.0.1".to_string(),
                9222,
                "/devtools/page/ABC".to_string()
            )
        );
        assert!(parse_websocket_url("wss://example.test/devtools").is_err());
    }

    #[test]
    fn the_websocket_key_is_base64_and_the_mask_is_four_bytes() {
        let key = websocket_key();
        assert_eq!(key.len(), 24);
        assert!(key.ends_with("=="));
        let mask = websocket_mask();
        assert_eq!(mask.len(), 4);
        assert_ne!(mask, [0, 0, 0, 0]);
    }

    #[test]
    fn the_selector_expressions_are_valid_javascript_strings() {
        let expression =
            one_visible_expression(&["button:has-text(\"Continue\")", "input[type=submit]"]);
        assert!(expression.contains("data-um-login-target"));
        let expression = selector_count_expression(&["input[name*=\"otp\" i]"]);
        assert!(expression.contains("querySelectorAll"));
        let expression = credential_field_expression(&["input[name=\"Password\"]"]);
        assert!(expression.contains("return 'filled'"));
        assert!(expression.contains(":-webkit-autofill"));
    }

    #[test]
    fn the_click_expressions_scroll_then_check_actionability() {
        let selector = "[data-um-login-target=\"abc\"]";
        let scroll = click_scroll_expression(selector);
        assert!(scroll.contains("scrollIntoView"));
        assert!(scroll.contains("behavior:'instant'"));
        let actionable = click_actionability_expression(selector);
        assert!(actionable.contains("getBoundingClientRect"));
        assert!(actionable.contains("elementFromPoint"));
        assert!(actionable.contains("getComputedStyle"));
        assert!(actionable.contains("r.width>0&&r.height>0"));
        assert!(actionable.contains("aria-disabled"));
        assert!(actionable.contains("not-visible"));
        assert!(actionable.contains("outside-viewport"));
        assert!(actionable.contains("obscured"));
        let arm = click_arm_expression();
        assert!(arm.contains("addEventListener('click'"));
        let receipt = click_receipt_expression();
        assert!(receipt.contains("__umLoginClickReceived"));
    }

    #[test]
    fn a_click_retries_three_times_and_returns_the_last_error() {
        let mut calls = 0usize;
        let result = retry_click(|| {
            calls += 1;
            Err(ClickFailure::Actionability(format!("attempt-{calls}")))
        });
        assert_eq!(calls, 3);
        assert_eq!(result, Err("attempt-3".to_string()));
    }

    #[test]
    fn a_click_stops_retrying_after_success() {
        let mut calls = 0usize;
        let result = retry_click(|| {
            calls += 1;
            if calls == 2 {
                Ok(())
            } else {
                Err(ClickFailure::NotReceived("not yet".to_string()))
            }
        });
        assert_eq!(calls, 2);
        assert_eq!(result, Ok(()));
    }

    #[test]
    fn a_fatal_click_error_is_not_retried() {
        let mut calls = 0usize;
        let result = retry_click(|| {
            calls += 1;
            Err(ClickFailure::Fatal("connection lost".to_string()))
        });
        assert_eq!(calls, 1);
        assert_eq!(result, Err("connection lost".to_string()));
    }
}
