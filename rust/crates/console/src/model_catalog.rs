//! #84: the local `model.catalog` sidecar.
//!
//! `model_sync` is not migrated to Rust yet. The local role executes
//! `model.catalog` by running `console.model_actions.model_catalog` in a
//! ONE-SHOT child process: the child prints exactly one JSON object and exits,
//! and the executor waits for it (bounded) before returning. A failed, timed-out
//! or malformed child is an explicit error, never a fabricated empty catalogue.
//! No long-lived Python process is started or reused.

use std::io::Read;
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;
use crate::operation_manager::{ActionExecutor, ActionFailure, OperationContext};

pub const DEFAULT_SIDECAR_TIMEOUT: Duration = Duration::from_secs(120);

/// The sidecar imports the retained Python handler in the repo root and prints
/// one envelope object. The handler's native exceptions are mapped to the same
/// `OperationError` codes the Python manager records; anything else is
/// `catalog_failed`. The child always exits, so stdout reaches EOF.
const SIDECAR_SCRIPT: &str = r#"
import json
import sys

payload = json.loads(sys.argv[1])
try:
    from console.model_actions import model_catalog
    result = model_catalog(payload.get("target", ""), payload.get("params") or {})
except Exception as exc:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        message = " ".join(str(exc).split())[:200]
    else:
        code = "catalog_failed"
        message = " ".join(("模型目录查询失败：%s: %s" % (type(exc).__name__, exc)).split())[:200]
    sys.stdout.write(json.dumps(
        {"ok": False, "error": {"code": code, "message": message}},
        ensure_ascii=False))
    sys.exit(3)
sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
"#;

/// Runs the Python `model.catalog` handler once per operation.
pub struct PythonCatalogExecutor {
    interpreter: PathBuf,
    working_dir: PathBuf,
    timeout: Duration,
}

impl PythonCatalogExecutor {
    pub fn new(interpreter: impl Into<PathBuf>, working_dir: impl Into<PathBuf>) -> Self {
        Self {
            interpreter: interpreter.into(),
            working_dir: working_dir.into(),
            timeout: DEFAULT_SIDECAR_TIMEOUT,
        }
    }

    pub fn from_config(config: &ConsoleConfig) -> Self {
        Self::new(config.python_exe.clone(), config.repo_root.clone())
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    fn command(&self, payload: &str) -> Command {
        let mut command = Command::new(&self.interpreter);
        command
            .arg("-c")
            .arg(SIDECAR_SCRIPT)
            .arg(payload)
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
        command
    }
}

impl ActionExecutor for PythonCatalogExecutor {
    fn execute(
        &self,
        action: &str,
        target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        if action != "model.catalog" {
            return Err(ActionFailure::new(
                "invalid_action",
                format!("unknown action {action:?}"),
            ));
        }
        ctx.raise_if_cancelled()?;
        let payload = serde_json::to_string(&json!({
            "target": target,
            "params": params,
        }))
        .map_err(|_| ActionFailure::new("internal_error", "模型目录 sidecar 参数无法序列化"))?;
        let mut child = self.command(&payload).spawn().map_err(|error| {
            ActionFailure::new(
                "catalog_failed",
                format!("模型目录 sidecar 启动失败：{error}"),
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
        let deadline = Instant::now() + self.timeout;
        let status = loop {
            if ctx.is_cancelled() {
                reap(&mut child);
                join_reader(out_reader);
                join_reader(err_reader);
                return Err(ActionFailure::cancelled());
            }
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
                        "catalog_timeout",
                        format!("模型目录 sidecar 超时（{} 秒）", self.timeout.as_secs()),
                    ));
                }
                Err(error) => {
                    reap(&mut child);
                    join_reader(out_reader);
                    join_reader(err_reader);
                    return Err(ActionFailure::new(
                        "catalog_failed",
                        format!("模型目录 sidecar 状态读取失败：{error}"),
                    ));
                }
            }
        };
        let stdout = join_reader(out_reader).unwrap_or_default();
        let stderr = join_reader(err_reader).unwrap_or_default();
        parse_sidecar_output(&status, &stdout, &stderr)
    }
}

fn parse_sidecar_output(
    status: &ExitStatus,
    stdout: &str,
    stderr: &str,
) -> Result<Value, ActionFailure> {
    let envelope = match serde_json::from_str::<Value>(stdout.trim()) {
        Ok(Value::Object(envelope)) => envelope,
        _ => {
            return Err(ActionFailure::new(
                "catalog_failed",
                sidecar_failure_message("模型目录 sidecar 未返回合法 JSON", status, stderr),
            ))
        }
    };
    if envelope.get("ok") == Some(&Value::Bool(true)) {
        if !status.success() {
            return Err(ActionFailure::new(
                "catalog_failed",
                format!(
                    "模型目录 sidecar 退出码非零（{}）但声明成功",
                    exit_code(status)
                ),
            ));
        }
        return match envelope.get("result") {
            Some(result @ Value::Object(_)) => Ok(result.clone()),
            _ => Err(ActionFailure::new(
                "catalog_failed",
                "模型目录 sidecar 结果不是 JSON 对象",
            )),
        };
    }
    let error = envelope.get("error").and_then(Value::as_object);
    let code = error
        .and_then(|error| error.get("code"))
        .and_then(Value::as_str)
        .unwrap_or("catalog_failed");
    let message = error
        .and_then(|error| error.get("message"))
        .and_then(Value::as_str)
        .unwrap_or("模型目录 sidecar 执行失败");
    Err(sidecar_failure(code, message))
}

fn sidecar_failure(code: &str, message: &str) -> ActionFailure {
    if code == "cancelled" {
        return ActionFailure::cancelled();
    }
    let code = if code == "invalid_params" {
        "invalid_params"
    } else {
        "catalog_failed"
    };
    ActionFailure::new(code, message.to_string())
}

fn sidecar_failure_message(prefix: &str, status: &ExitStatus, stderr: &str) -> String {
    let detail = stderr.split_whitespace().collect::<Vec<_>>().join(" ");
    let detail: String = detail.chars().take(200).collect();
    if detail.is_empty() {
        format!("{prefix}（退出码 {}）", exit_code(status))
    } else {
        format!("{prefix}（退出码 {}）：{detail}", exit_code(status))
    }
}

fn exit_code(status: &ExitStatus) -> String {
    status
        .code()
        .map(|code| code.to_string())
        .unwrap_or_else(|| "signal".to_string())
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
