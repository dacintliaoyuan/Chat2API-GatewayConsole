//! The overview maintenance menu (#161).
//!
//! The app instance gateway is the UM service itself, so the menu's five actions
//! target the same supervised process. Availability follows the current
//! gateway/service/auth state; a closed action keeps its reason instead of
//! disappearing. The app instance has no `console-state.sqlite3` maintenance-record
//! source, so the records area says 不适用 instead of rendering an empty table.

use crate::account::{self, AuthRead, AuthState};
use crate::gateway::GatewayState;
use crate::overview::Tone;
use crate::um_service::ServiceView;

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
            Self::Start => "启动本实例网关；启动后客户端请求重新被受理。",
            Self::Stop => "停止本实例网关；经它的请求会失败，需要手动启动。",
            Self::Restart => "重启期间经本实例网关的请求会失败，进行中的请求会被中断。",
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
                title: "启动本实例网关？".to_string(),
                body: self.impact().to_string(),
                label: "启动".to_string(),
                danger: false,
            },
            Self::Stop => Confirm {
                title: "停止本实例网关？".to_string(),
                body: self.impact().to_string(),
                label: "停止".to_string(),
                danger: true,
            },
            Self::Restart => Confirm {
                title: "重启本实例网关？".to_string(),
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
    pub at: String,
    pub action: String,
    pub result: String,
}

/// The maintenance-record area. `not_applicable` keeps the UI from drawing a
/// table when no source exists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordsView {
    pub status: String,
    pub tone: Tone,
    pub not_applicable: bool,
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
    Menu {
        items: Action::ALL
            .into_iter()
            .map(|action| row(state, action))
            .collect(),
        records: records(),
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
                (false, Some("UM 服务运行中不需要启动"))
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
        rows: Vec::new(),
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
            "已发起重启本实例网关；结果见设置页网关状态".to_string(),
            Tone::Neutral,
        ),
        _ => (String::new(), Tone::Neutral),
    }
}

/// The Slint surface of the maintenance menu.
pub trait MaintenanceSurface {
    fn set_menu_rows(&self, rows: &[MenuRow]);
    fn set_records(&self, records: &RecordsView);
    fn set_status(&self, status: &str, tone: Tone);
}

/// Applies one menu read to the overview.
pub fn apply_menu<S: MaintenanceSurface>(surface: &S, menu: &Menu) {
    surface.set_menu_rows(&menu.items);
    surface.set_records(&menu.records);
}

/// Applies one finished action to the menu's status line.
pub fn apply_result<S: MaintenanceSurface>(surface: &S, result: &ActionResult) {
    surface.set_status(&result.status, result.tone);
}
