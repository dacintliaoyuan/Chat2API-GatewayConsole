//! The overview maintenance menu (#161).
//!
//! The app instance gateway is the UM service itself, so the menu's five actions
//! target the same supervised process. Availability follows the current
//! gateway/service/auth state; a closed action keeps its reason instead of
//! disappearing. The app instance has no `console-state.sqlite3` maintenance-record
//! source, so the records area says 不适用 instead of rendering an empty table.

use std::time::Duration;

use serde_json::Value;

use crate::account::{self, AuthRead, AuthState};
use crate::gateway::GatewayState;
use crate::overview::Tone;
use crate::um_service::ServiceView;

/// The Beijing offset used to format record times, so the rows stay readable on
/// every machine and the tests have one fixed clock.
fn beijing() -> chrono::FixedOffset {
    chrono::FixedOffset::east_opt(8 * 3600).expect("valid Beijing UTC offset")
}

fn clock(at: Option<i64>, format: &str, missing: &str) -> String {
    use chrono::TimeZone;
    at.and_then(|at| beijing().timestamp_millis_opt(at).single())
        .map(|time| time.format(format).to_string())
        .unwrap_or_else(|| missing.to_string())
}

/// The web console's elapsed copy: milliseconds, seconds or minutes+seconds.
fn elapsed(millis: i64) -> String {
    if millis < 0 {
        return "—".to_string();
    }
    if millis < 1000 {
        return format!("{millis} 毫秒");
    }
    let seconds = millis as f64 / 1000.0;
    if seconds < 60.0 {
        if seconds < 10.0 {
            let text = format!("{seconds:.1}");
            return format!("{} 秒", text.trim_end_matches(".0"));
        }
        return format!("{} 秒", seconds.round() as i64);
    }
    let minutes = (seconds / 60.0).floor() as i64;
    format!(
        "{minutes} 分 {} 秒",
        (seconds - minutes as f64 * 60.0).round() as i64
    )
}

fn action_label(action: &str) -> String {
    match action {
        "account.um_status" => "读取 UM 状态".to_string(),
        "account.um_verify" | "verify" => "核验登录".to_string(),
        "account.login" | "login" => "检查并按需登录".to_string(),
        "service.start" | "start" => "启动本机网关".to_string(),
        "service.stop" | "stop" => "停止本机网关".to_string(),
        "service.restart" | "restart" => "重启本机网关".to_string(),
        "" => "未知操作".to_string(),
        other => other.to_string(),
    }
}

/// One state word and tone. An unknown state is 未知, never a blank cell.
fn state_label(state: &str) -> (&'static str, Tone) {
    match state {
        "queued" => ("排队中", Tone::Neutral),
        "running" => ("执行中", Tone::Neutral),
        "succeeded" => ("成功", Tone::Good),
        "failed" => ("失败", Tone::Bad),
        "cancelled" => ("已取消", Tone::Neutral),
        "interrupted" => ("已中断", Tone::Warn),
        _ => ("未知", Tone::Neutral),
    }
}

/// The app instance has no maintenance-record source; the area says so instead of
/// showing an empty table.
pub const RECORDS_NOT_APPLICABLE: &str = "不适用（本机实例无维护记录来源）";

/// One maintenance action. The index is the Slint callback payload.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Verify,
    Login,
    Start,
    Stop,
    Restart,
}

impl Action {
    pub const ALL: [Action; 5] = [
        Action::Verify,
        Action::Login,
        Action::Start,
        Action::Stop,
        Action::Restart,
    ];

    pub fn index(self) -> i32 {
        match self {
            Self::Verify => 0,
            Self::Login => 1,
            Self::Start => 2,
            Self::Stop => 3,
            Self::Restart => 4,
        }
    }

    pub fn from_index(index: i32) -> Option<Self> {
        Self::ALL.into_iter().find(|action| action.index() == index)
    }

    pub fn label(self) -> &'static str {
        match self {
            Self::Verify => "核验登录",
            Self::Login => "检查并按需登录",
            Self::Start => "启动",
            Self::Stop => "停止",
            Self::Restart => "重启",
        }
    }

    /// What the action does to the running instance, shown before confirming.
    pub fn impact(self) -> &'static str {
        match self {
            Self::Verify => "向上游确认当前登录是否有效；不调用模型，不自动登录。",
            Self::Login => "先核验；只有凭据缺失或被拒时才打开专用登录窗口。",
            Self::Start => "启动本机网关；启动后客户端请求重新被受理。",
            Self::Stop => "停止本机网关；经它的请求会失败，需要手动启动。",
            Self::Restart => "重启期间经本机网关的请求会失败，进行中的请求会被中断。",
        }
    }

    pub fn danger(self) -> bool {
        matches!(self, Self::Stop | Self::Restart)
    }

    pub fn confirm(self) -> Confirm {
        match self {
            Self::Verify => Confirm {
                title: "核验登录？".to_string(),
                body: self.impact().to_string(),
                label: "核验登录".to_string(),
                danger: false,
            },
            Self::Login => Confirm {
                title: "检查并按需登录 UM？".to_string(),
                body: self.impact().to_string(),
                label: "检查并按需登录".to_string(),
                danger: false,
            },
            Self::Start => Confirm {
                title: "启动本机网关？".to_string(),
                body: self.impact().to_string(),
                label: "启动".to_string(),
                danger: false,
            },
            Self::Stop => Confirm {
                title: "停止本机网关？".to_string(),
                body: self.impact().to_string(),
                label: "停止".to_string(),
                danger: true,
            },
            Self::Restart => Confirm {
                title: "重启本机网关？".to_string(),
                body: self.impact().to_string(),
                label: "重启".to_string(),
                danger: true,
            },
        }
    }
}

/// The second-step copy of one action.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Confirm {
    pub title: String,
    pub body: String,
    pub label: String,
    pub danger: bool,
}

/// One menu row as the overview renders it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MenuRow {
    pub action: Action,
    pub label: String,
    pub impact: String,
    pub enabled: bool,
    /// Empty while enabled; the concrete reason while closed.
    pub reason: String,
    pub confirm: Confirm,
}

/// One maintenance-record row. The app instance has no source yet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordRow {
    pub key: String,
    pub at: String,
    pub action: String,
    pub state: String,
    pub state_tone: Tone,
    pub result: String,
    pub took: String,
    pub steps: Vec<RecordStep>,
}

/// One step of a maintenance record, expanded under its row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordStep {
    pub at: String,
    pub note: String,
}

/// The maintenance-record area. `not_applicable` keeps the UI from drawing a
/// table when no source exists; `failed` keeps a read failure from becoming an
/// empty table; `empty` marks a source that legally returned no rows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordsView {
    pub status: String,
    pub tone: Tone,
    pub not_applicable: bool,
    pub empty: bool,
    pub failed: bool,
    /// The concrete gateway reason behind a not-applicable or failed read.
    pub detail: String,
    pub rows: Vec<RecordRow>,
}

/// The five actions plus the records area.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Menu {
    pub items: Vec<MenuRow>,
    pub records: RecordsView,
}

/// The gateway state reduced to what the menu needs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GatewayPhase {
    Running,
    Starting,
    Down,
}

impl GatewayPhase {
    pub fn from_state(state: &GatewayState) -> Self {
        match state {
            GatewayState::Running { .. } => Self::Running,
            GatewayState::Starting => Self::Starting,
            _ => Self::Down,
        }
    }
}

/// The current facts the availability rules read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MenuState {
    pub gateway: GatewayPhase,
    /// The service snapshot says 运行中, or the gateway itself is running.
    pub service_running: bool,
    /// ADR 0011: the operator owns the visible window; no automatic submit.
    pub manual_takeover: bool,
    pub login_running: bool,
}

impl MenuState {
    pub fn new(
        gateway: &GatewayState,
        service: Option<&ServiceView>,
        auth: Option<&AuthRead>,
    ) -> Self {
        let (manual_takeover, login_running) = match auth {
            Some(AuthRead::Status(status)) => (
                account::manual_takeover_status(status),
                status.state == AuthState::LoggingIn,
            ),
            _ => (false, false),
        };
        Self {
            gateway: GatewayPhase::from_state(gateway),
            service_running: service.is_some_and(|service| service.state == "运行中")
                || matches!(gateway, GatewayState::Running { .. }),
            manual_takeover,
            login_running,
        }
    }
}

/// Builds the five rows and the records area for one state.
pub fn menu(state: &MenuState) -> Menu {
    menu_with_records(state, records())
}

/// Builds the five rows plus one records read.
pub fn menu_with_records(state: &MenuState, records: RecordsView) -> Menu {
    Menu {
        items: Action::ALL
            .into_iter()
            .map(|action| row(state, action))
            .collect(),
        records,
    }
}

fn row(state: &MenuState, action: Action) -> MenuRow {
    let (enabled, reason) = availability(state, action);
    MenuRow {
        action,
        label: action.label().to_string(),
        impact: action.impact().to_string(),
        enabled,
        reason: reason.unwrap_or_default().to_string(),
        confirm: action.confirm(),
    }
}

fn availability(state: &MenuState, action: Action) -> (bool, Option<&'static str>) {
    match action {
        Action::Verify => match state.gateway {
            GatewayPhase::Starting => (false, Some("网关正在启动，请稍候")),
            GatewayPhase::Down => (false, Some("网关未运行，无法核验登录")),
            GatewayPhase::Running => (true, None),
        },
        Action::Login => match state.gateway {
            GatewayPhase::Starting => (false, Some("网关正在启动，请稍候")),
            GatewayPhase::Down => (false, Some("网关未运行，无法按需登录")),
            GatewayPhase::Running if state.manual_takeover => {
                (false, Some("人工接管中禁止自动提交"))
            }
            GatewayPhase::Running if state.login_running => {
                (false, Some("登录进行中，等待本轮结束"))
            }
            GatewayPhase::Running => (true, None),
        },
        Action::Start => {
            if state.service_running || state.gateway == GatewayPhase::Running {
                (false, Some("本机网关运行中不需要启动"))
            } else if state.gateway == GatewayPhase::Starting {
                (false, Some("网关正在启动，请稍候"))
            } else {
                (true, None)
            }
        }
        Action::Stop => match state.gateway {
            GatewayPhase::Starting => (false, Some("网关正在启动，请稍候")),
            GatewayPhase::Down => (false, Some("网关未运行，无需停止")),
            GatewayPhase::Running => (true, None),
        },
        Action::Restart => match state.gateway {
            GatewayPhase::Starting => (false, Some("网关正在启动，请稍候")),
            GatewayPhase::Down => (false, Some("网关未运行，无需重启")),
            GatewayPhase::Running => (true, None),
        },
    }
}

/// The records area for the app instance: there is no maintenance-record
/// source, so it says 不适用 and carries no rows.
pub fn records() -> RecordsView {
    RecordsView {
        status: RECORDS_NOT_APPLICABLE.to_string(),
        tone: Tone::Neutral,
        not_applicable: true,
        empty: false,
        failed: false,
        detail: String::new(),
        rows: Vec::new(),
    }
}

impl RecordsView {
    /// A read failure keeps its reason and is never folded into an empty table.
    pub fn failure(reason: impl Into<String>) -> Self {
        Self {
            status: format!("维护记录读取失败：{}", reason.into()),
            tone: Tone::Bad,
            not_applicable: false,
            empty: false,
            failed: true,
            detail: String::new(),
            rows: Vec::new(),
        }
    }
}

/// Classifies one gateway `/admin/um/maintenance` payload into the records area.
///
/// `status: "unavailable"` is the gateway declaring that this instance has no
/// record source: the area says 不适用 with the gateway's reason. Any other shape
/// must carry a `rows` array; a malformed payload is a read failure, not an empty
/// table.
pub fn records_from(payload: &Value) -> RecordsView {
    let Some(object) = payload.as_object() else {
        return RecordsView::failure("数据格式无效：顶层不是对象");
    };
    if object.get("status").and_then(Value::as_str) == Some("unavailable") {
        return RecordsView {
            status: RECORDS_NOT_APPLICABLE.to_string(),
            tone: Tone::Neutral,
            not_applicable: true,
            empty: false,
            failed: false,
            detail: object
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            rows: Vec::new(),
        };
    }
    let rows = match object.get("rows") {
        Some(Value::Array(rows)) => rows,
        Some(_) => return RecordsView::failure("数据格式无效：rows 不是数组"),
        None => {
            let coverage = object.get("coverage");
            if coverage
                .and_then(|coverage| coverage.get("status"))
                .and_then(Value::as_str)
                == Some("unavailable")
            {
                let reason = coverage
                    .and_then(|coverage| coverage.get("error"))
                    .and_then(Value::as_str)
                    .or_else(|| {
                        coverage
                            .and_then(|coverage| coverage.get("note"))
                            .and_then(Value::as_str)
                    })
                    .unwrap_or("维护记录来源不可用");
                return RecordsView::failure(format!("维护记录来源不可用：{reason}"));
            }
            return RecordsView::failure("数据格式无效：缺少 rows");
        }
    };
    let mut parsed = Vec::with_capacity(rows.len());
    for row in rows {
        match record_row(row) {
            Some(row) => parsed.push(row),
            None => return RecordsView::failure("数据格式无效：记录行不是对象"),
        }
    }
    let scope = object
        .get("scope")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let empty = parsed.is_empty();
    RecordsView {
        status: if empty {
            "读取范围内没有维护操作记录。".to_string()
        } else if scope.is_empty() {
            format!("最近 {} 条维护操作记录", parsed.len())
        } else {
            scope
        },
        tone: Tone::Neutral,
        not_applicable: false,
        empty,
        failed: false,
        detail: String::new(),
        rows: parsed,
    }
}

fn record_row(row: &Value) -> Option<RecordRow> {
    let object = row.as_object()?;
    let at = object.get("at").and_then(Value::as_i64);
    let finished = object.get("finishedAt").and_then(Value::as_i64);
    let (state, state_tone) =
        state_label(object.get("state").and_then(Value::as_str).unwrap_or(""));
    let took = match (at, finished) {
        (Some(at), Some(finished)) if finished >= at => elapsed(finished - at),
        _ => "—".to_string(),
    };
    let steps = object
        .get("steps")
        .and_then(Value::as_array)
        .map(|steps| {
            steps
                .iter()
                .filter_map(|step| {
                    let step = step.as_object()?;
                    Some(RecordStep {
                        at: clock(step.get("at").and_then(Value::as_i64), "%H:%M:%S", "—"),
                        note: step
                            .get("note")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    Some(RecordRow {
        key: object
            .get("key")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        at: clock(at, "%m-%d %H:%M:%S", "时间未知"),
        action: action_label(object.get("action").and_then(Value::as_str).unwrap_or("")),
        state: state.to_string(),
        state_tone,
        result: object
            .get("result")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        took,
        steps,
    })
}

/// The gateway's maintenance-record read surface.
pub trait RecordsApi {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String>;
}

pub struct HttpRecordsApi {
    agent: ureq::Agent,
}

impl HttpRecordsApi {
    pub fn new() -> Self {
        Self::with_timeout(Duration::from_millis(2000))
    }

    pub fn with_timeout(timeout: Duration) -> Self {
        Self {
            agent: ureq::AgentBuilder::new().timeout(timeout).build(),
        }
    }
}

impl Default for HttpRecordsApi {
    fn default() -> Self {
        Self::new()
    }
}

impl RecordsApi for HttpRecordsApi {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String> {
        let response = self
            .agent
            .get(url)
            .set("X-Admin-Control", token)
            .call()
            .map_err(|error| format!("{url}: {error}"))?;
        response
            .into_json::<Value>()
            .map_err(|error| format!("{url}: 维护记录不是有效 JSON：{error}"))
    }
}

/// Reads the gateway's maintenance records. A read failure keeps its reason and
/// never becomes an empty table.
pub fn read_records(api: &dyn RecordsApi, base: &str, token: &str) -> RecordsView {
    let url = format!("{}/admin/um/maintenance", base.trim_end_matches('/'));
    match api.get_json(&url, token) {
        Ok(payload) => records_from(&payload),
        Err(error) => RecordsView::failure(error),
    }
}

/// One finished maintenance action, as the menu's status line shows it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ActionResult {
    pub status: String,
    pub tone: Tone,
}

/// Maps one finished action. A failure keeps its reason and is never folded into
/// success.
pub fn action_result(action: Action, outcome: Result<String, String>) -> ActionResult {
    match outcome {
        Ok(detail) => ActionResult {
            status: format!("{}已完成：{detail}", action.label()),
            tone: Tone::Good,
        },
        Err(reason) => ActionResult {
            status: format!("{}失败：{reason}", action.label()),
            tone: Tone::Bad,
        },
    }
}

/// The status line for a delegated action whose result is shown on its own card.
///
/// It says where the result is; it never claims success before that card reports.
pub fn delegated_status(action: Action) -> (String, Tone) {
    match action {
        Action::Verify => (
            "已发起核验登录；结果见「最近核验」".to_string(),
            Tone::Neutral,
        ),
        Action::Login => (
            "已发起检查并按需登录；结果见「UM 登录」".to_string(),
            Tone::Neutral,
        ),
        Action::Restart => (
            "已发起重启本机网关；结果见设置页本机网关状态".to_string(),
            Tone::Neutral,
        ),
        _ => (String::new(), Tone::Neutral),
    }
}

/// The Slint surface of the maintenance menu.
pub trait MaintenanceSurface {
    fn set_menu_rows(&self, rows: &[MenuRow]);
    fn set_records(&self, records: &RecordsView);
    fn set_records_detail(&self, detail: &str);
    fn set_status(&self, status: &str, tone: Tone);
}

/// Applies one menu read to the overview.
pub fn apply_menu<S: MaintenanceSurface>(surface: &S, menu: &Menu) {
    surface.set_menu_rows(&menu.items);
    surface.set_records(&menu.records);
}

/// Applies one records read to the overview.
pub fn apply_records<S: MaintenanceSurface>(surface: &S, records: &RecordsView) {
    surface.set_records(records);
}

/// Applies one finished action to the menu's status line.
pub fn apply_result<S: MaintenanceSurface>(surface: &S, result: &ActionResult) {
    surface.set_status(&result.status, result.tone);
}
