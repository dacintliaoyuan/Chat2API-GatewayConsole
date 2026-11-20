//! Temporary Windows adapter. Only browser operations cross the private pipes.

use std::io::{BufRead, BufReader, Write};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{mpsc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::browser::{
    find_browser, BrowserUnavailable, CredentialField, LaunchRequest, LoginBrowser,
    LoginBrowserFactory, LoginFailure, LoginPage, VisibleTarget, DUO_CONFIRMATION_SELECTORS,
};
use super::profile::cleanup_profile_browsers;

const WORKER: &str = include_str!("playwright_worker.py");
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const RPC_TIMEOUT: Duration = Duration::from_secs(20);
const START_TIMEOUT: Duration = Duration::from_secs(30);
const CLOSE_TIMEOUT: Duration = Duration::from_secs(5);

pub(super) struct PlaywrightFactory(pub PathBuf);

impl LoginBrowserFactory for PlaywrightFactory {
    fn resolve_browser(&self) -> Result<PathBuf, BrowserUnavailable> {
        find_browser()
    }

    fn launch(&self, request: &LaunchRequest) -> Result<Box<dyn LoginBrowser>, LoginFailure> {
        let mut child = Command::new(&self.0)
            .args(["-I", "-X", "utf8", "-u", "-c", WORKER])
            .env_remove("CHAT2API_UM_ACCOUNT")
            .env_remove("CHAT2API_UM_PASSWORD")
            .env_remove("CHAT2API_UPSTREAM_TOKEN")
            .env_remove("CHAT2API_LOCAL_API_KEY")
            .env_remove("DEBUG")
            .env_remove("PWDEBUG")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .creation_flags(CREATE_NO_WINDOW)
            .spawn()
            .map_err(|_| {
                LoginFailure::unavailable("playwright_start_failed", "Python worker spawn failed")
            })?;
        let stdin = child.stdin.take().expect("private worker stdin");
        let stdout = child.stdout.take().expect("private worker stdout");
        let (sender, receiver) = mpsc::sync_channel(1);
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if sender.send(line).is_err() {
                    break;
                }
            }
        });
        let mut worker = Worker {
            child,
            stdin,
            receiver,
            profile: request.profile_dir.clone(),
            healthy: true,
            context_closed: false,
            next_marker: 0,
        };
        let started = worker.exchange(
            &json!({
                "executable": request.executable,
                "profile_dir": request.profile_dir,
                "headful": request.headful,
                "initial_url": request.initial_url,
            }),
            START_TIMEOUT,
        );
        if started != Ok(Value::Bool(true)) {
            return Err(LoginFailure::unavailable(
                "playwright_start_failed",
                "Python Playwright or Edge could not start",
            ));
        }
        Ok(Box::new(PlaywrightPage {
            worker: Mutex::new(worker),
        }))
    }

    fn cleanup_profile(&self, profile_dir: &Path) {
        cleanup_profile_browsers(profile_dir);
    }
}

struct Worker {
    child: Child,
    stdin: ChildStdin,
    receiver: mpsc::Receiver<String>,
    profile: PathBuf,
    healthy: bool,
    context_closed: bool,
    next_marker: u64,
}

impl Worker {
    fn exchange(&mut self, request: &Value, timeout: Duration) -> Result<Value, String> {
        if !self.healthy {
            return Err("playwright_worker_disconnected".into());
        }
        let transport = self.send_and_receive(request, timeout);
        let Some(reply) = transport else {
            // A late reply cannot be matched to the next command.
            self.healthy = false;
            return Err("playwright_worker_disconnected".into());
        };
        match reply.get("error").and_then(Value::as_str) {
            Some("TargetClosedError") => Err("playwright_page_closed".into()),
            Some(_) => Err("playwright_page_operation_failed".into()),
            None => Ok(reply.get("value").cloned().unwrap_or(Value::Null)),
        }
    }

    fn send_and_receive(&mut self, request: &Value, timeout: Duration) -> Option<Value> {
        serde_json::to_writer(&mut self.stdin, request).ok()?;
        self.stdin.write_all(b"\n").ok()?;
        let reply = self.receiver.recv_timeout(timeout).ok()?;
        serde_json::from_str(&reply).ok()
    }

    fn close(&mut self) -> Result<(), String> {
        // The window may be closed by the operator. Its worker can still
        // acknowledge a graceful context close.
        if !self.healthy {
            return Err("playwright_worker_disconnected".into());
        }
        let deadline = Instant::now() + CLOSE_TIMEOUT;
        if self.exchange(&json!({"op": "close"}), CLOSE_TIMEOUT)? != Value::Bool(true) {
            return Err("playwright_context_close_failed".into());
        }
        while Instant::now() < deadline {
            if let Ok(Some(status)) = self.child.try_wait() {
                self.context_closed = status.success();
                return self
                    .context_closed
                    .then_some(())
                    .ok_or_else(|| "playwright_context_close_failed".into());
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        Err("playwright_context_close_timeout".into())
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        if !matches!(self.child.try_wait(), Ok(Some(_))) {
            let _ = Command::new("taskkill.exe")
                .args(["/pid", &self.child.id().to_string(), "/t", "/f"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(CREATE_NO_WINDOW)
                .status();
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
        if !self.context_closed {
            cleanup_profile_browsers(&self.profile);
        }
    }
}

struct PlaywrightPage {
    worker: Mutex<Worker>,
}

impl PlaywrightPage {
    fn call(&self, op: &str, args: Value) -> Result<Value, String> {
        self.worker
            .lock()
            .expect("playwright worker poisoned")
            .exchange(&json!({"op": op, "args": args}), RPC_TIMEOUT)
    }
}

impl LoginBrowser for PlaywrightPage {
    fn page(&self) -> &dyn LoginPage {
        self
    }

    fn close(&mut self) -> Result<(), String> {
        self.worker
            .get_mut()
            .expect("playwright worker poisoned")
            .close()
    }
}

impl LoginPage for PlaywrightPage {
    fn url(&self) -> String {
        self.call("url", Value::Null)
            .ok()
            .and_then(|value| value.as_str().map(str::to_owned))
            .unwrap_or_default()
    }

    fn is_closed(&self) -> bool {
        self.call("is_closed", Value::Null) != Ok(Value::Bool(false))
    }

    fn read_token(&self) -> Option<String> {
        self.call("read_token", Value::Null)
            .ok()?
            .as_str()
            .filter(|token| !token.is_empty())
            .map(str::to_owned)
    }

    fn drop_token(&self) -> Result<(), String> {
        self.call("drop_token", Value::Null).map(|_| ())
    }

    fn catalog_access(&self, base_url: &str, candidate: &str, budget: Duration) -> bool {
        self.call("catalog_access", json!([base_url, candidate, budget.as_millis()])) == Ok(Value::Bool(true))
    }

    fn reload(&self) -> Result<(), String> {
        self.call("reload", Value::Null).map(|_| ())
    }

    fn wait(&self, duration: Duration) -> Result<(), String> {
        std::thread::sleep(duration);
        Ok(())
    }

    fn bring_to_front(&self) -> Result<(), String> {
        self.call("front", Value::Null).map(|_| ())
    }

    fn goto(&self, url: &str) -> Result<(), String> {
        self.call("goto", json!(url)).map(|_| ())
    }

    fn any_visible(&self, selectors: &[&str]) -> bool {
        self.call("visible", json!(selectors)) == Ok(Value::Bool(true))
    }

    fn any_visible_text(&self, selectors: &[&str]) -> bool {
        self.any_visible(selectors)
    }

    fn credential_field(&self, selectors: &[&str]) -> CredentialField {
        match self
            .call("field", json!(selectors))
            .as_ref()
            .ok()
            .and_then(Value::as_str)
        {
            Some("filled") => CredentialField::Filled,
            Some("protected") => CredentialField::ProtectedAutofill,
            _ => CredentialField::Missing,
        }
    }

    fn one_visible(&self, selectors: &[&str]) -> Option<VisibleTarget> {
        let mut worker = self.worker.lock().expect("playwright worker poisoned");
        worker.next_marker += 1;
        let marker = worker.next_marker;
        let found = worker.exchange(
            &json!({"op": "target", "args": {"selectors": selectors, "marker": marker.to_string()}}),
            RPC_TIMEOUT,
        );
        (found == Ok(Value::Bool(true))).then(|| VisibleTarget {
            selector: format!("[data-um-playwright-target=\"{marker}\"]"),
            index: 0,
        })
    }

    fn click(&self, _target: &VisibleTarget) -> Result<(), String> {
        self.call("click", Value::Null).map(|_| ())
    }

    fn fill(&self, selectors: &[&str], value: &str) -> Result<(), String> {
        self.call("fill", json!({"selectors": selectors, "value": value}))
            .map(|_| ())
    }

    fn duo_requires_confirmation(&self) -> bool {
        self.any_visible(&DUO_CONFIRMATION_SELECTORS)
    }
}
