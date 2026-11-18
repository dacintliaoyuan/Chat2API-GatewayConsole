//! Shared one-shot Python sidecar plumbing (#84, #87).
//!
//! Both the retained `model.catalog` executor and the local retained-action
//! executor run a Python handler in a ONE-SHOT child process: the child prints
//! exactly one JSON envelope and exits. A failed, timed-out, cancelled or
//! malformed child is an explicit error, never a fabricated result. No long-lived
//! Python process is started or reused.

use std::io::Read;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::operation_manager::{ActionFailure, OperationContext};

/// The per-executor failure vocabulary. `collapse_error_codes` maps every envelope
/// code except `invalid_params` onto `failure_code` (the #84 catalogue contract);
/// otherwise the handler's own code is preserved.
#[derive(Debug, Clone, Copy)]
pub(crate) struct SidecarLabels {
    pub failure_code: &'static str,
    pub timeout_code: &'static str,
    pub label: &'static str,
    pub default_message: &'static str,
    pub require_object_result: bool,
    pub collapse_error_codes: bool,
}

/// Runs `script` with one JSON `payload` argument and parses its one-object
/// stdout envelope. The child is killed on cancellation or timeout.
pub(crate) fn run_sidecar(
    interpreter: &Path,
    working_dir: &Path,
    script: &str,
    payload: &Value,
    timeout: Duration,
    ctx: &OperationContext,
    labels: SidecarLabels,
) -> Result<Value, ActionFailure> {
    ctx.raise_if_cancelled()?;
    let payload = serde_json::to_string(payload).map_err(|_| {
        ActionFailure::new("internal_error", format!("{} 参数无法序列化", labels.label))
    })?;
    let mut child = command(interpreter, working_dir, script, &payload)
        .spawn()
        .map_err(|error| {
            ActionFailure::new(
                labels.failure_code,
                format!("{} 启动失败：{error}", labels.label),
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
                    labels.timeout_code,
                    format!("{} 超时（{} 秒）", labels.label, timeout.as_secs()),
                ));
            }
            Err(error) => {
                reap(&mut child);
                join_reader(out_reader);
                join_reader(err_reader);
                return Err(ActionFailure::new(
                    labels.failure_code,
                    format!("{} 状态读取失败：{error}", labels.label),
                ));
            }
        }
    };
    let stdout = join_reader(out_reader).unwrap_or_default();
    let stderr = join_reader(err_reader).unwrap_or_default();
    parse_sidecar_output(&status, &stdout, &stderr, labels)
}

fn command(interpreter: &Path, working_dir: &Path, script: &str, payload: &str) -> Command {
    let mut command = Command::new(interpreter);
    command
        .arg("-c")
        .arg(script)
        .arg(payload)
        .current_dir(working_dir)
        // #95: Windows Python defaults stdio to the ANSI code page (cp936 on a
        // Chinese host) while this parent always decodes the envelope as UTF-8.
        // Pin the child's stdio encoding instead of inheriting it.
        .env("PYTHONIOENCODING", "utf-8")
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

fn parse_sidecar_output(
    status: &ExitStatus,
    stdout: &str,
    stderr: &str,
    labels: SidecarLabels,
) -> Result<Value, ActionFailure> {
    let envelope = match serde_json::from_str::<Value>(stdout.trim()) {
        Ok(Value::Object(envelope)) => envelope,
        _ => {
            return Err(ActionFailure::new(
                labels.failure_code,
                sidecar_failure_message(labels.label, status, stderr),
            ))
        }
    };
    if envelope.get("ok") == Some(&Value::Bool(true)) {
        if !status.success() {
            return Err(ActionFailure::new(
                labels.failure_code,
                format!(
                    "{} 退出码非零（{}）但声明成功",
                    labels.label,
                    exit_code(status)
                ),
            ));
        }
        return match envelope.get("result") {
            Some(result) if !labels.require_object_result || result.is_object() => {
                Ok(result.clone())
            }
            _ => Err(ActionFailure::new(
                labels.failure_code,
                format!("{} 结果不是 JSON 对象", labels.label),
            )),
        };
    }
    let error = envelope.get("error").and_then(Value::as_object);
    let code = error
        .and_then(|error| error.get("code"))
        .and_then(Value::as_str)
        .unwrap_or(labels.failure_code);
    let message = error
        .and_then(|error| error.get("message"))
        .and_then(Value::as_str)
        .unwrap_or(labels.default_message);
    Err(sidecar_failure(code, message, labels))
}

fn sidecar_failure(code: &str, message: &str, labels: SidecarLabels) -> ActionFailure {
    if code == "cancelled" {
        return ActionFailure::cancelled();
    }
    if labels.collapse_error_codes && code != "invalid_params" {
        return ActionFailure::new(labels.failure_code, message.to_string());
    }
    let code = if code.is_empty() {
        labels.failure_code
    } else {
        code
    };
    ActionFailure::new(code, message.to_string())
}

fn sidecar_failure_message(label: &str, status: &ExitStatus, stderr: &str) -> String {
    let detail = stderr.split_whitespace().collect::<Vec<_>>().join(" ");
    let detail: String = detail.chars().take(200).collect();
    if detail.is_empty() {
        format!("{label} 未返回合法 JSON（退出码 {}）", exit_code(status))
    } else {
        format!(
            "{label} 未返回合法 JSON（退出码 {}）：{detail}",
            exit_code(status)
        )
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
