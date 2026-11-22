//! #146: the desktop instance's admin read faces.
//!
//! The desktop App reads one loopback port. These handlers expose the local
//! management facts the App needs without assembling a second console host: the UM
//! model catalog, the in-process context probes, the gateway service state, the
//! login-round history the gateway already writes, the bounded service log and the
//! connection hints. Every handler is loopback + control-token protected by
//! `control_allowed` in the router.

use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::config::GatewayConfig;
use crate::error::GatewayError;
use crate::login::history::{read_records, sanitize_reason};
use crate::readonly::ProxyState;

/// The declared-context probe ladder, in input characters.
pub const PROBE_INPUT_STEPS: [i64; 4] = [4000, 16000, 64000, 256000];
/// One probe request caps its output at this many tokens.
pub const PROBE_MAX_TOKENS: i64 = 64;
/// Probe history entries kept per model.
pub const PROBE_HISTORY_MAX: usize = 20;
/// Default probe batch request budget.
pub const PROBE_DEFAULT_MAX_REQUESTS: i64 = 20;
/// The largest log page the admin read returns.
pub const LOG_MAX_LIMIT: i64 = 200;
/// The bounded tail the admin log read scans.
pub const LOG_MAX_TAIL_BYTES: u64 = 1024 * 1024;
/// The largest login-round page the admin read returns.
pub const LOGIN_ROUND_MAX_LIMIT: i64 = 400;

const LOG_SOURCE: &str = "chat2api";
const LOG_SOURCES: [&str; 1] = [LOG_SOURCE];
const LOG_LEVELS: [&str; 6] = ["error", "warn", "info", "debug", "unknown", "raw"];
const CONTEXT_LIMIT_MARKERS: [&str; 8] = [
    "context length",
    "maximum context",
    "max context",
    "input length",
    "too long",
    "length limit",
    "token limit",
    "上下文",
];
pub(crate) const PROBE_TIMEOUT: Duration = Duration::from_secs(90);

/// One upstream context-probe answer.
pub(crate) enum ProbeCall {
    Response { status: u16, body: Value },
    Failure(&'static str),
}

/// One in-memory probe record per model.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct ProbeRecord {
    status: String,
    observed_floor: Option<i64>,
    observed_ceiling: Option<i64>,
    attempts: i64,
    last_run: Option<String>,
    last_success: Option<String>,
    last_error: Option<String>,
    token_evidence: Option<i64>,
    history: Vec<ProbeHistory>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct ProbeHistory {
    status: String,
    observed_floor: Option<i64>,
    observed_ceiling: Option<i64>,
    attempts: i64,
    last_run: Option<String>,
    last_success: Option<String>,
    last_error: Option<String>,
    token_evidence: Option<i64>,
    reason: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct ProbeStateFile {
    version: u32,
    records: BTreeMap<String, ProbeRecord>,
}

#[derive(Debug, Clone, Default)]
struct ProbeRun {
    cancel: Arc<AtomicBool>,
}

/// The admin read state shared by every request of this gateway process.
pub struct AdminState {
    bound_addr: Mutex<Option<SocketAddr>>,
    records: Arc<Mutex<BTreeMap<String, ProbeRecord>>>,
    run: Arc<Mutex<Option<ProbeRun>>>,
    last_stop: Arc<Mutex<Option<String>>>,
    budget: Mutex<(i64, i64)>,
    state_file: Option<PathBuf>,
    state_error: Arc<Mutex<Option<String>>>,
}

impl AdminState {
    pub fn new(config: &GatewayConfig) -> Self {
        let state_file = config.effective_model_probe_state_file();
        let (records, state_error) = load_probe_state(state_file.as_deref());
        Self {
            bound_addr: Mutex::new(None),
            records: Arc::new(Mutex::new(records)),
            run: Arc::new(Mutex::new(None)),
            last_stop: Arc::new(Mutex::new(None)),
            budget: Mutex::new((PROBE_DEFAULT_MAX_REQUESTS, PROBE_MAX_TOKENS)),
            state_file,
            state_error: Arc::new(Mutex::new(state_error)),
        }
    }

    pub fn set_bound_addr(&self, addr: SocketAddr) {
        *self.bound_addr.lock().expect("admin bound addr poisoned") = Some(addr);
    }

    pub fn bound_addr(&self) -> Option<SocketAddr> {
        *self.bound_addr.lock().expect("admin bound addr poisoned")
    }

    fn last_test(&self, model_id: &str) -> Value {
        let records = self.records.lock().expect("admin records poisoned");
        records
            .get(model_id)
            .map(probe_record_json)
            .unwrap_or(Value::Null)
    }

    /// The probe history and queue. With a state file the history survives a
    /// gateway restart; without one it is process-local.
    pub fn probes_snapshot(&self) -> Value {
        let records = self.records.lock().expect("admin records poisoned");
        let running = self.run.lock().expect("admin run poisoned").is_some();
        let queue: Vec<Value> = records
            .iter()
            .filter(|(_, record)| matches!(record.status.as_str(), "pending" | "running"))
            .map(|(model_id, record)| json!({"model_id": model_id, "status": record.status}))
            .collect();
        let rows: Vec<Value> = records
            .iter()
            .map(|(model_id, record)| {
                let mut row = probe_record_json(record);
                row["model_id"] = json!(model_id);
                row
            })
            .collect();
        let budget = *self.budget.lock().expect("admin budget poisoned");
        json!({
            "status": "ok",
            "running": running,
            "queue": queue,
            "records": rows,
            "steps": PROBE_INPUT_STEPS,
            "budget": {"max_requests": budget.0, "max_tokens": budget.1},
            "stop_requested": self.last_stop.lock().expect("admin stop poisoned").is_some(),
            "state_error": self
                .state_error
                .lock()
                .expect("admin state error poisoned")
                .clone(),
            "note": "探测历史保存在实例数据目录；无路径时只在进程内，网关重启后为空",
        })
    }

    /// Queue one probe batch and run it on a background thread.
    pub fn start_probes(
        &self,
        proxy: Arc<ProxyState>,
        models: Vec<String>,
        max_requests: i64,
        max_tokens: i64,
        retest: bool,
    ) -> Result<Value, GatewayError> {
        let mut deduplicated = Vec::new();
        for model in models {
            if model.is_empty() {
                return Err(GatewayError::invalid_request(
                    "models must contain non-empty model ids",
                ));
            }
            if !deduplicated.contains(&model) {
                deduplicated.push(model);
            }
        }
        if deduplicated.is_empty() {
            return Err(GatewayError::invalid_request(
                "models must be a non-empty array",
            ));
        }
        let cancel = Arc::new(AtomicBool::new(false));
        {
            let mut run = self.run.lock().expect("admin run poisoned");
            if run.is_some() {
                return Err(GatewayError::new(
                    409,
                    "A context probe batch is already running",
                    "probe_run_in_progress",
                ));
            }
            *run = Some(ProbeRun {
                cancel: Arc::clone(&cancel),
            });
        }
        *self.last_stop.lock().expect("admin stop poisoned") = None;
        *self.budget.lock().expect("admin budget poisoned") = (max_requests, max_tokens);
        let mut queued = 0;
        {
            let mut records = self.records.lock().expect("admin records poisoned");
            for model_id in &deduplicated {
                let record = records.entry(model_id.clone()).or_default();
                if !retest && record.status == "verified" {
                    continue;
                }
                record.status = "pending".to_string();
                record.last_error = None;
                queued += 1;
            }
        }
        let records = Arc::clone(&self.records);
        let run_slot = Arc::clone(&self.run);
        let last_stop = Arc::clone(&self.last_stop);
        let state_file = self.state_file.clone();
        let state_error = Arc::clone(&self.state_error);
        let thread_models = deduplicated.clone();
        if let Err(error) = thread::Builder::new()
            .name("um-context-probes".to_string())
            .spawn(move || {
                run_probe_batch(
                    proxy,
                    records,
                    run_slot,
                    last_stop,
                    state_file,
                    state_error,
                    thread_models,
                    cancel,
                    max_requests,
                    max_tokens,
                    retest,
                );
            })
        {
            *self.run.lock().expect("admin run poisoned") = None;
            return Err(GatewayError::new(
                500,
                format!("cannot start the context probe runner: {error}"),
                "probe_runner_unavailable",
            ));
        }
        self.persist();
        Ok(json!({
            "status": "started",
            "queued": queued,
            "models": deduplicated,
            "max_requests": max_requests,
            "max_tokens": max_tokens,
            "note": "探测在网关进程内按步进顺序执行；DELETE 停止后续请求",
        }))
    }

    /// The gateway reports itself as the only UM service; the App supervises
    /// this same process, so there is no second service to invent.
    pub fn services_payload(&self) -> Value {
        let bound = self.bound_addr();
        let port = bound.map(|addr| addr.port());
        let service = json!({
            "key": "chat2api",
            "displayName": "UM 网关",
            "port": port,
            "state": if port.is_some() { "Running" } else { "Unknown" },
            "healthy": port.map(|_| true),
            "detail": if port.is_some() {
                "本实例网关正在运行"
            } else {
                "监听地址尚未确认"
            },
            "processIds": if port.is_some() {
                json!([std::process::id()])
            } else {
                json!([])
            },
        });
        json!({
            "services": [service],
            "checkedAt": epoch_millis(),
            "cached": false,
            "cacheAgeSeconds": Value::Null,
        })
    }

    /// An idempotent start: the process answering the request is already the
    /// UM service. A missing bound address is reported, never folded into running.
    pub fn start_service(&self) -> Result<Value, GatewayError> {
        let payload = self.services_payload();
        let service = payload["services"][0].clone();
        if service["state"] != json!("Running") {
            return Err(GatewayError::new(
                503,
                "The gateway has not confirmed its listening address",
                "service_start_failed",
            )
            .with_detail(json!({
                "reason": "bound_address_unknown",
                "service": service,
            })));
        }
        Ok(json!({
            "status": "already_running",
            "started": false,
            "service": service,
            "note": "本实例网关就是该服务；启动是幂等的，不拉起第二个进程",
        }))
    }

    /// Queue one probe batch from the admin request body.
    pub fn start_probes_from_body(
        &self,
        proxy: Arc<ProxyState>,
        body: &Map<String, Value>,
    ) -> Result<Value, GatewayError> {
        let allowed = ["models", "max_requests", "max_tokens", "retest"];
        if let Some(unknown) = body.keys().find(|key| !allowed.contains(&key.as_str())) {
            return Err(GatewayError::invalid_request(format!(
                "unknown field: {unknown}"
            )));
        }
        let models = match body.get("models") {
            Some(Value::Array(items)) => {
                let mut models = Vec::new();
                for item in items {
                    let Some(model) = item.as_str().filter(|model| !model.is_empty()) else {
                        return Err(GatewayError::invalid_request(
                            "models must be an array of non-empty model ids",
                        ));
                    };
                    models.push(model.to_string());
                }
                models
            }
            _ => {
                return Err(GatewayError::invalid_request(
                    "models must be a non-empty array",
                ))
            }
        };
        if models.is_empty() {
            return Err(GatewayError::invalid_request(
                "models must be a non-empty array",
            ));
        }
        let max_requests =
            bounded_probe_int(body, "max_requests", PROBE_DEFAULT_MAX_REQUESTS, 1, 20)?;
        let max_tokens =
            bounded_probe_int(body, "max_tokens", PROBE_MAX_TOKENS, 1, PROBE_MAX_TOKENS)?;
        let retest = match body.get("retest") {
            None => false,
            Some(Value::Bool(value)) => *value,
            Some(_) => return Err(GatewayError::invalid_request("retest must be a boolean")),
        };
        self.start_probes(proxy, models, max_requests, max_tokens, retest)
    }

    /// Stop the queued models after the in-flight request returns.
    pub fn stop_probes(&self) -> Value {
        let mut run = self.run.lock().expect("admin run poisoned");
        let stopped = match run.as_mut() {
            Some(run) => {
                run.cancel.store(true, Ordering::SeqCst);
                true
            }
            None => false,
        };
        *self.last_stop.lock().expect("admin stop poisoned") =
            stopped.then(|| "stopped_by_user".to_string());
        json!({
            "status": if stopped { "stopping" } else { "idle" },
            "stopped": stopped,
            "note": if stopped {
                "已请求停止；在途请求返回后不再发起后续探测"
            } else {
                "当前没有运行中的探测批次"
            },
        })
    }

    fn persist(&self) {
        persist_probe_records(self.state_file.as_deref(), &self.records, &self.state_error);
    }
}

fn load_probe_state(path: Option<&Path>) -> (BTreeMap<String, ProbeRecord>, Option<String>) {
    let Some(path) = path else {
        return (BTreeMap::new(), None);
    };
    let text = match fs::read_to_string(path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return (BTreeMap::new(), None)
        }
        Err(error) => {
            return (
                BTreeMap::new(),
                Some(format!("probe_state_unreadable: {error}")),
            )
        }
    };
    let state = match serde_json::from_str::<ProbeStateFile>(&text) {
        Ok(state) if state.version == 1 => state,
        Ok(_) => {
            return (
                BTreeMap::new(),
                Some("probe_state_unsupported_version".to_string()),
            )
        }
        Err(error) => {
            return (
                BTreeMap::new(),
                Some(format!("probe_state_invalid: {error}")),
            )
        }
    };
    let mut records = state.records;
    for record in records.values_mut() {
        if matches!(record.status.as_str(), "pending" | "running") {
            record.status = "incomplete".to_string();
            record.last_error = Some("process_exit".to_string());
            record
                .history
                .push(history_from(record, Some("process_exit")));
            trim_history(record);
        }
    }
    (records, None)
}

fn persist_probe_records(
    path: Option<&Path>,
    records: &Mutex<BTreeMap<String, ProbeRecord>>,
    state_error: &Mutex<Option<String>>,
) {
    let Some(path) = path else {
        return;
    };
    let records = records.lock().expect("admin records poisoned").clone();
    let result = (|| -> std::io::Result<()> {
        if let Some(parent) = path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            fs::create_dir_all(parent)?;
        }
        let state = ProbeStateFile {
            version: 1,
            records,
        };
        let text = serde_json::to_string(&state)
            .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
        fs::write(path, text)
    })();
    *state_error.lock().expect("admin state error poisoned") = match result {
        Ok(()) => None,
        Err(error) => Some(format!("probe_state_write_failed: {error}")),
    };
}

fn probe_record_json(record: &ProbeRecord) -> Value {
    json!({
        "status": record.status,
        "observed_floor": record.observed_floor,
        "observed_ceiling": record.observed_ceiling,
        "attempts": record.attempts,
        "last_run": record.last_run,
        "last_success": record.last_success,
        "last_error": record.last_error,
        "token_evidence": record.token_evidence,
        "history": record.history.iter().map(probe_history_json).collect::<Vec<_>>(),
    })
}

fn probe_history_json(entry: &ProbeHistory) -> Value {
    json!({
        "status": entry.status,
        "observed_floor": entry.observed_floor,
        "observed_ceiling": entry.observed_ceiling,
        "attempts": entry.attempts,
        "last_run": entry.last_run,
        "last_success": entry.last_success,
        "last_error": entry.last_error,
        "token_evidence": entry.token_evidence,
        "reason": entry.reason,
    })
}

fn history_from(record: &ProbeRecord, reason: Option<&str>) -> ProbeHistory {
    ProbeHistory {
        status: record.status.clone(),
        observed_floor: record.observed_floor,
        observed_ceiling: record.observed_ceiling,
        attempts: record.attempts,
        last_run: record.last_run.clone(),
        last_success: record.last_success.clone(),
        last_error: record.last_error.clone(),
        token_evidence: record.token_evidence,
        reason: reason.map(str::to_string),
    }
}

#[allow(clippy::too_many_arguments)]
fn run_probe_batch(
    proxy: Arc<ProxyState>,
    records: Arc<Mutex<BTreeMap<String, ProbeRecord>>>,
    run_slot: Arc<Mutex<Option<ProbeRun>>>,
    last_stop: Arc<Mutex<Option<String>>>,
    state_file: Option<PathBuf>,
    state_error: Arc<Mutex<Option<String>>>,
    models: Vec<String>,
    cancel: Arc<AtomicBool>,
    max_requests: i64,
    max_tokens: i64,
    retest: bool,
) {
    let mut used = 0i64;
    let mut stop_reason: Option<String> = None;
    for (index, model_id) in models.iter().enumerate() {
        if cancel.load(Ordering::SeqCst) {
            stop_reason = Some("stopped_by_user".to_string());
        }
        if stop_reason.is_some() {
            mark_cancelled(&records, &models[index..], stop_reason.as_deref());
            persist_probe_records(state_file.as_deref(), &records, &state_error);
            break;
        }
        let pending_steps = {
            let mut records = records.lock().expect("admin records poisoned");
            let record = records.entry(model_id.clone()).or_default();
            record.status = "running".to_string();
            let floor = record.observed_floor;
            PROBE_INPUT_STEPS
                .iter()
                .copied()
                .filter(|step| retest || floor.map_or(true, |floor| *step > floor))
                .collect::<Vec<_>>()
        };
        if pending_steps.is_empty() {
            let mut guard = records.lock().expect("admin records poisoned");
            if let Some(record) = guard.get_mut(model_id) {
                record.status = "verified".to_string();
                record
                    .history
                    .push(history_from(record, Some("already_covered")));
                trim_history(record);
            }
            drop(guard);
            persist_probe_records(state_file.as_deref(), &records, &state_error);
            continue;
        }
        let mut completed = true;
        for step in pending_steps {
            if cancel.load(Ordering::SeqCst) {
                stop_reason = Some("stopped_by_user".to_string());
                completed = false;
                break;
            }
            if used >= max_requests {
                stop_reason = Some("budget_exhausted".to_string());
                completed = false;
                break;
            }
            used += 1;
            {
                let mut records = records.lock().expect("admin records poisoned");
                if let Some(record) = records.get_mut(model_id) {
                    record.attempts += 1;
                    record.last_run = Some(now_iso());
                }
            }
            match proxy.probe_chat(model_id, step, max_tokens) {
                ProbeCall::Failure(reason) => {
                    let mut records = records.lock().expect("admin records poisoned");
                    if let Some(record) = records.get_mut(model_id) {
                        record.last_error = Some(format!("transient:{reason}"));
                    }
                    completed = false;
                    break;
                }
                ProbeCall::Response { status, body } if status < 400 => {
                    let mut records = records.lock().expect("admin records poisoned");
                    if let Some(record) = records.get_mut(model_id) {
                        record.observed_floor = Some(step);
                        record.last_success = Some(now_iso());
                        record.last_error = None;
                        if let Some(tokens) = prompt_tokens(&body) {
                            if record.token_evidence.map_or(true, |stored| tokens > stored) {
                                record.token_evidence = Some(tokens);
                            }
                        }
                    }
                }
                ProbeCall::Response { status, body } => {
                    let mut records = records.lock().expect("admin records poisoned");
                    if let Some(record) = records.get_mut(model_id) {
                        if context_limit_exceeded(status, &body) {
                            record.status = "failed".to_string();
                            record.observed_ceiling = Some(step);
                            record.last_error = Some("ContextLimitRefused".to_string());
                        } else {
                            record.last_error = Some(format!("HTTP{status}"));
                        }
                    }
                    completed = false;
                    break;
                }
            }
        }
        {
            let mut records = records.lock().expect("admin records poisoned");
            if let Some(record) = records.get_mut(model_id) {
                if completed {
                    record.status = "verified".to_string();
                    record.last_error = None;
                } else if record.status != "failed" {
                    record.status = if stop_reason.as_deref() == Some("stopped_by_user") {
                        "cancelled".to_string()
                    } else {
                        "failed".to_string()
                    };
                }
                let reason = stop_reason.as_deref().or(if completed {
                    Some("probe_completed")
                } else {
                    None
                });
                record.history.push(history_from(record, reason));
                trim_history(record);
            }
        }
        persist_probe_records(state_file.as_deref(), &records, &state_error);
        if stop_reason.is_some() {
            mark_cancelled(&records, &models[index + 1..], stop_reason.as_deref());
            persist_probe_records(state_file.as_deref(), &records, &state_error);
            break;
        }
    }
    if let Some(reason) = &stop_reason {
        *last_stop.lock().expect("admin stop poisoned") = Some(reason.clone());
    }
    *run_slot.lock().expect("admin run poisoned") = None;
}

fn mark_cancelled(
    records: &Arc<Mutex<BTreeMap<String, ProbeRecord>>>,
    models: &[String],
    reason: Option<&str>,
) {
    let mut records = records.lock().expect("admin records poisoned");
    for model_id in models {
        if let Some(record) = records.get_mut(model_id) {
            if !matches!(record.status.as_str(), "pending" | "running") {
                continue;
            }
            record.status = "cancelled".to_string();
            record.last_error = reason.map(str::to_string);
            record.history.push(history_from(record, reason));
            trim_history(record);
        }
    }
}

fn trim_history(record: &mut ProbeRecord) {
    if record.history.len() > PROBE_HISTORY_MAX {
        let excess = record.history.len() - PROBE_HISTORY_MAX;
        record.history.drain(..excess);
    }
}

fn prompt_tokens(body: &Value) -> Option<i64> {
    body.get("usage")
        .and_then(|usage| usage.get("prompt_tokens"))
        .and_then(Value::as_i64)
}

fn context_limit_exceeded(status: u16, body: &Value) -> bool {
    if !matches!(status, 400 | 413 | 422) {
        return false;
    }
    let text = body.to_string().to_ascii_lowercase();
    CONTEXT_LIMIT_MARKERS
        .iter()
        .any(|marker| text.contains(marker))
}

fn bounded_probe_int(
    body: &Map<String, Value>,
    name: &str,
    default: i64,
    minimum: i64,
    maximum: i64,
) -> Result<i64, GatewayError> {
    match body.get(name) {
        None => Ok(default),
        Some(Value::Number(number)) => match number.as_i64() {
            Some(value) if (minimum..=maximum).contains(&value) => Ok(value),
            _ => Err(GatewayError::invalid_request(format!(
                "{name} must be an integer in {minimum}..{maximum}"
            ))),
        },
        Some(_) => Err(GatewayError::invalid_request(format!(
            "{name} must be an integer in {minimum}..{maximum}"
        ))),
    }
}

fn epoch_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn epoch_millis() -> i64 {
    (epoch_now() * 1000.0).round() as i64
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// The durable login-round history the gateway already writes (#115/#116).
/// A missing file is a legal empty read; an unreadable file is never folded
/// into an empty list, and an unconfigured path stays explicit.
pub fn login_rounds(config: &GatewayConfig, raw_target: &str) -> Result<Value, GatewayError> {
    let query = query_pairs(raw_target)?;
    let round =
        match query_first(&query, "round") {
            None => None,
            Some(value) => Some(value.parse::<u64>().map_err(|_| {
                GatewayError::invalid_request("round must be a non-negative integer")
            })?),
        };
    let search = query_first(&query, "q")
        .unwrap_or("")
        .trim()
        .to_lowercase()
        .chars()
        .take(120)
        .collect::<String>();
    let limit = match query_first(&query, "limit") {
        None => 200,
        Some(value) => value
            .parse::<i64>()
            .ok()
            .filter(|limit| (1..=LOGIN_ROUND_MAX_LIMIT).contains(limit))
            .ok_or_else(|| {
                GatewayError::invalid_request(format!(
                    "limit must be an integer in 1..{LOGIN_ROUND_MAX_LIMIT}"
                ))
            })?,
    };
    let scope = "本机实例 UM 登录轮次历史（最近 7 天 / 4 MiB）· 只读，不触发探测或登录";
    let Some(path) = config.effective_login_history_file() else {
        return Ok(json!({
            "status": "unconfigured",
            "error": "login_history_unconfigured",
            "rounds": [],
            "count": 0,
            "truncated": false,
            "scope": scope,
            "coverage": {
                "status": "unconfigured",
                "error": "login_history_unconfigured",
                "source": Value::Null,
                "bounded": true,
                "note": "未配置登录历史路径不是合法空列表",
            },
        }));
    };
    let records = match read_records(&path) {
        Ok(records) => records,
        Err(error) => {
            return Ok(json!({
                "status": "unavailable",
                "error": format!("login_history_unreadable: {error}"),
                "rounds": [],
                "count": 0,
                "truncated": false,
                "scope": scope,
                "coverage": {
                    "status": "unavailable",
                    "error": "login_history_unreadable",
                    "source": path.file_name().and_then(|name| name.to_str()).unwrap_or("login-history.jsonl"),
                    "bounded": true,
                    "note": "读取失败/损坏不是合法空列表",
                },
            }));
        }
    };
    let mut rows: Vec<Value> = records
        .iter()
        .filter(|record| round.map_or(true, |round| record.round == round))
        .filter(|record| {
            search.is_empty()
                || record
                    .reason
                    .as_deref()
                    .is_some_and(|reason| reason.to_lowercase().contains(&search))
                || record
                    .outcome
                    .as_deref()
                    .is_some_and(|outcome| outcome.to_lowercase().contains(&search))
                || record
                    .trigger_source
                    .as_deref()
                    .is_some_and(|source| source.to_lowercase().contains(&search))
                || record.rejected_before_expiry_status.is_some_and(|status| {
                    format!("旧凭据尚未到期，已被 UM 拒绝 HTTP {status}")
                        .to_lowercase()
                        .contains(&search)
                })
        })
        .map(login_round_row)
        .collect();
    let truncated = rows.len() > limit as usize;
    rows.truncate(limit as usize);
    Ok(json!({
        "status": "ok",
        "error": Value::Null,
        "rounds": rows,
        "count": rows.len(),
        "truncated": truncated,
        "scope": format!("{scope} · 本页 {} 条", rows.len()),
        "coverage": {
            "status": "ok",
            "error": Value::Null,
            "source": path.file_name().and_then(|name| name.to_str()).unwrap_or("login-history.jsonl"),
            "bounded": true,
            "note": "读取失败/损坏不是合法空列表",
        },
    }))
}

fn login_round_row(record: &crate::login::history::LoginRoundRecord) -> Value {
    json!({
        "key": format!("{}:{}", record.run_id, record.round),
        "runId": record.run_id.chars().take(120).collect::<String>(),
        "round": record.round,
        "state": record.state,
        "triggerSource": bounded_text(record.trigger_source.as_deref(), 40),
        "driver": bounded_text(record.driver.as_deref(), 20),
        "visible": record.visible,
        "startedAt": (record.started_epoch * 1000.0).round() as i64,
        "endedAt": record.ended_epoch.map(|epoch| (epoch * 1000.0).round() as i64),
        "submitCount": record.submit_count,
        "outcome": bounded_text(record.outcome.as_deref(), 40),
        "reason": record.reason.as_deref().map(sanitize_reason),
        "detail": record.detail.as_deref().map(sanitize_reason),
        "upstreamStatus": record.upstream_status,
        "evidenceVerdict": bounded_text(record.evidence_verdict.as_deref(), 40),
        "rejectedBeforeExpiryStatus": record.rejected_before_expiry_status,
    })
}

fn bounded_text(value: Option<&str>, max: usize) -> Value {
    value
        .map(|raw| {
            raw.chars()
                .filter(|character| !character.is_control())
                .take(max)
                .collect::<String>()
        })
        .map(Value::String)
        .unwrap_or(Value::Null)
}

fn query_pairs(raw_target: &str) -> Result<Vec<(String, String)>, GatewayError> {
    let Some((_, query)) = raw_target.split_once('?') else {
        return Ok(Vec::new());
    };
    let mut pairs = Vec::new();
    for part in query.split('&') {
        if part.is_empty() {
            continue;
        }
        let (name, value) = part.split_once('=').unwrap_or((part, ""));
        pairs.push((percent_decode(name)?, percent_decode(value)?));
    }
    Ok(pairs)
}

fn query_first<'a>(pairs: &'a [(String, String)], name: &str) -> Option<&'a str> {
    pairs
        .iter()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value.as_str())
}

fn percent_decode(raw: &str) -> Result<String, GatewayError> {
    let bytes = raw.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'%' => {
                let hex = raw.get(index + 1..index + 3).ok_or_else(invalid_query)?;
                let value = u8::from_str_radix(hex, 16).map_err(|_| invalid_query())?;
                output.push(value);
                index += 3;
            }
            b'+' => {
                output.push(b' ');
                index += 1;
            }
            byte => {
                output.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8(output).map_err(|_| invalid_query())
}

fn invalid_query() -> GatewayError {
    GatewayError::invalid_request("Invalid query string")
}

/// The bounded service-log history. A missing file is a legal empty read with a
/// `missing` coverage status; an unconfigured path stays explicit.
pub fn logs(config: &GatewayConfig, raw_target: &str) -> Result<Value, GatewayError> {
    let query = query_pairs(raw_target)?;
    let since = parse_time_query(&query, "since")?;
    let until = parse_time_query(&query, "until")?;
    let levels: Vec<String> = query
        .iter()
        .filter(|(name, _)| name == "level")
        .map(|(_, value)| value.to_ascii_lowercase())
        .collect();
    if levels
        .iter()
        .any(|level| !LOG_LEVELS.contains(&level.as_str()))
    {
        return Err(GatewayError::invalid_request(format!(
            "level must be one of {}",
            LOG_LEVELS.join(", ")
        )));
    }
    let source = query_first(&query, "source").unwrap_or(LOG_SOURCE);
    if source != "all" && !LOG_SOURCES.contains(&source) {
        return Err(GatewayError::invalid_request(format!(
            "source must be one of all, {}",
            LOG_SOURCES.join(", ")
        )));
    }
    let cursor = match query_first(&query, "cursor") {
        None => None,
        Some(value) => Some(
            value
                .parse::<i64>()
                .ok()
                .filter(|value| *value >= 0)
                .ok_or_else(|| GatewayError::invalid_request("Invalid log cursor"))?,
        ),
    };
    let limit = match query_first(&query, "limit") {
        None => 100,
        Some(value) => value
            .parse::<i64>()
            .ok()
            .filter(|limit| (1..=LOG_MAX_LIMIT).contains(limit))
            .ok_or_else(|| {
                GatewayError::invalid_request(format!(
                    "limit must be an integer in 1..{LOG_MAX_LIMIT}"
                ))
            })?,
    };
    let Some(path) = &config.service_log else {
        return Ok(empty_logs_payload(
            "unconfigured",
            Some("not_configured"),
            None,
            "未配置服务日志路径不是合法空列表",
        ));
    };
    let source_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("gateway.log");
    let (raw_lines, window_start, scanned_bytes) = match read_log_tail(path) {
        Ok(tail) => tail,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(empty_logs_payload(
                "missing",
                Some("file_missing"),
                Some(source_name),
                "日志文件缺失不是合法空列表",
            ))
        }
        Err(_) => {
            return Ok(empty_logs_payload(
                "error",
                Some("io_error"),
                Some(source_name),
                "日志读取失败不是合法空列表",
            ))
        }
    };
    let rows_read = raw_lines.len();
    let mut undated = 0;
    let mut rows: Vec<Value> = raw_lines
        .into_iter()
        .filter_map(|(offset, text)| {
            let row = parse_log_row(offset, &text);
            let at = row.get("at").and_then(Value::as_i64);
            if at.is_none() {
                if since.is_some() || until.is_some() {
                    undated += 1;
                    return None;
                }
            }
            if let Some(since) = since {
                if at.map_or(true, |at| at < since) {
                    return None;
                }
            }
            if let Some(until) = until {
                if at.map_or(true, |at| at > until) {
                    return None;
                }
            }
            if !levels.is_empty() {
                let level = row.get("level").and_then(Value::as_str).unwrap_or("");
                if !levels.iter().any(|filter| filter == level) {
                    return None;
                }
            }
            Some(row)
        })
        .collect();
    rows.sort_by(|left, right| {
        let left_at = left.get("at").and_then(Value::as_i64);
        let right_at = right.get("at").and_then(Value::as_i64);
        right_at.cmp(&left_at).then_with(|| {
            left.get("key")
                .and_then(Value::as_str)
                .unwrap_or("")
                .cmp(right.get("key").and_then(Value::as_str).unwrap_or(""))
        })
    });
    if let Some(cursor) = cursor {
        rows.retain(|row| {
            row.get("key")
                .and_then(Value::as_str)
                .and_then(|key| key.parse::<i64>().ok())
                .is_some_and(|offset| offset < cursor)
        });
    }
    let earlier = rows.len() > limit as usize;
    let earlier = earlier.then(|| {
        rows[limit as usize - 1]
            .get("key")
            .and_then(Value::as_str)
            .unwrap_or("0")
            .to_string()
    });
    rows.truncate(limit as usize);
    Ok(json!({
        "status": "ok",
        "error": Value::Null,
        "lines": rows,
        "sources": LOG_SOURCES,
        "scope": format!(
            "chat2api 运行日志有界尾读（最多 {} KiB / {} 行）· 不是完整历史",
            LOG_MAX_TAIL_BYTES / 1024,
            LOG_MAX_LIMIT
        ),
        "earlier": earlier,
        "coverage": {
            "status": "ok",
            "error": Value::Null,
            "source": source_name,
            "scanned_bytes": scanned_bytes,
            "window_start_offset": window_start,
            "rows_read": rows_read,
            "rows_returned": rows.len(),
            "undated_rows": undated,
            "truncated": window_start > 0,
            "bounded": true,
            "note": "有界尾读；不是完整历史",
        },
    }))
}

fn parse_time_query(query: &[(String, String)], name: &str) -> Result<Option<i64>, GatewayError> {
    match query_first(query, name) {
        None => Ok(None),
        Some(value) => value.parse::<i64>().map(Some).map_err(|_| {
            GatewayError::invalid_request(format!("{name} must be epoch milliseconds"))
        }),
    }
}

fn empty_logs_payload(
    status: &str,
    error: Option<&str>,
    source: Option<&str>,
    note: &str,
) -> Value {
    json!({
        "status": status,
        "error": error,
        "lines": [],
        "sources": LOG_SOURCES,
        "scope": format!(
            "chat2api 运行日志有界尾读（最多 {} KiB / {} 行）· 不是完整历史",
            LOG_MAX_TAIL_BYTES / 1024,
            LOG_MAX_LIMIT
        ),
        "earlier": Value::Null,
        "coverage": {
            "status": status,
            "error": error,
            "source": source,
            "scanned_bytes": 0,
            "window_start_offset": 0,
            "rows_read": 0,
            "rows_returned": 0,
            "undated_rows": 0,
            "truncated": false,
            "bounded": true,
            "note": note,
        },
    })
}

fn read_log_tail(path: &Path) -> std::io::Result<(Vec<(u64, String)>, u64, u64)> {
    let metadata = fs::metadata(path)?;
    let size = metadata.len();
    let start = size.saturating_sub(LOG_MAX_TAIL_BYTES);
    let mut file = fs::File::open(path)?;
    file.seek(SeekFrom::Start(start))?;
    let mut data = Vec::new();
    file.read_to_end(&mut data)?;
    let mut lines = Vec::new();
    let mut cursor = 0usize;
    let mut offset = start;
    if start > 0 {
        let Some(index) = data.iter().position(|byte| *byte == b'\n') else {
            return Ok((Vec::new(), start, data.len() as u64));
        };
        cursor = index + 1;
        offset = start + cursor as u64;
    }
    while cursor < data.len() {
        let end = data[cursor..]
            .iter()
            .position(|byte| *byte == b'\n')
            .map(|index| cursor + index)
            .unwrap_or(data.len());
        let text = String::from_utf8_lossy(&data[cursor..end])
            .trim()
            .to_string();
        if !text.is_empty() {
            lines.push((offset, text));
        }
        cursor = end.saturating_add(1);
        offset = start + cursor as u64;
    }
    Ok((lines, start, data.len() as u64))
}

fn parse_log_row(offset: u64, text: &str) -> Value {
    let parsed = serde_json::from_str::<Value>(text).ok();
    let object = parsed.as_ref().and_then(Value::as_object);
    let event = object
        .and_then(|object| object.get("event"))
        .and_then(Value::as_str);
    let at = object
        .and_then(|object| object.get("time"))
        .and_then(|time| {
            time.as_i64().or_else(|| {
                time.as_str().and_then(|text| {
                    chrono::DateTime::parse_from_rfc3339(text)
                        .ok()
                        .map(|time| time.timestamp_millis())
                })
            })
        });
    let level = object
        .and_then(|object| object.get("level"))
        .and_then(Value::as_str)
        .map(|level| level.to_ascii_lowercase())
        .filter(|level| LOG_LEVELS.contains(&level.as_str()))
        .unwrap_or_else(|| {
            if event == Some("request_summary") {
                "error".to_string()
            } else if parsed.is_some() {
                "info".to_string()
            } else {
                "raw".to_string()
            }
        });
    let request_id = object
        .and_then(|object| object.get("request_id"))
        .and_then(Value::as_str)
        .map(str::to_string);
    let display = if event == Some("request_summary") {
        format!(
            "request_summary {} {} final={} tries={}",
            object
                .and_then(|object| object.get("request_id"))
                .and_then(Value::as_str)
                .unwrap_or("?"),
            object
                .and_then(|object| object.get("protocol"))
                .and_then(Value::as_str)
                .unwrap_or("?"),
            object
                .and_then(|object| object.get("final"))
                .and_then(Value::as_str)
                .unwrap_or("?"),
            object
                .and_then(|object| object.get("tries"))
                .and_then(Value::as_u64)
                .unwrap_or(0),
        )
    } else {
        redact_log_text(text)
    };
    json!({
        "key": offset.to_string(),
        "at": at,
        "level": level,
        "source": LOG_SOURCE,
        "text": display,
        "requestId": request_id,
    })
}

fn redact_log_text(text: &str) -> String {
    let mut output = String::new();
    let mut rest = text;
    loop {
        let lowered = rest.to_ascii_lowercase();
        let mut best: Option<(usize, usize)> = None;
        for marker in ["bearer ", "token=", "password=", "api_key=", "cookie="] {
            if let Some(index) = lowered.find(marker) {
                let end = index + marker.len();
                let value_end = rest[end..]
                    .find(char::is_whitespace)
                    .map(|offset| end + offset)
                    .unwrap_or(rest.len());
                if best.map_or(true, |(start, _)| index < start) {
                    best = Some((index, value_end));
                }
            }
        }
        let Some((start, end)) = best else {
            output.push_str(rest);
            break;
        };
        output.push_str(&rest[..start]);
        output.push_str("[redacted]");
        rest = &rest[end..];
    }
    output
        .chars()
        .filter(|character| !character.is_control() || *character == '\t')
        .collect()
}

/// The App's connection hints: the actual bound loopback port, the OpenAI
/// compatible Base URL and whether this instance requires a local API key.
pub fn connection(admin: &AdminState, require_local_api_key: bool) -> Result<Value, GatewayError> {
    let Some(addr) = admin.bound_addr() else {
        return Err(GatewayError::new(
            503,
            "The gateway has not confirmed its listening address",
            "connection_info_unavailable",
        )
        .with_detail(json!({"reason": "bound_address_unknown"})));
    };
    let port = addr.port();
    let base_url = format!("http://127.0.0.1:{port}/v1");
    let (auth, key_state) = if require_local_api_key {
        ("bearer", "required")
    } else {
        ("loopback_no_key", "not_required")
    };
    let mut hints = vec![
        "在客户端（例如 Magpie）里把上面的地址填成 OpenAI 兼容接口的 Base URL；模型 ID 用模型目录里的 ID。",
    ];
    if require_local_api_key {
        hints.push("本实例要求调用密钥；密钥来自 CHAT2API_LOCAL_API_KEY，读面不返回明文。");
    } else {
        hints.push("本机回环实例当前不要求调用密钥（CHAT2API_LOCAL_API_KEY 未设置）；这是运行实例自己的事实。");
    }
    Ok(json!({
        "ok": true,
        "service": "chat2api",
        "display_name": "UM 网关",
        "port": port,
        "health_path": "/health",
        "base_url": base_url,
        "protocol": "openai-compatible",
        "interface_type": "openai-compatible",
        "protocol_path": "/v1",
        "runtime_state": "running",
        "runtime_healthy": true,
        "auth": auth,
        "key_state": key_state,
        "key_plaintext": Value::Null,
        "key_source": "env:CHAT2API_LOCAL_API_KEY",
        "key_in_use": key_state,
        "hints": hints,
        "reason": "",
        "evidence": [
            "监听端口来自本进程实际绑定的回环地址",
            "接口类型与 Base URL 由网关自己的角色决定，不从显示名猜算",
        ],
        "checked_epoch": epoch_millis(),
    }))
}

/// The gateway has no maintenance-operation source: the 8001 console host owns
/// `console-state.sqlite3` and is not assembled inside this instance. The read
/// stays explicitly unavailable instead of returning an empty table.
pub fn maintenance() -> Value {
    json!({
        "status": "unavailable",
        "error": "maintenance_source_unavailable",
        "reason": "本机实例不运行 8001 控制台，没有 console-state.sqlite3 维护操作记录来源",
        "rows": [],
        "scope": "本机实例维护操作记录：不适用",
        "earlier": Value::Null,
        "coverage": {
            "status": "unavailable",
            "error": "maintenance_source_unavailable",
            "source": "console-state.sqlite3",
            "bounded": true,
            "note": "无来源不是合法空列表；不伪造空表",
        },
    })
}

/// The admin model catalog. The upstream `/api/models` read and its cache stay the
/// only source; virtual effort variants are folded back into `efforts` so the App
/// shows one row per base model.
///
/// `fresh=1` is the settings page's strict refresh: the read goes to the upstream
/// and fails with `models_unavailable` instead of serving the previous cache. The
/// plain read keeps the cache fallback, so the App can tell a strict refresh
/// failure from an ordinary read.
pub fn model_catalog(proxy: &ProxyState, raw_target: &str) -> Result<Value, GatewayError> {
    let query = query_pairs(raw_target)?;
    let fresh = match query_first(&query, "fresh") {
        None | Some("0") | Some("false") => false,
        Some("1") | Some("true") => true,
        Some(_) => return Err(GatewayError::invalid_request("fresh must be 1 or 0")),
    };
    let (payload, _marker) = proxy.models(fresh)?;
    let mut models = Vec::new();
    if let Some(items) = payload.get("data").and_then(Value::as_array) {
        for item in items {
            let Some(id) = item
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
            else {
                continue;
            };
            if is_virtual_model(id) {
                continue;
            }
            let name = item
                .get("name")
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
                .unwrap_or(id);
            let context = context_of(item);
            let (efforts, default_effort) = efforts_of(item, id);
            models.push(json!({
                "id": id,
                "name": name,
                "context": context,
                "context_known": context.is_some(),
                "efforts": efforts,
                "default_effort": default_effort,
                "last_test": proxy.admin_state().last_test(id),
            }));
        }
    }
    Ok(json!({
        "models": models,
        "source": "um",
        "fresh": fresh,
        "checked_epoch": epoch_now(),
        "note": "目录来自上游 /api/models 与网关缓存；缺失上下文或思考档位保持 null，不猜值",
    }))
}

fn is_virtual_model(model_id: &str) -> bool {
    ["-Fast", "-Low", "-Medium", "-High"]
        .iter()
        .any(|suffix| model_id.ends_with(suffix))
}

fn context_of(model: &Value) -> Option<i64> {
    for key in [
        "max_model_len",
        "context_length",
        "contextWindow",
        "context_window",
    ] {
        let Some(value) = model.get(key) else {
            continue;
        };
        let parsed = match value {
            Value::Number(number) => number.as_i64(),
            Value::String(text) => text.trim().parse::<i64>().ok(),
            _ => None,
        };
        if parsed.is_some_and(|value| value > 0) {
            return parsed;
        }
    }
    None
}

fn efforts_of(model: &Value, model_id: &str) -> (Value, Value) {
    let from_entry = model
        .get("efforts")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .filter(|effort| !effort.is_empty())
                .map(|effort| Value::String(effort.to_string()))
                .collect::<Vec<_>>()
        })
        .filter(|items: &Vec<Value>| !items.is_empty());
    if let Some(efforts) = from_entry {
        let default = model
            .get("default_effort")
            .and_then(Value::as_str)
            .filter(|effort| !effort.is_empty())
            .map(|effort| Value::String(effort.to_string()))
            .unwrap_or(Value::Null);
        return (Value::Array(efforts), default);
    }
    let (efforts, default) = known_efforts(model_id);
    (
        Value::Array(efforts.into_iter().map(Value::String).collect()),
        default
            .map(|effort| Value::String(effort.to_string()))
            .unwrap_or(Value::Null),
    )
}

fn known_efforts(model_id: &str) -> (Vec<String>, Option<String>) {
    let (efforts, default) = match model_id {
        "DeepSeek-V4-Flash-0731" => (&["low", "medium", "high", "max"][..], Some("high")),
        "GLM-5.2-NVFP4" => (
            &["low", "medium", "high", "xhigh", "max"][..],
            Some("medium"),
        ),
        "Qwen3.8-27B" => (&["low", "medium", "xhigh"][..], Some("xhigh")),
        "gpt-oss-120b" => (&["low", "medium", "high"][..], Some("medium")),
        _ => return (Vec::new(), None),
    };
    (
        efforts.iter().map(|effort| effort.to_string()).collect(),
        default.map(str::to_string),
    )
}

// The login rounds, logs, maintenance and connection handlers are added by the
// following slices.
