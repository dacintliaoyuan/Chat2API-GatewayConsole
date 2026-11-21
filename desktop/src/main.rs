#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Slint desktop shell for the local console (issues #123/#124/#125).
//!
//! First open skips the UM login, reads the app instance's own gateway health and
//! live log, saves the model API port and keeps the window in the tray. The app
//! attaches to an already-running instance or starts exactly one managed gateway
//! child on the configured loopback port. It never touches the existing 8000/8001
//! pair, never opens its own TCP listener, and hands the saved UM credentials to
//! its own gateway through `CHAT2API_UM_ACCOUNT` / `CHAT2API_UM_PASSWORD`; the
//! gateway performs the automatic re-login while the app only reads
//! `/admin/auth/status`.

mod tray;

use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU16, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::TimeZone;
use slint::{CloseRequestResponse, ComponentHandle, Model, ModelRc, SharedString, Timer, TimerMode, VecModel};
use um_desktop::account::{
    self, account_view, apply_account, AccountForm, AccountSurface, AccountView, AuthAdmin,
    AuthRead, AuthState, HttpAuthAdmin, LoadOutcome,
};
use um_desktop::config::{self, DesktopConfig, LoadOutcome as ConfigLoadOutcome};
use um_desktop::gateway::{
    load_control_token, resolve_gateway_binary, GatewayState, GatewayStatus,
    GatewaySupervisor, HttpGateway, InstancePaths, PortSwitch, PortSwitchProgress, StopReason, SystemLauncher,
    ThreadSleeper,
};
use um_desktop::instance::{self, InstanceError};
use um_desktop::logstream::{matches_line, read_history, LevelFilter, LogStream, StreamLine};
use um_desktop::overview::{self, HttpProbe, Overview, Tone};
use um_desktop::shell::{ShellAction, ShellEvent, ShellState};
use um_desktop::usage::{self, Coverage, Range, UsageView};

slint::include_modules!();

static PAUSED: AtomicBool = AtomicBool::new(false);
static QUIT_REQUESTED: AtomicBool = AtomicBool::new(false);
static QUIT: AtomicBool = AtomicBool::new(false);
static REFRESH_LOGS: AtomicBool = AtomicBool::new(false);
static LOGIN_IN_FLIGHT: AtomicBool = AtomicBool::new(false);
static FILTER_EPOCH: AtomicU64 = AtomicU64::new(0);
static APPLIED_FILTER_EPOCH: AtomicU64 = AtomicU64::new(0);

/// One log-page update produced by the reader thread.
#[derive(Debug, Clone, PartialEq, Eq)]
struct LogUpdate {
    entries: Vec<StreamLine>,
    appended: Vec<StreamLine>,
    reset: bool,
    trimmed: usize,
    failure: Option<String>,
}

impl LogUpdate {
    fn failed(reason: String) -> Self {
        Self {
            entries: Vec::new(),
            appended: Vec::new(),
            reset: false,
            trimmed: 0,
            failure: Some(reason),
        }
    }
}

fn status_tone(tone: Tone) -> i32 {
    match tone {
        Tone::Good => 0,
        Tone::Warn => 1,
        Tone::Bad => 2,
    }
}

fn clock(at: Option<i64>) -> String {
    at.and_then(|at| chrono::Local.timestamp_millis_opt(at).single())
        .map(|time| time.format("%H:%M:%S").to_string())
        .unwrap_or_else(|| "—".to_string())
}

impl AccountSurface for MainWindow {
    fn set_saved_account(&self, value: &str) {
        self.set_account_saved(value.into());
    }

    fn set_saved_detail(&self, value: &str) {
        self.set_account_saved_detail(value.into());
    }

    fn set_can_clear(&self, value: bool) {
        self.set_account_can_clear(value);
    }

    fn set_can_continue_login(&self, value: bool) {
        self.set_account_can_continue(value);
    }

    fn set_overview_label(&self, value: &str) {
        self.set_account_label(value.into());
    }

    fn set_overview_detail(&self, value: &str) {
        self.set_account_detail(value.into());
    }

    fn set_overview_tone(&self, tone: Tone) {
        self.set_account_tone(status_tone(tone));
    }

    fn set_login_status(&self, value: &str) {
        self.set_login_status(value.into());
    }

    fn set_credential_status(&self, value: &str) {
        self.set_credential_status(value.into());
    }
}

fn open_account_form(window: &MainWindow, form: &Rc<RefCell<AccountForm>>, saved: &LoadOutcome) {
    let saved_account = match saved {
        LoadOutcome::Loaded(credentials) => Some(credentials.account.as_str()),
        _ => None,
    };
    *form.borrow_mut() = AccountForm::new(saved_account);
    window.set_credential_account_input(form.borrow().account().to_string().into());
    window.set_credential_password_input("".into());
    window.set_password_visible(false);
    window.set_credential_status("".into());
    window.set_credential_tone(3);
    window.set_show_intro(false);
    window.set_page(2);
    window.set_account_form_visible(true);
}

fn read_auth(auth_admin: &HttpAuthAdmin, token_path: &Path, gateway: &GatewayStatus) -> AuthRead {
    if !matches!(gateway.state, GatewayState::Running { .. }) {
        return AuthRead::Unavailable(format!("本实例网关未就绪：{}", gateway.state.detail()));
    }
    let token = match load_control_token(token_path) {
        Ok(token) => token,
        Err(error) => return AuthRead::Unavailable(error),
    };
    match auth_admin.read_status(&gateway.base_url, &token) {
        Ok(status) => AuthRead::Status(status),
        Err(error) => AuthRead::Unavailable(error),
    }
}

/// Reads the current status; a non-valid first login opens the visible Edge.
fn read_or_start_login(
    auth_admin: &HttpAuthAdmin,
    token_path: &Path,
    gateway: &GatewayStatus,
) -> AuthRead {
    let auth = read_auth(auth_admin, token_path, gateway);
    let AuthRead::Status(status) = &auth else {
        return auth;
    };
    if !account::first_login_needed(status) {
        return auth;
    }
    let token = match load_control_token(token_path) {
        Ok(token) => token,
        Err(error) => return AuthRead::Unavailable(error),
    };
    account::first_login(auth_admin, &gateway.base_url, &token, status)
}

fn save_message(auth: &AuthRead, view: &AccountView) -> (String, Tone) {
    match auth {
        AuthRead::Status(status) => match status.state {
            AuthState::Valid => ("账号已保存；本实例已登录".to_string(), Tone::Good),
            AuthState::LoggingIn => (
                "账号已保存；本实例网关正在自动重登…".to_string(),
                Tone::Warn,
            ),
            _ => (format!("账号已保存；{}", view.state_detail), Tone::Warn),
        },
        AuthRead::Unavailable(reason) => {
            (format!("账号已保存；认证状态不可用：{reason}"), Tone::Bad)
        }
    }
}

fn app_dir() -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn gateway_bin_override() -> Option<PathBuf> {
    std::env::var_os("UM_DESKTOP_GATEWAY_BIN")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

const LOG_ROW_HEIGHT: f32 = 36.0;

fn log_row(line: &StreamLine) -> LogRow {
    let at = line.entry.at.and_then(|at| chrono::Local.timestamp_millis_opt(at).single());
    let (time, precise) = match at {
        Some(time) => {
            let text = if time.date_naive() == chrono::Local::now().date_naive() {
                time.format("%H:%M:%S").to_string()
            } else {
                time.format("%m-%d %H:%M:%S").to_string()
            };
            (text, time.format("%Y-%m-%d %H:%M:%S %:z").to_string())
        }
        None => ("时间未知".to_string(), "时间未知".to_string()),
    };
    let level = match line.entry.level.to_ascii_lowercase().as_str() {
        "error" => "错误",
        "warn" | "warning" => "警告",
        "info" => "信息",
        "debug" => "调试",
        "raw" => "原始",
        _ => "未分级",
    };
    LogRow {
        id: format!("{}:{:x}", line.id.offset, line.id.hash).into(),
        at: time.into(),
        level: level.into(),
        text: line.entry.text.clone().into(),
        detail: format!("{precise}  {level}\n\n{}", line.detail).into(),
    }
}

fn apply_overview(window: &MainWindow, overview: &Overview, account: &AccountView) {
    window.set_gateway_value(overview.gateway.value.clone().into());
    window.set_gateway_detail(overview.gateway.detail.clone().into());
    window.set_gateway_tone(status_tone(overview.gateway.tone));
    window.set_supervision_value(overview.supervision.value.clone().into());
    window.set_supervision_detail(overview.supervision.detail.clone().into());
    window.set_supervision_tone(status_tone(overview.supervision.tone));
    window.set_upstream_value(overview.upstream.value.clone().into());
    window.set_upstream_detail(overview.upstream.detail.clone().into());
    window.set_upstream_tone(status_tone(overview.upstream.tone));
    apply_account(window, account);
    window.set_status_line(overview::status_line(overview).into());
    window.set_last_update(clock(Some(chrono::Local::now().timestamp_millis())).into());
}

fn apply_logs(window: &MainWindow, update: &LogUpdate) {
    if window.get_log_period() != 0 {
        return;
    }
    if let Some(reason) = &update.failure {
        window.set_log_status(format!("日志读取失败：{reason}").into());
        window.set_log_tone(status_tone(Tone::Bad));
        return;
    }

    let level = LevelFilter::from_index(window.get_log_filter_level());
    let keyword = window.get_log_filter_keyword().trim().to_lowercase();
    let epoch = FILTER_EPOCH.load(Ordering::SeqCst);
    let filter_changed = APPLIED_FILTER_EPOCH.swap(epoch, Ordering::SeqCst) != epoch;
    let follow = window.get_log_follow();
    let model = window.get_logs();
    let Some(model) = model.as_any().downcast_ref::<VecModel<LogRow>>() else {
        return;
    };
    let matches = |line: &&StreamLine| matches_line(line, level, &keyword);
    let added = update.appended.iter().filter(matches).count();
    if update.reset || update.trimmed > 0 || filter_changed {
        let rows: Vec<_> = update.entries.iter().filter(matches).map(log_row).collect();
        let scroll = window.get_log_scroll_y().min(0.0);
        let first = (-scroll / LOG_ROW_HEIGHT).floor() as usize;
        let remainder = -scroll - first as f32 * LOG_ROW_HEIGHT;
        let anchor = (!follow && !filter_changed && !update.reset)
            .then(|| model.row_data(first))
            .flatten();
        let new_first = anchor.and_then(|anchor| rows.iter().position(|row| row.id == anchor.id));
        model.set_vec(rows);
        if !follow {
            if let Some(new_first) = new_first {
                window.set_log_scroll_y(-(new_first as f32 * LOG_ROW_HEIGHT + remainder));
            } else {
                window.set_log_scroll_y(0.0);
            }
        }
    } else if added > 0 {
        model.extend(update.appended.iter().filter(matches).map(log_row));
    }
    if follow || filter_changed {
        window.set_new_log_count(0);
    } else {
        window.set_new_log_count(window.get_new_log_count().saturating_add(added as i32));
    }

    let total = update.entries.len();
    let visible = model.row_count();
    let status = if total == 0 {
        "本实例暂无日志记录".to_string()
    } else if visible == 0 {
        format!("最新 {total} 条中没有匹配的日志")
    } else if visible == total {
        format!("实时 · 最近 {total} 条")
    } else {
        format!("实时 · 显示 {visible} / {total} 条")
    };
    window.set_log_total(total as i32);
    window.set_log_status(status.into());
    window.set_log_tone(3);
    window.set_log_version(window.get_log_version().wrapping_add(1));
}

#[derive(Default)]
struct HistoryNav {
    pages: Vec<Option<u64>>,
    older: Option<u64>,
    generation: u64,
}

fn load_historical_logs(
    window: &MainWindow,
    path: &Path,
    navigation: &Arc<Mutex<HistoryNav>>,
    direction: i32,
) {
    let period = window.get_log_period();
    if period == 0 {
        window.set_log_status("正在读取实时日志…".into());
        window.set_log_follow(true);
        FILTER_EPOCH.fetch_add(1, Ordering::SeqCst);
        REFRESH_LOGS.store(true, Ordering::SeqCst);
        return;
    }

    let (before, generation) = {
        let mut nav = navigation.lock().expect("log history navigation poisoned");
        match direction {
            1 => match nav.older {
                Some(cursor) => nav.pages.push(Some(cursor)),
                None => return,
            },
            -1 if nav.pages.len() > 1 => {
                nav.pages.pop();
            }
            -1 => return,
            _ => {
                nav.pages.clear();
                nav.pages.push(None);
            }
        }
        nav.generation = nav.generation.wrapping_add(1);
        (nav.pages.last().copied().flatten(), nav.generation)
    };
    let path = path.to_path_buf();
    let weak = window.as_weak();
    let navigation = Arc::clone(navigation);
    let level = LevelFilter::from_index(window.get_log_filter_level());
    let keyword = window.get_log_filter_keyword().to_string();
    window.set_older_logs(false);
    window.set_newer_logs(false);
    window.set_log_status("正在读取历史日志…".into());
    window.set_log_tone(3);
    std::thread::spawn(move || {
        let now = chrono::Local::now();
        let days = match period { 2 => 6, 3 => 29, _ => 0 };
        let since = now.date_naive()
            .checked_sub_days(chrono::Days::new(days))
            .and_then(|date| date.and_hms_opt(0, 0, 0))
            .and_then(|time| time.and_local_timezone(chrono::Local).earliest())
            .map_or_else(|| (now - chrono::Duration::days(days as i64 + 1)).timestamp_millis(), |time| time.timestamp_millis());
        let result = read_history(&path, before, since, level, &keyword);
        let _ = slint::invoke_from_event_loop(move || {
            let Some(window) = weak.upgrade() else { return };
            let mut nav = navigation.lock().expect("log history navigation poisoned");
            if window.get_log_period() != period || nav.generation != generation {
                return;
            }
            let model = window.get_logs();
            let Some(model) = model.as_any().downcast_ref::<VecModel<LogRow>>() else { return };
            match result {
                Ok(result) => {
                    let count = result.lines.len();
                    nav.older = result.older;
                    window.set_older_logs(nav.older.is_some());
                    window.set_newer_logs(nav.pages.len() > 1);
                    model.set_vec(result.lines.iter().map(log_row).collect::<Vec<_>>());
                    window.set_log_scroll_y(0.0);
                    let text = if count == 0 {
                        "本页没有符合条件的记录".to_string()
                    } else {
                        format!("历史日志 · 本页 {count} 条")
                    };
                    window.set_log_status(text.into());
                }
                Err(error) => {
                    nav.older = None;
                    window.set_newer_logs(nav.pages.len() > 1);
                    model.set_vec(Vec::new());
                    window.set_log_status(format!("历史日志读取失败：{error}").into());
                    window.set_log_tone(status_tone(Tone::Bad));
                }
            }
        });
    });
}

fn coverage_tone(coverage: Coverage) -> i32 {
    match coverage {
        Coverage::Unavailable => status_tone(Tone::Bad),
        Coverage::Partial => status_tone(Tone::Warn),
        Coverage::Complete | Coverage::Empty => 3,
    }
}

fn apply_usage(window: &MainWindow, view: UsageView) {
    let to_ticks = |labels: Vec<String>| {
        let ticks: Vec<SharedString> = labels.into_iter().map(Into::into).collect();
        ModelRc::from(ticks.as_slice())
    };
    let token_axis = to_ticks(view.token_ticks);
    let request_axis = to_ticks(view.request_ticks);
    let points = if matches!(view.coverage, Coverage::Empty | Coverage::Unavailable) {
        Vec::new()
    } else {
        view.buckets.into_iter().map(|bucket| UsagePoint {
            label: bucket.label.clone().into(),
            detail_label: format!("北京时间 {}", bucket.label).into(),
            token_value: bucket.tokens.into(),
            request_value: bucket.requests.into(),
            token_height: bucket.token_fraction.unwrap_or(-1.0),
            request_height: bucket.request_fraction.unwrap_or(-1.0),
        }).collect::<Vec<_>>()
    };
    window.set_token_ticks(token_axis);
    window.set_request_ticks(request_axis);
    window.set_token_total(view.token_total.into());
    window.set_request_total(view.request_total.into());
    window.set_usage_points(ModelRc::from(points.as_slice()));
    window.set_usage_status(view.message.into());
    window.set_usage_tone(coverage_tone(view.coverage));
    window.set_usage_loading(false);
}

#[derive(Default)]
struct RequestNav {
    pages: Vec<Option<String>>,
    earlier: Option<String>,
    rows: Vec<usage::RequestRow>,
    now: i64,
    generation: u64,
}

fn load_requests(window: &MainWindow, ledger: &Path, nav: &Arc<Mutex<RequestNav>>, direction: i32) {
    let (cursor, now, generation) = {
        let mut nav = nav.lock().expect("request navigation poisoned");
        match direction {
            1 => match nav.earlier.take() {
                Some(cursor) => nav.pages.push(Some(cursor)),
                None => return,
            },
            -1 if nav.pages.len() > 1 => {
                nav.pages.pop();
                nav.earlier = None;
            }
            -1 => return,
            _ => {
                nav.pages.clear();
                nav.pages.push(None);
                nav.earlier = None;
                nav.now = chrono::Utc::now().timestamp_millis();
            }
        }
        nav.generation = nav.generation.wrapping_add(1);
        (nav.pages.last().cloned().flatten(), nav.now, nav.generation)
    };
    let range = Range::from_index(window.get_request_period());
    let result = window.get_request_result();
    let keyword = window.get_request_keyword().to_string();
    let weak = window.as_weak();
    let ledger = ledger.to_path_buf();
    let nav = Arc::clone(nav);
    window.set_selected_request("".into());
    window.set_request_scroll_y(0.0);
    window.set_request_status("正在读取请求记录…".into());
    window.set_request_tone(3);
    window.set_older_requests(false);
    window.set_newer_requests(false);
    std::thread::spawn(move || {
        let page = usage::read_requests(&ledger, range, result, &keyword, cursor.as_deref(), now);
        let _ = slint::invoke_from_event_loop(move || {
            let Some(window) = weak.upgrade() else { return; };
            let mut nav = nav.lock().expect("request navigation poisoned");
            if nav.generation != generation { return; }
            let rows = page.rows.iter().map(|row| RequestRow {
                id: row.key.clone().into(),
                at: row.at.clone().into(),
                model: row.model.clone().into(),
                outcome: row.result.clone().into(),
                tone: match row.result.as_str() {
                    "成功" => status_tone(Tone::Good),
                    "失败" => status_tone(Tone::Bad),
                    _ => status_tone(Tone::Warn),
                },
                summary: format!("{}  {}", row.duration, row.request_id.as_deref().unwrap_or("")).into(),
                detail: "".into(),
            }).collect::<Vec<_>>();
            let count = rows.len();
            window.set_requests(ModelRc::from(rows.as_slice()));
            if count == 0 || !page.message.is_empty() {
                window.set_request_status(page.message.into());
            } else {
                window.set_request_status(format!("北京时间 · 本页 {count} 条").into());
            }
            window.set_request_tone(coverage_tone(page.coverage));
            window.set_older_requests(page.earlier.is_some());
            window.set_newer_requests(nav.pages.len() > 1);
            nav.rows = page.rows;
            nav.earlier = page.earlier;
        });
    });
}

fn show_request(window: &MainWindow, log: &Path, nav: &Mutex<RequestNav>, key: &str) {
    let nav = nav.lock().expect("request navigation poisoned");
    let Some(row) = nav.rows.iter().find(|row| row.key == key).cloned() else { return; };
    let key = row.key.clone();
    let weak = window.as_weak();
    let log = log.to_path_buf();
    window.set_selected_request(key.clone().into());
    window.set_request_detail_title("请求详情".into());
    window.set_request_detail(usage::request_detail(&row, None).into());
    std::thread::spawn(move || {
        let logs = usage::read_request_logs(&log, row.request_id.as_deref(), chrono::Utc::now().timestamp_millis());
        let _ = slint::invoke_from_event_loop(move || {
            if let Some(window) = weak.upgrade() {
                if window.get_selected_request().as_str() == key {
                    window.set_request_detail(usage::request_detail(&row, Some(&logs)).into());
                }
            }
        });
    });
}

#[cfg(windows)]
fn allow_motion() -> bool {
    use windows::Win32::UI::WindowsAndMessaging::{
        SystemParametersInfoW, SPI_GETCLIENTAREAANIMATION, SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS,
    };
    let mut allowed = 0i32;
    unsafe {
        SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION,
            0,
            Some((&mut allowed as *mut i32).cast()),
            SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0),
        ).is_ok() && allowed != 0
    }
}

fn hide_now(window: &MainWindow) {
    PAUSED.store(true, Ordering::SeqCst);
    let _ = window.hide();
}

fn show_now(window: &MainWindow) {
    let _ = window.show();
    window.window().set_minimized(false);
    PAUSED.store(false, Ordering::SeqCst);
    REFRESH_LOGS.store(true, Ordering::SeqCst);
}

type SharedSupervisor = Arc<Mutex<Option<GatewaySupervisor>>>;
type SharedStatus = Arc<Mutex<GatewayStatus>>;

fn request_quit(window: &MainWindow, supervisor: &SharedSupervisor, status: &SharedStatus) {
    if QUIT_REQUESTED.swap(true, Ordering::SeqCst) {
        return;
    }
    let supervisor = Arc::clone(supervisor);
    let status = Arc::clone(status);
    let weak = window.as_weak();
    window.set_status_line("正在停止本实例网关…".into());
    std::thread::spawn(move || {
        let stopped = {
            let mut guard = supervisor.lock().expect("gateway supervisor poisoned");
            guard.as_mut().map(|supervisor| supervisor.stop(StopReason::UserRequested))
        };
        if let Some(stopped) = stopped {
            let failure = match &stopped.state {
                GatewayState::StopFailed { reason } => Some(reason.clone()),
                _ => None,
            };
            *status.lock().expect("gateway status poisoned") = stopped;
            if let Some(error) = failure {
                QUIT_REQUESTED.store(false, Ordering::SeqCst);
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(window) = weak.upgrade() {
                        show_now(&window);
                        window.set_status_line(format!("网关未停止：{error}").into());
                    }
                });
                return;
            }
        }
        QUIT.store(true, Ordering::SeqCst);
    });
}

#[cfg(windows)]
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let _guard = match instance::open_or_activate(
        instance::APP_MUTEX,
        &instance::Win32Window::new(instance::WINDOW_TITLE),
    ) {
        Ok(guard) => guard,
        Err(InstanceError::AlreadyRunning { activated }) => {
            if !activated {
                eprintln!("um-desktop: 已有实例在运行，但无法激活它的窗口");
            }
            return Ok(());
        }
        Err(InstanceError::Unsupported(reason)) => {
            return Err(Box::new(std::io::Error::other(format!(
                "um-desktop: 单实例不可用：{reason}"
            ))));
        }
    };

    let window = MainWindow::new()?;
    window.set_allow_motion(allow_motion());
    let config_path = config::default_config_path();
    let (loaded_config, settings_status, settings_tone) = match &config_path {
        Ok(path) => match config::load_from(path) {
            ConfigLoadOutcome::Missing => (
                DesktopConfig::default(),
                "使用默认端口 7999（尚未保存设置）；启动时接入已有实例或拉起本实例网关".to_string(),
                Tone::Warn,
            ),
            ConfigLoadOutcome::Loaded(config) => (
                config,
                format!(
                    "已加载配置：{}；启动时接入已有实例或拉起本实例网关",
                    path.display()
                ),
                Tone::Good,
            ),
            ConfigLoadOutcome::Invalid(reason) => (
                DesktopConfig::default(),
                format!("配置不可用：{reason}；使用默认端口 7999"),
                Tone::Bad,
            ),
        },
        Err(reason) => (DesktopConfig::default(), reason.clone(), Tone::Bad),
    };
    let app_data_root = config_path
        .as_ref()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."));
    let paths = InstancePaths::under(&app_data_root);
    let current_config = Arc::new(Mutex::new(loaded_config.clone()));
    window.set_port_input(loaded_config.model_api_port.to_string().into());
    window.set_active_address(config::model_api_bind(&loaded_config).into());
    window.set_config_path(
        config_path
            .as_ref()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|_| "（未定位）".to_string())
            .into(),
    );
    window.set_instance_path(paths.instance_dir.display().to_string().into());
    window.set_settings_status(settings_status.into());
    window.set_settings_tone(status_tone(settings_tone));
    window.set_logs(ModelRc::from(Rc::new(VecModel::<LogRow>::default())));

    let credentials_path = config_path
        .as_ref()
        .ok()
        .and_then(|path| path.parent())
        .map(account::credentials_path);
    let saved_credentials = match &credentials_path {
        Some(path) => account::load_from(path),
        None => LoadOutcome::Unusable("LOCALAPPDATA 未设置，无法定位凭据文件".to_string()),
    };
    window.set_show_intro(account::intro_visible(
        loaded_config.intro_dismissed,
        &saved_credentials,
    ));
    let saved_state = Arc::new(Mutex::new(saved_credentials.clone()));
    let auth_state: Arc<Mutex<Option<AuthRead>>> = Arc::new(Mutex::new(None));
    let account_form = Rc::new(RefCell::new(AccountForm::new(None)));
    let initial_account = account_view(&saved_credentials, None);
    apply_account(&window, &initial_account);

    let program = resolve_gateway_binary(gateway_bin_override(), &app_dir());
    let mut supervisor = GatewaySupervisor::new(
        loaded_config.model_api_port,
        paths.clone(),
        program,
        Arc::new(HttpGateway::new()),
        Box::new(SystemLauncher),
        Box::new(ThreadSleeper),
    );
    if let LoadOutcome::Loaded(credentials) = &saved_credentials {
        supervisor.set_credentials(Some(credentials.clone()));
    }
    let gateway_supervisor = Arc::new(Mutex::new(Some(supervisor)));
    let gateway_status = Arc::new(Mutex::new(GatewayStatus {
        state: GatewayState::Starting,
        port: loaded_config.model_api_port,
        base_url: paths.base_url(loaded_config.model_api_port),
        supervision_log: paths.supervision_log.clone(),
    }));
    let current_port = Arc::new(AtomicU16::new(loaded_config.model_api_port));

    {
        let supervisor = Arc::clone(&gateway_supervisor);
        let status = Arc::clone(&gateway_status);
        std::thread::spawn(move || {
            let next = {
                let mut guard = supervisor
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                match guard.as_mut() {
                    Some(supervisor) => supervisor.start_or_attach(),
                    None => return,
                }
            };
            *status
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = next;
        });
    }

    let probe = HttpProbe::new();
    let auth_admin = Arc::new(HttpAuthAdmin::new());
    let service_log = paths.service_log.clone();
    let control_token_path = paths.control_token.clone();
    {
        let weak = window.as_weak();
        let gateway_status = Arc::clone(&gateway_status);
        let current_port = Arc::clone(&current_port);
        let saved_state = Arc::clone(&saved_state);
        let auth_state = Arc::clone(&auth_state);
        let auth_admin = Arc::clone(&auth_admin);
        std::thread::spawn(move || {
            let mut last = None;
            while !QUIT.load(Ordering::SeqCst) {
                let base = format!("http://127.0.0.1:{}", current_port.load(Ordering::SeqCst));
                let supervision = gateway_status
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .card();
                let overview = overview::read_overview(&probe, &base, supervision);
                let auth = match load_control_token(&control_token_path) {
                    Ok(token) => match auth_admin.read_status(&base, &token) {
                        Ok(status) => AuthRead::Status(status),
                        Err(error) => AuthRead::Unavailable(error),
                    },
                    Err(error) => AuthRead::Unavailable(error),
                };
                let saved = saved_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let account = account_view(&saved, Some(&auth));
                *auth_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(auth);
                if last.as_ref().is_none_or(|(last_overview, last_account)| *last_overview != overview || *last_account != account) {
                    last = Some((overview.clone(), account.clone()));
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            apply_overview(&window, &overview, &account);
                        }
                    });
                }
                std::thread::sleep(Duration::from_secs(if PAUSED.load(Ordering::SeqCst) { 3 } else { 1 }));
            }
        });
    }

    {
        let weak = window.as_weak();
        let service_log = service_log.clone();
        std::thread::spawn(move || {
            let mut last_failure = None;
            let mut stream: Option<LogStream> = None;
            while !QUIT.load(Ordering::SeqCst) {
                if !PAUSED.load(Ordering::SeqCst) {
                    let refresh = REFRESH_LOGS.swap(false, Ordering::SeqCst);
                    let update = if let Some(opened) = stream.as_mut() {
                        match opened.poll() {
                            Ok(outcome) if refresh || outcome.reset || !outcome.appended.is_empty() || last_failure.is_some() => Some(LogUpdate {
                                entries: opened.lines().iter().cloned().collect(),
                                appended: outcome.appended,
                                reset: outcome.reset || last_failure.is_some(),
                                trimmed: outcome.trimmed,
                                failure: None,
                            }),
                            Ok(_) => None,
                            Err(reason) if refresh || last_failure.as_ref() != Some(&reason) => Some(LogUpdate::failed(reason)),
                            Err(_) => None,
                        }
                    } else {
                        match LogStream::open(&service_log) {
                            Ok(opened) => {
                                let entries = opened.lines().iter().cloned().collect();
                                stream = Some(opened);
                                Some(LogUpdate { entries, appended: Vec::new(), reset: true, trimmed: 0, failure: None })
                            }
                            Err(reason) if refresh || last_failure.as_ref() != Some(&reason) => Some(LogUpdate::failed(reason)),
                            Err(_) => None,
                        }
                    };
                    if let Some(update) = update {
                        last_failure = update.failure.clone();
                        let weak = weak.clone();
                        let _ = slint::invoke_from_event_loop(move || {
                            if let Some(window) = weak.upgrade() {
                                apply_logs(&window, &update);
                            }
                        });
                    }
                }
                std::thread::sleep(Duration::from_millis(160));
            }
        });
    }

    let tray: Option<tray::Tray> = match tray::install() {
        Ok(tray) => Some(tray),
        Err(error) => {
            eprintln!("um-desktop: 系统托盘不可用：{error}");
            None
        }
    };

    window.set_tray_available(tray.is_some());
    let tray_available = tray.is_some();
    let shell = Rc::new(RefCell::new(ShellState::new()));

    let weak_close = window.as_weak();
    let shell_close = shell.clone();
    let close_supervisor = Arc::clone(&gateway_supervisor);
    let close_status = Arc::clone(&gateway_status);
    window.window().on_close_requested(move || {
        if !tray_available {
            if let Some(window) = weak_close.upgrade() {
                request_quit(&window, &close_supervisor, &close_status);
            }
            return CloseRequestResponse::KeepWindowShown;
        }
        shell_close.borrow_mut().apply(ShellEvent::CloseRequested);
        if let Some(window) = weak_close.upgrade() {
            hide_now(&window);
        }
        CloseRequestResponse::HideWindow
    });

    let supervisor_quit = Arc::clone(&gateway_supervisor);
    let status_quit = Arc::clone(&gateway_status);
    let weak_quit = window.as_weak();
    window.on_quit_requested(move || {
        if let Some(window) = weak_quit.upgrade() {
            request_quit(&window, &supervisor_quit, &status_quit);
        }
    });

    let request_nav = Arc::new(Mutex::new(RequestNav::default()));
    let ledger = paths.usage_log.clone();
    let request_ledger = ledger.clone();
    let request_page_nav = Arc::clone(&request_nav);
    let weak_request = window.as_weak();
    window.on_query_requests(move |direction| {
        if let Some(window) = weak_request.upgrade() {
            load_requests(&window, &request_ledger, &request_page_nav, direction);
        }
    });
    let request_log = service_log.clone();
    let weak_request = window.as_weak();
    window.on_open_request(move |key| {
        if let Some(window) = weak_request.upgrade() {
            show_request(&window, &request_log, &request_nav, &key);
        }
    });

    let usage_range = Arc::new(AtomicI32::new(0));
    {
        let weak = window.as_weak();
        let range = Arc::clone(&usage_range);
        window.on_usage_range_changed(move |period| {
            range.store(period, Ordering::SeqCst);
            if let Some(window) = weak.upgrade() {
                window.set_usage_points(ModelRc::default());
                window.set_token_total("—".into());
                window.set_request_total("—".into());
                window.set_usage_loading(true);
            }
        });
    }
    {
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let mut last = None;
            while !QUIT.load(Ordering::SeqCst) {
                if !PAUSED.load(Ordering::SeqCst) {
                    let period = usage_range.load(Ordering::SeqCst);
                    let metadata = std::fs::metadata(&ledger).map(|meta| (meta.len(), meta.modified().ok()));
                    let day = chrono::Utc::now().with_timezone(&chrono::FixedOffset::east_opt(8 * 3600).expect("Beijing offset")).date_naive();
                    let stamp = (period, metadata.as_ref().copied().map_err(|error| error.kind()), day);
                    if last.as_ref() != Some(&stamp) {
                        last = Some(stamp);
                        let view = usage::read_usage(&ledger, Range::from_index(period), chrono::Utc::now().timestamp_millis());
                        let weak = weak.clone();
                        let _ = slint::invoke_from_event_loop(move || {
                            if let Some(window) = weak.upgrade() {
                                if window.get_usage_period() == period {
                                    apply_usage(&window, view);
                                }
                            }
                        });
                    }
                }
                std::thread::sleep(Duration::from_millis(350));
            }
        });
    }

    let weak_navigate = window.as_weak();
    let requests_opened = Cell::new(false);
    window.on_navigate(move |page| {
        if let Some(window) = weak_navigate.upgrade() {
            window.set_page(page);
            if page == 3 {
                REFRESH_LOGS.store(true, Ordering::SeqCst);
            } else if page == 1 && !requests_opened.replace(true) {
                window.invoke_query_requests(0);
            }
        }
    });

    window.on_refresh_logs(|| {
        REFRESH_LOGS.store(true, Ordering::SeqCst);
    });

    let history_navigation = Arc::new(Mutex::new(HistoryNav::default()));
    let weak_history = window.as_weak();
    let history_path = service_log.clone();
    let nav_for_filter = Arc::clone(&history_navigation);
    window.on_apply_log_filter(move |_, _| {
        if let Some(window) = weak_history.upgrade() {
            if window.get_log_period() != 0 {
                load_historical_logs(&window, &history_path, &nav_for_filter, 0);
            } else {
                FILTER_EPOCH.fetch_add(1, Ordering::SeqCst);
                REFRESH_LOGS.store(true, Ordering::SeqCst);
            }
        }
    });
    let weak_history = window.as_weak();
    window.on_log_history(move |_, direction| {
        if let Some(window) = weak_history.upgrade() {
            load_historical_logs(&window, &service_log, &history_navigation, direction);
        }
    });

    let weak_skip = window.as_weak();
    let skip_config = Arc::clone(&current_config);
    let skip_path = config_path.as_ref().ok().cloned();
    window.on_skip_login(move || {
        let Some(window) = weak_skip.upgrade() else { return };
        window.set_show_intro(false);
        let Some(path) = &skip_path else { return };
        let mut config = skip_config.lock().expect("desktop config poisoned");
        config.intro_dismissed = true;
        if !matches!(config::load_from(path), ConfigLoadOutcome::Invalid(_)) {
            if let Err(error) = config::save_to(path, &config) {
                window.set_settings_status(format!("无法保存首次登录偏好：{error}").into());
                window.set_settings_tone(status_tone(Tone::Bad));
            }
        }
    });

    let weak_configure = window.as_weak();
    let saved_configure = Arc::clone(&saved_state);
    let form_configure = Rc::clone(&account_form);
    window.on_configure_account(move || {
        let Some(window) = weak_configure.upgrade() else {
            return;
        };
        let saved = saved_configure
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        open_account_form(&window, &form_configure, &saved);
    });

    let weak_open = window.as_weak();
    let saved_open = Arc::clone(&saved_state);
    let form_open = Rc::clone(&account_form);
    window.on_open_account_form(move || {
        let Some(window) = weak_open.upgrade() else {
            return;
        };
        let saved = saved_open
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        open_account_form(&window, &form_open, &saved);
    });

    let weak_close_form = window.as_weak();
    let form_close = Rc::clone(&account_form);
    window.on_close_account_form(move || {
        let Some(window) = weak_close_form.upgrade() else {
            return;
        };
        *form_close.borrow_mut() = AccountForm::new(None);
        window.set_credential_password_input("".into());
        window.set_password_visible(false);
        window.set_account_form_visible(false);
    });

    let weak_toggle = window.as_weak();
    let form_toggle = Rc::clone(&account_form);
    window.on_toggle_password_visible(move || {
        let Some(window) = weak_toggle.upgrade() else {
            return;
        };
        let mut form = form_toggle.borrow_mut();
        form.toggle_password_visible();
        window.set_password_visible(form.password_visible());
    });

    let weak_save_credentials = window.as_weak();
    let form_save_credentials = Rc::clone(&account_form);
    let credentials_save_path = credentials_path.clone();
    let saved_save = Arc::clone(&saved_state);
    let auth_save = Arc::clone(&auth_state);
    let supervisor_save_credentials = Arc::clone(&gateway_supervisor);
    let status_save_credentials = Arc::clone(&gateway_status);
    let token_path_save = paths.control_token.clone();
    let auth_admin_save = Arc::clone(&auth_admin);
    window.on_save_credentials(move |account_text, password_text| {
        let Some(window) = weak_save_credentials.upgrade() else {
            return;
        };
        let credentials = {
            let mut form = form_save_credentials.borrow_mut();
            form.set_account(&account_text);
            form.set_password(&password_text);
            match form.validate() {
                Ok(credentials) => credentials,
                Err(error) => {
                    window.set_credential_status(format!("无法保存：{error}").into());
                    window.set_credential_tone(status_tone(Tone::Bad));
                    return;
                }
            }
        };
        let Some(path) = credentials_save_path.clone() else {
            window.set_credential_status("无法保存：LOCALAPPDATA 未设置".into());
            window.set_credential_tone(status_tone(Tone::Bad));
            return;
        };
        window.set_account_form_visible(false);
        window.set_account_busy(true);
        window.set_credential_status("正在保存账号密码并应用到本实例网关…".into());
        window.set_credential_tone(status_tone(Tone::Warn));

        let supervisor = Arc::clone(&supervisor_save_credentials);
        let gateway_status = Arc::clone(&status_save_credentials);
        let saved_state = Arc::clone(&saved_save);
        let auth_state = Arc::clone(&auth_save);
        let token_path = token_path_save.clone();
        let auth_admin = Arc::clone(&auth_admin_save);
        let weak = window.as_weak();
        std::thread::spawn(move || {
            if let Err(error) = account::save_to(&path, &credentials) {
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(window) = weak.upgrade() {
                        window.set_account_busy(false);
                        window.set_credential_status(format!("保存失败：{error}").into());
                        window.set_credential_tone(status_tone(Tone::Bad));
                        window.set_account_form_visible(true);
                    }
                });
                return;
            }
            *saved_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) =
                LoadOutcome::Loaded(credentials.clone());
            let restarted = {
                let mut guard = supervisor
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                match guard.as_mut() {
                    Some(supervisor) => {
                        supervisor.set_credentials(Some(credentials.clone()));
                        supervisor.restart(StopReason::CredentialsChanged)
                    }
                    None => return,
                }
            };
            *gateway_status
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = restarted.clone();
            let auth = read_or_start_login(&auth_admin, &token_path, &restarted);
            *auth_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(auth.clone());
            let view = account_view(&LoadOutcome::Loaded(credentials.clone()), Some(&auth));
            let (message, tone) = save_message(&auth, &view);
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    window.set_account_busy(false);
                    window.set_credential_password_input("".into());
                    window.set_password_visible(false);
                    window.set_account_form_visible(false);
                    apply_account(&window, &view);
                    window.set_credential_status(message.into());
                    window.set_credential_tone(status_tone(tone));
                }
            });
        });
    });

    let weak_clear = window.as_weak();
    let credentials_clear_path = credentials_path.clone();
    let saved_clear = Arc::clone(&saved_state);
    let auth_clear = Arc::clone(&auth_state);
    let supervisor_clear = Arc::clone(&gateway_supervisor);
    let status_clear = Arc::clone(&gateway_status);
    let token_path_clear = paths.control_token.clone();
    let auth_admin_clear = Arc::clone(&auth_admin);
    let form_clear = Rc::clone(&account_form);
    window.on_clear_credentials(move || {
        let Some(window) = weak_clear.upgrade() else {
            return;
        };
        let Some(path) = credentials_clear_path.clone() else {
            window.set_credential_status("无法清除：LOCALAPPDATA 未设置".into());
            window.set_credential_tone(status_tone(Tone::Bad));
            return;
        };
        *form_clear.borrow_mut() = AccountForm::new(None);
        window.set_credential_account_input("".into());
        window.set_credential_password_input("".into());
        window.set_password_visible(false);
        window.set_account_form_visible(false);
        window.set_credential_status("正在清除已保存的账号密码…".into());
        window.set_credential_tone(status_tone(Tone::Warn));

        let supervisor = Arc::clone(&supervisor_clear);
        let gateway_status = Arc::clone(&status_clear);
        let saved_state = Arc::clone(&saved_clear);
        let auth_state = Arc::clone(&auth_clear);
        let token_path = token_path_clear.clone();
        let auth_admin = Arc::clone(&auth_admin_clear);
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let cleared = match account::clear(&path) {
                Ok(cleared) => cleared,
                Err(error) => {
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            window.set_credential_status(format!("清除失败：{error}").into());
                            window.set_credential_tone(status_tone(Tone::Bad));
                        }
                    });
                    return;
                }
            };
            *saved_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = LoadOutcome::Missing;
            *auth_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = None;
            let restarted = {
                let mut guard = supervisor
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                match guard.as_mut() {
                    Some(supervisor) => {
                        supervisor.set_credentials(None);
                        supervisor.restart(StopReason::CredentialsChanged)
                    }
                    None => return,
                }
            };
            *gateway_status
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = restarted.clone();
            let auth = read_auth(&auth_admin, &token_path, &restarted);
            *auth_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(auth.clone());
            let view = account_view(&LoadOutcome::Missing, Some(&auth));
            let (message, tone) = if cleared {
                (
                    "已清除已保存的账号密码；本实例网关已重启".to_string(),
                    Tone::Good,
                )
            } else {
                (
                    "没有已保存的账号密码；本实例网关已重启".to_string(),
                    Tone::Warn,
                )
            };
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    apply_account(&window, &view);
                    window.set_credential_status(message.into());
                    window.set_credential_tone(status_tone(tone));
                }
            });
        });
    });

    let weak_continue = window.as_weak();
    let token_path_continue = paths.control_token.clone();
    let auth_admin_continue = Arc::clone(&auth_admin);
    let saved_continue = Arc::clone(&saved_state);
    let auth_continue = Arc::clone(&auth_state);
    let current_port_continue = Arc::clone(&current_port);
    window.on_continue_login(move || {
        let Some(window) = weak_continue.upgrade() else {
            return;
        };
        if LOGIN_IN_FLIGHT.swap(true, Ordering::SeqCst) {
            return;
        }
        window.set_account_busy(true);
        window.set_login_status("正在打开 Edge 登录窗口…".into());
        window.set_login_tone(status_tone(Tone::Warn));
        let base = format!(
            "http://127.0.0.1:{}",
            current_port_continue.load(Ordering::SeqCst)
        );
        let token_path = token_path_continue.clone();
        let auth_admin = Arc::clone(&auth_admin_continue);
        let saved_state = Arc::clone(&saved_continue);
        let auth_state = Arc::clone(&auth_continue);
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let result = match load_control_token(&token_path) {
                Ok(token) => auth_admin.start_visible_login(&base, &token),
                Err(error) => Err(error),
            };
            LOGIN_IN_FLIGHT.store(false, Ordering::SeqCst);
            let auth = match result {
                Ok(status) => AuthRead::Status(status),
                Err(error) => AuthRead::Unavailable(error),
            };
            *auth_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(auth.clone());
            let saved = saved_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .clone();
            let view = account_view(&saved, Some(&auth));
            let (message, tone) = match &auth {
                AuthRead::Status(status) => match status.state {
                    AuthState::Valid | AuthState::LoggingIn => (String::new(), Tone::Good),
                    _ => (format!("登录未完成：{}", view.state_detail), Tone::Bad),
                },
                AuthRead::Unavailable(reason) => {
                    (format!("无法打开可见登录：{reason}"), Tone::Bad)
                }
            };
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    window.set_account_busy(false);
                    apply_account(&window, &view);
                    window.set_login_status(message.into());
                    window.set_login_tone(status_tone(tone));
                }
            });
        });
    });

    let save_path = config_path.ok();
    let save_config = Arc::clone(&current_config);
    let save_supervisor = Arc::clone(&gateway_supervisor);
    let save_status = Arc::clone(&gateway_status);
    let save_port = Arc::clone(&current_port);
    let weak_save = window.as_weak();
    window.on_save_port(move |text| {
        let Some(window) = weak_save.upgrade() else {
            return;
        };
        let port = match config::validate_port(&text) {
            Ok(port) => port,
            Err(error) => {
                window.set_settings_status(format!("端口无效：{error}").into());
                window.set_settings_tone(status_tone(Tone::Bad));
                return;
            }
        };
        window.set_port_busy(true);
        window.set_settings_status(format!("正在检查端口 {port}…").into());
        window.set_settings_tone(status_tone(Tone::Warn));

        let supervisor = Arc::clone(&save_supervisor);
        let status = Arc::clone(&save_status);
        let current_port = Arc::clone(&save_port);
        let config = Arc::clone(&save_config);
        let path = save_path.clone();
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let (outcome, next_status) = {
                let mut guard = supervisor
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                let Some(supervisor) = guard.as_mut() else {
                    return;
                };
                let mut save = |port: u16| -> Result<(), String> {
                    let path = path
                        .as_ref()
                        .ok_or_else(|| "无法保存：LOCALAPPDATA 未设置".to_string())?;
                    let mut config = config
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    let next = DesktopConfig { model_api_port: port, ..*config };
                    config::save_to(path, &next)?;
                    *config = next;
                    Ok(())
                };
                let progress_window = weak.clone();
                let mut progress = move |stage| {
                    let text = match stage {
                        PortSwitchProgress::Waiting { in_flight } => format!("等待 {in_flight} 个在途请求结束…"),
                        PortSwitchProgress::Switching => format!("正在切换到 127.0.0.1:{port}…"),
                        PortSwitchProgress::Restoring => "正在恢复原端口…".to_string(),
                    };
                    let weak = progress_window.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            window.set_settings_status(text.into());
                        }
                    });
                };
                let outcome = supervisor.switch_port_with_progress(port, &mut save, &mut progress, &|| QUIT_REQUESTED.load(Ordering::SeqCst));
                (outcome, supervisor.status())
            };
            current_port.store(next_status.port, Ordering::SeqCst);
            *status
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = next_status.clone();
            let (message, tone) = match outcome {
                PortSwitch::Switched { to, .. } => (format!("已切换到 127.0.0.1:{to}"), Tone::Good),
                PortSwitch::Unchanged { port } => {
                    (format!("端口未变化：127.0.0.1:{port}"), Tone::Warn)
                }
                PortSwitch::Rejected { from, reason, .. } => (
                    format!("未切换：{reason}（仍为 127.0.0.1:{from}）"),
                    Tone::Bad,
                ),
                PortSwitch::Restored { from, reason, .. } => (
                    format!("新端口启动失败：{reason}；已恢复 127.0.0.1:{from}"),
                    Tone::Bad,
                ),
                PortSwitch::RestoreFailed {
                    from,
                    reason,
                    restore_error,
                    ..
                } => (
                    format!(
                        "新端口启动失败：{reason}；恢复 127.0.0.1:{from} 也失败：{restore_error}"
                    ),
                    Tone::Bad,
                ),
            };
            let next_port = next_status.port;
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    window.set_port_busy(false);
                    window.set_port_input(next_port.to_string().into());
                    window.set_active_address(format!("127.0.0.1:{next_port}").into());
                    window.set_settings_status(message.into());
                    window.set_settings_tone(status_tone(tone));
                }
            });
        });
    });

    let timer = Timer::default();
    let weak_timer = window.as_weak();
    let shell_timer = shell.clone();
    let supervisor_tray = Arc::clone(&gateway_supervisor);
    let status_tray = Arc::clone(&gateway_status);
    let auth_tray = Arc::clone(&auth_state);
    let mut tray = tray;
    let mut last_tip = String::new();
    timer.start(TimerMode::Repeated, Duration::from_millis(120), move || {
        let Some(window) = weak_timer.upgrade() else {
            return;
        };
        let paused = PAUSED.load(Ordering::SeqCst);
        let tip = {
            let auth = auth_tray
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            account::tray_tooltip(auth.as_ref(), paused)
        };
        if tip != last_tip {
            last_tip = tip;
            if let Some(active) = tray.as_ref() {
                active.set_tooltip(&last_tip);
            }
        }
        if let Some(active) = tray.as_ref() {
            match active.pump() {
                Some(tray::SHOW) => {
                    if shell_timer.borrow_mut().apply(ShellEvent::TrayShow)
                        == ShellAction::ShowWindow
                    {
                        show_now(&window);
                    }
                }
                Some(tray::QUIT) => {
                    request_quit(&window, &supervisor_tray, &status_tray);
                }
                _ => {}
            }
        }
        if QUIT.load(Ordering::SeqCst) {
            if let Some(active) = tray.take() {
                active.remove();
            }
            let _ = window.hide();
            let _ = slint::quit_event_loop();
        }
    });

    window.show()?;
    slint::run_event_loop_until_quit()?;
    Ok(())
}

#[cfg(not(windows))]
fn main() {
    eprintln!("um-desktop: 桌面版只在 Windows 上提供");
}
