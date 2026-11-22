//! The settings page's UM model catalog (#153).
//!
//! The App reads the instance gateway's `/admin/models` face and renders one row
//! per model: name/ID, context, thinking efforts and the last context test. The
//! read keeps a missing context or effort list as 「未知」 instead of inventing a
//! number, and an empty catalog is a legal empty state, not stale data.

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
                Some(detail) => format!("{url}: 本实例网关返回 HTTP {code}（{detail}）"),
                None => format!("{url}: 本实例网关返回 HTTP {code}"),
            }
        }
        ureq::Error::Transport(error) => match error.kind() {
            ureq::ErrorKind::ConnectionFailed => {
                format!("{url}: 无法连接本实例网关（连接被拒绝）")
            }
            _ => format!("{url}: 无法连接本实例网关"),
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
