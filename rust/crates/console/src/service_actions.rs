//! #92: local service actions executed by Rust against the managed Windows runtime.
//!
//! The service identity, the start/stop strategies and the cross-process service
//! lock stay in `invoke-console-runtime-json.ps1` + `lib/gateway-runtime.ps1`.
//! Rust invokes that PowerShell entry directly and parses its one-object JSON
//! envelope; no Python process is started. The operation worker already holds the
//! `service:<target>` resource lock, so the entry is called with
//! `-CallerHoldsLock` exactly like the Python adapter did.

use std::io::Read;
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;
use crate::operation_manager::{
    ActionExecutor, ActionFailure, OperationContext, LOCAL_SERVICE_DISABLED_REASON,
    LOCAL_SERVICE_TARGET,
};
use crate::retained_actions::RetainedActionExecutor;

pub const DEFAULT_POWERSHELL_EXE: &str = "pwsh";
pub const SERVICE_WRAPPER: &str = "invoke-console-runtime-json.ps1";
/// Python `_invoke_wrapper` defaults: 60 s for start/stop, 120 s for restart.
pub const START_STOP_TIMEOUT: Duration = Duration::from_secs(60);
pub const RESTART_TIMEOUT: Duration = Duration::from_secs(120);

/// The local role's action executor: service actions go to the managed Windows
/// runtime and the remaining model actions keep their retained in-process handlers.
pub struct LocalActionExecutor {
    service: PowerShellServiceExecutor,
    retained: RetainedActionExecutor,
}

impl LocalActionExecutor {
    pub fn new(service: PowerShellServiceExecutor, retained: RetainedActionExecutor) -> Self {
        Self { service, retained }
    }

    pub fn from_config(config: &ConsoleConfig) -> Self {
        Self::new(
            PowerShellServiceExecutor::from_config(config),
            RetainedActionExecutor::from_config(config),
        )
    }
}

impl ActionExecutor for LocalActionExecutor {
    fn execute(
        &self,
        action: &str,
        target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        match action {
            "service.start" | "service.stop" | "service.restart" => {
                self.service.execute(action, target, params, ctx)
            }
            _ => self.retained.execute(action, target, params, ctx),
        }
    }
}

/// Runs `service.start`/`service.stop`/`service.restart` through the
/// PowerShell runtime entry.
pub struct PowerShellServiceExecutor {
    powershell: PathBuf,
    wrapper: PathBuf,
    working_dir: PathBuf,
    start_stop_timeout: Duration,
    restart_timeout: Duration,
}

impl PowerShellServiceExecutor {
    pub fn new(
        powershell: impl Into<PathBuf>,
        wrapper: impl Into<PathBuf>,
        working_dir: impl Into<PathBuf>,
    ) -> Self {
        Self {
            powershell: powershell.into(),
            wrapper: wrapper.into(),
            working_dir: working_dir.into(),
            start_stop_timeout: START_STOP_TIMEOUT,
            restart_timeout: RESTART_TIMEOUT,
        }
    }

    pub fn from_config(config: &ConsoleConfig) -> Self {
        let working_dir = config.repo_root.clone();
        Self::new(
            DEFAULT_POWERSHELL_EXE,
            working_dir.join(SERVICE_WRAPPER),
            working_dir,
        )
    }

    pub fn with_timeouts(mut self, start_stop: Duration, restart: Duration) -> Self {
        self.start_stop_timeout = start_stop;
        self.restart_timeout = restart;
        self
    }
}

impl ActionExecutor for PowerShellServiceExecutor {
    fn execute(
        &self,
        action: &str,
        target: &str,
        _params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        let verb = match action {
            "service.start" => "start",
            "service.stop" => "stop",
            "service.restart" => "restart",
            _ => {
                return Err(ActionFailure::new(
                    "invalid_action",
                    format!("unknown action {action:?}"),
                ))
            }
        };
        if target != LOCAL_SERVICE_TARGET {
            return Err(ActionFailure::new(
                "action_disabled",
                format!("action {action} is not available: {LOCAL_SERVICE_DISABLED_REASON}"),
            ));
        }
        // Python's adapter checks cancellation before the single subprocess call
        // only: a start/stop that already launched is allowed to finish instead
        // of leaving a half-executed service transition.
        ctx.raise_if_cancelled()?;
        let timeout = if action == "service.restart" {
            self.restart_timeout
        } else {
            self.start_stop_timeout
        };
        self.run(verb, target, timeout)
    }
}

impl PowerShellServiceExecutor {
    fn run(&self, verb: &str, target: &str, timeout: Duration) -> Result<Value, ActionFailure> {
        let mut command = Command::new(&self.powershell);
        command
            .arg("-NoProfile")
            .arg("-ExecutionPolicy")
            .arg("Bypass")
            .arg("-File")
            .arg(&self.wrapper)
            .arg("-Action")
            .arg(verb)
            .arg("-Key")
            .arg(target)
            .arg("-CallerHoldsLock")
            .current_dir(&self.working_dir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // Keep the one-shot child console-less on the Windows local host.
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = command.spawn().map_err(|error| {
            ActionFailure::new(
                "runtime_unavailable",
                format!("cannot run console runtime wrapper: {error}"),
            )
        })?;
        let out_reader = child
            .stdout
            .take()
            .map(|stream| thread::spawn(move || read_all(stream)));
        let err_reader = child
            .stderr
            .take()
            .map(|stream| thread::spawn(move || read_all(stream)));
        let deadline = Instant::now() + timeout;
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break status,
                Ok(None) if Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(10));
                }
                Ok(None) => {
                    reap(&mut child);
                    join_reader(out_reader);
                    join_reader(err_reader);
                    return Err(ActionFailure::new(
                        "runtime_unavailable",
                        format!(
                            "console runtime wrapper timed out after {:.0}s",
                            timeout.as_secs_f64()
                        ),
                    ));
                }
                Err(error) => {
                    reap(&mut child);
                    join_reader(out_reader);
                    join_reader(err_reader);
                    return Err(ActionFailure::new(
                        "runtime_unavailable",
                        format!("cannot run console runtime wrapper: {error}"),
                    ));
                }
            }
        };
        let stdout = join_reader(out_reader).unwrap_or_default();
        let stderr = join_reader(err_reader).unwrap_or_default();
        parse_envelope(&status, &stdout, &stderr, target)
    }
}

fn parse_envelope(
    status: &ExitStatus,
    stdout: &str,
    stderr: &str,
    target: &str,
) -> Result<Value, ActionFailure> {
    let envelope = match serde_json::from_str::<Value>(stdout.trim()) {
        Ok(Value::Object(envelope)) => envelope,
        _ => {
            return Err(ActionFailure::new(
                "runtime_unavailable",
                format!(
                    "console runtime wrapper returned non-JSON output (rc={})",
                    exit_code(status)
                ),
            ))
        }
    };
    if envelope.get("ok") == Some(&Value::Bool(true)) {
        return Ok(envelope
            .get("result")
            .filter(|value| value.is_object())
            .cloned()
            .unwrap_or_else(|| json!({"key": target})));
    }
    let detail = envelope
        .get("detail")
        .map(value_text)
        .unwrap_or_default();
    let detail = if detail.is_empty() {
        envelope
            .get("error")
            .map(value_text)
            .unwrap_or_default()
    } else {
        detail
    };
    let message = if detail.is_empty() {
        sanitize_detail(stderr)
    } else {
        sanitize_detail(&detail)
    };
    let code = if status.success() {
        "runtime_error"
    } else {
        "runtime_unavailable"
    };
    Err(ActionFailure::new(code, message))
}

fn value_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// Python `_sanitize_detail`: one line, bounded to 300 chars.
fn sanitize_detail(raw: &str) -> String {
    raw.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(300)
        .collect()
}

fn exit_code(status: &ExitStatus) -> i32 {
    status.code().unwrap_or(-1)
}

fn read_all(mut stream: impl Read) -> String {
    let mut bytes = Vec::new();
    let _ = stream.read_to_end(&mut bytes);
    String::from_utf8_lossy(&bytes).into_owned()
}

fn reap(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn join_reader(reader: Option<thread::JoinHandle<String>>) -> Option<String> {
    reader.and_then(|reader| reader.join().ok())
}


impl crate::local_um_auth::ServiceControl for PowerShellServiceExecutor {
    fn start(&self, ctx: &OperationContext) -> Result<(), ActionFailure> {
        self.execute("service.start", LOCAL_SERVICE_TARGET, &Map::new(), ctx)
            .map(|_| ())
    }

    fn stop(&self, ctx: &OperationContext) -> Result<(), ActionFailure> {
        self.execute("service.stop", LOCAL_SERVICE_TARGET, &Map::new(), ctx)
            .map(|_| ())
    }
}
