//! #93: the local 8001 UM authentication actions executed in Rust.
//!
//! `account.um_status` and `account.um_verify` read the local gateway's
//! `/admin/auth/*` control face directly, and `account.login` orchestrates the
//! gateway's shared re-authentication round without a Python sidecar. The status
//! read never takes the login's exclusive resource lock; the active verify and
//! login actions stay mutually exclusive through their operation resource.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;
use crate::operation_manager::{ActionExecutor, ActionFailure, OperationContext};
use crate::retained_actions::RetainedActionExecutor;
use crate::runtime::RuntimeFacts;

pub const STATUS_TIMEOUT: Duration = Duration::from_secs(10);
pub const VERIFY_TIMEOUT: Duration = Duration::from_secs(25);
pub const LOGIN_TIMEOUT: Duration = Duration::from_secs(10);
pub const CANCEL_TIMEOUT: Duration = Duration::from_secs(10);
pub const STATUS_POLL_TIMEOUT: Duration = Duration::from_secs(5);

const STATE_NOTE: &str = "内存态（未主动核验）：仅读取 chat2api 自身认证状态，未向上游发起核验请求";
const VERIFY_NOTE: &str =
    "受限核验：经 chat2api 受限端点单次探测上游目录，未调用 refresh、未改认证状态；rejected 表示凭据/访问被拒，不武断等同过期";

const KNOWN_STATES: [&str; 5] = ["valid", "needs_login", "logging_in", "failed", "unknown"];
const VERIFY_STATES: [&str; 4] = ["valid", "rejected", "needs_login", "unknown"];
const RECOVERY_STATES: [&str; 5] = ["idle", "waiting", "running", "exhausted", "stopped"];

const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug)]
struct AdminResponse {
    status: u16,
    body: Vec<u8>,
}

#[derive(Debug, Clone, Copy)]
enum AdminError {
    Unreachable,
    Protocol,
}

/// The local gateway's control-token HTTP face.
struct AdminClient {
    port: u16,
    control_token_file: Option<PathBuf>,
}

impl AdminClient {
    fn new(port: u16, control_token_file: Option<&Path>) -> Self {
        Self {
            port,
            control_token_file: control_token_file.map(Path::to_path_buf),
        }
    }

    fn control_token(&self) -> Result<String, ActionFailure> {
        read_control_token(self.control_token_file.as_deref())
    }

    fn get(&self, path: &str, token: &str, timeout: Duration) -> Result<AdminResponse, AdminError> {
        self.call("GET", path, Some(token), None, timeout)
    }

    fn post_json(
        &self,
        path: &str,
        token: &str,
        payload: &Value,
        timeout: Duration,
    ) -> Result<AdminResponse, AdminError> {
        self.call("POST", path, Some(token), Some(payload), timeout)
    }

    fn post(
        &self,
        path: &str,
        token: &str,
        timeout: Duration,
    ) -> Result<AdminResponse, AdminError> {
        self.call("POST", path, Some(token), None, timeout)
    }

    fn call(
        &self,
        method: &str,
        path: &str,
        token: Option<&str>,
        body: Option<&Value>,
        timeout: Duration,
    ) -> Result<AdminResponse, AdminError> {
        let address = SocketAddr::from(([127, 0, 0, 1], self.port));
        let mut stream =
            TcpStream::connect_timeout(&address, timeout).map_err(|_| AdminError::Unreachable)?;
        let _ = stream.set_read_timeout(Some(timeout));
        let _ = stream.set_write_timeout(Some(timeout));
        let body = body
            .map(|value| serde_json::to_vec(value).unwrap_or_default())
            .unwrap_or_default();
        let mut head = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n",
            self.port
        );
        if let Some(token) = token {
            head.push_str("X-Admin-Control: ");
            head.push_str(token);
            head.push_str("\r\n");
        }
        if !body.is_empty() {
            head.push_str("Content-Type: application/json\r\n");
        }
        head.push_str(&format!("Content-Length: {}\r\n\r\n", body.len()));
        stream
            .write_all(head.as_bytes())
            .map_err(|_| AdminError::Unreachable)?;
        stream
            .write_all(&body)
            .map_err(|_| AdminError::Unreachable)?;
        stream.flush().map_err(|_| AdminError::Unreachable)?;
        read_admin_response(&mut stream)
    }

    /// One `/admin/auth/status` read with an already-read control token.
    fn read_status(&self, token: &str, timeout: Duration) -> Result<Value, ActionFailure> {
        let response = self
            .get("/admin/auth/status", token, timeout)
            .map_err(status_transport_error)?;
        match response.status {
            200 => {}
            401 | 403 => {
                return Err(ActionFailure::new(
                    "control_denied",
                    "管理控制头被拒绝（控制令牌不匹配）",
                ))
            }
            status => {
                return Err(ActionFailure::new(
                    "status_query_failed",
                    format!("查询失败：chat2api 返回 HTTP {status}，凭据状态未知（未知 ≠ 失效）"),
                ))
            }
        }
        let payload: Value =
            serde_json::from_slice(&response.body).map_err(|_| status_schema_error())?;
        let object = payload.as_object().ok_or_else(status_schema_error)?;
        let state = object
            .get("state")
            .and_then(Value::as_str)
            .ok_or_else(status_schema_error)?;
        Ok(json!({
            "state": state,
            "label": text_field(object.get("label")),
            "error": text_field(object.get("error")),
            "recovery": recovery_summary(object.get("recovery")),
            "login_window_deadline_epoch": number_field(object.get("login_window_deadline_epoch")),
            "login_window_s": number_field(object.get("login_window_s")),
            "primary": text_field(object.get("primary")),
            "login": login_summary(object.get("login")),
        }))
    }
}

/// D18-8: end THIS login round through the gateway's control-token-gated
/// `POST /admin/auth/cancel`.
///
/// The call reuses the same `AdminClient` (control-token read, transport and
/// error mapping) as `account.um_status`; it does not stop or restart the
/// gateway. The gateway answers `{cancelled, state, detail}` with `state` either
/// `cancelled` or `no_login_in_progress`. A 200/202 answer is relayed as-is
/// (normalized to the three fields), while a missing token, an unreachable
/// gateway, a non-2xx answer or a payload outside that contract is a classified
/// `ActionFailure`; none of them fabricates a successful cancel.
pub fn cancel_login(
    gateway_port: u16,
    control_token_file: Option<&Path>,
) -> Result<Value, ActionFailure> {
    let client = AdminClient::new(gateway_port, control_token_file);
    let token = client.control_token()?;
    let response = client
        .post("/admin/auth/cancel", &token, CANCEL_TIMEOUT)
        .map_err(cancel_transport_error)?;
    match response.status {
        200 | 202 => {}
        401 | 403 => {
            return Err(ActionFailure::new(
                "control_denied",
                "管理控制头被拒绝（控制令牌不匹配）",
            ))
        }
        status => {
            return Err(ActionFailure::new(
                "login_cancel_failed",
                format!("登录取消被拒：chat2api 返回 HTTP {status}；服务未停止，登录状态未知"),
            ))
        }
    }
    let payload: Value =
        serde_json::from_slice(&response.body).map_err(|_| cancel_schema_error())?;
    let object = payload.as_object().ok_or_else(cancel_schema_error)?;
    let cancelled = object
        .get("cancelled")
        .and_then(Value::as_bool)
        .ok_or_else(cancel_schema_error)?;
    let state = object
        .get("state")
        .and_then(Value::as_str)
        .filter(|state| !state.is_empty())
        .ok_or_else(cancel_schema_error)?;
    Ok(json!({
        "cancelled": cancelled,
        "state": state,
        "detail": text_field(object.get("detail")),
    }))
}

fn cancel_transport_error(error: AdminError) -> ActionFailure {
    match error {
        AdminError::Unreachable => ActionFailure::new(
            "login_cancel_unreachable",
            "登录取消失败：chat2api 服务不可达；服务未停止，登录状态未知",
        ),
        AdminError::Protocol => cancel_schema_error(),
    }
}

fn cancel_schema_error() -> ActionFailure {
    ActionFailure::new(
        "login_cancel_schema",
        "登录取消响应不符合契约；请刷新登录状态核对",
    )
}

fn read_admin_response(stream: &mut TcpStream) -> Result<AdminResponse, AdminError> {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 8192];
    let header_end = loop {
        let read = stream
            .read(&mut chunk)
            .map_err(|_| AdminError::Unreachable)?;
        if read == 0 {
            // A closed connection without any response bytes is a transport
            // failure, not a malformed answer.
            return Err(AdminError::Unreachable);
        }
        buffer.extend_from_slice(&chunk[..read]);
        if let Some(index) = find_bytes(&buffer, b"\r\n\r\n") {
            break index;
        }
        if buffer.len() > MAX_RESPONSE_BYTES {
            return Err(AdminError::Protocol);
        }
    };
    let head = String::from_utf8_lossy(&buffer[..header_end]).into_owned();
    let mut lines = head.split("\r\n");
    let status_line = lines.next().ok_or(AdminError::Protocol)?;
    let mut parts = status_line.split_whitespace();
    let _version = parts.next().ok_or(AdminError::Protocol)?;
    let status = parts
        .next()
        .ok_or(AdminError::Protocol)?
        .parse::<u16>()
        .map_err(|_| AdminError::Protocol)?;
    let mut content_length = None;
    let mut chunked = false;
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            return Err(AdminError::Protocol);
        };
        if name.eq_ignore_ascii_case("content-length") {
            content_length = Some(
                value
                    .trim()
                    .parse::<usize>()
                    .map_err(|_| AdminError::Protocol)?,
            );
        }
        if name.eq_ignore_ascii_case("transfer-encoding")
            && value.to_ascii_lowercase().contains("chunked")
        {
            chunked = true;
        }
    }
    let mut body = buffer[header_end + 4..].to_vec();
    if let Some(length) = content_length {
        while body.len() < length {
            let read = stream
                .read(&mut chunk)
                .map_err(|_| AdminError::Unreachable)?;
            if read == 0 {
                return Err(AdminError::Protocol);
            }
            body.extend_from_slice(&chunk[..read]);
        }
        body.truncate(length);
    } else if chunked {
        body = read_chunked_body(stream, body)?;
    } else {
        loop {
            let read = stream
                .read(&mut chunk)
                .map_err(|_| AdminError::Unreachable)?;
            if read == 0 {
                break;
            }
            body.extend_from_slice(&chunk[..read]);
            if body.len() > MAX_RESPONSE_BYTES {
                return Err(AdminError::Protocol);
            }
        }
    }
    Ok(AdminResponse { status, body })
}

fn read_chunked_body(stream: &mut TcpStream, mut raw: Vec<u8>) -> Result<Vec<u8>, AdminError> {
    let mut body = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let line_end = loop {
            if let Some(index) = find_bytes(&raw, b"\r\n") {
                break index;
            }
            let read = stream
                .read(&mut chunk)
                .map_err(|_| AdminError::Unreachable)?;
            if read == 0 {
                return Err(AdminError::Protocol);
            }
            raw.extend_from_slice(&chunk[..read]);
            if raw.len() > MAX_RESPONSE_BYTES {
                return Err(AdminError::Protocol);
            }
        };
        let line = String::from_utf8_lossy(&raw[..line_end]).into_owned();
        raw.drain(..line_end + 2);
        let size_text = line.split(';').next().unwrap_or_default().trim();
        let size = usize::from_str_radix(size_text, 16).map_err(|_| AdminError::Protocol)?;
        if size == 0 {
            return Ok(body);
        }
        while raw.len() < size + 2 {
            let read = stream
                .read(&mut chunk)
                .map_err(|_| AdminError::Unreachable)?;
            if read == 0 {
                return Err(AdminError::Protocol);
            }
            raw.extend_from_slice(&chunk[..read]);
        }
        body.extend_from_slice(&raw[..size]);
        raw.drain(..size + 2);
        if body.len() > MAX_RESPONSE_BYTES {
            return Err(AdminError::Protocol);
        }
    }
}

fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn read_control_token(path: Option<&Path>) -> Result<String, ActionFailure> {
    let Some(path) = path else {
        return Err(ActionFailure::new(
            "control_token_absent",
            "管理控制令牌文件缺席，无法查询 UM 认证状态",
        ));
    };
    let raw = match std::fs::read(path) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err(ActionFailure::new(
                "control_token_absent",
                "管理控制令牌文件缺席，无法查询 UM 认证状态",
            ));
        }
        Err(error) => {
            return Err(ActionFailure::new(
                "control_token_unreadable",
                format!(
                    "管理控制令牌文件不可读（{:?}）；状态未知，UM 认证状态查询拒绝执行",
                    error.kind()
                ),
            ));
        }
    };
    let text = match String::from_utf8(raw) {
        Ok(text) => text,
        Err(_) => {
            return Err(ActionFailure::new(
                "control_token_unreadable",
                "管理控制令牌文件不可读（Utf8Error）；状态未知，UM 认证状态查询拒绝执行",
            ));
        }
    };
    let value = text.trim();
    if value.is_empty() {
        return Err(ActionFailure::new(
            "control_token_empty",
            "管理控制令牌文件为空，无法查询 UM 认证状态",
        ));
    }
    if value.contains(['\r', '\n']) {
        return Err(ActionFailure::new(
            "control_token_unreadable",
            "管理控制令牌文件不可读（含换行）；状态未知，UM 认证状态查询拒绝执行",
        ));
    }
    Ok(value.to_string())
}

/// One read-only `/admin/auth/status` observation for the display-state face.
///
/// This uses the same control-token read and status parser as the UM auth actions
/// (`account.um_status`), so the console does not grow a second gateway client.
/// The read never changes the gateway auth state. A failed read keeps `state=None`
/// with a classified `read_error`; the display-state mapper then fails closed
/// instead of inventing a login state or a deadline.
pub fn auth_observation(gateway_port: u16, control_token_file: Option<&Path>) -> Value {
    let client = AdminClient::new(gateway_port, control_token_file);
    match client
        .control_token()
        .and_then(|token| client.read_status(&token, STATUS_TIMEOUT))
    {
        Ok(status) => json!({
            "state": status.get("state").cloned().unwrap_or(Value::Null),
            "label": status.get("label").cloned().unwrap_or(Value::Null),
            "error": status.get("error").cloned().unwrap_or(Value::Null),
            "primary": status.get("primary").cloned().unwrap_or(Value::Null),
            "observed_at": epoch_seconds(),
            "login_window_deadline_epoch": status
                .get("login_window_deadline_epoch")
                .cloned()
                .unwrap_or(Value::Null),
            "read_error": Value::Null,
        }),
        Err(failure) => json!({
            "state": Value::Null,
            "label": Value::Null,
            "error": Value::Null,
            "primary": Value::Null,
            "observed_at": Value::Null,
            "login_window_deadline_epoch": Value::Null,
            "read_error": {
                "code": failure.code,
                "message": failure.message,
            },
        }),
    }
}

fn text_field(value: Option<&Value>) -> Option<String> {
    value.and_then(Value::as_str).map(str::to_string)
}

fn number_field(value: Option<&Value>) -> Option<f64> {
    let value = value?;
    if value.is_boolean() {
        return None;
    }
    let number = value.as_f64()?;
    number.is_finite().then_some(number)
}

fn int_field(value: Option<&Value>) -> Option<i64> {
    let value = value?;
    if value.is_boolean() {
        return None;
    }
    value.as_i64()
}

fn recovery_summary(value: Option<&Value>) -> Option<Value> {
    let object = value?.as_object()?;
    let state = object.get("state")?.as_str()?;
    if !RECOVERY_STATES.contains(&state) {
        return None;
    }
    let round = int_field(object.get("round"))?;
    let session_starts = int_field(object.get("session_starts"))?;
    let max_sessions = int_field(object.get("max_sessions"))?;
    Some(json!({
        "state": state,
        "round": round,
        "session_starts": session_starts,
        "max_sessions": max_sessions,
        "next_attempt_epoch": number_field(object.get("next_attempt_epoch")),
        "pause_reason": text_field(object.get("pause_reason")),
        "primary": text_field(object.get("primary")),
    }))
}

fn login_summary(value: Option<&Value>) -> Option<Value> {
    let object = value?.as_object()?;
    let phase = text_field(object.get("phase"));
    let origin = text_field(object.get("origin"));
    let manual_reason = text_field(object.get("manual_reason"));
    if phase.is_none() && origin.is_none() && manual_reason.is_none() {
        return None;
    }
    Some(json!({
        "phase": phase,
        "origin": origin,
        "manual_reason": manual_reason,
    }))
}

fn status_schema_error() -> ActionFailure {
    ActionFailure::new("status_schema", "auth status 响应形状与预期不符")
}

fn verify_schema_error() -> ActionFailure {
    ActionFailure::new("verify_schema", "受限核验响应形状与预期不符")
}

fn epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

/// The managed Windows runtime's service start/stop seam.
///
/// `PowerShellServiceExecutor` implements this seam, so the login orchestration
/// starts/stops the local gateway through the same managed runtime as `service.*`.
/// A login against an already running gateway never touches this seam.
pub trait ServiceControl: Send + Sync {
    fn start(&self, ctx: &OperationContext) -> Result<(), ActionFailure>;
    fn stop(&self, ctx: &OperationContext) -> Result<(), ActionFailure>;
}

/// The local UM auth action executor.
pub struct UmAuthExecutor {
    client: AdminClient,
    runtime: Arc<dyn RuntimeFacts>,
    service: Arc<dyn ServiceControl>,
}

impl UmAuthExecutor {
    pub fn from_config(
        config: &ConsoleConfig,
        runtime: Arc<dyn RuntimeFacts>,
        service: Arc<dyn ServiceControl>,
    ) -> Self {
        Self::new(config, runtime, service)
    }

    pub fn new(
        config: &ConsoleConfig,
        runtime: Arc<dyn RuntimeFacts>,
        service: Arc<dyn ServiceControl>,
    ) -> Self {
        Self {
            client: AdminClient::new(config.gateway_port, config.control_token_file.as_deref()),
            runtime,
            service,
        }
    }

    pub fn handles(action: &str) -> bool {
        matches!(
            action,
            "account.um_status" | "account.um_verify" | "account.login"
        )
    }

    fn status(&self) -> Result<Value, ActionFailure> {
        let token = self.client.control_token()?;
        let status = self.read_status(&token, STATUS_TIMEOUT)?;
        let mut result = status.as_object().cloned().unwrap_or_default();
        let state = result
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        result.insert("checked_epoch".to_string(), json!(epoch_seconds()));
        result.insert("source".to_string(), json!("memory-state"));
        result.insert("verified".to_string(), json!(false));
        result.insert(
            "state_known".to_string(),
            json!(KNOWN_STATES.contains(&state.as_str())),
        );
        result.insert("note".to_string(), json!(STATE_NOTE));
        Ok(Value::Object(result))
    }

    /// One `/admin/auth/status` read with an already-read control token.
    fn read_status(&self, token: &str, timeout: Duration) -> Result<Value, ActionFailure> {
        self.client.read_status(token, timeout)
    }

    fn login(
        &self,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.raise_if_cancelled()?;
        let force = bool_param(params, "force")?;
        let visible = bool_param(params, "visible")?;
        if visible && !force {
            return Err(ActionFailure::new(
                "invalid_params",
                "visible=true 只能与 force=true 一起使用；可见窗口只用于显式强制重新登录",
            ));
        }
        if force {
            let _ = ctx.report_step(&format!(
                "开始 UM 登录编排：显式强制重新登录（{}）",
                if visible { "可见窗口" } else { "headless" }
            ));
        } else {
            let _ = ctx.report_step("开始 UM 登录编排：先核验当前认证，仅在需要时重新登录");
        }

        let token = self.client.control_token()?;
        let _ = ctx.report_step("已读取管理控制令牌");

        let start_wait_s = budget_value(params, "start_wait_s", 30.0);
        let login_budget_s = budget_value(params, "login_budget_s", 190.0);
        let interval_s = budget_value(params, "poll_interval_s", 1.0);

        let snapshot = self.runtime.snapshot().map_err(|_| {
            ActionFailure::new(
                "status_unavailable",
                "无法读取服务快照，无法判定 chat2api 初始状态",
            )
        })?;
        let was_running = snapshot.display_state() == "Running";
        let _ = ctx.report_step(&format!(
            "chat2api 初始状态：{}",
            if was_running { "Running" } else { "未运行" }
        ));

        let mut started_by_us = false;
        if !was_running {
            self.start_and_confirm(ctx, start_wait_s, interval_s)?;
            started_by_us = true;
            let _ = ctx.report_step("chat2api 已启动（快照确认 Running）");
        }

        let outcome = self.login_round(&token, force, visible, login_budget_s, interval_s, ctx);
        let (kind, fact) = self.restore_service(ctx, started_by_us);
        let _ = ctx.report_step(&fact);
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(failure) => return Err(reraise_after_restore(failure, kind, &fact)),
        };
        match outcome {
            LoginOutcome::Valid {
                skipped,
                waited,
                at_recheck,
            } => {
                if kind == RestoreKind::Failed {
                    let outcome_text = if skipped {
                        "当前账号有效，无需重新登录"
                    } else {
                        "登录已完成（state=valid）"
                    };
                    return Err(ActionFailure::new(
                        "stop_failed",
                        format!("{outcome_text}，但{fact}"),
                    ));
                }
                Ok(json!({
                    "was_running": was_running,
                    "restored": kind == RestoreKind::Restored,
                    "final_state": "valid",
                    "login_skipped": skipped,
                    "login_waited_s": waited,
                    "note": success_note(waited, at_recheck, &fact, skipped),
                }))
            }
            LoginOutcome::NotSettled {
                state,
                waited,
                read_error,
            } => Err(ActionFailure::new(
                "login_timeout_not_settled",
                timeout_message(&state, waited, &fact, read_error.as_ref()),
            )),
        }
    }

    fn login_round(
        &self,
        token: &str,
        force: bool,
        visible: bool,
        login_budget_s: f64,
        interval_s: f64,
        ctx: &OperationContext,
    ) -> Result<LoginOutcome, ActionFailure> {
        if force {
            let _ = ctx.report_step(&format!(
                "显式强制重新登录：跳过按需核验，直接发送登录请求（{}）",
                if visible { "可见窗口" } else { "headless" }
            ));
            self.post_force_login(token, visible)?;
            let _ = ctx.report_step("登录请求已被 chat2api 接受（202），开始等待登录完成");
            return self.wait_login_valid(token, ctx, login_budget_s, interval_s);
        }
        let _ = ctx.report_step("通过受限核验端点确认当前认证，尚未发送登录请求");
        let verification = self.verify().map_err(|failure| {
            ActionFailure::new(
                &failure.code,
                format!("无法确认当前认证，暂不打开登录：{}", failure.message),
            )
        })?;
        ctx.raise_if_cancelled()?;
        let verify_state = verification["verify"].as_str().unwrap_or_default();
        match verify_state {
            "valid" => {
                let _ = ctx.report_step("当前账号有效，无需重新登录，未发送登录请求");
                Ok(LoginOutcome::Valid {
                    skipped: true,
                    waited: 0.0,
                    at_recheck: false,
                })
            }
            "needs_login" | "rejected" => {
                self.post_force_login(token, false)?;
                let _ = ctx.report_step("登录请求已被 chat2api 接受（202），开始等待登录完成");
                self.wait_login_valid(token, ctx, login_budget_s, interval_s)
            }
            _ => Err(ActionFailure::new(
                "login_verification_unknown",
                "无法确认当前认证，暂不打开登录；受限核验结果为 unknown",
            )),
        }
    }

    fn start_and_confirm(
        &self,
        ctx: &OperationContext,
        budget_s: f64,
        interval_s: f64,
    ) -> Result<(), ActionFailure> {
        let _ = ctx.report_step("chat2api 未在运行，经服务控制启动");
        self.service.start(ctx).map_err(|failure| {
            ActionFailure::new(
                "start_failed",
                format!(
                    "启动 chat2api 失败（{}），未发送登录请求，也未执行停止",
                    failure.code
                ),
            )
        })?;
        let deadline = Instant::now() + Duration::from_secs_f64(budget_s);
        loop {
            if self
                .runtime
                .snapshot()
                .map(|snapshot| snapshot.display_state() == "Running")
                .unwrap_or(false)
            {
                return Ok(());
            }
            ctx.raise_if_cancelled()?;
            if Instant::now() >= deadline {
                return Err(ActionFailure::new(
                    "start_failed",
                    format!(
                        "启动已请求但 {budget_s:.0}s 内快照未观察到 Running；\
                         未发送登录请求，也未执行停止（服务未确认启动成功）"
                    ),
                ));
            }
            thread::sleep(Duration::from_secs_f64(interval_s));
        }
    }

    fn post_force_login(&self, token: &str, visible: bool) -> Result<(), ActionFailure> {
        let response = self
            .client
            .post_json(
                "/admin/auth/login",
                token,
                &json!({"visible": visible}),
                LOGIN_TIMEOUT,
            )
            .map_err(|error| match error {
                AdminError::Unreachable => {
                    ActionFailure::new("login_unreachable", "登录请求未完成：chat2api 服务不可达")
                }
                AdminError::Protocol => {
                    ActionFailure::new("login_unreachable", "登录请求未完成：chat2api 响应不可解析")
                }
            })?;
        if response.status == 202 {
            return Ok(());
        }
        let payload = serde_json::from_slice::<Value>(&response.body).ok();
        if let Some(code) = payload.as_ref().and_then(response_code) {
            return Err(login_round_error(code, None));
        }
        Err(ActionFailure::new(
            "login_unreachable",
            format!(
                "登录请求未完成：chat2api 返回 HTTP {}（期望 202）",
                response.status
            ),
        ))
    }

    fn wait_login_valid(
        &self,
        token: &str,
        ctx: &OperationContext,
        budget_s: f64,
        interval_s: f64,
    ) -> Result<LoginOutcome, ActionFailure> {
        let started = Instant::now();
        let deadline = started + Duration::from_secs_f64(budget_s);
        loop {
            ctx.raise_if_cancelled()?;
            let status = self.read_status_for_poll(token);
            let state = classify_auth_status(&status)?;
            if state == "valid" {
                return Ok(LoginOutcome::Valid {
                    skipped: false,
                    waited: started.elapsed().as_secs_f64(),
                    at_recheck: false,
                });
            }
            if Instant::now() >= deadline {
                break;
            }
            thread::sleep(Duration::from_secs_f64(interval_s));
        }
        ctx.raise_if_cancelled()?;
        let status = self.read_status_for_poll(token);
        let state = classify_auth_status(&status)?;
        if state == "valid" {
            return Ok(LoginOutcome::Valid {
                skipped: false,
                waited: started.elapsed().as_secs_f64(),
                at_recheck: true,
            });
        }
        Ok(LoginOutcome::NotSettled {
            state,
            waited: started.elapsed().as_secs_f64(),
            read_error: status.read_error,
        })
    }

    fn read_status_for_poll(&self, token: &str) -> PollStatus {
        match self.read_status(token, STATUS_POLL_TIMEOUT) {
            Ok(status) => PollStatus {
                status: Some(status),
                read_error: None,
            },
            Err(error) => PollStatus {
                status: None,
                read_error: Some(error),
            },
        }
    }

    fn restore_service(
        &self,
        ctx: &OperationContext,
        started_by_us: bool,
    ) -> (RestoreKind, String) {
        if !started_by_us {
            return (
                RestoreKind::Kept,
                "chat2api 原本运行，未执行启停，服务保持运行".to_string(),
            );
        }
        if let Err(failure) = self.service.stop(ctx) {
            return (
                RestoreKind::Failed,
                format!(
                    "服务停止未确认（停止调用失败 {}），可能残留运行状态",
                    failure.code
                ),
            );
        }
        match self.runtime.snapshot() {
            Err(_) => (
                RestoreKind::Failed,
                "停止后快照不可用，未能确认 Stopped，可能残留运行状态".to_string(),
            ),
            Ok(snapshot) if snapshot.display_state() != "Stopped" => (
                RestoreKind::Failed,
                format!(
                    "服务停止未确认，可能残留运行状态（复验状态={}）",
                    snapshot.display_state()
                ),
            ),
            Ok(_) => (
                RestoreKind::Restored,
                "服务已停止并复验 Stopped".to_string(),
            ),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RestoreKind {
    Kept,
    Restored,
    Failed,
}

enum LoginOutcome {
    Valid {
        skipped: bool,
        waited: f64,
        at_recheck: bool,
    },
    NotSettled {
        state: String,
        waited: f64,
        read_error: Option<ActionFailure>,
    },
}

struct PollStatus {
    status: Option<Value>,
    read_error: Option<ActionFailure>,
}

fn bool_param(params: &Map<String, Value>, key: &str) -> Result<bool, ActionFailure> {
    match params.get(key) {
        None => Ok(false),
        Some(Value::Bool(value)) => Ok(*value),
        Some(_) => Err(ActionFailure::new(
            "invalid_params",
            format!("{key} must be a bool"),
        )),
    }
}

fn budget_value(params: &Map<String, Value>, key: &str, default: f64) -> f64 {
    params
        .get(key)
        .and_then(Value::as_f64)
        .filter(|value| *value >= 0.0)
        .map(|value| value.min(3600.0))
        .unwrap_or(default)
}

const TERMINAL_MANUAL_REASONS: [&str; 4] = [
    "umpass_mfa_required",
    "umpass_prefill_missing",
    "umpass_autofill_protected",
    "unrecognized_page",
];
const MANUAL_TAKEOVER_PREFIXES: [&str; 2] = ["manual_takeover_required:", "manual_takeover:"];
const DUO_PROMPT_WAITING_REASON: &str = "duo_prompt_waiting";

fn terminal_manual_reason(values: &[Option<&str>]) -> Option<&'static str> {
    for value in values {
        let Some(value) = value else { continue };
        if value.is_empty() {
            continue;
        }
        let mut reason = *value;
        for prefix in MANUAL_TAKEOVER_PREFIXES {
            if let Some(stripped) = reason.strip_prefix(prefix) {
                reason = stripped;
                break;
            }
        }
        for known in TERMINAL_MANUAL_REASONS {
            if reason == known {
                return Some(known);
            }
        }
    }
    None
}

fn login_round_code(value: &str) -> Option<&'static str> {
    let code = value.trim().split(':').next().unwrap_or_default().trim();
    match code {
        "cancelled" | "login_cancelled" => Some("cancelled"),
        "login_busy" | "login_already_running" => Some("login_busy"),
        "login_unavailable" | "visible_login_unavailable" => Some("login_unavailable"),
        _ => None,
    }
}

fn login_round_code_from_status(status: &Value) -> Option<&'static str> {
    let object = status.as_object()?;
    let recovery = object.get("recovery").and_then(Value::as_object);
    for value in [
        object.get("state"),
        object.get("error"),
        object.get("primary"),
        recovery.and_then(|recovery| recovery.get("pause_reason")),
    ] {
        if let Some(code) = value.and_then(Value::as_str).and_then(login_round_code) {
            return Some(code);
        }
    }
    None
}

fn login_cancelled(status: &Value) -> bool {
    let Some(object) = status.as_object() else {
        return false;
    };
    let recovery = object.get("recovery").and_then(Value::as_object);
    let pause_reason = recovery
        .and_then(|recovery| recovery.get("pause_reason"))
        .and_then(Value::as_str);
    ["state", "error", "primary"]
        .iter()
        .any(|key| object.get(*key).and_then(Value::as_str) == Some("cancelled"))
        || pause_reason == Some("cancelled")
}

fn login_round_error(code: &str, detail: Option<&str>) -> ActionFailure {
    if code == "cancelled" {
        return ActionFailure::cancelled();
    }
    let mut message = if code == "login_busy" {
        "登录未开始：同一 profile 已有登录浏览器占用（login_busy）".to_string()
    } else {
        "登录未开始：无法打开登录窗口（login_unavailable / visible_login_unavailable）".to_string()
    };
    if let Some(detail) = detail.filter(|detail| !detail.trim().is_empty()) {
        message.push('：');
        message.push_str(&single_line(detail));
    }
    ActionFailure::new(code, message)
}

fn terminal_login_failure(status: &Value) -> Option<ActionFailure> {
    let object = status.as_object()?;
    let error = object.get("error").and_then(Value::as_str);
    let primary = object.get("primary").and_then(Value::as_str);
    let recovery = object.get("recovery").and_then(Value::as_object);
    let recovery_state = recovery
        .and_then(|recovery| recovery.get("state"))
        .and_then(Value::as_str);
    let pause_reason = recovery
        .and_then(|recovery| recovery.get("pause_reason"))
        .and_then(Value::as_str);

    if let Some(reason) = terminal_manual_reason(&[error, pause_reason, primary]) {
        let mut message = format!(
            "登录需要人工接管（manual_takeover_required:{reason}）；本轮已结束，浏览器已关闭"
        );
        if let Some(error) = error.filter(|error| terminal_manual_reason(&[Some(error)]).is_none())
        {
            message.push_str(&format!("；清理失败：{error}"));
        }
        return Some(ActionFailure::new(
            "login_manual_takeover_required",
            message,
        ));
    }
    if recovery_state == Some("exhausted") {
        let reason = non_empty(pause_reason)
            .or_else(|| non_empty(error))
            .unwrap_or("failed");
        let mut message = format!("登录失败（{reason}）；自动恢复已结束");
        if let Some(primary) = non_empty(primary).filter(|primary| *primary != reason) {
            message.push_str(&format!("；原始原因：{primary}"));
        }
        return Some(ActionFailure::new("login_failed", message));
    }
    None
}

fn classify_auth_status(status: &PollStatus) -> Result<String, ActionFailure> {
    let Some(value) = &status.status else {
        if let Some(error) = &status.read_error {
            if matches!(error.code.as_str(), "control_denied" | "status_schema") {
                return Err(ActionFailure::new(
                    &error.code,
                    format!("无法读取认证状态，登录等待已结束：{}", error.message),
                ));
            }
        }
        return Ok(String::new());
    };
    let state = value["state"].as_str().unwrap_or_default().to_string();
    if state == "valid" {
        return Ok(state);
    }
    if let Some(error) = &status.read_error {
        if matches!(error.code.as_str(), "control_denied" | "status_schema") {
            return Err(ActionFailure::new(
                &error.code,
                format!("无法读取认证状态，登录等待已结束：{}", error.message),
            ));
        }
    }
    if login_cancelled(value) {
        return Err(ActionFailure::cancelled());
    }
    let login = value.get("login");
    if state == "unknown"
        && status.read_error.is_none()
        && login.is_none()
        && value
            .get("recovery")
            .and_then(Value::as_object)
            .and_then(|recovery| recovery.get("state"))
            .and_then(Value::as_str)
            == Some("idle")
    {
        return Err(ActionFailure::new(
            "login_verification_unknown",
            "上游认证暂时无法核验；本轮登录等待已结束，后台将自动重新核验",
        ));
    }
    if login
        .and_then(Value::as_object)
        .and_then(|login| login.get("manual_reason"))
        .and_then(Value::as_str)
        == Some(DUO_PROMPT_WAITING_REASON)
    {
        return Err(ActionFailure::new(
            "login_manual_takeover_required",
            "登录需要本人确认 Duo 推送；本轮仍在等待，可打开可见窗口",
        ));
    }
    if let Some(code) = login_round_code_from_status(value) {
        return Err(login_round_error(code, None));
    }
    if let Some(failure) = terminal_login_failure(value) {
        return Err(failure);
    }
    Ok(state)
}

fn response_code(payload: &Value) -> Option<&'static str> {
    let object = payload.as_object()?;
    let mut candidates: Vec<Option<&str>> = vec![
        object.get("code").and_then(Value::as_str),
        object.get("error").and_then(Value::as_str),
        object.get("reason").and_then(Value::as_str),
        object.get("state").and_then(Value::as_str),
        object.get("message").and_then(Value::as_str),
    ];
    if let Some(error) = object.get("error").and_then(Value::as_object) {
        candidates.extend([
            error.get("code").and_then(Value::as_str),
            error.get("type").and_then(Value::as_str),
            error.get("reason").and_then(Value::as_str),
            error.get("state").and_then(Value::as_str),
            error.get("message").and_then(Value::as_str),
        ]);
        match error.get("detail") {
            Some(Value::Object(detail)) => candidates.extend([
                detail.get("code").and_then(Value::as_str),
                detail.get("reason").and_then(Value::as_str),
                detail.get("state").and_then(Value::as_str),
            ]),
            Some(Value::String(detail)) => candidates.push(Some(detail.as_str())),
            _ => {}
        }
    }
    candidates.into_iter().flatten().find_map(login_round_code)
}

fn reraise_after_restore(failure: ActionFailure, kind: RestoreKind, fact: &str) -> ActionFailure {
    if failure.code == "cancelled" {
        if kind == RestoreKind::Failed {
            return ActionFailure::new("stop_failed", format!("已响应取消，但{fact}"));
        }
        return failure;
    }
    if failure.code == "login_manual_takeover_required" && kind == RestoreKind::Kept {
        return failure;
    }
    ActionFailure::new(
        &failure.code,
        single_line(&format!("{}；{}", failure.message, fact)),
    )
}

fn success_note(waited: f64, at_recheck: bool, fact: &str, login_skipped: bool) -> String {
    let mut parts = vec![if login_skipped {
        "当前账号有效，无需重新登录（state=valid）".to_string()
    } else {
        "登录完成（state=valid）".to_string()
    }];
    if !login_skipped {
        parts.push(format!("等待 {waited:.1}s"));
    }
    if at_recheck {
        parts.push("预算耗尽后的复查确认".to_string());
    }
    parts.push(fact.to_string());
    parts.join("；")
}

fn timeout_message(
    final_state: &str,
    waited: f64,
    fact: &str,
    read_error: Option<&ActionFailure>,
) -> String {
    let mut observed = if final_state.is_empty() {
        "不可达".to_string()
    } else {
        final_state.to_string()
    };
    if let Some(error) = read_error {
        observed.push_str(&format!("（读取失败：{}）", error.code));
    }
    format!(
        "登录在控制台等待上限内未确认完成（等待 {waited:.1}s，复查状态={observed}）；\
         控制台超时不保证后端线程已结束，实际状态可能仍在变化；{fact}"
    )
}

fn non_empty(value: Option<&str>) -> Option<&str> {
    value.filter(|value| !value.is_empty())
}

fn single_line(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn status_transport_error(error: AdminError) -> ActionFailure {
    match error {
        AdminError::Unreachable => ActionFailure::new(
            "status_unreachable",
            "查询失败：chat2api 服务不可达，凭据状态未知（未知 ≠ 失效）",
        ),
        AdminError::Protocol => status_schema_error(),
    }
}

impl UmAuthExecutor {
    fn verify(&self) -> Result<Value, ActionFailure> {
        let token = self.client.control_token()?;
        let response = self
            .client
            .get("/admin/auth/verify", &token, VERIFY_TIMEOUT)
            .map_err(verify_transport_error)?;
        match response.status {
            200 => {}
            401 | 403 => {
                return Err(ActionFailure::new(
                    "control_denied",
                    "管理控制头被拒绝（控制令牌不匹配）",
                ))
            }
            status => {
                return Err(ActionFailure::new(
                    "verify_query_failed",
                    format!(
                        "查询失败：chat2api 返回 HTTP {status}（受限核验端点），\
                         核验状态未知（未知 ≠ 失效）"
                    ),
                ))
            }
        }
        let payload: Value =
            serde_json::from_slice(&response.body).map_err(|_| verify_schema_error())?;
        let object = payload.as_object().ok_or_else(verify_schema_error)?;
        let verify = object
            .get("verify")
            .and_then(Value::as_str)
            .filter(|value| VERIFY_STATES.contains(value))
            .ok_or_else(verify_schema_error)?;
        let mut note = VERIFY_NOTE.to_string();
        let checked_epoch = match int_field(object.get("checked_epoch")) {
            Some(value) => value,
            None => {
                note.push_str("；端点未带时间");
                epoch_seconds()
            }
        };
        Ok(json!({
            "verify": verify,
            "reason": text_field(object.get("reason")),
            "upstream_status": int_field(object.get("upstream_status")),
            "checked_epoch": checked_epoch,
            "note": note,
        }))
    }
}

fn verify_transport_error(error: AdminError) -> ActionFailure {
    match error {
        AdminError::Unreachable => ActionFailure::new(
            "status_unreachable",
            "查询失败：chat2api 服务不可达，核验状态未知（未知 ≠ 失效）",
        ),
        AdminError::Protocol => verify_schema_error(),
    }
}

impl ActionExecutor for UmAuthExecutor {
    fn execute(
        &self,
        action: &str,
        _target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        match action {
            "account.um_status" => self.status(),
            "account.um_verify" => self.verify(),
            "account.login" => self.login(params, ctx),
            _ => Err(ActionFailure::new(
                "invalid_action",
                format!("unknown action {action:?}"),
            )),
        }
    }
}

/// The local action executor: the migrated UM auth actions run in Rust, and the
/// retained model actions run in-process.
pub struct LocalActionExecutor {
    auth: UmAuthExecutor,
    retained: Arc<RetainedActionExecutor>,
}

impl LocalActionExecutor {
    pub fn new(auth: UmAuthExecutor, retained: Arc<RetainedActionExecutor>) -> Self {
        Self { auth, retained }
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
        if UmAuthExecutor::handles(action) {
            return self.auth.execute(action, target, params, ctx);
        }
        self.retained.execute(action, target, params, ctx)
    }
}
