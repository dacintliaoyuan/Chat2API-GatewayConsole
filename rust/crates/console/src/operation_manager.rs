//! S6a: the console operation write face (`console/operations.py`).
//!
//! The Rust owner persists the SAME `console-state.sqlite3` schema 1 the Python
//! manager writes: one `console_meta` marker, one `operations` row per submit,
//! idempotency keys, per-resource generations and cooperative cancellation.
//! Unfinished rows are marked `interrupted` on start and never replayed.
//!
//! Action execution itself is an injected seam: the candidate never spawns a
//! process, never starts or stops the 8003 gateway and never fakes a privileged
//! success. An unwired executor answers `share_broker_unwired`.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OpenFlags, Row};
use serde_json::{json, Map, Value};

use crate::error::ConsoleError;
use crate::lock::{LockError, ResourceLock};

pub const CONSOLE_STATE_SCHEMA_VERSION: i64 = 1;
pub const MAX_PARAM_BYTES: usize = 4096;
pub const MAX_STEPS: usize = 32;
pub const MAX_STEP_NOTE_CHARS: usize = 200;
pub const MAX_ERROR_CHARS: usize = 200;
pub const DEFAULT_OPERATION_LIMIT: i64 = 50;
pub const MAX_OPERATION_LIMIT: i64 = 200;
pub const DEFAULT_ACQUIRE_TIMEOUT: Duration = Duration::from_secs(15);

const TERMINAL_STATES: [&str; 4] = ["succeeded", "failed", "cancelled", "interrupted"];

const CREATE_SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS console_meta(
 key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY,
 action TEXT NOT NULL,
 target TEXT NOT NULL DEFAULT '',
 params_json TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL,
 idempotency_key TEXT UNIQUE,
 resource TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL,
 started_at REAL,
 finished_at REAL,
 result_json TEXT,
 error_json TEXT,
 steps_json TEXT NOT NULL DEFAULT '[]',
 cancel_requested INTEGER NOT NULL DEFAULT 0);
";

#[derive(Debug, Clone, PartialEq)]
pub struct OperationError {
    pub code: String,
    pub message: String,
    pub reason: Option<String>,
}

impl OperationError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
            reason: None,
        }
    }

    pub fn with_reason(mut self, reason: impl Into<String>) -> Self {
        self.reason = Some(reason.into());
        self
    }
}

impl std::fmt::Display for OperationError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for OperationError {}

/// A registered-but-disabled action; never a simulated success.
pub fn action_disabled(action: &str, reason: &str) -> OperationError {
    OperationError::new(
        "action_disabled",
        format!("action {action} is not available: {reason}"),
    )
    .with_reason(reason)
}

/// The executor's structured failure, mirrored by the operation row's error.
#[derive(Debug, Clone, PartialEq)]
pub struct ActionFailure {
    pub code: String,
    pub message: String,
}

impl ActionFailure {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }

    pub fn cancelled() -> Self {
        Self::new("cancelled", "adapter honoured the cancel request")
    }
}

/// One whitelisted console action.
#[derive(Clone)]
pub struct ActionSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub param_schema: Value,
    pub bump_generation_on_success: bool,
    pub cancelable: bool,
    pub enabled: bool,
    pub disabled_reason: Option<&'static str>,
    resource: fn(&str, &Map<String, Value>) -> Result<String, OperationError>,
}

impl ActionSpec {
    fn resource(
        &self,
        target: &str,
        params: &Map<String, Value>,
    ) -> Result<String, OperationError> {
        (self.resource)(target, params)
    }
}

/// The cloud role registry: exactly the six UM share actions.
pub fn cloud_registry() -> Vec<ActionSpec> {
    vec![
        ActionSpec {
            name: "service.restart",
            description: "重启 UM 分享入口（停止后显式启动；中断在途工作）",
            param_schema: json!({}),
            bump_generation_on_success: true,
            cancelable: true,
            enabled: true,
            disabled_reason: None,
            resource: service_resource,
        },
        ActionSpec {
            name: "service.start",
            description: "启动 UM 分享入口（Linux pid 文件与真实进程身份）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: true,
            disabled_reason: None,
            resource: service_resource,
        },
        ActionSpec {
            name: "service.stop",
            description: "停止 UM 分享入口（先持久化停用意图，再发信号）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: true,
            disabled_reason: None,
            resource: service_resource,
        },
        ActionSpec {
            name: "share.diagnostics",
            description: "UM 分享分层诊断（只读；含维护视图与故障留证）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: true,
            disabled_reason: None,
            resource: share_resource,
        },
        ActionSpec {
            name: "share.gateway_restart",
            description: "重启分享入口 8100（单层：经受限 broker 宿主协议）",
            param_schema: json!({
                "restart_budget_s": {"type": "int", "min": 10, "max": 180},
            }),
            bump_generation_on_success: true,
            cancelable: true,
            enabled: true,
            disabled_reason: None,
            resource: share_resource,
        },
        ActionSpec {
            name: "share.status",
            description: "UM 分享分层状态观察（只读）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: true,
            disabled_reason: None,
            resource: share_resource,
        },
    ]
}

/// The honest reason carried by every local catalogue card whose executor is not
/// wired yet.
pub const LOCAL_DISABLED_ACTION_REASON: &str = "本机 Rust 控制台尚未接线该动作执行器（后续票）";

/// The local role registry: `model.catalog` is executable; the other catalogue
/// cards are registered but disabled, so Python's 403 `action_disabled` refusal is
/// preserved instead of degrading to 404 `invalid_action`. A disabled action never
/// executes and never creates an operation row.
pub fn local_registry() -> Vec<ActionSpec> {
    vec![
        ActionSpec {
            name: "service.start",
            description: "启动本机 UM runtime（经 runtime 权威）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: false,
            disabled_reason: Some(LOCAL_DISABLED_ACTION_REASON),
            resource: disabled_resource,
        },
        ActionSpec {
            name: "service.stop",
            description: "停止本机 UM runtime（经 runtime 权威）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: false,
            disabled_reason: Some(LOCAL_DISABLED_ACTION_REASON),
            resource: disabled_resource,
        },
        ActionSpec {
            name: "service.restart",
            description: "重启本机 UM runtime",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: false,
            disabled_reason: Some(LOCAL_DISABLED_ACTION_REASON),
            resource: disabled_resource,
        },
        ActionSpec {
            name: "account.um_status",
            description: "UM 认证状态查询（内存态只读）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: false,
            disabled_reason: Some(LOCAL_DISABLED_ACTION_REASON),
            resource: disabled_resource,
        },
        ActionSpec {
            name: "account.um_verify",
            description: "UM 账号主动核验",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: false,
            disabled_reason: Some(LOCAL_DISABLED_ACTION_REASON),
            resource: disabled_resource,
        },
        ActionSpec {
            name: "account.login",
            description: "账号登录/重新授权（UM 编排）",
            param_schema: json!({}),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: false,
            disabled_reason: Some(LOCAL_DISABLED_ACTION_REASON),
            resource: disabled_resource,
        },
        ActionSpec {
            name: "model.catalog",
            description: "模型目录只读查询（各来源 /v1/models；无探测、无计费、无启停）",
            param_schema: json!({
                "sources": {"type": "str", "max_len": 512},
                "fresh": {"type": "bool"},
            }),
            bump_generation_on_success: false,
            cancelable: true,
            enabled: true,
            disabled_reason: None,
            resource: model_catalog_resource,
        },
    ]
}

fn disabled_resource(
    _target: &str,
    _params: &Map<String, Value>,
) -> Result<String, OperationError> {
    Err(OperationError::new(
        "action_disabled",
        "action is disabled and cannot execute",
    ))
}

fn model_catalog_resource(
    _target: &str,
    _params: &Map<String, Value>,
) -> Result<String, OperationError> {
    Ok("model:catalog".to_string())
}

fn share_resource(_target: &str, _params: &Map<String, Value>) -> Result<String, OperationError> {
    Ok("share:orchestration".to_string())
}

fn service_resource(target: &str, _params: &Map<String, Value>) -> Result<String, OperationError> {
    if target != "share-gateway" {
        return Err(action_disabled(
            "service",
            "云端角色只装配 UM 分享：运行目标只有 share-gateway；其他本地服务不在本部署授权集合内",
        ));
    }
    Ok("share:orchestration".to_string())
}

/// The action-execution seam. `None` = production default = not wired.
pub trait ActionExecutor: Send + Sync {
    fn execute(
        &self,
        action: &str,
        target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure>;
}

/// Honest default: no privileged action is executed without an authorized
/// deployment package.
pub struct UnwiredActionExecutor;

impl ActionExecutor for UnwiredActionExecutor {
    fn execute(
        &self,
        _action: &str,
        _target: &str,
        _params: &Map<String, Value>,
        _ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        Err(ActionFailure::new(
            "share_broker_unwired",
            "broker 操作门未接线（部署授权包装配后可用）；不冒充可用",
        ))
    }
}

pub struct OperationContext {
    op_id: String,
    cancel: Arc<AtomicBool>,
    report: Arc<dyn Fn(String) + Send + Sync>,
}

impl OperationContext {
    pub fn op_id(&self) -> &str {
        &self.op_id
    }

    pub fn is_cancelled(&self) -> bool {
        self.cancel.load(Ordering::SeqCst)
    }

    /// `OperationContext.report_step`: a non-empty note is bounded to 200 chars.
    pub fn report_step(&self, note: &str) -> Result<(), OperationError> {
        let note: String = note.trim().chars().take(MAX_STEP_NOTE_CHARS).collect();
        if note.is_empty() {
            return Err(OperationError::new(
                "invalid_step",
                "step note must be a non-empty string",
            ));
        }
        (self.report)(note);
        Ok(())
    }

    pub fn raise_if_cancelled(&self) -> Result<(), ActionFailure> {
        if self.is_cancelled() {
            return Err(ActionFailure::cancelled());
        }
        Ok(())
    }
}

struct Inner {
    connection: Connection,
    generations: HashMap<String, i64>,
}

/// Lazy manager construction, like Python's `get_console_manager`: the
/// read-only faces never create the state database or mark rows interrupted.
pub struct OperationManagerSlot {
    inner: Mutex<Option<Arc<OperationManager>>>,
    db_path: Option<PathBuf>,
    lock_dir: Option<PathBuf>,
    executor: Arc<dyn ActionExecutor>,
    registry: Vec<ActionSpec>,
}

impl OperationManagerSlot {
    pub fn new(
        db_path: Option<PathBuf>,
        lock_dir: Option<PathBuf>,
        executor: Arc<dyn ActionExecutor>,
    ) -> Self {
        Self::with_registry(db_path, lock_dir, executor, cloud_registry())
    }

    pub fn with_registry(
        db_path: Option<PathBuf>,
        lock_dir: Option<PathBuf>,
        executor: Arc<dyn ActionExecutor>,
        registry: Vec<ActionSpec>,
    ) -> Self {
        Self {
            inner: Mutex::new(None),
            db_path,
            lock_dir,
            executor,
            registry,
        }
    }

    pub fn get(&self) -> Result<Arc<OperationManager>, ConsoleError> {
        let mut inner = self
            .inner
            .lock()
            .map_err(|_| ConsoleError::console_unavailable())?;
        if let Some(manager) = inner.as_ref() {
            return Ok(Arc::clone(manager));
        }
        let Some(path) = &self.db_path else {
            return Err(ConsoleError::console_unavailable());
        };
        let lock_dir = self.lock_dir.clone().unwrap_or_else(|| {
            path.parent()
                .filter(|parent| !parent.as_os_str().is_empty())
                .map(|parent| parent.join("console-locks"))
                .unwrap_or_else(|| path.with_extension("locks"))
        });
        let manager = Arc::new(OperationManager::new(
            path.clone(),
            lock_dir,
            self.registry.clone(),
            Arc::clone(&self.executor),
        )?);
        *inner = Some(Arc::clone(&manager));
        Ok(manager)
    }
}

pub struct OperationManager {
    db_path: PathBuf,
    lock_dir: PathBuf,
    acquire_timeout: Duration,
    registry: Vec<ActionSpec>,
    executor: Arc<dyn ActionExecutor>,
    inner: Mutex<Inner>,
    cancel_events: Mutex<HashMap<String, Arc<AtomicBool>>>,
}

impl OperationManager {
    pub fn new(
        db_path: PathBuf,
        lock_dir: PathBuf,
        registry: Vec<ActionSpec>,
        executor: Arc<dyn ActionExecutor>,
    ) -> Result<Self, ConsoleError> {
        Self::with_timeout(
            db_path,
            lock_dir,
            registry,
            executor,
            DEFAULT_ACQUIRE_TIMEOUT,
        )
    }

    pub fn with_timeout(
        db_path: PathBuf,
        lock_dir: PathBuf,
        registry: Vec<ActionSpec>,
        executor: Arc<dyn ActionExecutor>,
        acquire_timeout: Duration,
    ) -> Result<Self, ConsoleError> {
        if let Some(parent) = db_path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|_| ConsoleError::console_unavailable())?;
            }
        }
        std::fs::create_dir_all(&lock_dir).map_err(|_| ConsoleError::console_unavailable())?;
        let connection =
            Connection::open(&db_path).map_err(|_| ConsoleError::console_unavailable())?;
        initialize_schema(&connection)?;
        mark_interrupted(&connection)?;
        Ok(Self {
            db_path,
            lock_dir,
            acquire_timeout,
            registry,
            executor,
            inner: Mutex::new(Inner {
                connection,
                generations: HashMap::new(),
            }),
            cancel_events: Mutex::new(HashMap::new()),
        })
    }

    pub fn db_path(&self) -> &PathBuf {
        &self.db_path
    }

    pub fn action_info(&self) -> Value {
        let mut actions: Vec<Value> = self
            .registry
            .iter()
            .map(|spec| {
                json!({
                    "action": spec.name,
                    "enabled": spec.enabled,
                    "disabled_reason": spec.disabled_reason,
                    "cancelable": spec.cancelable,
                    "description": spec.description,
                    "param_schema": spec.param_schema,
                })
            })
            .collect();
        actions.sort_by(|left, right| {
            left["action"]
                .as_str()
                .unwrap_or_default()
                .cmp(right["action"].as_str().unwrap_or_default())
        });
        json!({"actions": actions})
    }

    pub fn list(&self, limit: i64) -> Result<Value, ConsoleError> {
        let limit = limit.clamp(1, MAX_OPERATION_LIMIT);
        let inner = self
            .inner
            .lock()
            .map_err(|_| ConsoleError::console_unavailable())?;
        let mut statement = inner
            .connection
            .prepare("SELECT * FROM operations ORDER BY created_at DESC, id DESC LIMIT ?")
            .map_err(|_| ConsoleError::console_unavailable())?;
        let rows = statement
            .query_map([limit], crate::operations::operation_view)
            .map_err(|_| ConsoleError::console_unavailable())?;
        let mut operations = Vec::new();
        for row in rows {
            operations.push(row.map_err(|_| ConsoleError::console_unavailable())?);
        }
        Ok(json!({"operations": operations}))
    }

    pub fn get(&self, operation_id: &str) -> Result<Option<Value>, ConsoleError> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| ConsoleError::console_unavailable())?;
        let mut statement = inner
            .connection
            .prepare("SELECT * FROM operations WHERE id=?")
            .map_err(|_| ConsoleError::console_unavailable())?;
        let mut rows = statement
            .query([operation_id])
            .map_err(|_| ConsoleError::console_unavailable())?;
        match rows
            .next()
            .map_err(|_| ConsoleError::console_unavailable())?
        {
            Some(row) => Ok(Some(
                crate::operations::operation_view(row)
                    .map_err(|_| ConsoleError::console_unavailable())?,
            )),
            None => Ok(None),
        }
    }

    /// Submit one action. `Ok((deduplicated, view))` maps to 201/200.
    pub fn submit(
        self: &Arc<Self>,
        action: &str,
        target: &str,
        params: Option<&Map<String, Value>>,
        idempotency_key: Option<&str>,
    ) -> Result<(bool, Value), OperationError> {
        let spec = self
            .registry
            .iter()
            .find(|spec| spec.name == action)
            .ok_or_else(|| {
                OperationError::new("invalid_action", format!("unknown action {action:?}"))
            })?
            .clone();
        if !spec.enabled {
            return Err(action_disabled(
                spec.name,
                spec.disabled_reason.unwrap_or("action is disabled"),
            ));
        }
        if let Some(idempotency_key) = idempotency_key {
            if !(1..=128).contains(&idempotency_key.chars().count()) {
                return Err(OperationError::new(
                    "invalid_idempotency_key",
                    "idempotency_key must be 1-128 chars",
                ));
            }
        }
        let target = validate_target(&spec, target)?;
        let params = validate_params(&spec, params)?;
        let resource = spec.resource(&target, &params)?;
        let params_json = serde_json::to_string(&params).unwrap_or_else(|_| "{}".to_string());
        let op_id = random_id();
        let inner = self
            .inner
            .lock()
            .map_err(|_| OperationError::new("internal_error", "console state lock poisoned"))?;
        if let Some(idempotency_key) = idempotency_key {
            if let Some((existing, stored_params)) =
                find_by_idempotency(&inner.connection, idempotency_key)?
            {
                if existing["action"].as_str() != Some(action)
                    || existing["target"].as_str() != Some(target.as_str())
                    || !same_params(&stored_params, &params)
                {
                    return Err(OperationError::new(
                        "idempotency_key_conflict",
                        "idempotency key already used for a different action/target/params",
                    ));
                }
                let mut view = existing;
                view["deduplicated"] = json!(true);
                return Ok((true, view));
            }
        }
        let generation = inner.generations.get(&resource).copied().unwrap_or(0);
        inner
            .connection
            .execute(
                "INSERT INTO operations(id, action, target, params_json, state, idempotency_key, resource, generation, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                rusqlite::params![
                    op_id,
                    action,
                    target,
                    params_json,
                    "queued",
                    idempotency_key,
                    resource,
                    generation,
                    epoch_seconds(),
                ],
            )
            .map_err(|_| OperationError::new("internal_error", "console state write failed"))?;
        drop(inner);
        let cancel = Arc::new(AtomicBool::new(false));
        self.cancel_events
            .lock()
            .map_err(|_| OperationError::new("internal_error", "console state lock poisoned"))?
            .insert(op_id.clone(), Arc::clone(&cancel));
        let manager = Arc::clone(self);
        let worker_id = op_id.clone();
        thread::spawn(move || manager.run_worker(worker_id, cancel));
        let view = self
            .get(&op_id)
            .map_err(|_| OperationError::new("internal_error", "console state read failed"))?
            .unwrap_or(Value::Null);
        Ok((false, view))
    }

    pub fn cancel(&self, operation_id: &str) -> Result<Value, OperationError> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| OperationError::new("internal_error", "console state lock poisoned"))?;
        let row = load_row(&inner.connection, operation_id)?.ok_or_else(|| {
            OperationError::new(
                "operation_not_found",
                format!("no operation {operation_id:?}"),
            )
        })?;
        let state = row["state"].as_str().unwrap_or_default();
        if TERMINAL_STATES.contains(&state) {
            return Err(OperationError::new(
                "operation_already_finished",
                format!("operation already finished ({state})"),
            ));
        }
        inner
            .connection
            .execute(
                "UPDATE operations SET cancel_requested=1 WHERE id=?",
                [operation_id],
            )
            .map_err(|_| OperationError::new("internal_error", "console state write failed"))?;
        if state == "queued" {
            finish_locked(
                &inner.connection,
                operation_id,
                "cancelled",
                None,
                Some(json!({
                    "code": "cancelled",
                    "message": "cancelled while queued; never executed",
                })),
            )?;
        }
        if let Ok(events) = self.cancel_events.lock() {
            if let Some(event) = events.get(operation_id) {
                event.store(true, Ordering::SeqCst);
            }
        }
        drop(inner);
        self.get(operation_id)
            .map_err(|_| OperationError::new("internal_error", "console state read failed"))?
            .ok_or_else(|| {
                OperationError::new(
                    "operation_not_found",
                    format!("no operation {operation_id:?}"),
                )
            })
    }

    fn run_worker(self: Arc<Self>, op_id: String, cancel: Arc<AtomicBool>) {
        let result = self.execute_worker(&op_id, &cancel);
        if let Err(failure) = result {
            let _ = self.finish(
                &op_id,
                "failed",
                None,
                Some(json!({"code": failure.code, "message": single_line(&failure.message)})),
            );
        }
        if let Ok(mut events) = self.cancel_events.lock() {
            events.remove(&op_id);
        }
    }

    fn execute_worker(
        self: &Arc<Self>,
        op_id: &str,
        cancel: &Arc<AtomicBool>,
    ) -> Result<(), ActionFailure> {
        let (row, spec) = {
            let inner = self
                .inner
                .lock()
                .map_err(|_| ActionFailure::new("internal_error", "console state lock poisoned"))?;
            let row = load_row(&inner.connection, op_id)
                .map_err(|_| ActionFailure::new("internal_error", "console state read failed"))?
                .ok_or_else(|| ActionFailure::new("internal_error", "operation row missing"))?;
            if row["state"].as_str() != Some("queued") {
                return Ok(());
            }
            let action = row["action"].as_str().unwrap_or_default();
            let spec = self
                .registry
                .iter()
                .find(|spec| spec.name == action)
                .cloned()
                .ok_or_else(|| ActionFailure::new("invalid_action", "unknown action"))?;
            (row, spec)
        };
        let target = row["target"].as_str().unwrap_or_default().to_string();
        let params: Map<String, Value> = row["params_json"]
            .as_str()
            .and_then(|text| serde_json::from_str(text).ok())
            .unwrap_or_default();
        let lock = match ResourceLock::acquire(
            &self.lock_dir,
            row["resource"].as_str().unwrap_or_default(),
            &format!("console-op:{op_id}"),
            self.acquire_timeout,
            cancel,
        ) {
            Ok(lock) => lock,
            Err(LockError::Cancelled) => {
                return self.finish_cancelled(op_id, "cancelled before execution");
            }
            Err(LockError::Busy) => {
                return Err(ActionFailure::new(
                    "resource_busy_timeout",
                    format!(
                        "resource lock wait exceeded {:.1}s",
                        self.acquire_timeout.as_secs_f64()
                    ),
                ))
            }
            Err(LockError::Open(message)) => {
                return Err(ActionFailure::new("lock_open_failed", message))
            }
        };
        let moved = {
            let inner = self
                .inner
                .lock()
                .map_err(|_| ActionFailure::new("internal_error", "console state lock poisoned"))?;
            inner
                .connection
                .execute(
                    "UPDATE operations SET state=?, started_at=COALESCE(?, started_at) WHERE id=? AND state IN ('queued','running')",
                    rusqlite::params!["running", epoch_seconds(), op_id],
                )
                .map_err(|_| ActionFailure::new("internal_error", "console state write failed"))?
                == 1
        };
        if !moved {
            drop(lock);
            return Ok(());
        }
        let manager = Arc::clone(self);
        let report_id = op_id.to_string();
        let report = Arc::new(move |note: String| {
            let _ = manager.add_step(&report_id, &note);
        });
        let ctx = OperationContext {
            op_id: op_id.to_string(),
            cancel: Arc::clone(cancel),
            report,
        };
        let outcome = self.executor.execute(spec.name, &target, &params, &ctx);
        drop(lock);
        match outcome {
            Ok(value) => {
                self.complete(op_id, &spec, value);
                Ok(())
            }
            Err(failure) if failure.code == "cancelled" => {
                self.finish_cancelled(op_id, "adapter honoured the cancel request")
            }
            Err(failure) => Err(failure),
        }
    }

    fn complete(&self, op_id: &str, spec: &ActionSpec, value: Value) {
        let Ok(mut inner) = self.inner.lock() else {
            return;
        };
        let Ok(Some(row)) = load_row(&inner.connection, op_id) else {
            return;
        };
        if row["state"].as_str() != Some("running") {
            return;
        }
        let resource = row["resource"].as_str().unwrap_or_default().to_string();
        let generation = row["generation"].as_i64().unwrap_or_default();
        if generation != inner.generations.get(&resource).copied().unwrap_or(0) {
            let _ = finish_locked(
                &inner.connection,
                op_id,
                "cancelled",
                None,
                Some(json!({
                    "code": "superseded",
                    "message": "resource changed; late result discarded",
                })),
            );
            return;
        }
        let result = match value {
            Value::Object(_) => value,
            other => json!({"value": other}),
        };
        if row["cancel_requested"].as_i64().unwrap_or_default() != 0 {
            let _ = add_step_locked(
                &inner.connection,
                op_id,
                "cancel was requested but the operation finished",
            );
        }
        let _ = finish_locked(&inner.connection, op_id, "succeeded", Some(result), None);
        if spec.bump_generation_on_success {
            let next = inner.generations.get(&resource).copied().unwrap_or(0) + 1;
            inner.generations.insert(resource.clone(), next);
            let _ = inner.connection.execute(
                "UPDATE operations SET state='cancelled', finished_at=?, error_json=? WHERE state='queued' AND resource=? AND generation<?",
                rusqlite::params![
                    epoch_seconds(),
                    json!({
                        "code": "superseded",
                        "message": "superseded by a newer operation on the same resource",
                    })
                    .to_string(),
                    resource,
                    next,
                ],
            );
        }
    }

    fn finish_cancelled(&self, op_id: &str, message: &str) -> Result<(), ActionFailure> {
        self.finish(
            op_id,
            "cancelled",
            None,
            Some(json!({"code": "cancelled", "message": message})),
        )
        .map(|_| ())
    }

    fn finish(
        &self,
        op_id: &str,
        state: &str,
        result: Option<Value>,
        error: Option<Value>,
    ) -> Result<bool, ActionFailure> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| ActionFailure::new("internal_error", "console state lock poisoned"))?;
        finish_locked(&inner.connection, op_id, state, result, error)
            .map_err(|_| ActionFailure::new("internal_error", "console state write failed"))
    }

    fn add_step(&self, op_id: &str, note: &str) -> Result<(), OperationError> {
        let inner = self
            .inner
            .lock()
            .map_err(|_| OperationError::new("internal_error", "console state lock poisoned"))?;
        add_step_locked(&inner.connection, op_id, note)
            .map_err(|_| OperationError::new("internal_error", "console state write failed"))
    }

    pub fn close(self) {}
}

fn initialize_schema(connection: &Connection) -> Result<(), ConsoleError> {
    let tables: Vec<String> = {
        let mut statement = connection
            .prepare("SELECT name FROM sqlite_master WHERE type='table'")
            .map_err(|_| ConsoleError::console_unavailable())?;
        let rows = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|_| ConsoleError::console_unavailable())?;
        rows.collect::<Result<_, _>>()
            .map_err(|_| ConsoleError::console_unavailable())?
    };
    if !tables.iter().any(|table| table == "console_meta") {
        if tables.iter().any(|table| table != "operations") {
            return Err(ConsoleError::console_unavailable());
        }
        connection
            .execute_batch(CREATE_SCHEMA)
            .map_err(|_| ConsoleError::console_unavailable())?;
        connection
            .execute(
                "INSERT OR REPLACE INTO console_meta(key, value) VALUES('schema_version', ?)",
                [CONSOLE_STATE_SCHEMA_VERSION.to_string()],
            )
            .map_err(|_| ConsoleError::console_unavailable())?;
        return Ok(());
    }
    let version: Option<String> = connection
        .query_row(
            "SELECT value FROM console_meta WHERE key='schema_version'",
            [],
            |row| row.get(0),
        )
        .map_err(|_| ConsoleError::console_unavailable())?;
    if version
        .as_deref()
        .and_then(|value| value.parse::<i64>().ok())
        != Some(CONSOLE_STATE_SCHEMA_VERSION)
    {
        return Err(ConsoleError::console_unavailable());
    }
    connection
        .execute_batch(CREATE_SCHEMA)
        .map_err(|_| ConsoleError::console_unavailable())?;
    Ok(())
}

fn mark_interrupted(connection: &Connection) -> Result<(), ConsoleError> {
    connection
        .execute(
            "UPDATE operations SET state='interrupted', finished_at=?, error_json=? WHERE state IN ('queued','running')",
            rusqlite::params![
                epoch_seconds(),
                json!({
                    "code": "interrupted_by_restart",
                    "message": "service restart interrupted the operation",
                })
                .to_string(),
            ],
        )
        .map_err(|_| ConsoleError::console_unavailable())?;
    Ok(())
}

fn validate_target(spec: &ActionSpec, target: &str) -> Result<String, OperationError> {
    let _ = spec;
    if target.chars().count() > 64 {
        return Err(OperationError::new(
            "invalid_target",
            "target must be 1-64 chars",
        ));
    }
    if target.is_empty() {
        return Err(OperationError::new(
            "invalid_target",
            "target must be 1-64 chars",
        ));
    }
    Ok(target.to_string())
}

fn validate_params(
    spec: &ActionSpec,
    params: Option<&Map<String, Value>>,
) -> Result<Map<String, Value>, OperationError> {
    let params = params.cloned().unwrap_or_default();
    let raw = serde_json::to_string(&params)
        .map_err(|_| OperationError::new("invalid_params", "params must be finite JSON"))?;
    if raw.len() > MAX_PARAM_BYTES {
        return Err(OperationError::new("invalid_params", "params too large"));
    }
    let schema = spec.param_schema.as_object().cloned().unwrap_or_default();
    let mut unknown: Vec<&String> = params
        .keys()
        .filter(|key| !schema.contains_key(*key))
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(OperationError::new(
            "invalid_params",
            format!("unknown param(s): {unknown:?}"),
        ));
    }
    for (key, value) in &params {
        let rule = schema.get(key).and_then(Value::as_object);
        if value.is_null() && rule.and_then(|rule| rule.get("nullable")) == Some(&json!(true)) {
            continue;
        }
        let expected = rule
            .and_then(|rule| rule.get("type"))
            .and_then(Value::as_str);
        match expected {
            Some("str") => {
                let Some(text) = value.as_str() else {
                    return Err(OperationError::new(
                        "invalid_params",
                        format!("{key} must be a string"),
                    ));
                };
                let max_len = rule
                    .and_then(|rule| rule.get("max_len"))
                    .and_then(Value::as_i64)
                    .unwrap_or(128);
                if text.chars().count() as i64 > max_len {
                    return Err(OperationError::new(
                        "invalid_params",
                        format!("{key} too long"),
                    ));
                }
            }
            Some("int") => {
                let Some(number) = value.as_i64().filter(|_| !value.is_boolean()) else {
                    return Err(OperationError::new(
                        "invalid_params",
                        format!("{key} must be an int"),
                    ));
                };
                if let Some(min) = rule
                    .and_then(|rule| rule.get("min"))
                    .and_then(Value::as_i64)
                {
                    if number < min {
                        return Err(OperationError::new(
                            "invalid_params",
                            format!("{key} out of range"),
                        ));
                    }
                }
                if let Some(max) = rule
                    .and_then(|rule| rule.get("max"))
                    .and_then(Value::as_i64)
                {
                    if number > max {
                        return Err(OperationError::new(
                            "invalid_params",
                            format!("{key} out of range"),
                        ));
                    }
                }
            }
            Some("bool") => {
                if !value.is_boolean() {
                    return Err(OperationError::new(
                        "invalid_params",
                        format!("{key} must be a bool"),
                    ));
                }
            }
            Some("object") => {
                if !value.is_object() {
                    return Err(OperationError::new(
                        "invalid_params",
                        format!("{key} must be an object"),
                    ));
                }
            }
            _ => {
                return Err(OperationError::new(
                    "invalid_params",
                    format!("invalid rule for {key}"),
                ))
            }
        }
    }
    Ok(params)
}

fn same_params(stored_json: &str, submitted: &Map<String, Value>) -> bool {
    fn canonical(value: &Value) -> Option<Value> {
        match value {
            Value::Object(map) => {
                let mut output = Map::new();
                for (key, value) in map {
                    output.insert(key.clone(), canonical(value)?);
                }
                Some(Value::Object(output))
            }
            Value::Array(items) => Some(Value::Array(
                items.iter().map(canonical).collect::<Option<Vec<_>>>()?,
            )),
            Value::Number(number) if !number.as_f64().map(f64::is_finite).unwrap_or(false) => None,
            other => Some(other.clone()),
        }
    }
    let stored = serde_json::from_str::<Value>(stored_json)
        .ok()
        .and_then(|value| canonical(&value))
        .filter(Value::is_object);
    let submitted = canonical(&Value::Object(submitted.clone()));
    stored.is_some() && submitted.is_some() && stored == submitted
}

fn find_by_idempotency(
    connection: &Connection,
    key: &str,
) -> Result<Option<(Value, String)>, OperationError> {
    let mut statement = connection
        .prepare("SELECT * FROM operations WHERE idempotency_key=?")
        .map_err(|_| OperationError::new("internal_error", "console state read failed"))?;
    let mut rows = statement
        .query([key])
        .map_err(|_| OperationError::new("internal_error", "console state read failed"))?;
    match rows
        .next()
        .map_err(|_| OperationError::new("internal_error", "console state read failed"))?
    {
        Some(row) => {
            let view = crate::operations::operation_view(row)
                .map_err(|_| OperationError::new("internal_error", "console state read failed"))?;
            let params = row
                .get::<_, Option<String>>("params_json")
                .ok()
                .flatten()
                .unwrap_or_default();
            Ok(Some((view, params)))
        }
        None => Ok(None),
    }
}

fn load_row(connection: &Connection, op_id: &str) -> Result<Option<Value>, OperationError> {
    let mut statement = connection
        .prepare("SELECT * FROM operations WHERE id=?")
        .map_err(|_| OperationError::new("internal_error", "console state read failed"))?;
    let mut rows = statement
        .query([op_id])
        .map_err(|_| OperationError::new("internal_error", "console state read failed"))?;
    match rows
        .next()
        .map_err(|_| OperationError::new("internal_error", "console state read failed"))?
    {
        Some(row) => Ok(Some(crate::operations::operation_view(row).map_err(
            |_| OperationError::new("internal_error", "console state read failed"),
        )?)),
        None => Ok(None),
    }
}

fn finish_locked(
    connection: &Connection,
    op_id: &str,
    state: &str,
    result: Option<Value>,
    error: Option<Value>,
) -> Result<bool, OperationError> {
    let changed = connection
        .execute(
            "UPDATE operations SET state=?, result_json=?, error_json=?, finished_at=? WHERE id=? AND state IN ('queued','running')",
            rusqlite::params![
                state,
                result.map(|value| value.to_string()),
                error.map(|value| value.to_string()),
                epoch_seconds(),
                op_id,
            ],
        )
        .map_err(|_| OperationError::new("internal_error", "console state write failed"))?;
    Ok(changed == 1)
}

fn add_step_locked(connection: &Connection, op_id: &str, note: &str) -> Result<(), OperationError> {
    let steps_json: Option<String> = connection
        .query_row(
            "SELECT steps_json FROM operations WHERE id=?",
            [op_id],
            |row| row.get(0),
        )
        .map_err(|_| OperationError::new("internal_error", "console state read failed"))?;
    let mut steps: Vec<Value> = steps_json
        .as_deref()
        .and_then(|text| serde_json::from_str(text).ok())
        .unwrap_or_default();
    steps.push(json!({"at": epoch_seconds(), "note": note}));
    if steps.len() > MAX_STEPS {
        steps = steps.split_off(steps.len() - MAX_STEPS);
    }
    connection
        .execute(
            "UPDATE operations SET steps_json=? WHERE id=?",
            rusqlite::params![Value::Array(steps).to_string(), op_id],
        )
        .map_err(|_| OperationError::new("internal_error", "console state write failed"))?;
    Ok(())
}

fn epoch_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or_default()
}

fn single_line(text: &str) -> String {
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    text.chars().take(MAX_ERROR_CHARS).collect()
}

fn random_id() -> String {
    let mut bytes = [0u8; 16];
    if getrandom::getrandom(&mut bytes).is_err() {
        // Never reuse an id when entropy is unavailable.
        panic!("console operation id cannot read OS entropy");
    }
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Test seam: one row's Python `_view` map.
pub fn operation_row_view_for_tests(row: &Row<'_>) -> rusqlite::Result<Value> {
    crate::operations::operation_view(row)
}

/// Test seam: read one operation row with the manager's own view.
pub fn read_operation_for_tests(
    manager: &OperationManager,
    op_id: &str,
) -> Result<Option<Value>, OperationError> {
    load_row(
        &manager
            .inner
            .lock()
            .map_err(|_| OperationError::new("internal_error", "lock poisoned"))?
            .connection,
        op_id,
    )
}

/// Test seam: the lock directory the manager uses.
pub fn lock_dir_for_tests(manager: &OperationManager) -> &std::path::Path {
    &manager.lock_dir
}

/// Test seam: `OpenFlags`-free existence probe for a row.
pub fn operation_exists_for_tests(manager: &OperationManager, op_id: &str) -> bool {
    load_row(
        &manager.inner.lock().expect("manager lock").connection,
        op_id,
    )
    .ok()
    .flatten()
    .is_some()
}

/// Test seam: open a second read-only handle for the same state file.
pub fn open_readonly_for_tests(path: &std::path::Path) -> rusqlite::Result<Connection> {
    Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
}
