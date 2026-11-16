//! The browser seam and the real Chromium-family driver.
//!
//! The login state machine only touches [`LoginPage`]. Tests inject a fake
//! factory and a fake page, so no test opens a real browser. The production
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
const LINUX_BROWSER_NAMES: [&str; 4] = [
    "chromium",
    "chromium-browser",
    "google-chrome",
    "microsoft-edge",
];
const WINDOWS_EDGE_ROOTS: [&str; 3] = ["PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"];

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

/// The narrow page facade the login state machine drives.
pub trait LoginPage: Send {
    fn url(&self) -> String;
    fn is_closed(&self) -> bool;
    fn read_token(&self) -> Option<String>;
    fn drop_token(&self) -> Result<(), String>;
    fn reload(&self) -> Result<(), String>;
    fn wait(&self, duration: Duration) -> Result<(), String>;
    fn bring_to_front(&self) -> Result<(), String>;
    fn goto(&self, url: &str) -> Result<(), String>;
    fn any_visible(&self, selectors: &[&str]) -> bool;
    fn field_filled(&self, selectors: &[&str]) -> bool;
    fn one_visible(&self, selectors: &[&str]) -> Option<VisibleTarget>;
    fn click(&self, target: &VisibleTarget) -> Result<(), String>;
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
    command.arg(&request.initial_url);
    command.stdout(Stdio::null()).stderr(Stdio::null());
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
        kill_child_tree(&self.child)
    }
}

impl Drop for RealBrowser {
    fn drop(&mut self) {
        let _ = kill_child_tree(&self.child);
    }
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

    fn field_filled(&self, selectors: &[&str]) -> bool {
        let expression = field_filled_expression(selectors);
        matches!(self.evaluate(&expression), Ok(Value::Bool(true)))
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
        let expression = format!(
            "(function(){{var el=document.querySelector({selector});\
             if(!el)return false;el.click();return true;}})()"
        );
        match self.evaluate(&expression) {
            Ok(Value::Bool(true)) => Ok(()),
            Ok(_) => Err("login target disappeared".to_string()),
            Err(error) => Err(error),
        }
    }
}

impl RealPage {
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
         if(found.indexOf(items[j])<0)found.push(items[j]);}}}}return found.length;}})({selectors})"
    )
}

fn field_filled_expression(selectors: &[&str]) -> String {
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
         if(found.indexOf(items[j])<0)found.push(items[j]);}}}}if(found.length!==1)return false;\
         var value=found[0].value||'';return value.trim().length>0;}})({selectors})"
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

    fn call(&mut self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id;
        self.next_id += 1;
        let message = json!({"id": id, "method": method, "params": params});
        self.websocket.send_text(&message.to_string())?;
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
        let expression = field_filled_expression(&["input[name=\"Password\"]"]);
        assert!(expression.contains("value.trim().length>0"));
    }
}
