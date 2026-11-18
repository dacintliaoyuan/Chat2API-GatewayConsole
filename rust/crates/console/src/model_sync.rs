//! #94: model-sync actions executed directly by the Rust console.
//!
//! The local 8001 console used to run `model.catalog` and the five
//! `model.context_*` actions through one-shot Python sidecars. This module owns
//! the same retained model-sync surfaces in-process:
//!
//! * catalogue reads against the fixed local gateway endpoint (normal and strict
//!   raw-fresh), with the same serialized shape and warning semantics;
//! * the declared-context probe queue preview and billed run, including budgets,
//!   cancellation, recovery evidence and generation archival;
//! * manual context overrides stored in the SAME `model-sync-state.json` under the
//!   SAME lock file and transactional replace Python `model_sync` uses.
//!
//! The state file format is shared: an existing Python state (or a Rust-written
//! state) is read and migrated idempotently. No Python process is started.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use crate::config::ConsoleConfig;
use crate::operation_manager::{ActionExecutor, ActionFailure, OperationContext};

pub const ACTIVE_SOURCES: [&str; 1] = ["um"];
pub const KNOWN_SOURCES: [&str; 5] = ["um", "m365", "qoder_cn", "qoder_global", "cliproxy"];
pub const PAUSED_SOURCES: [&str; 4] = ["m365", "qoder_cn", "qoder_global", "cliproxy"];
pub const PROBE_INPUT_STEPS: [i64; 4] = [4000, 16000, 64000, 256000];
pub const CONTEXT_PROBE_MAX_TOKENS: i64 = 64;
pub const PROBE_SCHEME_REVISION: i64 = 1;
pub const PROBE_HISTORY_MAX: usize = 20;
pub const CONTEXT_OVERRIDE_MAX: i64 = 10_000_000;
pub const STATE_VERSION: i64 = 4;

pub const DECLARED_CONTEXT_UNKNOWN_NOTE: &str = "声明未知，探测仍可测量实际可用长度";
pub const COST_NOTE: &str = "未定价/无法核算：本地网关无计价依据，不编造金额";
pub const OVERRIDE_APPLY_NOTE: &str =
    "覆盖已保存到 model-sync 状态；客户端同步已退役，覆盖保存≠客户端已应用";
pub const OVERRIDES_NOTE: &str =
    "只读读面：declared=目录声明、override=已保存覆盖、effective=有效值；覆盖保存≠客户端已应用";

const PROBE_PROGRESS_MAX: usize = 20;
const PROBE_RESULT_MAX_ROWS: usize = 100;
const PROBE_RECOVERY_MAX_RECORDS: usize = 100;
const ERROR_LIMIT: usize = 200;

const UM_MODELS_FRESH_HEADER: &str = "X-Chat2Api-Models";
const UM_MODELS_FRESH_VALUE: &str = "raw-fresh";
const UM_MODELS_FRESH_MARKER: &str = "X-Chat2Api-Raw-Models";
const UM_MODELS_FRESH_MARKER_VALUE: &str = "1";

const CATALOG_TIMEOUT: Duration = Duration::from_secs(10);
const PROBE_TIMEOUT: Duration = Duration::from_secs(90);

const HTTP_HEADER_MAX: usize = 64 * 1024;
const HTTP_BODY_MAX: usize = 64 * 1024 * 1024;

const AUTH_PAGE_MARKERS: [&str; 10] = [
    "login",
    "sign in",
    "signin",
    "password",
    "unauthorized",
    "forbidden",
    "auth required",
    "authentication required",
    "登录",
    "认证",
];

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

const RENAME_RETRY_DELAYS_MS: [u64; 6] = [25, 50, 100, 200, 400, 800];

/// One normalized catalogue row (`model_sync.sync_core.CatalogModel`).
#[derive(Debug, Clone, PartialEq)]
pub struct CatalogModel {
    pub source: String,
    pub model_id: String,
    pub context: Option<i64>,
    pub efforts: Vec<String>,
    pub default_effort: Option<String>,
    pub publish: bool,
    pub display_name: Option<String>,
    pub efforts_source: Option<String>,
}

impl CatalogModel {
    fn new(source: &str, model_id: &str) -> Self {
        Self {
            source: source.to_string(),
            model_id: model_id.to_string(),
            context: None,
            efforts: Vec::new(),
            default_effort: None,
            publish: true,
            display_name: None,
            efforts_source: None,
        }
    }
}

/// Normalized source catalogues: source -> models.
pub type Catalogs = BTreeMap<String, Vec<CatalogModel>>;
/// A validated probe selection request: source -> all queued models (`None`)
/// or an explicit model-id set.
pub type ProbeSelectionRequest = BTreeMap<String, Option<BTreeSet<String>>>;
/// A resolved probe selection: source -> model-id set.
pub type ProbeSelection = BTreeMap<String, BTreeSet<String>>;

/// The local model-sync layout, derived from the console config like Python
/// `model_sync.sync` derives it from the repo root.
#[derive(Debug, Clone)]
pub struct ModelSyncPaths {
    pub data_dir: PathBuf,
    pub state_file: PathBuf,
    pub backup_dir: PathBuf,
    pub lock_file: PathBuf,
    pub recovery_file: PathBuf,
    pub gateway_port: u16,
}

impl ModelSyncPaths {
    pub fn from_config(config: &ConsoleConfig) -> Self {
        let data_dir = config.repo_root.join("data");
        Self {
            state_file: data_dir.join("model-sync-state.json"),
            backup_dir: data_dir.join("model-sync-backups"),
            lock_file: data_dir.join("model-sync.lock"),
            recovery_file: data_dir.join("model-sync-probe-recovery.json"),
            data_dir,
            gateway_port: config.gateway_port,
        }
    }
}

/// Why one model-sync action failed. `code` is the console operation error code;
/// `message` is the single-line reason recorded on the operation row.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelSyncError {
    pub code: String,
    pub message: String,
}

impl ModelSyncError {
    fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: single_line(&message.into()),
        }
    }

    fn invalid(message: impl Into<String>) -> Self {
        Self::new("invalid_params", message)
    }
}

impl From<ModelSyncError> for ActionFailure {
    fn from(error: ModelSyncError) -> Self {
        ActionFailure::new(&error.code, error.message)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum HttpFailure {
    Timeout,
    ConnectionError,
    ProtocolError,
    Cancelled,
}

impl HttpFailure {
    fn name(self) -> &'static str {
        match self {
            Self::Timeout => "Timeout",
            Self::ConnectionError => "ConnectionError",
            Self::ProtocolError => "ProtocolError",
            Self::Cancelled => "Cancelled",
        }
    }
}

struct HttpResponse {
    status: u16,
    headers: HashMap<String, String>,
    body: Vec<u8>,
}

/// The in-process executor for the retained model-sync actions.
pub struct ModelSyncExecutor {
    paths: ModelSyncPaths,
    last_fresh: Mutex<Option<FreshCache>>,
}

#[derive(Debug, Clone)]
struct FreshCache {
    collected_at: Option<String>,
    checked_epoch: i64,
    models: Vec<CatalogModel>,
}

impl ModelSyncExecutor {
    pub fn new(paths: ModelSyncPaths) -> Self {
        Self {
            paths,
            last_fresh: Mutex::new(None),
        }
    }

    pub fn from_config(config: &ConsoleConfig) -> Self {
        Self::new(ModelSyncPaths::from_config(config))
    }

    pub fn paths(&self) -> &ModelSyncPaths {
        &self.paths
    }
}

impl ActionExecutor for ModelSyncExecutor {
    fn execute(
        &self,
        action: &str,
        _target: &str,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        match action {
            "model.catalog" => self.model_catalog(params, ctx),
            "model.context_overrides" => self.model_context_overrides(params, ctx),
            "model.context_probe_preview" => self.model_context_probe_preview(params, ctx),
            "model.context_probe_run" => self.model_context_probe_run(params, ctx),
            "model.context_override_set" => self.model_context_override_set(params, ctx),
            "model.context_override_clear" => self.model_context_override_clear(params, ctx),
            _ => Err(ActionFailure::new(
                "invalid_action",
                format!("unknown model-sync action {action:?}"),
            )),
        }
    }
}

impl ModelSyncExecutor {
    fn model_catalog(
        &self,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.raise_if_cancelled()?;
        let unknown: Vec<&String> = params
            .keys()
            .filter(|key| !matches!(key.as_str(), "sources" | "fresh"))
            .collect();
        if !unknown.is_empty() {
            let mut unknown = unknown;
            unknown.sort();
            return Err(ModelSyncError::invalid(format!("未知参数：{}", join(&unknown, ","))).into());
        }
        let fresh = match params.get("fresh") {
            None => false,
            Some(Value::Bool(value)) => *value,
            Some(_) => return Err(ModelSyncError::invalid("fresh 必须为布尔值").into()),
        };
        let sources = match params.get("sources") {
            None | Some(Value::Null) => None,
            Some(value) => {
                let sources = scope_str_list(value, "sources")?;
                if sources.is_empty() {
                    return Err(ModelSyncError::invalid(
                        "sources 不能为空列表；全部模型请省略 sources",
                    )
                    .into());
                }
                let unique: BTreeSet<&String> = sources.iter().collect();
                if unique.len() != sources.len() {
                    return Err(ModelSyncError::invalid("sources 不能重复").into());
                }
                let unknown_sources: Vec<&String> = sources
                    .iter()
                    .filter(|source| !ACTIVE_SOURCES.contains(&source.as_str()))
                    .collect();
                if !unknown_sources.is_empty() {
                    return Err(ModelSyncError::invalid(format!(
                        "sources 必须为当前启用来源 {} 的子集（退役来源已不再同步）：{}",
                        ACTIVE_SOURCES.join(", "),
                        join(&unknown_sources, ",")
                    ))
                    .into());
                }
                Some(sources)
            }
        };
        if fresh {
            let fresh_scope = ["um".to_string()];
            if sources.as_deref() != Some(&fresh_scope[..]) {
                return Err(ModelSyncError::invalid(
                    "严格刷新只支持 sources=['um']（控制台刷新只选 UM，不触其他来源）",
                )
                .into());
            }
            let report = self.collect_fresh_um_catalog(ctx);
            if report.status == "cancelled" {
                return Err(ActionFailure::cancelled());
            }
            return Ok(fresh_result(report));
        }
        let (catalogs, warnings) = self.collect_catalogs(sources.as_deref(), ctx)?;
        let sources = sources.unwrap_or_else(|| {
            ACTIVE_SOURCES
                .iter()
                .map(|source| source.to_string())
                .collect()
        });
        Ok(json!({
            "catalogs": serialize_catalogs(&catalogs),
            "warnings": warnings,
            "sources": sources,
            "checked_epoch": epoch_seconds(),
        }))
    }

    /// `model.context_overrides`: read-only declaration/override/effective rows.
    fn model_context_overrides(
        &self,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.raise_if_cancelled()?;
        let unknown: Vec<&String> = params
            .keys()
            .filter(|key| !matches!(key.as_str(), "local_only" | "models"))
            .collect();
        if !unknown.is_empty() {
            return Err(ModelSyncError::invalid("设置读面含未知参数").into());
        }
        let local_only = match params.get("local_only") {
            None => false,
            Some(Value::Bool(value)) => *value,
            Some(_) => return Err(ModelSyncError::invalid("local_only 必须为布尔值").into()),
        };
        if params.contains_key("models") && !local_only {
            return Err(ModelSyncError::invalid("models 只用于本地设置读取").into());
        }
        let catalogs = if local_only {
            let models = match params.get("models") {
                None => Map::new(),
                Some(Value::Object(models)) => models.clone(),
                Some(_) => {
                    return Err(ModelSyncError::invalid(
                        "models 必须为来源到模型 id 列表的对象",
                    )
                    .into())
                }
            };
            local_only_catalogs(&models)?
        } else {
            let (catalogs, _warnings) = self.collect_catalogs(None, ctx)?;
            catalogs
        };
        let state = load_state(&self.paths);
        Ok(json!({
            "overrides": context_override_surface(&state, &catalogs),
            "note": OVERRIDES_NOTE,
        }))
    }

    /// `model.context_probe_preview`: a pure read of the persisted queue.
    fn model_context_probe_preview(
        &self,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.raise_if_cancelled()?;
        let (selection_request, retest) = probe_run_selection(params)?;
        let state = load_state(&self.paths);
        let records = state
            .get("contextProbes")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let selection = resolve_probe_selection(selection_request.as_ref(), &records)?;
        let steps = PROBE_INPUT_STEPS.to_vec();
        let mut entries = Vec::new();
        let mut estimated_requests_total = 0i64;
        let mut estimated_input_chars_total = 0i64;
        let mut runnable_records = 0i64;
        let mut blocked_records = 0i64;
        let mut idle_records = 0i64;
        for (key, record) in &records {
            let Some(record) = record.as_object() else {
                continue;
            };
            let status = record.get("status").cloned().unwrap_or(Value::Null);
            let status_name = status.as_str().unwrap_or_default();
            let floor = int_or_none(record.get("observed_floor"));
            let ceiling = int_or_none(record.get("observed_ceiling"));
            let attempts = int_or_none(record.get("attempts"));
            let declared = int_or_none(record.get("declared_context"));
            let (prefix, suffix) = split_key(key);
            let source = record
                .get("source")
                .and_then(Value::as_str)
                .filter(|source| !source.is_empty())
                .map(str::to_string)
                .unwrap_or(prefix);
            let model_id = record
                .get("model_id")
                .and_then(Value::as_str)
                .filter(|model_id| !model_id.is_empty())
                .map(str::to_string)
                .unwrap_or(suffix);
            let probe_endpoint = source == "um";
            let paused = PAUSED_SOURCES.contains(&source.as_str());
            let executable = probe_endpoint;
            let selected = selection.as_ref().map_or(true, |selection| {
                selection
                    .get(&source)
                    .map(|ids| ids.contains(&model_id))
                    .unwrap_or(false)
            });
            let eligible = matches!(status_name, "pending" | "stale")
                || (matches!(status_name, "verified" | "failed") && retest);
            let estimate_floor = if status_name == "pending" { floor } else { None };
            let ladder: Vec<i64> = steps
                .iter()
                .copied()
                .filter(|step| estimate_floor.map_or(true, |floor| *step > floor))
                .collect();
            let will_request =
                selected && eligible && executable && !ladder.is_empty();
            let estimated_requests = if will_request { ladder.len() as i64 } else { 0 };
            let input_chars_estimate: i64 = if will_request {
                ladder.iter().sum()
            } else {
                0
            };
            let estimate_note = if !selected {
                idle_records += 1;
                Some("本轮未选中（不在请求集合），实际零请求")
            } else if !eligible {
                idle_records += 1;
                None
            } else if !probe_endpoint {
                blocked_records += 1;
                Some(if paused {
                    "该来源已暂停，实际零请求"
                } else {
                    "该来源未登记探测端点，实际零请求"
                })
            } else if !executable {
                blocked_records += 1;
                Some("该来源凭据不可读（token 缺失），实际零请求")
            } else if ladder.is_empty() {
                idle_records += 1;
                Some("既有实测证据已覆盖全部步进，无需新请求")
            } else {
                runnable_records += 1;
                if matches!(status_name, "verified" | "failed") {
                    Some("retest：先归档旧代次再按全梯重测")
                } else {
                    None
                }
            };
            estimated_requests_total += estimated_requests;
            estimated_input_chars_total += input_chars_estimate;
            let history = record.get("history");
            let history_items: Vec<Value> = match history {
                Some(Value::Array(items)) => {
                    let start = items.len().saturating_sub(PROBE_HISTORY_MAX);
                    items[start..]
                        .iter()
                        .filter_map(Value::as_object)
                        .map(|item| {
                            let mut row = Map::new();
                            for field in [
                                "evidence_revision",
                                "observed_floor",
                                "observed_ceiling",
                                "token_evidence",
                                "attempts",
                            ] {
                                row.insert(
                                    field.to_string(),
                                    int_or_none(item.get(field)).map_or(Value::Null, Value::from),
                                );
                            }
                            for field in [
                                "status",
                                "last_run",
                                "last_success",
                                "last_error",
                                "stale_reason",
                                "reason",
                                "archived_at",
                            ] {
                                row.insert(
                                    field.to_string(),
                                    item.get(field)
                                        .and_then(Value::as_str)
                                        .map(single_line)
                                        .map(Value::String)
                                        .unwrap_or(Value::Null),
                                );
                            }
                            row.insert("character_unit".to_string(), json!("chars"));
                            row.insert("token_unit".to_string(), json!("tokens"));
                            Value::Object(row)
                        })
                        .collect()
                }
                _ => Vec::new(),
            };
            let declared_context_known = record
                .get("declared_context_known")
                .cloned()
                .unwrap_or_else(|| Value::Bool(declared.is_some()));
            let scheme_revision = record
                .get("scheme_revision")
                .cloned()
                .filter(|value| !value.is_null())
                .unwrap_or_else(|| json!(1));
            entries.push(json!({
                "source": source,
                "model_id": model_id,
                "declared_context": declared,
                "declared_context_known": declared_context_known,
                "declared_note": record.get("declared_note").cloned().unwrap_or(Value::Null),
                "declared_at": record.get("declared_at").cloned().unwrap_or(Value::Null),
                "status": status,
                "observed_floor": floor,
                "observed_ceiling": ceiling,
                "attempts": attempts,
                "last_run": record.get("last_run").cloned().unwrap_or(Value::Null),
                "last_success": record.get("last_success").cloned().unwrap_or(Value::Null),
                "last_error": record.get("last_error").cloned().unwrap_or(Value::Null),
                "evidence_revision": record.get("evidence_revision").cloned().unwrap_or(Value::Null),
                "stale_reason": record.get("stale_reason").cloned().unwrap_or(Value::Null),
                "token_evidence": record.get("token_evidence").cloned().unwrap_or(Value::Null),
                "scheme_revision": scheme_revision,
                "history_count": history
                    .and_then(Value::as_array)
                    .map(Vec::len)
                    .unwrap_or(0),
                "history": history_items,
                "probe_endpoint": probe_endpoint,
                "paused": paused,
                "executable": executable,
                "estimate_note": estimate_note,
                "input_unit": "chars",
                "output_unit": "tokens",
                "estimated_requests": estimated_requests,
                "input_chars_estimate": input_chars_estimate,
                "output_token_cap": CONTEXT_PROBE_MAX_TOKENS,
            }));
        }
        let budget = budget_note(&steps);
        Ok(json!({
            "records": entries,
            "steps": steps,
            "totals": {
                "estimated_requests": estimated_requests_total,
                "estimated_input_chars": estimated_input_chars_total,
                "runnable_records": runnable_records,
                "blocked_records": blocked_records,
                "idle_records": idle_records,
            },
            "unit_note": "步进与 input_chars_estimate 为字符数（input_unit=chars），输出上限为 token（output_unit=tokens）；observed_floor/observed_ceiling 是历史字符步进证据，不是 token 容量；token_evidence 为某次成功请求的上游输入 Token 报告（下界证据），不是容量；0 是合法值，判断未知须用 is None（缺席即 Token 容量未知）；不做字符→token 换算；scheme_revision 缺字段=引入修订号前的祖传记录，按修订 1 解释；evidence_revision None=引入修订号前的祖传证据；declared_context 为空即声明未知（declared_context_known=False），探测仍可测量实际可用长度；记录缺席表示未知，不表示已实测或零容量",
            "budget_note": budget,
            "cost_note": COST_NOTE,
            "recovery": read_probe_recovery(&self.paths),
            "note": "队列在每次目录同步后播种；本预览不发请求、不写状态（凭据文件只读读一次）",
        }))
    }

    /// `model.context_probe_run`: REAL billed long-input requests. The
    /// operation layer must submit an explicit UM model set; the whole batch
    /// budget and the cancel hook are enforced inside the runner. Completed
    /// evidence is persisted even when the run is cancelled or the budget runs
    /// out, and a failed state transaction preserves it in the recovery file.
    fn model_context_probe_run(
        &self,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.raise_if_cancelled()?;
        let max_requests = match params.get("max_requests") {
            None => 20,
            Some(Value::Number(number)) => match number.as_i64() {
                Some(value) if (1..=20).contains(&value) => value,
                _ => return Err(ModelSyncError::invalid("max_requests 必须为 1-20 的整数").into()),
            },
            Some(_) => {
                return Err(ModelSyncError::invalid("max_requests 必须为 1-20 的整数").into())
            }
        };
        let max_tokens = match params.get("max_tokens") {
            None => CONTEXT_PROBE_MAX_TOKENS,
            Some(Value::Number(number)) => match number.as_i64() {
                Some(value) if (1..=CONTEXT_PROBE_MAX_TOKENS).contains(&value) => value,
                _ => {
                    return Err(ModelSyncError::invalid(format!(
                        "max_tokens 必须为 1-{CONTEXT_PROBE_MAX_TOKENS} 的整数"
                    ))
                    .into())
                }
            },
            Some(_) => {
                return Err(ModelSyncError::invalid(format!(
                    "max_tokens 必须为 1-{CONTEXT_PROBE_MAX_TOKENS} 的整数"
                ))
                .into())
            }
        };
        let (selection_request, mut retest) = probe_run_selection(params)?;
        let explicit_models = selection_request
            .as_ref()
            .map(|request| {
                request
                    .iter()
                    .filter_map(|(source, ids)| {
                        ids.as_ref().map(|ids| (source.clone(), ids.clone()))
                    })
                    .collect::<ProbeSelection>()
            })
            .filter(|models| !models.is_empty());
        let Some(explicit_models) = explicit_models else {
            return Err(ModelSyncError::invalid(
                "必须提交明确的 UM 来源与模型 ID 集合；空选或遗漏参数不回退全队列",
            )
            .into());
        };
        if explicit_models.keys().count() != 1 || !explicit_models.contains_key("um") {
            return Err(ModelSyncError::invalid("操作层上下文探测只接受明确的 UM 来源").into());
        }
        let _lock = self.acquire_sync_lock("probe_failed", "上下文探测失败")?;
        let mut state = load_state(&self.paths);
        merge_probe_recovery(&self.paths, &mut state);
        let mut records = state
            .get("contextProbes")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        let seed_catalogs =
            self.resolve_probe_selection_catalogs(&explicit_models, &records, ctx)?;
        if !seed_catalogs.is_empty() {
            let seed_filter: ProbeSelection = seed_catalogs
                .iter()
                .map(|(source, models)| {
                    (
                        source.clone(),
                        models
                            .iter()
                            .map(|model| model.model_id.clone())
                            .collect(),
                    )
                })
                .collect();
            seed_context_probes(&seed_catalogs, &mut state, Some(&seed_filter), true);
            records = state
                .get("contextProbes")
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
        }
        let selection = resolve_probe_selection(selection_request.as_ref(), &records)?;
        if !retest {
            retest = explicit_models.iter().any(|(source, ids)| {
                ids.iter().any(|model_id| {
                    records
                        .get(&format!("{source}|{model_id}"))
                        .and_then(Value::as_object)
                        .and_then(|record| record.get("status"))
                        .and_then(Value::as_str)
                        .map(|status| matches!(status, "verified" | "failed"))
                        .unwrap_or(false)
                })
            });
        }
        let outcome = self.probe_context_queue(
            (max_requests, max_tokens),
            &mut records,
            ctx,
            selection.as_ref(),
            retest,
        )?;
        if let Some(object) = state.as_object_mut() {
            object.insert("contextProbes".to_string(), Value::Object(records.clone()));
        }
        if let Err(error) = write_state_transaction(&self.paths, &state) {
            let recovery = self.preserve_probe_evidence(
                &records,
                selection.as_ref(),
                &outcome,
                &error,
            );
            return Err(ModelSyncError::new(
                "probe_failed",
                format!("上下文探测失败：ConfigPublishError: {}{recovery}", error.message),
            )
            .into());
        }
        clear_probe_recovery(&self.paths);
        report_probe_progress(ctx, &outcome);
        Ok(probe_result(
            &outcome,
            selection.as_ref(),
            retest,
            ctx.op_id(),
        ))
    }

    fn resolve_probe_selection_catalogs(
        &self,
        explicit_models: &ProbeSelection,
        records: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Catalogs, ActionFailure> {
        let mut present: ProbeSelection = BTreeMap::new();
        for (key, record) in records {
            let Some(record) = record.as_object() else {
                continue;
            };
            let (prefix, suffix) = split_key(key);
            let source = record
                .get("source")
                .and_then(Value::as_str)
                .filter(|source| !source.is_empty())
                .map(str::to_string)
                .unwrap_or(prefix);
            let model_id = record
                .get("model_id")
                .and_then(Value::as_str)
                .filter(|model_id| !model_id.is_empty())
                .map(str::to_string)
                .unwrap_or(suffix);
            present.entry(source).or_default().insert(model_id);
        }
        let mut missing: ProbeSelection = BTreeMap::new();
        for (source, ids) in explicit_models {
            let present_ids = present.get(source).cloned().unwrap_or_default();
            let gap: BTreeSet<String> = ids.difference(&present_ids).cloned().collect();
            if !gap.is_empty() {
                missing.insert(source.clone(), gap);
            }
        }
        // The operation layer validates queued models too: a model that left the
        // live catalogue must be rejected before any billed request.
        let checked = explicit_models;
        let checked_sources: Vec<String> = checked.keys().cloned().collect();
        let (catalogs, _warnings) = self.collect_catalogs(Some(&checked_sources), ctx)?;
        let mut selected: Catalogs = BTreeMap::new();
        for (source, ids) in checked {
            let Some(models) = catalogs.get(source) else {
                return Err(ModelSyncError::new(
                    "catalog_failed",
                    format!("来源 {source} 目录不可用，无法确认所选模型（读取失败，不是退役）"),
                )
                .into());
            };
            let by_id: BTreeMap<&str, &CatalogModel> = models
                .iter()
                .map(|model| (model.model_id.as_str(), model))
                .collect();
            let mut absent: Vec<&String> = ids
                .iter()
                .filter(|model_id| !by_id.contains_key(model_id.as_str()))
                .collect();
            if !absent.is_empty() {
                absent.sort();
                return Err(ModelSyncError::invalid(format!(
                    "以下模型不在当前 {source} 目录（退役或非法）：{}",
                    join(&absent, ", ")
                ))
                .into());
            }
            if let Some(gap) = missing.get(source) {
                let mut rows = Vec::new();
                for model_id in gap {
                    rows.push((*by_id[model_id.as_str()]).clone());
                }
                selected.insert(source.clone(), rows);
            }
        }
        Ok(selected)
    }

    fn probe_context_queue(
        &self,
        budget: (i64, i64),
        records: &mut Map<String, Value>,
        ctx: &OperationContext,
        selection: Option<&ProbeSelection>,
        retest: bool,
    ) -> Result<ProbeOutcome, ModelSyncError> {
        let max_requests = budget.0;
        let max_tokens = budget.1.min(CONTEXT_PROBE_MAX_TOKENS);
        let mut used = 0i64;
        let mut stop_reason: Option<&str> = None;
        let mut results: Vec<Value> = Vec::new();
        let mut processed: BTreeSet<String> = BTreeSet::new();
        let keys: Vec<String> = records.keys().cloned().collect();
        'outer: for key in keys {
            let Some(record_value) = records.get_mut(&key) else {
                continue;
            };
            let Some(record) = record_value.as_object_mut() else {
                continue;
            };
            let (prefix, suffix) = split_key(&key);
            let source = record
                .get("source")
                .and_then(Value::as_str)
                .filter(|source| !source.is_empty())
                .map(str::to_string)
                .unwrap_or(prefix);
            let model_id = record
                .get("model_id")
                .and_then(Value::as_str)
                .filter(|model_id| !model_id.is_empty())
                .map(str::to_string)
                .unwrap_or(suffix);
            if let Some(selection) = selection {
                if !selection
                    .get(&source)
                    .map(|ids| ids.contains(&model_id))
                    .unwrap_or(false)
                {
                    continue;
                }
            }
            let mut status = record
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if !(matches!(status.as_str(), "pending" | "stale")
                || (matches!(status.as_str(), "verified" | "failed") && retest))
            {
                continue;
            }
            if source != "um" {
                results.push(json!({
                    "key": key,
                    "source": source,
                    "model_id": model_id,
                    "status": status,
                    "requests": 0,
                    "executed": false,
                    "note": if PAUSED_SOURCES.contains(&source.as_str()) {
                        "该来源已暂停，跳过（零请求）"
                    } else {
                        "该来源未登记探测端点，跳过（零请求）"
                    },
                }));
                processed.insert(key);
                continue;
            }
            let previous_status = status.clone();
            if status == "stale" {
                let reason = record
                    .get("stale_reason")
                    .and_then(Value::as_str)
                    .unwrap_or("stale")
                    .to_string();
                let archived = archive_probe_generation(record, &reason);
                if archived || record_int(record, "evidence_revision", 0) < 1 {
                    record.insert(
                        "evidence_revision".to_string(),
                        json!(record_int(record, "evidence_revision", 0) + 1),
                    );
                }
                record.insert("scheme_revision".to_string(), json!(PROBE_SCHEME_REVISION));
                record.insert("status".to_string(), json!("pending"));
                status = "pending".to_string();
            } else if matches!(status.as_str(), "verified" | "failed") {
                archive_probe_generation(record, "retest");
                record.insert(
                    "evidence_revision".to_string(),
                    json!(record_int(record, "evidence_revision", 0) + 1),
                );
                record.insert("scheme_revision".to_string(), json!(PROBE_SCHEME_REVISION));
                record.insert("stale_reason".to_string(), Value::Null);
                record.insert("status".to_string(), json!("pending"));
                status = "pending".to_string();
            }
            results.push(json!({
                "key": key,
                "source": source,
                "model_id": model_id,
                "status": status,
                "requests": 0,
                "executed": false,
                "note": "",
            }));
            let row_index = results.len() - 1;
            if status != previous_status {
                results[row_index]["previous_status"] = json!(previous_status);
            }
            if matches!(previous_status.as_str(), "verified" | "failed") && retest {
                results[row_index]["retest"] = json!(true);
            }
            processed.insert(key.clone());
            let floor = record.get("observed_floor").and_then(Value::as_i64);
            let pending_steps: Vec<i64> = PROBE_INPUT_STEPS
                .iter()
                .copied()
                .filter(|step| floor.map_or(true, |floor| *step > floor))
                .collect();
            if pending_steps.is_empty() {
                record.insert("status".to_string(), json!("verified"));
                record.insert("stale_reason".to_string(), Value::Null);
                results[row_index]["status"] = json!("verified");
                results[row_index]["note"] = json!("既有实测证据已覆盖全部步进，无需新请求");
                continue;
            }
            let mut completed = true;
            let mut row_requests = 0i64;
            for step in pending_steps {
                if used >= max_requests {
                    stop_reason = Some("budget");
                    completed = false;
                    break;
                }
                if ctx.is_cancelled() {
                    stop_reason = Some("cancelled");
                    completed = false;
                    break;
                }
                used += 1;
                row_requests += 1;
                results[row_index]["requests"] = json!(row_requests);
                results[row_index]["executed"] = json!(true);
                record.insert(
                    "attempts".to_string(),
                    json!(record_int(record, "attempts", 0) + 1),
                );
                record.insert("last_run".to_string(), json!(now_iso()));
                let marker = format!("run{used:03}");
                let body = json!({
                    "model": model_id,
                    "messages": [{
                        "role": "user",
                        "content": format!(
                            "The user message below contains numbered anchor lines. Reply with ONLY the text of the FINAL anchor line, verbatim, nothing else.\n\n{}",
                            context_probe_input(step, &marker)
                        ),
                    }],
                    "max_tokens": max_tokens,
                    "stream": false,
                });
                match self.probe_post(&body, ctx) {
                    Err(HttpFailure::Cancelled) => {
                        stop_reason = Some("cancelled");
                        completed = false;
                        break;
                    }
                    Err(error) => {
                        record.insert("last_error".to_string(), json!(error.name()));
                        results[row_index]["note"] = json!(format!(
                            "transient:{}，不构成容量结论",
                            error.name()
                        ));
                        completed = false;
                        break;
                    }
                    Ok((http_status, response_body)) => {
                        if http_status < 400 {
                            if tail_anchor(&response_body, &marker) {
                                record.insert("observed_floor".to_string(), json!(step));
                                record.insert("last_success".to_string(), json!(now_iso()));
                                if let Some(tokens) = trusted_prompt_tokens(&response_body) {
                                    let stored = record
                                        .get("token_evidence")
                                        .and_then(Value::as_i64);
                                    if stored.map_or(true, |stored| tokens > stored) {
                                        record.insert("token_evidence".to_string(), json!(tokens));
                                    }
                                }
                                record.remove("last_error");
                                results[row_index]["note"] = json!(format!("floor={step}"));
                                continue;
                            }
                            record.insert(
                                "last_error".to_string(),
                                json!("InconclusiveResponse"),
                            );
                            results[row_index]["note"] =
                                json!("回复未引用末段锚点，停止该记录本轮");
                            completed = false;
                            break;
                        }
                        if explicit_context_limit(http_status, &response_body) {
                            record.insert("status".to_string(), json!("failed"));
                            record.insert("observed_ceiling".to_string(), json!(step));
                            record.insert(
                                "last_error".to_string(),
                                json!("ContextLimitRefused"),
                            );
                            record.insert("stale_reason".to_string(), Value::Null);
                            results[row_index]["status"] = json!("failed");
                            results[row_index]["note"] =
                                json!(format!("上游明确上下文超限，失败上界={step}"));
                            completed = false;
                            break;
                        }
                        record.insert("last_error".to_string(), json!("HTTPError"));
                        results[row_index]["note"] = json!(format!(
                            "transient:HTTP{}，不构成容量结论",
                            http_status
                        ));
                        completed = false;
                        break;
                    }
                }
            }
            if completed {
                record.insert("status".to_string(), json!("verified"));
                record.insert("stale_reason".to_string(), Value::Null);
                results[row_index]["status"] = json!("verified");
                if results[row_index]["note"].as_str() == Some("") {
                    results[row_index]["note"] = json!("步进表全部成功，实测通过");
                }
            }
            if stop_reason.is_some() {
                break 'outer;
            }
        }
        if let Some(reason) = stop_reason {
            let stop_note = if reason == "budget" {
                "预算耗尽，本轮未执行"
            } else {
                "已取消，本轮未执行"
            };
            for row in results.iter_mut() {
                if row["requests"].as_i64().unwrap_or_default() == 0
                    && row["note"].as_str().map(str::is_empty).unwrap_or(true)
                {
                    row["note"] = json!(stop_note);
                }
            }
            for (key, record) in records.iter() {
                if processed.contains(key) {
                    continue;
                }
                let Some(record) = record.as_object() else {
                    continue;
                };
                let (prefix, suffix) = split_key(key);
                let source = record
                    .get("source")
                    .and_then(Value::as_str)
                    .filter(|source| !source.is_empty())
                    .map(str::to_string)
                    .unwrap_or(prefix);
                let model_id = record
                    .get("model_id")
                    .and_then(Value::as_str)
                    .filter(|model_id| !model_id.is_empty())
                    .map(str::to_string)
                    .unwrap_or(suffix);
                if let Some(selection) = selection {
                    if !selection
                        .get(&source)
                        .map(|ids| ids.contains(&model_id))
                        .unwrap_or(false)
                    {
                        continue;
                    }
                }
                let status = record
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if !(matches!(status, "pending" | "stale")
                    || (matches!(status, "verified" | "failed") && retest))
                {
                    continue;
                }
                results.push(json!({
                    "key": key,
                    "source": source,
                    "model_id": model_id,
                    "status": status,
                    "requests": 0,
                    "executed": false,
                    "note": stop_note,
                }));
            }
        }
        Ok(ProbeOutcome {
            used,
            stopped: stop_reason.map(str::to_string),
            results,
            max_requests,
            max_tokens,
        })
    }

    fn probe_post(
        &self,
        body: &Value,
        ctx: &OperationContext,
    ) -> Result<(u16, Value), HttpFailure> {
        let payload = serde_json::to_vec(body).map_err(|_| HttpFailure::ProtocolError)?;
        let response = http_request(
            self.paths.gateway_port,
            "POST",
            "/v1/chat/completions",
            &[],
            Some(&payload),
            PROBE_TIMEOUT,
            Some(&|| ctx.is_cancelled()),
        )?;
        let parsed = serde_json::from_slice::<Value>(&response.body).ok();
        let body = match parsed {
            Some(value) => value,
            None if response.status >= 400 => {
                let text = String::from_utf8_lossy(&response.body);
                json!({"error": text.chars().take(300).collect::<String>()})
            }
            None => return Err(HttpFailure::ProtocolError),
        };
        Ok((response.status, body))
    }

    fn preserve_probe_evidence(
        &self,
        records: &Map<String, Value>,
        selection: Option<&ProbeSelection>,
        outcome: &ProbeOutcome,
        error: &ModelSyncError,
    ) -> String {
        let payload = json!({
            "saved_at": now_iso(),
            "persistence": "failed",
            "error": single_line(&format!("ModelSyncError: {}", error.message)),
            "selection": selection_payload(selection),
            "requests_used": outcome.used,
            "stopped": outcome.stopped,
            "results": outcome.results.iter().take(PROBE_RESULT_MAX_ROWS).cloned().collect::<Vec<_>>(),
            "records": selected_record_keys(selection, records),
            "note": "状态事务保存失败；本文件保留本轮已计费证据，未写入主状态",
        });
        match write_probe_recovery(&self.paths, &payload) {
            Ok(path) => format!("；已计费证据保留到恢复文件 {}", path.file_name().unwrap_or_default().to_string_lossy()),
            Err(error) => format!(
                "；已计费证据恢复文件写入失败：{}",
                error.message
            ),
        }
    }

    /// `model.context_override_set`: validate the identity against the current
    /// read-only catalogue, then write only the model-sync state file under the
    /// shared lock and transaction.
    fn model_context_override_set(
        &self,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.raise_if_cancelled()?;
        let (source, model_id, expected) = override_identity_params(params)?;
        let context = match params.get("context") {
            Some(Value::Number(number)) => match number.as_i64() {
                Some(context) if context > 0 && context <= CONTEXT_OVERRIDE_MAX => context,
                _ => return Err(ModelSyncError::invalid("context 必须为正整数").into()),
            },
            _ => return Err(ModelSyncError::invalid("context 必须为正整数").into()),
        };
        let (catalogs, _warnings) =
            self.collect_catalogs(Some(std::slice::from_ref(&source)), ctx)?;
        let Some(models) = catalogs.get(&source) else {
            return Err(ModelSyncError::invalid(format!(
                "来源 {source} 当前目录不可用，无法确认模型身份"
            ))
            .into());
        };
        if !models.iter().any(|model| model.model_id == model_id) {
            return Err(ModelSyncError::invalid(format!(
                "模型不在当前目录：{source}|{model_id}"
            ))
            .into());
        }
        let _lock = self.acquire_sync_lock("override_failed", "覆盖保存失败")?;
        let mut state = load_state(&self.paths);
        let mut overrides = context_overrides_of_state(&state);
        let key = format!("{source}|{model_id}");
        let stored_at = overrides
            .get(&key)
            .and_then(Value::as_object)
            .and_then(|entry| entry.get("set_at"))
            .and_then(Value::as_str)
            .map(str::to_string);
        if stored_at != expected {
            return Err(ModelSyncError::new(
                "version_conflict",
                format!(
                    "覆盖版本冲突（{key}）：读面 override_at={}，当前存储={}；请基于最新读面重试",
                    display_opt(&expected),
                    display_opt(&stored_at)
                ),
            )
            .into());
        }
        let set_at = now_iso();
        let entry = json!({
            "context": context,
            "set_at": set_at,
            "source": "manual",
        });
        overrides.insert(key, entry);
        if let Some(object) = state.as_object_mut() {
            object.insert("contextOverrides".to_string(), Value::Object(overrides));
        }
        self.apply_state(&state, "override_failed", "覆盖保存失败")?;
        Ok(json!({
            "source": source,
            "model_id": model_id,
            "context": context,
            "set_at": set_at,
            "note": OVERRIDE_APPLY_NOTE,
        }))
    }

    /// `model.context_override_clear`: delete one override under the shared
    /// lock. A missing override is an idempotent success and writes nothing.
    fn model_context_override_clear(
        &self,
        params: &Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.raise_if_cancelled()?;
        let (source, model_id, expected) = override_identity_params(params)?;
        let _lock = self.acquire_sync_lock("override_failed", "覆盖清除失败")?;
        let mut state = load_state(&self.paths);
        let mut overrides = context_overrides_of_state(&state);
        let key = format!("{source}|{model_id}");
        let Some(existing) = overrides.get(&key).cloned() else {
            return Ok(json!({
                "source": source,
                "model_id": model_id,
                "cleared": true,
                "existed": false,
                "changed": false,
                "note": "该覆盖不存在（幂等成功，零写入）；目录声明值即为当前有效值",
            }));
        };
        let stored_at = existing
            .as_object()
            .and_then(|entry| entry.get("set_at"))
            .and_then(Value::as_str)
            .map(str::to_string);
        if stored_at != expected {
            return Err(ModelSyncError::new(
                "version_conflict",
                format!(
                    "恢复版本冲突（{key}）：读面版本令牌={}，当前存储={}；请基于最新读面重试",
                    display_opt(&expected),
                    display_opt(&stored_at)
                ),
            )
            .into());
        }
        overrides.remove(&key);
        if let Some(object) = state.as_object_mut() {
            object.insert("contextOverrides".to_string(), Value::Object(overrides));
        }
        self.apply_state(&state, "override_failed", "覆盖清除失败")?;
        Ok(json!({
            "source": source,
            "model_id": model_id,
            "cleared": true,
            "existed": true,
            "changed": true,
            "note": "覆盖已清除；目录声明值即为当前有效值",
        }))
    }

    fn acquire_sync_lock(
        &self,
        code: &str,
        prefix: &str,
    ) -> Result<ModelSyncLock, ActionFailure> {
        ModelSyncLock::acquire(&self.paths).map_err(|_| {
            ModelSyncError::new(
                code,
                format!("{prefix}：RuntimeError: 另一个模型同步正在运行"),
            )
            .into()
        })
    }

    fn apply_state(
        &self,
        state: &Value,
        code: &str,
        prefix: &str,
    ) -> Result<(), ActionFailure> {
        write_state_transaction(&self.paths, state).map_err(|error| {
            ModelSyncError::new(code, format!("{prefix}：ConfigPublishError: {}", error.message))
                .into()
        })
    }

    /// `model_sync.collect_catalogs`: a single-source failure is a warning, not a
    /// failed action. `ValueError` from an invalid source selection is rejected.
    fn collect_catalogs(
        &self,
        sources: Option<&[String]>,
        ctx: &OperationContext,
    ) -> Result<(Catalogs, Vec<String>), ModelSyncError> {
        let selected: Vec<String> = match sources {
            None => ACTIVE_SOURCES
                .iter()
                .map(|source| source.to_string())
                .collect(),
            Some(sources) => {
                if sources
                    .iter()
                    .any(|source| !ACTIVE_SOURCES.contains(&source.as_str()))
                {
                    return Err(ModelSyncError::invalid(format!(
                        "sources 必须为当前启用来源 {} 的子集；退役来源（m365 / qoder_cn / qoder_global / cliproxy）不再同步",
                        ACTIVE_SOURCES.join(", ")
                    )));
                }
                let mut unique = Vec::new();
                for source in sources {
                    if !unique.contains(source) {
                        unique.push(source.clone());
                    }
                }
                unique
            }
        };
        let mut catalogs = BTreeMap::new();
        let mut warnings = Vec::new();
        if selected.iter().any(|source| source == "um") {
            match self.get_um_models(ctx) {
                Ok(payload) => match um_catalog_models(&payload) {
                    Ok(models) => {
                        catalogs.insert("um".to_string(), models);
                    }
                    Err(error) => warnings.push(format!("UM目录不可用：{}", error.name())),
                },
                Err(error) => {
                    if error == HttpFailure::Cancelled {
                        return Err(ModelSyncError::new("cancelled", "adapter honoured the cancel request"));
                    }
                    warnings.push(format!("UM目录不可用：{}", error.name()));
                }
            }
        }
        Ok((catalogs, warnings))
    }

    fn get_um_models(&self, ctx: &OperationContext) -> Result<Value, HttpFailure> {
        let response = http_request(
            self.paths.gateway_port,
            "GET",
            "/v1/models",
            &[],
            None,
            CATALOG_TIMEOUT,
            Some(&|| ctx.is_cancelled()),
        )?;
        if response.status >= 400 {
            return Err(HttpFailure::ProtocolError);
        }
        serde_json::from_slice::<Value>(&response.body).map_err(|_| HttpFailure::ProtocolError)
    }

    fn get_um_models_fresh(&self, ctx: &OperationContext) -> Result<HttpResponse, HttpFailure> {
        http_request(
            self.paths.gateway_port,
            "GET",
            "/v1/models",
            &[(UM_MODELS_FRESH_HEADER, UM_MODELS_FRESH_VALUE)],
            None,
            CATALOG_TIMEOUT,
            Some(&|| ctx.is_cancelled()),
        )
    }

    /// `model_sync.collect_fresh_um_catalog`: one report, never raises for an
    /// upstream failure. The last successful models are retained in memory and
    /// returned only with `stale_fallback=true`.
    fn collect_fresh_um_catalog(&self, ctx: &OperationContext) -> FreshReport {
        let checked_epoch = epoch_seconds();
        let mut report = FreshReport::unavailable(checked_epoch);
        let response = match self.get_um_models_fresh(ctx) {
            Ok(response) => response,
            Err(error) => {
                if error == HttpFailure::Cancelled {
                    report.error = Some("cancelled".to_string());
                    report.status = "cancelled";
                } else {
                    report.error = Some(format!("UM目录不可用：{}", error.name()));
                }
                return self.with_last_success(report);
            }
        };
        report.http_status = Some(response.status);
        let marker = response
            .headers
            .get(&UM_MODELS_FRESH_MARKER.to_ascii_lowercase())
            .map(|value| value == UM_MODELS_FRESH_MARKER_VALUE)
            .unwrap_or(false);
        report.marker = marker;
        if response.status == 401 || response.status == 403 {
            report.status = "auth_required";
            report.error = Some(format!("UM目录认证失败（HTTP {}）", response.status));
            return self.with_last_success(report);
        }
        if response.status >= 400 {
            report.status = "unavailable";
            report.error = Some(format!("UM目录不可用（HTTP {}）", response.status));
            return self.with_last_success(report);
        }
        if !marker {
            report.status = "not_fresh";
            report.error = Some(
                "UM目录响应缺少 raw-fresh 标记（可能来自旧缓存），不宣告刷新成功".to_string(),
            );
            return self.with_last_success(report);
        }
        let payload = serde_json::from_slice::<Value>(&response.body).ok();
        let Some(payload) = payload else {
            if looks_like_html(&response.body) {
                if looks_like_auth_page(&response.body) {
                    report.status = "auth_required";
                    report.error = Some("UM目录返回认证页（非目录数据）".to_string());
                } else {
                    report.status = "error_page";
                    report.error = Some("UM目录返回错误页（非目录数据）".to_string());
                }
            } else {
                report.status = "invalid_shape";
                report.error = Some("UM目录响应不是合法 JSON".to_string());
            }
            return self.with_last_success(report);
        };
        let Some(data) = payload.get("data").and_then(Value::as_array) else {
            let error_text = payload_error_text(&payload);
            if has_auth_marker(&error_text) {
                report.status = "auth_required";
                report.error = Some("UM目录返回认证失败（非目录数据）".to_string());
            } else if !error_text.is_empty() {
                report.status = "error_page";
                report.error = Some("UM目录返回错误（非目录数据）".to_string());
            } else {
                report.status = "invalid_shape";
                report.error = Some("UM目录响应结构非法（缺少 data 列表）".to_string());
            }
            return self.with_last_success(report);
        };
        let _ = data;
        let error_text = payload_error_text(&payload);
        if !error_text.is_empty() {
            if has_auth_marker(&error_text) {
                report.status = "auth_required";
                report.error = Some("UM目录返回认证失败（非目录数据）".to_string());
            } else {
                report.status = "error_page";
                report.error = Some("UM目录返回错误（非目录数据）".to_string());
            }
            return self.with_last_success(report);
        }
        match um_catalog_models(&payload) {
            Ok(models) => {
                report.collected_at = Some(now_iso());
                report.model_count = models.len();
                report.models = models;
                report.status = if report.model_count == 0 { "empty" } else { "fresh" };
                report.success = true;
                report.fresh = true;
                if let Ok(mut cache) = self.last_fresh.lock() {
                    *cache = Some(FreshCache {
                        collected_at: report.collected_at.clone(),
                        checked_epoch: report.checked_epoch,
                        models: report.models.clone(),
                    });
                }
            }
            Err(_) => {
                report.status = "invalid_shape";
                report.error = Some("UM目录响应结构非法".to_string());
                return self.with_last_success(report);
            }
        }
        report
    }

    fn with_last_success(&self, mut report: FreshReport) -> FreshReport {
        let cache = self
            .last_fresh
            .lock()
            .ok()
            .and_then(|cache| cache.clone());
        let Some(cache) = cache else {
            report.models.clear();
            report.model_count = 0;
            return report;
        };
        report.stale_fallback = true;
        report.last_success = Some(json!({
            "collected_at": cache.collected_at,
            "checked_epoch": cache.checked_epoch,
            "model_count": cache.models.len(),
        }));
        report.models = cache.models;
        report.model_count = report.models.len();
        report
    }
}

#[derive(Default)]
struct FreshReport {
    checked_epoch: i64,
    success: bool,
    fresh: bool,
    status: &'static str,
    collected_at: Option<String>,
    http_status: Option<u16>,
    marker: bool,
    model_count: usize,
    models: Vec<CatalogModel>,
    error: Option<String>,
    stale_fallback: bool,
    last_success: Option<Value>,
}

impl FreshReport {
    fn unavailable(checked_epoch: i64) -> Self {
        Self {
            checked_epoch,
            status: "unavailable",
            ..Self::default()
        }
    }
}

fn fresh_result(report: FreshReport) -> Value {
    let refresh = json!({
        "source": "um",
        "requested": true,
        "success": report.success,
        "fresh": report.fresh,
        "status": report.status,
        "collected_at": report.collected_at,
        "checked_epoch": report.checked_epoch,
        "http_status": report.http_status,
        "marker": report.marker,
        "model_count": report.model_count,
        "error": report.error,
        "stale_fallback": report.stale_fallback,
        "last_success": report.last_success,
    });
    let mut catalogs = BTreeMap::new();
    if report.success || !report.models.is_empty() {
        catalogs.insert("um".to_string(), report.models);
    }
    json!({
        "catalogs": serialize_catalogs(&catalogs),
        "warnings": if report.success { json!([]) } else {
            json!([report.error.clone().unwrap_or_else(|| "UM目录刷新失败".to_string())])
        },
        "sources": ["um"],
        "checked_epoch": report.checked_epoch,
        "refresh": refresh,
        "fresh": report.fresh,
        "stale": report.stale_fallback,
        "collected_at": report.collected_at,
        "source": "um",
        "success": report.success,
    })
}

fn um_catalog_models(payload: &Value) -> Result<Vec<CatalogModel>, HttpFailure> {
    let Some(object) = payload.as_object() else {
        return Err(HttpFailure::ProtocolError);
    };
    let Some(data) = object.get("data") else {
        return Ok(Vec::new());
    };
    if data.is_null() {
        return Ok(Vec::new());
    }
    let Some(rows) = data.as_array() else {
        return Err(HttpFailure::ProtocolError);
    };
    let mut models = Vec::new();
    for raw in rows {
        let Some(raw) = raw.as_object() else {
            return Err(HttpFailure::ProtocolError);
        };
        let model_id = value_to_string(raw.get("id")).trim().to_string();
        if model_id.is_empty() || is_virtual_model(&model_id) {
            continue;
        }
        let (efforts, default) = known_efforts(&model_id);
        models.push(CatalogModel {
            source: "um".to_string(),
            model_id,
            context: context_of(raw),
            efforts_source: if efforts.is_empty() {
                None
            } else {
                Some("known".to_string())
            },
            efforts,
            default_effort: default,
            publish: true,
            display_name: None,
        });
    }
    Ok(models)
}

fn serialize_catalogs(catalogs: &Catalogs) -> Value {
    let mut serialized = Map::new();
    for (source, models) in catalogs {
        let rows: Vec<Value> = models
            .iter()
            .map(|model| {
                json!({
                    "id": model.model_id,
                    "display_name": model.display_name,
                    "context": model.context,
                    "context_state": if model.context.is_some() { "declared" } else { "unknown" },
                    "output_token_cap": {"value": 16384, "source": "compat_default"},
                    "efforts": model.efforts,
                    "default_effort": model.default_effort,
                    "publish": model.publish,
                    "efforts_source": model.efforts_source,
                })
            })
            .collect();
        serialized.insert(source.clone(), Value::Array(rows));
    }
    Value::Object(serialized)
}

fn empty_state() -> Value {
    json!({
        "version": STATE_VERSION,
        "catalogCounts": {},
        "contextProbes": {},
        "contextOverrides": {},
    })
}

/// `model_sync.load_state`: a missing or unreadable state is an empty state;
/// a readable state is migrated in memory (the next transactional write persists
/// the upgrade).
fn load_state(paths: &ModelSyncPaths) -> Value {
    let Ok(text) = fs::read_to_string(&paths.state_file) else {
        return empty_state();
    };
    let Ok(mut state) = serde_json::from_str::<Value>(&text) else {
        return empty_state();
    };
    migrate_state(&mut state);
    state
}

fn migrate_state(state: &mut Value) {
    let Some(object) = state.as_object_mut() else {
        return;
    };
    let version = int_value(object.get("version").unwrap_or(&Value::Null)).unwrap_or(1);
    if version < 2 {
        object.insert("probes".to_string(), json!({}));
    }
    if version < STATE_VERSION {
        object.remove("pinned");
        object.insert("version".to_string(), json!(STATE_VERSION));
    }
}

fn local_only_catalogs(
    models: &Map<String, Value>,
) -> Result<Catalogs, ModelSyncError> {
    let mut catalogs = BTreeMap::new();
    let mut total = 0usize;
    for (source, ids) in models {
        if !ACTIVE_SOURCES.contains(&source.as_str()) {
            return Err(ModelSyncError::invalid(format!(
                "models 含未知或已暂停来源：{source}"
            )));
        }
        let Some(ids) = ids.as_array() else {
            return Err(ModelSyncError::invalid(format!(
                "models[{source}] 必须为 model_id 列表"
            )));
        };
        let mut rows = Vec::new();
        for id in ids {
            let Some(model_id) = id.as_str().filter(|id| !id.trim().is_empty()) else {
                return Err(ModelSyncError::invalid(format!(
                    "models[{source}] 含空 model_id"
                )));
            };
            rows.push(CatalogModel::new(source, model_id));
        }
        if rows.len() > 64 {
            return Err(ModelSyncError::invalid(format!(
                "models[{source}] 超过每源 64 个上限"
            )));
        }
        total += rows.len();
        catalogs.insert(source.clone(), rows);
    }
    if total > 256 {
        return Err(ModelSyncError::invalid("models 总条目超过 256 上限"));
    }
    Ok(catalogs)
}

fn context_overrides_of_state(state: &Value) -> Map<String, Value> {
    state
        .get("contextOverrides")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn valid_override_context(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_i64)
        .map(|context| context > 0 && context <= CONTEXT_OVERRIDE_MAX)
        .unwrap_or(false)
}

fn override_applies(entry: &Value, declared: Option<i64>) -> bool {
    let Some(object) = entry.as_object() else {
        return false;
    };
    let context = object.get("context").and_then(Value::as_i64);
    context
        .filter(|context| *context > 0 && *context <= CONTEXT_OVERRIDE_MAX)
        .map(|context| Some(context) != declared)
        .unwrap_or(false)
}

fn context_override_surface(
    state: &Value,
    catalogs: &Catalogs,
) -> Value {
    let overrides = context_overrides_of_state(state);
    let probes = state
        .get("contextProbes")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut rows: BTreeMap<String, Value> = BTreeMap::new();
    for (source, models) in catalogs {
        for model in models {
            let key = format!("{}|{}", source, model.model_id);
            let declared_at = probes
                .get(&key)
                .and_then(Value::as_object)
                .and_then(|probe| probe.get("declared_at"))
                .cloned()
                .unwrap_or(Value::Null);
            rows.insert(
                key,
                json!({
                    "source": source,
                    "model_id": model.model_id,
                    "declared_context": model.context,
                    "override_context": null,
                    "effective_context": model.context,
                    "effective_source": if model.context.is_some() { "declared" } else { "unknown" },
                    "declared_at": declared_at,
                    "override_at": null,
                    "override_applied": false,
                    "override_state": "none",
                }),
            );
        }
    }
    for (key, entry) in overrides {
        let (source, model_id) = split_key(&key);
        let Some(row) = rows.get_mut(&key) else {
            let declared_at = probes
                .get(&key)
                .and_then(Value::as_object)
                .and_then(|probe| probe.get("declared_at"))
                .cloned()
                .unwrap_or(Value::Null);
            let mut row = json!({
                "source": source,
                "model_id": model_id,
                "declared_context": null,
                "override_context": null,
                "effective_context": null,
                "effective_source": "unknown",
                "declared_at": declared_at,
                "override_at": null,
                "override_applied": false,
                "override_state": "model_missing",
            });
            if let Some(entry) = entry.as_object() {
                row["override_context"] = entry.get("context").cloned().unwrap_or(Value::Null);
                row["override_at"] = entry.get("set_at").cloned().unwrap_or(Value::Null);
            }
            rows.insert(key, row);
            continue;
        };
        let Some(entry) = entry.as_object() else {
            row["override_state"] = json!("malformed");
            continue;
        };
        row["override_context"] = entry.get("context").cloned().unwrap_or(Value::Null);
        row["override_at"] = entry.get("set_at").cloned().unwrap_or(Value::Null);
        let declared = row["declared_context"].as_i64();
        if override_applies(&Value::Object(entry.clone()), declared) {
            row["override_applied"] = json!(true);
            row["override_state"] = json!("applied");
            row["effective_context"] = entry.get("context").cloned().unwrap_or(Value::Null);
            row["effective_source"] = json!("override");
        } else if valid_override_context(entry.get("context")) {
            row["override_state"] = json!("equal");
        } else {
            row["override_state"] = json!("malformed");
        }
    }
    Value::Array(rows.into_values().collect())
}

fn split_key(key: &str) -> (String, String) {
    match key.split_once('|') {
        Some((source, model_id)) => (source.to_string(), model_id.to_string()),
        None => (key.to_string(), String::new()),
    }
}

fn override_identity_params(
    params: &Map<String, Value>,
) -> Result<(String, String, Option<String>), ModelSyncError> {
    let source = match params.get("source").and_then(Value::as_str) {
        Some(source) if !source.trim().is_empty() => source.trim().to_string(),
        _ => return Err(ModelSyncError::invalid("source 必须为非空字符串")),
    };
    let model_id = match params.get("model_id").and_then(Value::as_str) {
        Some(model_id) if !model_id.trim().is_empty() => model_id.trim().to_string(),
        _ => return Err(ModelSyncError::invalid("model_id 必须为非空字符串")),
    };
    let expected = match params.get("expected_set_at") {
        None | Some(Value::Null) => None,
        Some(Value::String(expected)) if !expected.trim().is_empty() => {
            Some(expected.clone())
        }
        Some(_) => {
            return Err(ModelSyncError::invalid(
                "expected_set_at 必须为读面返回的 override_at 字符串或缺省（表示预期无既有覆盖）",
            ))
        }
    };
    Ok((source, model_id, expected))
}

fn display_opt(value: &Option<String>) -> String {
    value.clone().unwrap_or_else(|| "None".to_string())
}

/// The Python `model_sync.SyncLock`: the lock file's byte 0 is held for the
/// whole read/write section, so a Python rollback process and this process cannot
/// write the same state concurrently.
struct ModelSyncLock {
    file: Option<File>,
}

impl ModelSyncLock {
    fn acquire(paths: &ModelSyncPaths) -> Result<Self, ModelSyncError> {
        fs::create_dir_all(&paths.data_dir)
            .map_err(|error| ModelSyncError::new("lock_open_failed", error.to_string()))?;
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&paths.lock_file)
            .map_err(|error| ModelSyncError::new("lock_open_failed", error.to_string()))?;
        if file
            .metadata()
            .map(|metadata| metadata.len() == 0)
            .unwrap_or(false)
        {
            file.write_all(b"0")
                .and_then(|_| file.flush())
                .map_err(|error| ModelSyncError::new("lock_open_failed", error.to_string()))?;
        }
        file.seek(SeekFrom::Start(0))
            .map_err(|error| ModelSyncError::new("lock_open_failed", error.to_string()))?;
        lock_file(&file)
            .map_err(|error| ModelSyncError::new("lock_open_failed", error.to_string()))?;
        Ok(Self { file: Some(file) })
    }
}

impl Drop for ModelSyncLock {
    fn drop(&mut self) {
        if let Some(file) = self.file.take() {
            let _ = unlock_file(&file);
        }
    }
}

#[cfg(windows)]
#[repr(C)]
struct Overlapped {
    internal: usize,
    internal_high: usize,
    offset: u32,
    offset_high: u32,
    h_event: *mut core::ffi::c_void,
}

#[cfg(windows)]
extern "system" {
    fn LockFileEx(
        file: *mut core::ffi::c_void,
        flags: u32,
        reserved: u32,
        bytes_low: u32,
        bytes_high: u32,
        overlapped: *mut Overlapped,
    ) -> i32;
    fn UnlockFileEx(
        file: *mut core::ffi::c_void,
        reserved: u32,
        bytes_low: u32,
        bytes_high: u32,
        overlapped: *mut Overlapped,
    ) -> i32;
}

#[cfg(windows)]
fn lock_file(file: &File) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    const LOCKFILE_FAIL_IMMEDIATELY: u32 = 1;
    const LOCKFILE_EXCLUSIVE_LOCK: u32 = 2;
    let mut overlapped = Overlapped {
        internal: 0,
        internal_high: 0,
        offset: 0,
        offset_high: 0,
        h_event: std::ptr::null_mut(),
    };
    // SAFETY: the file handle is owned by `file` for the call, and the
    // overlapped structure lives on this stack frame.
    let result = unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut overlapped,
        )
    };
    if result == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(windows)]
fn unlock_file(file: &File) -> std::io::Result<()> {
    use std::os::windows::io::AsRawHandle;
    let mut overlapped = Overlapped {
        internal: 0,
        internal_high: 0,
        offset: 0,
        offset_high: 0,
        h_event: std::ptr::null_mut(),
    };
    // SAFETY: the handle is owned by `file`; the lock range matches acquire.
    let result = unsafe {
        UnlockFileEx(
            file.as_raw_handle(),
            0,
            1,
            0,
            &mut overlapped,
        )
    };
    if result == 0 {
        Err(std::io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(unix)]
fn lock_file(file: &File) -> std::io::Result<()> {
    use std::os::unix::io::AsRawFd;
    // SAFETY: the fd is owned by `file` for the call.
    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(unix)]
fn unlock_file(file: &File) -> std::io::Result<()> {
    use std::os::unix::io::AsRawFd;
    // SAFETY: the fd is owned by `file` for the call.
    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) };
    if result == 0 {
        Ok(())
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(not(any(windows, unix)))]
fn lock_file(_file: &File) -> std::io::Result<()> {
    Ok(())
}

#[cfg(not(any(windows, unix)))]
fn unlock_file(_file: &File) -> std::io::Result<()> {
    Ok(())
}

/// `model_sync.apply_config_transaction` for the one retained state file: a
/// same-directory temp file + fsync + replace with bounded Windows retries, a
/// timestamped backup of the previous bytes, and 30 retained backup generations.
fn write_state_transaction(paths: &ModelSyncPaths, state: &Value) -> Result<(), ModelSyncError> {
    let text = serde_json::to_string_pretty(state)
        .map(|text| text + "\n")
        .map_err(|error| ModelSyncError::new("publish_failed", error.to_string()))?;
    let stamp = chrono::Local::now()
        .format("%Y%m%d-%H%M%S-%f")
        .to_string();
    let backup_dir = paths.backup_dir.join(stamp);
    fs::create_dir_all(&backup_dir)
        .map_err(|error| ModelSyncError::new("publish_failed", error.to_string()))?;
    let original = fs::read(&paths.state_file).ok();
    if let Some(original) = &original {
        fs::write(backup_dir.join("model-sync-state.json.bak"), original)
            .map_err(|error| ModelSyncError::new("publish_failed", error.to_string()))?;
    }
    if let Some(parent) = paths.state_file.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| ModelSyncError::new("publish_failed", error.to_string()))?;
    }
    let temporary = paths.state_file.with_file_name(format!(
        "model-sync-state.json.{}.{}.tmp",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default()
    ));
    let write_result = (|| -> std::io::Result<()> {
        {
            let mut handle = File::create(&temporary)?;
            handle.write_all(text.as_bytes())?;
            handle.sync_all()?;
        }
        replace_with_retry(&temporary, &paths.state_file)
    })();
    if let Err(error) = write_result {
        let _ = fs::remove_file(&temporary);
        return Err(ModelSyncError::new(
            "publish_failed",
            format!("发布失败（{}：{}）", paths.state_file.display(), error),
        ));
    }
    if let Ok(entries) = fs::read_dir(&paths.backup_dir) {
        let mut directories: Vec<PathBuf> = entries
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.is_dir())
            .collect();
        directories.sort();
        directories.reverse();
        for old in directories.iter().skip(30) {
            let _ = fs::remove_dir_all(old);
        }
    }
    Ok(())
}

fn replace_with_retry(source: &Path, target: &Path) -> std::io::Result<()> {
    let delays = std::iter::once(&0u64).chain(RENAME_RETRY_DELAYS_MS.iter());
    for (attempt, delay) in delays.enumerate() {
        match fs::rename(source, target) {
            Ok(()) => return Ok(()),
            Err(error) => {
                let retryable = matches!(
                    error.kind(),
                    std::io::ErrorKind::PermissionDenied
                        | std::io::ErrorKind::AlreadyExists
                        | std::io::ErrorKind::WouldBlock
                );
                if !retryable || attempt == RENAME_RETRY_DELAYS_MS.len() {
                    return Err(error);
                }
                std::thread::sleep(Duration::from_millis(*delay));
            }
        }
    }
    Ok(())
}

fn probe_run_selection(
    params: &Map<String, Value>,
) -> Result<(Option<ProbeSelectionRequest>, bool), ModelSyncError> {
    let sources = match params.get("sources") {
        None | Some(Value::Null) => None,
        Some(value) => Some(scope_str_list(value, "sources")?),
    };
    let models = match params.get("models") {
        None | Some(Value::Null) => None,
        Some(Value::Object(models)) if !models.is_empty() => {
            for (source, ids) in models {
                if source.is_empty() {
                    return Err(ModelSyncError::invalid("models 键必须为非空来源名"));
                }
                let Some(ids) = ids.as_array() else {
                    return Err(ModelSyncError::invalid(format!(
                        "models[{source}] 必须为非空 model_id 列表"
                    )));
                };
                if ids.is_empty()
                    || ids
                        .iter()
                        .any(|id| id.as_str().map(str::is_empty).unwrap_or(true))
                {
                    return Err(ModelSyncError::invalid(format!(
                        "models[{source}] 必须为非空 model_id 列表"
                    )));
                }
            }
            Some(models.clone())
        }
        Some(_) => {
            return Err(ModelSyncError::invalid(
                "models 必须为非空的 {source: [model_id, ...]} 对象",
            ))
        }
    };
    let retest = match params.get("retest") {
        None => false,
        Some(Value::Bool(value)) => *value,
        Some(_) => return Err(ModelSyncError::invalid("retest 必须为布尔值")),
    };
    let request = normalize_probe_selection(sources, models)?;
    Ok((request, retest))
}

fn normalize_probe_selection(
    sources: Option<Vec<String>>,
    models: Option<Map<String, Value>>,
) -> Result<Option<ProbeSelectionRequest>, ModelSyncError> {
    if sources.is_none() && models.is_none() {
        return Ok(None);
    }
    let mut request: ProbeSelectionRequest = BTreeMap::new();
    if let Some(sources) = &sources {
        if sources.is_empty() {
            return Err(ModelSyncError::invalid(
                "sources 必须为非空字符串的非空无重复列表",
            ));
        }
        let unique: BTreeSet<&String> = sources.iter().collect();
        if unique.len() != sources.len()
            || sources.iter().any(|source| source.is_empty())
        {
            return Err(ModelSyncError::invalid(
                "sources 必须为非空字符串的非空无重复列表",
            ));
        }
        let mut unknown: Vec<&String> = sources
            .iter()
            .filter(|source| !KNOWN_SOURCES.contains(&source.as_str()))
            .collect();
        if !unknown.is_empty() {
            unknown.sort();
            return Err(ModelSyncError::invalid(format!(
                "sources 含未知来源：{}",
                join(&unknown, ", ")
            )));
        }
        let mut paused: Vec<&String> = sources
            .iter()
            .filter(|source| PAUSED_SOURCES.contains(&source.as_str()))
            .collect();
        if !paused.is_empty() {
            paused.sort();
            return Err(ModelSyncError::invalid(format!(
                "sources 含已暂停来源（不进入计费队列）：{}",
                join(&paused, ", ")
            )));
        }
        for source in sources {
            request.insert(source.clone(), None);
        }
    }
    if let Some(models) = &models {
        if models.is_empty() {
            return Err(ModelSyncError::invalid(
                "models 必须为非空的 {source: [model_id, ...]} 对象",
            ));
        }
        for (source, ids) in models {
            if source.is_empty() || !KNOWN_SOURCES.contains(&source.as_str()) {
                return Err(ModelSyncError::invalid(format!(
                    "models 含未知来源：{:?}",
                    source
                )));
            }
            if PAUSED_SOURCES.contains(&source.as_str()) {
                return Err(ModelSyncError::invalid(format!(
                    "models 含已暂停来源（不进入计费队列）：{source}"
                )));
            }
            if let Some(sources) = &sources {
                if !sources.contains(source) {
                    return Err(ModelSyncError::invalid(format!(
                        "models 的来源 {source} 不在所选 sources 内"
                    )));
                }
            }
            let Some(ids) = ids.as_array() else {
                return Err(ModelSyncError::invalid(format!(
                    "models[{source}] 必须为非空字符串的非空无重复列表"
                )));
            };
            let mut set = BTreeSet::new();
            for id in ids {
                let Some(id) = id.as_str().filter(|id| !id.is_empty()) else {
                    return Err(ModelSyncError::invalid(format!(
                        "models[{source}] 必须为非空字符串的非空无重复列表"
                    )));
                };
                if !set.insert(id.to_string()) {
                    return Err(ModelSyncError::invalid(format!(
                        "models[{source}] 必须为非空字符串的非空无重复列表"
                    )));
                }
            }
            if set.is_empty() {
                return Err(ModelSyncError::invalid(format!(
                    "models[{source}] 必须为非空字符串的非空无重复列表"
                )));
            }
            request.insert(source.clone(), Some(set));
        }
    }
    Ok(Some(request))
}

fn resolve_probe_selection(
    request: Option<&ProbeSelectionRequest>,
    records: &Map<String, Value>,
) -> Result<Option<ProbeSelection>, ModelSyncError> {
    let Some(request) = request else {
        return Ok(None);
    };
    let mut present: ProbeSelection = BTreeMap::new();
    for (key, record) in records {
        let Some(record) = record.as_object() else {
            continue;
        };
        let (prefix, suffix) = split_key(key);
        let source = record
            .get("source")
            .and_then(Value::as_str)
            .filter(|source| !source.is_empty())
            .map(str::to_string)
            .unwrap_or(prefix);
        let model_id = record
            .get("model_id")
            .and_then(Value::as_str)
            .filter(|model_id| !model_id.is_empty())
            .map(str::to_string)
            .unwrap_or(suffix);
        present.entry(source).or_default().insert(model_id);
    }
    let mut resolved = BTreeMap::new();
    let mut missing: Vec<String> = Vec::new();
    for (source, ids) in request {
        let queued = present.get(source).cloned().unwrap_or_default();
        if let Some(ids) = ids {
            for model_id in ids.difference(&queued) {
                missing.push(format!("{source}|{model_id}"));
            }
            resolved.insert(source.clone(), ids.clone());
        } else {
            resolved.insert(source.clone(), queued);
        }
    }
    if !missing.is_empty() {
        missing.sort();
        return Err(ModelSyncError::invalid(format!(
            "以下模型不在探测队列：{}",
            join(&missing, ", ")
        )));
    }
    Ok(Some(resolved))
}

fn budget_note(steps: &[i64]) -> String {
    let steps_text = format!(
        "({})",
        steps
            .iter()
            .map(|step| step.to_string())
            .collect::<Vec<_>>()
            .join(", ")
    );
    format!(
        "估算按固定步进表 {steps_text}（输入为字符数，不做字符→token 换算）；无探测端点或凭据不可读的来源（含已暂停）runner 零请求跳过，其记录估算为 0，totals 可核对总额；totals 三分口径 runnable+blocked+idle=records 条数：runnable=本轮会消耗请求（pending/stale 或 retest 下的 verified/failed，且有端点且凭据可读），blocked=本轮有资格但无端点或凭据不可读，idle=本轮不消耗请求（verified/failed 未 retest、未选中，或 pending 但既有证据已覆盖全梯——runner 零请求直接 verified）；全局请求预算口径：run 动作单次 max_requests ≤ 20、单请求输出 max_tokens ≤ {CONTEXT_PROBE_MAX_TOKENS}；未定价来源不做金额估算"
    )
}

fn read_probe_recovery(paths: &ModelSyncPaths) -> Value {
    fs::read_to_string(&paths.recovery_file)
        .ok()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .filter(Value::is_object)
        .unwrap_or(Value::Null)
}

struct ProbeOutcome {
    used: i64,
    stopped: Option<String>,
    results: Vec<Value>,
    max_requests: i64,
    max_tokens: i64,
}

fn seed_context_probes(
    catalogs: &Catalogs,
    state: &mut Value,
    model_filter: Option<&ProbeSelection>,
    allow_partial_catalog: bool,
) {
    let Some(state_object) = state.as_object_mut() else {
        return;
    };
    let mut queue = state_object
        .get("contextProbes")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let counts = state_object
        .get("catalogCounts")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let now = now_iso();
    for (source, models) in catalogs {
        let previous_count = if allow_partial_catalog {
            None
        } else {
            counts.get(source).and_then(Value::as_i64)
        };
        if !validate_catalog(source, models, previous_count) {
            continue;
        }
        if PAUSED_SOURCES.contains(&source.as_str()) {
            continue;
        }
        let selected = model_filter.and_then(|filter| filter.get(source));
        for model in models {
            if let Some(selected) = selected {
                if !selected.contains(&model.model_id) {
                    continue;
                }
            }
            let key = format!("{}|{}", source, model.model_id);
            let Some(existing) = queue.get_mut(&key) else {
                queue.insert(
                    key,
                    json!({
                        "source": source,
                        "model_id": model.model_id,
                        "declared_context": model.context,
                        "declared_context_known": model.context.is_some(),
                        "declared_note": if model.context.is_some() {
                            Value::Null
                        } else {
                            json!(DECLARED_CONTEXT_UNKNOWN_NOTE)
                        },
                        "declared_at": now,
                        "status": "pending",
                        "observed_floor": null,
                        "attempts": 0,
                        "last_run": null,
                        "last_error": null,
                        "last_success": null,
                        "token_evidence": null,
                        "evidence_revision": 1,
                        "stale_reason": null,
                        "scheme_revision": PROBE_SCHEME_REVISION,
                        "history": [],
                    }),
                );
                continue;
            };
            let Some(record) = existing.as_object_mut() else {
                continue;
            };
            let old_declared = record.get("declared_context").and_then(Value::as_i64);
            let declared_changed = old_declared != model.context;
            let record_scheme = record.get("scheme_revision").and_then(Value::as_i64);
            let scheme_mismatch =
                record_scheme.unwrap_or(1) != PROBE_SCHEME_REVISION;
            if !declared_changed && !scheme_mismatch {
                continue;
            }
            let reason = if declared_changed {
                "declared_context_changed"
            } else if record_scheme.is_some() {
                "probe_scheme_changed"
            } else {
                "probe_scheme_missing"
            };
            if declared_changed {
                record.insert("declared_context".to_string(), json!(model.context));
                record.insert("declared_at".to_string(), json!(now));
            }
            record.insert(
                "declared_context_known".to_string(),
                json!(model.context.is_some()),
            );
            record.insert(
                "declared_note".to_string(),
                if model.context.is_some() {
                    Value::Null
                } else {
                    json!(DECLARED_CONTEXT_UNKNOWN_NOTE)
                },
            );
            let archived = archive_probe_generation(record, reason);
            let revision = record_int(record, "evidence_revision", 0);
            if archived
                || record.get("status").and_then(Value::as_str) != Some("stale")
                || revision < 1
            {
                record.insert("evidence_revision".to_string(), json!(revision + 1));
            }
            record.insert("status".to_string(), json!("stale"));
            record.insert("stale_reason".to_string(), json!(reason));
            record.insert("scheme_revision".to_string(), json!(PROBE_SCHEME_REVISION));
        }
    }
    state_object.insert("contextProbes".to_string(), Value::Object(queue));
}

fn validate_catalog(
    source: &str,
    models: &[CatalogModel],
    previous_count: Option<i64>,
) -> bool {
    let _ = source;
    if models.is_empty() {
        return false;
    }
    let ids: Vec<&str> = models.iter().map(|model| model.model_id.as_str()).collect();
    if ids.iter().any(|id| id.trim().is_empty()) {
        return false;
    }
    let unique: BTreeSet<&str> = ids.iter().copied().collect();
    if unique.len() != ids.len() {
        return false;
    }
    if let Some(previous) = previous_count.filter(|count| *count > 0) {
        let drop_ratio = (previous - ids.len() as i64) as f64 / previous as f64;
        if drop_ratio > 0.5 {
            return false;
        }
    }
    true
}

fn archive_probe_generation(record: &mut Map<String, Value>, reason: &str) -> bool {
    let has_evidence = [
        "observed_floor",
        "observed_ceiling",
        "last_run",
        "last_success",
        "last_error",
        "token_evidence",
    ]
    .iter()
    .any(|field| record.get(*field).map(|value| !value.is_null()).unwrap_or(false))
        || record
            .get("attempts")
            .map(|value| match value {
                Value::Number(number) => number.as_i64().map(|value| value != 0).unwrap_or(false),
                Value::Bool(value) => *value,
                Value::String(text) => !text.is_empty(),
                _ => false,
            })
            .unwrap_or(false);
    if !has_evidence {
        return false;
    }
    let mut history = record
        .get("history")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    history.push(json!({
        "status": record.get("status").cloned().unwrap_or(Value::Null),
        "evidence_revision": record.get("evidence_revision").cloned().unwrap_or(Value::Null),
        "observed_floor": record.get("observed_floor").cloned().unwrap_or(Value::Null),
        "observed_ceiling": record.get("observed_ceiling").cloned().unwrap_or(Value::Null),
        "attempts": record.get("attempts").cloned().unwrap_or(Value::Null),
        "last_run": record.get("last_run").cloned().unwrap_or(Value::Null),
        "last_success": record.get("last_success").cloned().unwrap_or(Value::Null),
        "last_error": record.get("last_error").cloned().unwrap_or(Value::Null),
        "token_evidence": record.get("token_evidence").cloned().unwrap_or(Value::Null),
        "stale_reason": record.get("stale_reason").cloned().unwrap_or(Value::Null),
        "reason": reason,
        "archived_at": now_iso(),
    }));
    if history.len() > PROBE_HISTORY_MAX {
        history = history.split_off(history.len() - PROBE_HISTORY_MAX);
    }
    record.insert("history".to_string(), Value::Array(history));
    for field in [
        "observed_floor",
        "observed_ceiling",
        "last_run",
        "last_success",
        "last_error",
        "token_evidence",
        "stale_reason",
    ] {
        record.insert(field.to_string(), Value::Null);
    }
    record.insert("attempts".to_string(), json!(0));
    true
}

fn context_probe_input(length: i64, marker: &str) -> String {
    let template = "[ctxprobe %s] anchor %06d of the long-input retention check";
    let mut lines = Vec::new();
    let mut used = 0i64;
    let mut index = 0i64;
    while used < length {
        index += 1;
        let line = template
            .replace("%s", marker)
            .replace("%06d", &format!("{index:06}"));
        used += line.chars().count() as i64 + 1;
        lines.push(line);
    }
    lines.push(format!(
        "[ctxprobe {marker}] FINAL anchor — quote this line verbatim"
    ));
    lines.join("\n")
}

fn tail_anchor(body: &Value, marker: &str) -> bool {
    let Some(choices) = body.get("choices").and_then(Value::as_array) else {
        return false;
    };
    let token = format!("[ctxprobe {marker}] FINAL");
    choices.iter().any(|choice| {
        choice
            .get("message")
            .and_then(Value::as_object)
            .and_then(|message| message.get("content"))
            .and_then(Value::as_str)
            .map(|content| content.contains(&token))
            .unwrap_or(false)
    })
}

fn probe_error_text(body: &Value) -> String {
    let Some(object) = body.as_object() else {
        return value_to_string(Some(body));
    };
    for key in ["error", "message"] {
        if let Some(value) = object.get(key) {
            if value_truthy(value) {
                return value_to_string(Some(value));
            }
        }
    }
    String::new()
}

fn trusted_prompt_tokens(body: &Value) -> Option<i64> {
    let usage = body.get("usage").and_then(Value::as_object)?;
    let reported = |value: Option<&Value>| -> Option<i64> {
        value
            .and_then(Value::as_i64)
            .filter(|tokens| *tokens >= 0)
    };
    reported(usage.get("prompt_tokens")).or_else(|| reported(usage.get("input_tokens")))
}

fn explicit_context_limit(status: u16, body: &Value) -> bool {
    if !(400..500).contains(&status) {
        return false;
    }
    let text = probe_error_text(body).to_lowercase();
    CONTEXT_LIMIT_MARKERS
        .iter()
        .any(|marker| text.contains(marker))
}

fn report_probe_progress(ctx: &OperationContext, outcome: &ProbeOutcome) {
    let rows: Vec<&Value> = outcome.results.iter().collect();
    let executed: Vec<&&Value> = rows
        .iter()
        .filter(|row| row["requests"].as_i64().unwrap_or_default() > 0)
        .collect();
    for row in executed.iter().take(PROBE_PROGRESS_MAX) {
        let note = format!(
            "上下文探测 {}：{}，请求 {}",
            row["key"].as_str().unwrap_or_default(),
            row["status"].as_str().unwrap_or_default(),
            row["requests"].as_i64().unwrap_or_default()
        );
        let _ = ctx.report_step(&single_line(&note));
    }
    let mut summary = format!("上下文探测结束：发送 {} 请求", outcome.used);
    if let Some(stopped) = &outcome.stopped {
        summary.push_str(&format!("（{stopped} 停止）"));
    }
    let _ = ctx.report_step(&single_line(&summary));
}

fn probe_result(
    outcome: &ProbeOutcome,
    selection: Option<&ProbeSelection>,
    retest: bool,
    operation_id: &str,
) -> Value {
    let used = outcome.used;
    let rows: Vec<&Value> = outcome.results.iter().collect();
    let executed: Vec<&&Value> = rows
        .iter()
        .filter(|row| row["requests"].as_i64().unwrap_or_default() > 0)
        .collect();
    let not_executed: Vec<&&Value> = rows
        .iter()
        .filter(|row| row["requests"].as_i64().unwrap_or_default() == 0)
        .collect();
    let mut summary = format!("上下文探测结束：发送 {used} 请求");
    if let Some(stopped) = &outcome.stopped {
        summary.push_str(&format!("（{stopped} 停止）"));
    }
    json!({
        "requests_used": used,
        "stopped": outcome.stopped,
        "results": rows.iter().take(PROBE_RESULT_MAX_ROWS).map(|row| (*row).clone()).collect::<Vec<_>>(),
        "result_total": rows.len(),
        "results_truncated": rows.len() > PROBE_RESULT_MAX_ROWS,
        "executed_models": executed
            .iter()
            .take(PROBE_RESULT_MAX_ROWS)
            .map(|row| row["key"].clone())
            .collect::<Vec<_>>(),
        "executed_total": executed.len(),
        "not_executed": not_executed
            .iter()
            .take(PROBE_RESULT_MAX_ROWS)
            .map(|row| json!({
                "key": row["key"].clone(),
                "status": row["status"].clone(),
                "note": row["note"].clone(),
            }))
            .collect::<Vec<_>>(),
        "not_executed_total": not_executed.len(),
        "budget": {
            "max_requests": outcome.max_requests,
            "max_tokens": outcome.max_tokens,
            "requests_used": used,
            "remaining_requests": (outcome.max_requests - used).max(0),
        },
        "retest": retest,
        "selection": selection_payload(selection),
        "summary": single_line(&summary),
        "cost_note": COST_NOTE,
        "operation_id": operation_id,
    })
}

fn selection_payload(
    selection: Option<&ProbeSelection>,
) -> Value {
    let Some(selection) = selection else {
        return Value::Null;
    };
    let mut output = Map::new();
    for (source, ids) in selection {
        output.insert(
            source.clone(),
            Value::Array(ids.iter().map(|id| json!(id)).collect()),
        );
    }
    Value::Object(output)
}

fn selected_record_keys(
    selection: Option<&ProbeSelection>,
    records: &Map<String, Value>,
) -> Value {
    let mut output = Map::new();
    let mut keys: Vec<&String> = records.keys().collect();
    keys.sort();
    for key in keys {
        let Some(record) = records.get(key).and_then(Value::as_object) else {
            continue;
        };
        let (prefix, suffix) = split_key(key);
        let source = record
            .get("source")
            .and_then(Value::as_str)
            .filter(|source| !source.is_empty())
            .map(str::to_string)
            .unwrap_or(prefix);
        let model_id = record
            .get("model_id")
            .and_then(Value::as_str)
            .filter(|model_id| !model_id.is_empty())
            .map(str::to_string)
            .unwrap_or(suffix);
        if let Some(selection) = selection {
            if !selection
                .get(&source)
                .map(|ids| ids.contains(&model_id))
                .unwrap_or(false)
            {
                continue;
            }
        }
        output.insert(key.clone(), records[key].clone());
        if output.len() >= PROBE_RECOVERY_MAX_RECORDS {
            break;
        }
    }
    Value::Object(output)
}

fn write_probe_recovery(
    paths: &ModelSyncPaths,
    payload: &Value,
) -> Result<PathBuf, ModelSyncError> {
    let mut payload = payload.clone();
    if let Some(previous) = read_probe_recovery(paths).as_object().cloned() {
        let mut previous = previous;
        previous.remove("previous_recovery");
        if let Some(object) = payload.as_object_mut() {
            object.insert("previous_recovery".to_string(), Value::Object(previous));
        }
    }
    let text = serde_json::to_string_pretty(&payload)
        .map(|text| text + "\n")
        .map_err(|error| ModelSyncError::new("recovery_failed", error.to_string()))?;
    if let Some(parent) = paths.recovery_file.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| ModelSyncError::new("recovery_failed", error.to_string()))?;
    }
    let temporary = paths.recovery_file.with_file_name(format!(
        "model-sync-probe-recovery.json.{}.{}.tmp",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or_default()
    ));
    let write_result = (|| -> std::io::Result<()> {
        {
            let mut handle = File::create(&temporary)?;
            handle.write_all(text.as_bytes())?;
            handle.sync_all()?;
        }
        replace_with_retry(&temporary, &paths.recovery_file)
    })();
    if let Err(error) = write_result {
        let _ = fs::remove_file(&temporary);
        return Err(ModelSyncError::new("recovery_failed", error.to_string()));
    }
    Ok(paths.recovery_file.clone())
}

fn clear_probe_recovery(paths: &ModelSyncPaths) {
    let _ = fs::remove_file(&paths.recovery_file);
}

fn merge_probe_recovery(paths: &ModelSyncPaths, state: &mut Value) {
    let recovery = read_probe_recovery(paths);
    let Some(recovery) = recovery.as_object() else {
        return;
    };
    let Some(records) = recovery.get("records").and_then(Value::as_object) else {
        return;
    };
    let Some(state_object) = state.as_object_mut() else {
        return;
    };
    let mut queue = state_object
        .get("contextProbes")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let previous = recovery.get("previous_recovery").and_then(Value::as_object);
    let previous_records = previous.and_then(|previous| previous.get("records"));
    for source_records in [Some(records), previous_records.and_then(Value::as_object)] {
        let Some(source_records) = source_records else {
            continue;
        };
        for (key, record) in source_records {
            let Some(record) = record.as_object() else {
                continue;
            };
            let current = queue.get(key).and_then(Value::as_object);
            let Some(current) = current else {
                queue.insert(key.clone(), Value::Object(record.clone()));
                continue;
            };
            let record_last = record
                .get("last_run")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let current_last = current
                .get("last_run")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if record_last > current_last {
                queue.insert(key.clone(), Value::Object(record.clone()));
            }
        }
    }
    state_object.insert("contextProbes".to_string(), Value::Object(queue));
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

fn context_of(raw: &Map<String, Value>) -> Option<i64> {
    for key in ["max_model_len", "context_length", "contextWindow", "context_window"] {
        let Some(value) = raw.get(key) else { continue };
        if value.is_null() || value == &Value::Bool(false) || value == &json!(0) {
            continue;
        }
        if let Some(context) = int_value(value) {
            return Some(context);
        }
    }
    None
}

fn is_virtual_model(model_id: &str) -> bool {
    ["-Fast", "-Low", "-Medium", "-High"]
        .iter()
        .any(|suffix| model_id.ends_with(suffix))
}

fn scope_str_list(value: &Value, name: &str) -> Result<Vec<String>, ModelSyncError> {
    let parsed = match value {
        Value::String(text) => serde_json::from_str::<Value>(text).map_err(|_| {
            ModelSyncError::invalid(format!("{name} 必须为字符串数组"))
        })?,
        other => other.clone(),
    };
    let Some(items) = parsed.as_array() else {
        return Err(ModelSyncError::invalid(format!("{name} 必须为字符串数组")));
    };
    let mut values = Vec::new();
    for item in items {
        let Some(text) = item.as_str().filter(|text| !text.trim().is_empty()) else {
            return Err(ModelSyncError::invalid(format!("{name} 必须为字符串数组")));
        };
        values.push(text.to_string());
    }
    Ok(values)
}

fn payload_error_text(payload: &Value) -> String {
    let Some(object) = payload.as_object() else {
        return String::new();
    };
    for key in ["error", "detail"] {
        if let Some(value) = object.get(key) {
            if value_truthy(value) {
                return value_to_string(Some(value)).to_lowercase();
            }
        }
    }
    String::new()
}

fn value_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64().map(|value| value != 0.0).unwrap_or(true),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(object) => !object.is_empty(),
    }
}

fn has_auth_marker(text: &str) -> bool {
    AUTH_PAGE_MARKERS
        .iter()
        .any(|marker| text.contains(marker))
}

fn looks_like_html(raw: &[u8]) -> bool {
    let head: Vec<u8> = raw
        .iter()
        .copied()
        .skip_while(|byte| byte.is_ascii_whitespace())
        .take(512)
        .map(|byte| byte.to_ascii_lowercase())
        .collect();
    head.starts_with(b"<!doctype html") || head.windows(5).any(|window| window == b"<html")
}

fn looks_like_auth_page(raw: &[u8]) -> bool {
    let text = String::from_utf8_lossy(raw);
    let text: String = text.chars().take(4096).collect::<String>().to_lowercase();
    has_auth_marker(&text)
}

fn value_to_string(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(Value::Bool(value)) => {
            if *value {
                "True".to_string()
            } else {
                "False".to_string()
            }
        }
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Array(_)) | Some(Value::Object(_)) => value.unwrap().to_string(),
    }
}

fn int_value(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number.as_i64().or_else(|| {
            number
                .as_f64()
                .filter(|value| value.is_finite())
                .map(|value| value.trunc() as i64)
        }),
        Value::String(text) => text.trim().parse::<i64>().ok(),
        Value::Bool(value) => Some(i64::from(*value)),
        _ => None,
    }
}

fn int_or_none(value: Option<&Value>) -> Option<i64> {
    // Python `_int_or_none`: only a non-bool JSON integer is trusted; a
    // float, string, bool or null reads as unknown instead of being coerced.
    value.and_then(Value::as_i64)
}

fn record_int(record: &Map<String, Value>, field: &str, default: i64) -> i64 {
    match record.get(field) {
        Some(Value::Bool(_)) | None => default,
        Some(value) => value.as_i64().unwrap_or(default),
    }
}

fn single_line(text: &str) -> String {
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    text.chars().take(ERROR_LIMIT).collect()
}

fn join<T: std::fmt::Display>(items: &[T], separator: &str) -> String {
    items
        .iter()
        .map(|item| item.to_string())
        .collect::<Vec<_>>()
        .join(separator)
}

fn now_iso() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}

fn epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

fn http_request(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Option<&[u8]>,
    timeout: Duration,
    cancel: Option<&dyn Fn() -> bool>,
) -> Result<HttpResponse, HttpFailure> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, timeout.min(Duration::from_secs(10)))
        .map_err(|error| {
            if error.kind() == std::io::ErrorKind::TimedOut {
                HttpFailure::Timeout
            } else {
                HttpFailure::ConnectionError
            }
        })?;
    let _ = stream.set_read_timeout(Some(Duration::from_millis(100)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(10)));
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: application/json\r\nConnection: close\r\n"
    );
    for (name, value) in headers {
        request.push_str(name);
        request.push_str(": ");
        request.push_str(value);
        request.push_str("\r\n");
    }
    if let Some(body) = body {
        request.push_str("Content-Type: application/json\r\n");
        request.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    request.push_str("\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|_| HttpFailure::ConnectionError)?;
    if let Some(body) = body {
        stream
            .write_all(body)
            .map_err(|_| HttpFailure::ConnectionError)?;
    }
    stream.flush().map_err(|_| HttpFailure::ConnectionError)?;
    let deadline = Instant::now() + timeout;
    let mut raw = Vec::new();
    let mut buffer = [0u8; 8192];
    let header_end = loop {
        if cancelled(cancel) {
            return Err(HttpFailure::Cancelled);
        }
        if let Some(position) = raw.windows(4).position(|window| window == b"\r\n\r\n") {
            break position + 4;
        }
        if raw.len() > HTTP_HEADER_MAX {
            return Err(HttpFailure::ProtocolError);
        }
        if Instant::now() >= deadline {
            return Err(HttpFailure::Timeout);
        }
        match stream.read(&mut buffer) {
            Ok(0) => return Err(HttpFailure::ProtocolError),
            Ok(read) => raw.extend_from_slice(&buffer[..read]),
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock
                    || error.kind() == std::io::ErrorKind::TimedOut => {}
            Err(_) => return Err(HttpFailure::ConnectionError),
        }
    };
    let head = String::from_utf8_lossy(&raw[..header_end]);
    let mut lines = head.split("\r\n");
    let status = lines
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or(HttpFailure::ProtocolError)?;
    let mut response_headers = HashMap::new();
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            response_headers.insert(name.trim().to_ascii_lowercase(), value.trim().to_string());
        }
    }
    let mut body = raw[header_end..].to_vec();
    if response_headers
        .get("transfer-encoding")
        .map(|value| value.to_ascii_lowercase().contains("chunked"))
        .unwrap_or(false)
    {
        while !chunked_complete(&body)? {
            read_more(
                &mut stream,
                &mut body,
                &mut buffer,
                deadline,
                cancel,
            )?;
            if body.len() > HTTP_BODY_MAX {
                return Err(HttpFailure::ProtocolError);
            }
        }
    } else if let Some(length) = response_headers
        .get("content-length")
        .and_then(|value| value.parse::<usize>().ok())
    {
        while body.len() < length {
            read_more(
                &mut stream,
                &mut body,
                &mut buffer,
                deadline,
                cancel,
            )?;
            if body.len() > HTTP_BODY_MAX {
                return Err(HttpFailure::ProtocolError);
            }
        }
        body.truncate(length);
    } else {
        loop {
            match read_more(&mut stream, &mut body, &mut buffer, deadline, cancel) {
                Ok(true) => {}
                Ok(false) => break,
                Err(error) => return Err(error),
            }
            if body.len() > HTTP_BODY_MAX {
                return Err(HttpFailure::ProtocolError);
            }
        }
    }
    Ok(HttpResponse {
        status,
        headers: response_headers,
        body,
    })
}

fn cancelled(cancel: Option<&dyn Fn() -> bool>) -> bool {
    cancel.map(|cancel| cancel()).unwrap_or(false)
}

fn read_more(
    stream: &mut TcpStream,
    raw: &mut Vec<u8>,
    buffer: &mut [u8],
    deadline: Instant,
    cancel: Option<&dyn Fn() -> bool>,
) -> Result<bool, HttpFailure> {
    loop {
        if cancelled(cancel) {
            return Err(HttpFailure::Cancelled);
        }
        if Instant::now() >= deadline {
            return Err(HttpFailure::Timeout);
        }
        match stream.read(buffer) {
            Ok(0) => return Ok(false),
            Ok(read) => {
                raw.extend_from_slice(&buffer[..read]);
                return Ok(true);
            }
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock
                    || error.kind() == std::io::ErrorKind::TimedOut => {}
            Err(_) => return Err(HttpFailure::ConnectionError),
        }
    }
}

/// Whether the accumulated bytes contain a complete chunked body. This only
/// walks the framing; it never trusts a declared size beyond the buffer.
fn chunked_complete(raw: &[u8]) -> Result<bool, HttpFailure> {
    let mut index = 0;
    loop {
        let Some(line_end) = find_crlf(raw, index) else {
            return Ok(false);
        };
        let line = String::from_utf8_lossy(&raw[index..line_end]);
        let size_text = line.split(';').next().unwrap_or("").trim();
        let size = usize::from_str_radix(size_text, 16).map_err(|_| HttpFailure::ProtocolError)?;
        index = line_end + 2;
        if size == 0 {
            // The trailer (if any) must end at a CRLF; the terminator itself
            // is enough for the caller's JSON parser.
            return Ok(true);
        }
        if raw.len() < index + size + 2 {
            return Ok(false);
        }
        index += size;
        if raw.get(index..index + 2) != Some(b"\r\n") {
            return Err(HttpFailure::ProtocolError);
        }
        index += 2;
    }
}

fn find_crlf(raw: &[u8], from: usize) -> Option<usize> {
    raw.get(from..)
        .and_then(|slice| slice.windows(2).position(|window| window == b"\r\n"))
        .map(|position| from + position)
}
