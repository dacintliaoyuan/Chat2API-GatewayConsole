//! The settings page's UM model catalog (#153) and context probes (#154).
//!
//! The App reads the instance gateway's `/admin/models` face and renders one row
//! per model: name/ID, context, thinking efforts and the last context test. The
//! read keeps a missing context or effort list as 「未知」 instead of inventing a
//! number, and an empty catalog is a legal empty state, not stale data.
//!
//! The probe face reads `/admin/model-context/probes` for the batch queue, the per
//! model results and the history; a failure keeps its reason and a missing budget
//! or result field reads 「不可用」 instead of a zero.

use std::collections::BTreeMap;
use std::time::Duration;

use serde_json::Value;

use crate::gateway::ControlToken;
use crate::overview::Tone;

/// One model row the settings table draws.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelRow {
    pub id: String,
    pub name: String,
    pub context: String,
    pub efforts: String,
    pub last_test: String,
    /// The full facts behind the row; the row's 详情 shows this verbatim.
    pub detail: String,
}

/// One catalog read: the rows, the status copy, and whether the table is a
/// previous read kept because the latest refresh failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CatalogView {
    pub status: String,
    pub tone: Tone,
    pub stale: bool,
    /// True only when the latest read succeeded with a legal empty catalog;
    /// a failed first read is not the 「无模型」 state.
    pub empty: bool,
    pub models: Vec<ModelRow>,
}

/// Parses the gateway's `/admin/models` payload into the table rows.
///
/// A missing `models` array or a row without an id is not a legal empty catalog:
/// the error keeps its reason instead of showing an empty table.
pub fn parse_catalog(payload: &Value) -> Result<CatalogView, String> {
    let Some(items) = payload.get("models").and_then(Value::as_array) else {
        return Err("UM 模型目录格式无效：缺少 models 数组".to_string());
    };
    let mut models = Vec::with_capacity(items.len());
    for item in items {
        let Some(id) = item
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
        else {
            return Err("UM 模型目录格式无效：模型缺少 ID".to_string());
        };
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
            .unwrap_or(id);
        let context = context_label(item);
        let efforts = efforts_label(item);
        let last_test = last_test_label(item.get("last_test"));
        let detail = format!(
            "模型名称：{name}\n模型 ID：{id}\n上下文：{context}\n思考档位：{efforts}\n最近测试：{last_test}"
        );
        models.push(ModelRow {
            id: id.to_string(),
            name: name.to_string(),
            context,
            efforts,
            last_test,
            detail,
        });
    }
    Ok(CatalogView {
        status: if models.is_empty() {
            "UM 目录是空的：无模型".to_string()
        } else {
            format!("UM 目录共 {} 个模型", models.len())
        },
        tone: Tone::Neutral,
        stale: false,
        empty: models.is_empty(),
        models,
    })
}

/// The gateway's `/admin/models` read, behind a seam so the refresh state
/// machine is tested without a live gateway.
pub trait CatalogReader: Send + Sync {
    fn read_catalog(
        &self,
        base_url: &str,
        token: &ControlToken,
        fresh: bool,
    ) -> Result<Value, String>;
}

/// The production reader. Every settings-page read is strict: the gateway must go
/// to the upstream and must not answer from its cache.
pub struct HttpCatalogReader {
    agent: ureq::Agent,
}

impl HttpCatalogReader {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_secs(5))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpCatalogReader {
    fn default() -> Self {
        Self::new()
    }
}

impl CatalogReader for HttpCatalogReader {
    fn read_catalog(
        &self,
        base_url: &str,
        token: &ControlToken,
        fresh: bool,
    ) -> Result<Value, String> {
        let mut url = format!("{}/admin/models", base_url.trim_end_matches('/'));
        if fresh {
            url.push_str("?fresh=1");
        }
        let response = self
            .agent
            .get(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map_err(|error| catalog_error(&url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 模型目录不是有效 JSON：{error}"))
    }
}

/// One strict refresh: a failure keeps the previous rows and marks them stale, an
/// empty catalog replaces the old rows with the legal empty state.
pub fn refresh_catalog(
    reader: &dyn CatalogReader,
    base_url: &str,
    token: &ControlToken,
    previous: Option<&CatalogView>,
) -> CatalogView {
    match reader
        .read_catalog(base_url, token, true)
        .and_then(|payload| parse_catalog(&payload))
    {
        Ok(mut view) => {
            if view.models.is_empty() {
                view.status = "UM 目录是空的：无模型（后端合法地返回了 0 个模型）".to_string();
                view.tone = Tone::Neutral;
            } else {
                view.status = format!(
                    "已从 UM 取到最新目录（{}）；刷新只读目录，不探测、不改客户端配置。",
                    clock_now()
                );
                view.tone = Tone::Good;
            }
            view.stale = false;
            view.empty = view.models.is_empty();
            view
        }
        Err(reason) => catalog_failure(reason, previous),
    }
}

/// The failure view: a previous table stays visible and becomes stale; a first
/// read without a table keeps the reason and does not claim the 无模型 state.
pub fn catalog_failure(reason: String, previous: Option<&CatalogView>) -> CatalogView {
    match previous {
        Some(previous) => CatalogView {
            status: format!("刷新失败：{reason}；下表仍是上次取到的目录，已过期。"),
            tone: Tone::Bad,
            stale: true,
            empty: false,
            models: previous.models.clone(),
        },
        None => CatalogView {
            status: format!("刷新失败：{reason}"),
            tone: Tone::Bad,
            stale: false,
            empty: false,
            models: Vec::new(),
        },
    }
}

fn catalog_error(url: &str, error: ureq::Error) -> String {
    match error {
        ureq::Error::Status(code, response) => {
            let detail = response
                .into_json::<Value>()
                .ok()
                .and_then(|body| {
                    body.get("error")
                        .and_then(|error| error.get("message"))
                        .or_else(|| body.get("error"))
                        .and_then(Value::as_str)
                        .map(str::to_string)
                })
                .filter(|detail| !detail.is_empty() && detail.len() <= 120);
            match detail {
                Some(detail) => format!("{url}: 本机网关返回 HTTP {code}（{detail}）"),
                None => format!("{url}: 本机网关返回 HTTP {code}"),
            }
        }
        ureq::Error::Transport(error) => match error.kind() {
            ureq::ErrorKind::ConnectionFailed => {
                format!("{url}: 无法连接本机网关（连接被拒绝）")
            }
            _ => format!("{url}: 无法连接本机网关"),
        },
    }
}

fn clock_now() -> String {
    chrono::Local::now().format("%H:%M:%S").to_string()
}

/// The window fields the settings catalog card owns.
///
/// The Slint shell implements this for its `MainWindow`; the headless tests
/// implement it on the same window type so [`apply_catalog`] is asserted through
/// the visible properties, not through Slint internals.
pub trait CatalogSurface {
    fn set_models(&self, rows: &[ModelRow]);
    fn set_status(&self, status: &str);
    fn set_tone(&self, tone: Tone);
    fn set_stale(&self, stale: bool);
    fn set_empty(&self, empty: bool);
    fn set_loading(&self, loading: bool);
    fn selected_model(&self) -> String;
    fn set_selected_model(&self, id: &str);
    fn set_detail(&self, title: &str, body: &str);
}

/// Apply one read result to the settings catalog card.
///
/// A row's detail closes when the new catalog no longer contains the selected
/// model; a failed refresh that kept the rows keeps the open detail.
pub fn apply_catalog<S: CatalogSurface>(surface: &S, view: &CatalogView) {
    surface.set_models(&view.models);
    surface.set_status(&view.status);
    surface.set_tone(view.tone);
    surface.set_stale(view.stale);
    surface.set_empty(view.empty);
    surface.set_loading(false);
    let selected = surface.selected_model();
    if !selected.is_empty() && !view.models.iter().any(|model| model.id == selected) {
        surface.set_selected_model("");
        surface.set_detail("", "");
    }
}

/// Open one model's 详情, or close it when the same model is activated again.
pub fn open_model<S: CatalogSurface>(surface: &S, view: Option<&CatalogView>, id: &str) {
    if surface.selected_model() == id {
        surface.set_selected_model("");
        surface.set_detail("", "");
        return;
    }
    let Some(row) = view.and_then(|view| view.models.iter().find(|row| row.id == id)) else {
        return;
    };
    surface.set_selected_model(row.id.as_str());
    surface.set_detail(row.name.as_str(), row.detail.as_str());
}

/// The request page's model dropdown. The ledger only lists models that already
/// appear in the selected window, so a catalog model with no request yet would be
/// impossible to select; the union keeps every catalog model selectable without a
/// second HTTP read.
pub fn request_model_options(
    ledger_models: &[String],
    catalog: Option<&CatalogView>,
) -> Vec<String> {
    let mut models = ledger_models.to_vec();
    if let Some(catalog) = catalog {
        for model in &catalog.models {
            if !models.iter().any(|known| known == &model.id) {
                models.push(model.id.clone());
            }
        }
    }
    models.sort();
    models
}

fn context_label(item: &Value) -> String {
    let declared = item
        .get("context")
        .and_then(|value| match value {
            Value::Number(number) => number.as_i64(),
            Value::String(text) => text.trim().parse::<i64>().ok(),
            _ => None,
        })
        .filter(|tokens| *tokens > 0);
    match declared {
        Some(tokens) => format!("{} Token", thousands(tokens)),
        None => "未知".to_string(),
    }
}

fn efforts_label(item: &Value) -> String {
    let levels: Vec<&str> = item
        .get("efforts")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .filter(|level| !level.is_empty())
                .collect()
        })
        .unwrap_or_default();
    if levels.is_empty() {
        return "未知".to_string();
    }
    let mut label = levels.join(" · ");
    if let Some(default) = item
        .get("default_effort")
        .and_then(Value::as_str)
        .filter(|default| !default.is_empty())
    {
        label.push_str(&format!("（默认 {default}）"));
    }
    label
}

fn last_test_label(last_test: Option<&Value>) -> String {
    let Some(record) = last_test.filter(|record| record.is_object()) else {
        return "未测".to_string();
    };
    let status = record
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    let mut label = match status {
        "verified" => match record.get("observed_floor").and_then(Value::as_i64) {
            Some(floor) => format!("已验证 ≥{} Token", thousands(floor)),
            None => "已验证".to_string(),
        },
        "failed" => {
            let mut text = "未通过".to_string();
            if let Some(ceiling) = record.get("observed_ceiling").and_then(Value::as_i64) {
                text.push_str(&format!(" · 上限 {} Token", thousands(ceiling)));
            }
            text
        }
        "incomplete" => "未完成".to_string(),
        "cancelled" => "已取消".to_string(),
        "pending" => "等待中".to_string(),
        "running" => "测试中".to_string(),
        other => other.to_string(),
    };
    if let Some(error) = record
        .get("last_error")
        .and_then(Value::as_str)
        .filter(|error| !error.is_empty())
    {
        label.push_str(&format!(" · {error}"));
    }
    if let Some(at) = record.get("last_run").and_then(Value::as_str) {
        label.push_str(&format!(" · {}", time_label(at)));
    }
    label
}

fn time_label(raw: &str) -> String {
    chrono::DateTime::parse_from_rfc3339(raw)
        .ok()
        .map(|time| {
            time.with_timezone(&chrono::Local)
                .format("%m-%d %H:%M:%S")
                .to_string()
        })
        .unwrap_or_else(|| "时间未知".to_string())
}

fn thousands(value: i64) -> String {
    let digits = value.to_string();
    let mut grouped = String::with_capacity(digits.len() + digits.len() / 3);
    for (index, character) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index) % 3 == 0 {
            grouped.push(',');
        }
        grouped.push(character);
    }
    grouped
}

// ---- Context probes (#154) ------------------------------------------------

/// One model row of the settings page's context-probe batch list.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeRow {
    pub model_id: String,
    pub name: String,
    /// The visible test state: 等待中 / 测试中 / 已验证 / 未通过 / 已取消 / 未完成 / 未测.
    pub state: String,
    /// The result or failure reason; a missing result field reads 不可用.
    pub summary: String,
    pub tone: Tone,
    pub selected: bool,
}

/// One row of the collapsible history table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeHistoryRow {
    pub time: String,
    pub model: String,
    pub result: String,
    pub summary: String,
    pub tone: Tone,
}

/// One parsed `/admin/model-context/probes` snapshot plus the batch the App
/// submitted, when there is one, so progress can show 已完成 X/Y.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeView {
    pub status: String,
    pub tone: Tone,
    pub running: bool,
    pub stop_requested: bool,
    pub progress: String,
    pub budget: String,
    pub rows: Vec<ProbeRow>,
    pub history: Vec<ProbeHistoryRow>,
}

/// The gateway's admin probe face, behind a seam so the run/stop state machine is
/// tested without a live gateway.
pub trait ProbeClient: Send + Sync {
    fn read_probes(&self, base_url: &str, token: &ControlToken) -> Result<Value, String>;
    fn start_probes(
        &self,
        base_url: &str,
        token: &ControlToken,
        models: &[String],
    ) -> Result<Value, String>;
    fn stop_probes(&self, base_url: &str, token: &ControlToken) -> Result<Value, String>;
}

/// The production probe client. The gateway queue answers immediately; the probe
/// work itself runs inside the gateway process.
pub struct HttpProbeClient {
    agent: ureq::Agent,
}

impl HttpProbeClient {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_secs(5))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpProbeClient {
    fn default() -> Self {
        Self::new()
    }
}

impl ProbeClient for HttpProbeClient {
    fn read_probes(&self, base_url: &str, token: &ControlToken) -> Result<Value, String> {
        let url = format!(
            "{}/admin/model-context/probes",
            base_url.trim_end_matches('/')
        );
        let response = self
            .agent
            .get(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map_err(|error| catalog_error(&url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 上下文探测快照不是有效 JSON：{error}"))
    }

    fn start_probes(
        &self,
        base_url: &str,
        token: &ControlToken,
        models: &[String],
    ) -> Result<Value, String> {
        let url = format!(
            "{}/admin/model-context/probes",
            base_url.trim_end_matches('/')
        );
        let response = self
            .agent
            .post(&url)
            .set("X-Admin-Control", token.expose())
            .send_json(serde_json::json!({ "models": models }))
            .map_err(|error| catalog_error(&url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 上下文探测响应不是有效 JSON：{error}"))
    }

    fn stop_probes(&self, base_url: &str, token: &ControlToken) -> Result<Value, String> {
        let url = format!(
            "{}/admin/model-context/probes",
            base_url.trim_end_matches('/')
        );
        let response = self
            .agent
            .delete(&url)
            .set("X-Admin-Control", token.expose())
            .call()
            .map_err(|error| catalog_error(&url, error))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 停止探测响应不是有效 JSON：{error}"))
    }
}

/// Parses the gateway's probe snapshot into the batch rows, progress and history.
///
/// `batch` is the model list the App submitted; it gives the progress a known
/// total. `catalog` supplies every selectable model and its display name, so a
/// model without a test record still reads 未测 instead of disappearing.
pub fn parse_probe_snapshot(
    payload: &Value,
    batch: Option<&[String]>,
    catalog: Option<&CatalogView>,
) -> Result<ProbeView, String> {
    let records = payload
        .get("records")
        .and_then(Value::as_array)
        .ok_or_else(|| "上下文探测快照格式无效：缺少 records 数组".to_string())?;
    let queue = payload
        .get("queue")
        .and_then(Value::as_array)
        .ok_or_else(|| "上下文探测快照格式无效：缺少 queue 数组".to_string())?;
    let running = payload
        .get("running")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let stop_requested = payload
        .get("stop_requested")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    let mut by_model: BTreeMap<String, &Value> = BTreeMap::new();
    for record in records {
        let Some(model_id) = record
            .get("model_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
        else {
            return Err("上下文探测快照格式无效：记录缺少模型 ID".to_string());
        };
        by_model.insert(model_id.to_string(), record);
    }
    let mut queued: BTreeMap<String, String> = BTreeMap::new();
    for item in queue {
        let Some(model_id) = item
            .get("model_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
        else {
            return Err("上下文探测快照格式无效：队列缺少模型 ID".to_string());
        };
        let status = item
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("pending");
        queued.insert(model_id.to_string(), status.to_string());
    }

    let mut ids: Vec<String> = Vec::new();
    let mut push = |id: &str| {
        if !ids.iter().any(|known| known == id) {
            ids.push(id.to_string());
        }
    };
    if let Some(catalog) = catalog {
        for model in &catalog.models {
            push(&model.id);
        }
    }
    if let Some(batch) = batch {
        for id in batch {
            push(id);
        }
    }
    for id in by_model.keys().chain(queued.keys()) {
        push(id);
    }

    let names: BTreeMap<String, String> = catalog
        .map(|catalog| {
            catalog
                .models
                .iter()
                .map(|model| (model.id.clone(), model.name.clone()))
                .collect()
        })
        .unwrap_or_default();
    let rows: Vec<ProbeRow> = ids
        .iter()
        .map(|id| probe_row(id, by_model.get(id).copied(), queued.get(id), &names))
        .collect();
    let history = probe_history(&by_model);

    let (verified, failed, cancelled, incomplete) = probe_counts(&by_model);
    let status = if stop_requested {
        if running {
            "已请求停止：在途请求返回后不再开始后续模型".to_string()
        } else {
            "探测已停止：未开始的模型已取消".to_string()
        }
    } else if running {
        "正在执行上下文探测".to_string()
    } else if by_model.is_empty() {
        "尚未执行上下文探测".to_string()
    } else {
        format!(
            "最近一次探测已结束 · 已验证 {verified} · 未通过 {failed} · 已取消 {}",
            cancelled + incomplete
        )
    };
    let tone = if stop_requested || failed > 0 || cancelled + incomplete > 0 {
        Tone::Warn
    } else if !running && !by_model.is_empty() {
        Tone::Good
    } else {
        Tone::Neutral
    };
    let progress = if let Some(batch) = batch {
        let done = batch
            .iter()
            .filter(|id| {
                by_model
                    .get(*id)
                    .map(|record| {
                        record
                            .get("status")
                            .and_then(Value::as_str)
                            .is_some_and(is_probe_terminal)
                    })
                    .unwrap_or(false)
            })
            .count();
        format!("已完成 {done}/{} · 排队 {}", batch.len(), queued.len())
    } else {
        let done = by_model
            .values()
            .filter(|record| {
                record
                    .get("status")
                    .and_then(Value::as_str)
                    .is_some_and(is_probe_terminal)
            })
            .count();
        format!("已完成 {done} · 排队 {}", queued.len())
    };

    Ok(ProbeView {
        status,
        tone,
        running,
        stop_requested,
        progress,
        budget: probe_budget(payload),
        rows,
        history,
    })
}

/// A failed snapshot read keeps the previous rows and history visible; only the
/// status changes to the read reason. A first failure keeps the reason and no rows.
pub fn probe_failure(reason: String, previous: Option<&ProbeView>) -> ProbeView {
    match previous {
        Some(previous) => ProbeView {
            status: format!("读取上下文探测失败：{reason}"),
            tone: Tone::Bad,
            rows: previous.rows.clone(),
            history: previous.history.clone(),
            ..previous.clone()
        },
        None => ProbeView {
            status: format!("读取上下文探测失败：{reason}"),
            tone: Tone::Bad,
            running: false,
            stop_requested: false,
            progress: String::new(),
            budget: "请求预算：不可用".to_string(),
            rows: Vec::new(),
            history: Vec::new(),
        },
    }
}

/// The window fields the settings probe card owns.
pub trait ProbeSurface {
    fn probe_rows(&self) -> Vec<ProbeRow>;
    fn set_probe_rows(&self, rows: &[ProbeRow]);
    fn probe_selection(&self) -> Vec<String>;
    fn set_probe_selection(&self, ids: &[String]);
    fn set_probe_status(&self, status: &str);
    fn set_probe_tone(&self, tone: Tone);
    fn set_probe_progress(&self, progress: &str);
    fn set_probe_budget(&self, budget: &str);
    fn set_probe_running(&self, running: bool);
    fn set_probe_stop_requested(&self, requested: bool);
    fn set_probe_history(&self, rows: &[ProbeHistoryRow]);
    fn set_probe_loading(&self, loading: bool);
}

/// Apply one snapshot to the probe card, keeping the current selection and
/// pruning ids that are no longer in the model list.
pub fn apply_probe<S: ProbeSurface>(surface: &S, view: &ProbeView) {
    let available: Vec<String> = view.rows.iter().map(|row| row.model_id.clone()).collect();
    let selection: Vec<String> = surface
        .probe_selection()
        .into_iter()
        .filter(|id| available.iter().any(|known| known == id))
        .collect();
    let rows: Vec<ProbeRow> = view
        .rows
        .iter()
        .map(|row| ProbeRow {
            selected: selection.iter().any(|id| id == &row.model_id),
            ..row.clone()
        })
        .collect();
    surface.set_probe_rows(&rows);
    surface.set_probe_selection(&selection);
    surface.set_probe_status(&view.status);
    surface.set_probe_tone(view.tone);
    surface.set_probe_progress(&view.progress);
    surface.set_probe_budget(&view.budget);
    surface.set_probe_running(view.running);
    surface.set_probe_stop_requested(view.stop_requested);
    surface.set_probe_history(&view.history);
    surface.set_probe_loading(false);
}

/// Toggle one model in the batch selection. An unknown model id is ignored.
pub fn toggle_probe_model<S: ProbeSurface>(surface: &S, id: &str) {
    if id.is_empty() {
        return;
    }
    let mut selection = surface.probe_selection();
    if let Some(index) = selection.iter().position(|model| model == id) {
        selection.remove(index);
    } else if surface.probe_rows().iter().any(|row| row.model_id == id) {
        selection.push(id.to_string());
    } else {
        return;
    }
    apply_probe_selection(surface, selection);
}

/// Select every model the card shows: the catalog plus any probe record that is
/// not in the catalog yet, so 全选 never misses a visible row.
pub fn select_all_probes<S: ProbeSurface>(surface: &S, catalog: Option<&CatalogView>) {
    let mut ids: Vec<String> = Vec::new();
    if let Some(catalog) = catalog {
        ids.extend(catalog.models.iter().map(|model| model.id.clone()));
    }
    for row in surface.probe_rows() {
        if !ids.iter().any(|known| known == &row.model_id) {
            ids.push(row.model_id.clone());
        }
    }
    apply_probe_selection(surface, ids);
}

/// Clear the batch selection without touching the visible results.
pub fn clear_probe_selection<S: ProbeSurface>(surface: &S) {
    apply_probe_selection(surface, Vec::new());
}

fn apply_probe_selection<S: ProbeSurface>(surface: &S, selection: Vec<String>) {
    let mut rows = surface.probe_rows();
    for row in &mut rows {
        row.selected = selection.iter().any(|id| id == &row.model_id);
    }
    surface.set_probe_rows(&rows);
    surface.set_probe_selection(&selection);
}

/// Submit the selected models as one ordered batch. An empty selection is a
/// refusal with its reason, never an empty successful POST.
pub fn run_context_probe<S: ProbeSurface>(
    surface: &S,
    client: &dyn ProbeClient,
    base_url: &str,
    token: &ControlToken,
) -> Result<Value, String> {
    let selection = surface.probe_selection();
    if selection.is_empty() {
        surface.set_probe_status("请先勾选要探测的模型");
        surface.set_probe_tone(Tone::Warn);
        return Err("请先勾选要探测的模型".to_string());
    }
    match client.start_probes(base_url, token, &selection) {
        Ok(payload) => {
            let queued = payload
                .get("queued")
                .and_then(Value::as_u64)
                .unwrap_or(selection.len() as u64);
            surface.set_probe_status(&format!("已提交上下文探测：{queued} 个模型按顺序执行"));
            surface.set_probe_tone(Tone::Neutral);
            surface.set_probe_running(true);
            surface.set_probe_stop_requested(false);
            surface.set_probe_progress(&format!("已完成 0/{} · 排队 {queued}", selection.len()));
            surface.set_probe_loading(false);
            Ok(payload)
        }
        Err(reason) => {
            surface.set_probe_status(&format!("上下文探测启动失败：{reason}"));
            surface.set_probe_tone(Tone::Bad);
            surface.set_probe_running(false);
            surface.set_probe_loading(false);
            Err(reason)
        }
    }
}

/// Ask the gateway to stop after the in-flight request; the gateway marks every
/// model that never started as cancelled and the next snapshot shows it.
pub fn stop_context_probe<S: ProbeSurface>(
    surface: &S,
    client: &dyn ProbeClient,
    base_url: &str,
    token: &ControlToken,
) -> Result<Value, String> {
    match client.stop_probes(base_url, token) {
        Ok(payload) => {
            let stopped = payload
                .get("stopped")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if stopped {
                surface.set_probe_status("已请求停止：在途请求返回后不再开始后续模型");
                surface.set_probe_tone(Tone::Warn);
                surface.set_probe_stop_requested(true);
                surface.set_probe_running(true);
            } else {
                surface.set_probe_status("当前没有运行中的探测批次");
                surface.set_probe_tone(Tone::Neutral);
                surface.set_probe_stop_requested(false);
                surface.set_probe_running(false);
            }
            Ok(payload)
        }
        Err(reason) => {
            surface.set_probe_status(&format!("停止失败：{reason}"));
            surface.set_probe_tone(Tone::Bad);
            Err(reason)
        }
    }
}

fn probe_row(
    id: &str,
    record: Option<&Value>,
    queue_status: Option<&String>,
    names: &BTreeMap<String, String>,
) -> ProbeRow {
    let name = names.get(id).cloned().unwrap_or_else(|| id.to_string());
    let (state, summary, tone) = match record {
        Some(record) => {
            let status = record
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let (label, tone) = probe_status_label(status);
            (label.to_string(), probe_summary(status, record), tone)
        }
        None => match queue_status.map(String::as_str) {
            Some("running") => ("测试中".to_string(), "执行中".to_string(), Tone::Neutral),
            Some("pending") => ("等待中".to_string(), "排在后面".to_string(), Tone::Neutral),
            Some(other) => (other.to_string(), String::new(), Tone::Neutral),
            None => (
                "未测".to_string(),
                "尚未执行上下文探测".to_string(),
                Tone::Neutral,
            ),
        },
    };
    ProbeRow {
        model_id: id.to_string(),
        name,
        state,
        summary,
        tone,
        selected: false,
    }
}

fn probe_history(by_model: &BTreeMap<String, &Value>) -> Vec<ProbeHistoryRow> {
    let mut rows: Vec<(String, ProbeHistoryRow)> = Vec::new();
    for (model_id, record) in by_model {
        let Some(history) = record.get("history").and_then(Value::as_array) else {
            continue;
        };
        for entry in history {
            let status = entry
                .get("status")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let (result, tone) = probe_status_label(status);
            let raw_time = entry.get("last_run").and_then(Value::as_str);
            let time = raw_time
                .map(probe_time_label)
                .unwrap_or_else(|| "时间不可用".to_string());
            rows.push((
                raw_time.unwrap_or("").to_string(),
                ProbeHistoryRow {
                    time,
                    model: model_id.clone(),
                    result: result.to_string(),
                    summary: probe_summary(status, entry),
                    tone,
                },
            ));
        }
    }
    rows.sort_by(|left, right| right.0.cmp(&left.0));
    rows.into_iter().map(|(_, row)| row).collect()
}

fn probe_counts(by_model: &BTreeMap<String, &Value>) -> (usize, usize, usize, usize) {
    let mut verified = 0;
    let mut failed = 0;
    let mut cancelled = 0;
    let mut incomplete = 0;
    for record in by_model.values() {
        match record.get("status").and_then(Value::as_str) {
            Some("verified") => verified += 1,
            Some("failed") => failed += 1,
            Some("cancelled") => cancelled += 1,
            Some("incomplete") => incomplete += 1,
            _ => {}
        }
    }
    (verified, failed, cancelled, incomplete)
}

fn is_probe_terminal(status: &str) -> bool {
    matches!(status, "verified" | "failed" | "cancelled" | "incomplete")
}

fn probe_status_label(status: &str) -> (&'static str, Tone) {
    match status {
        "pending" => ("等待中", Tone::Neutral),
        "running" => ("测试中", Tone::Neutral),
        "verified" => ("已验证", Tone::Good),
        "failed" => ("未通过", Tone::Bad),
        "cancelled" => ("已取消", Tone::Warn),
        "incomplete" => ("未完成", Tone::Warn),
        _ => ("未知", Tone::Neutral),
    }
}

fn probe_summary(status: &str, record: &Value) -> String {
    match status {
        "verified" => match probe_number(record, "observed_floor") {
            Some(floor) => format!("≥{} Token", thousands(floor)),
            None => "下界不可用".to_string(),
        },
        "failed" => {
            let mut parts = vec![match probe_number(record, "observed_ceiling") {
                Some(ceiling) => format!("上限 {} Token", thousands(ceiling)),
                None => "上限不可用".to_string(),
            }];
            if let Some(reason) = probe_reason(record) {
                parts.push(format!("原因：{reason}"));
            } else {
                parts.push("原因：未记录".to_string());
            }
            parts.join(" · ")
        }
        "cancelled" | "incomplete" => match probe_reason(record) {
            Some(reason) => format!("原因：{reason}"),
            None => "原因：未记录".to_string(),
        },
        "pending" => "排在后面".to_string(),
        "running" => "执行中".to_string(),
        _ => String::new(),
    }
}

fn probe_budget(payload: &Value) -> String {
    let max_requests = payload
        .pointer("/budget/max_requests")
        .and_then(Value::as_i64)
        .filter(|value| *value > 0);
    let max_tokens = payload
        .pointer("/budget/max_tokens")
        .and_then(Value::as_i64)
        .filter(|value| *value > 0);
    match (max_requests, max_tokens) {
        (Some(max_requests), Some(max_tokens)) => {
            format!("请求预算：最多 {max_requests} 次 · 每次最多 {max_tokens} Token")
        }
        _ => "请求预算：不可用".to_string(),
    }
}

fn probe_number(record: &Value, key: &str) -> Option<i64> {
    record
        .get(key)
        .and_then(|value| match value {
            Value::Number(number) => number.as_i64(),
            Value::String(text) => text.trim().parse::<i64>().ok(),
            _ => None,
        })
        .filter(|value| *value > 0)
}

fn probe_reason(record: &Value) -> Option<String> {
    record
        .get("last_error")
        .and_then(Value::as_str)
        .filter(|reason| !reason.is_empty())
        .map(probe_reason_label)
}

fn probe_reason_label(raw: &str) -> String {
    match raw {
        "stopped_by_user" => "停止后续探测".to_string(),
        "budget_exhausted" => "请求预算用尽".to_string(),
        "process_exit" => "网关进程退出".to_string(),
        "already_covered" => "已覆盖现有下界".to_string(),
        "probe_completed" => "探测完成".to_string(),
        other => other.to_string(),
    }
}

fn probe_time_label(raw: &str) -> String {
    chrono::DateTime::parse_from_rfc3339(raw)
        .ok()
        .map(|time| {
            time.with_timezone(&chrono::Local)
                .format("%m-%d %H:%M:%S")
                .to_string()
        })
        .unwrap_or_else(|| "时间不可用".to_string())
}
