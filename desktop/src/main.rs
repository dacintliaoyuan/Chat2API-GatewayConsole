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
mod window_icon;

use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU16, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::TimeZone;
use slint::{
    CloseRequestResponse, ComponentHandle, Model, ModelExt, ModelRc, Timer, TimerMode, VecModel,
};
use um_desktop::account::{
    self, account_view, apply_account, AccountForm, AccountView, AuthAdmin, AuthRead, AuthState,
    FormError, HttpAuthAdmin, LoadOutcome, OnDemandLogin, VerifyOutcome,
};
use um_desktop::clipboard;
use um_desktop::config::{
    self, DesktopConfig, LoadOutcome as ConfigLoadOutcome, MotionPreference, SidebarMode, ThemeMode,
};
use um_desktop::dwm;
use um_desktop::gateway::{
    load_control_token, resolve_gateway_binary, GatewayState, GatewayStatus, GatewaySupervisor,
    HttpGateway, InstancePaths, PortSwitch, PortSwitchProgress, StopReason, SystemLauncher,
    ThreadSleeper,
};
use um_desktop::instance::{self, InstanceError};
use um_desktop::log_history::{
    self, append_history_rows, HistoryClient, HistoryQuery, HistoryRange, HttpHistoryClient,
    LevelSelection,
};
use um_desktop::logstream::{format_log_time, plan_log_page, LevelFilter, LogStream, StreamLine};
use um_desktop::maintenance;
use um_desktop::models;
use um_desktop::models::ProbeClient;
use um_desktop::overview::{self, HttpProbe, Overview, Tone};
use um_desktop::recent_issues;
use um_desktop::shell::{ShellAction, ShellEvent, ShellState};
use um_desktop::ui::*;
use um_desktop::um_service;
use um_desktop::usage::{self, Coverage, Range, UsageView};

static PAUSED: AtomicBool = AtomicBool::new(false);
static QUIT_REQUESTED: AtomicBool = AtomicBool::new(false);
static QUIT: AtomicBool = AtomicBool::new(false);
static REFRESH_LOGS: AtomicBool = AtomicBool::new(false);
static OVERVIEW_REFRESH: AtomicU64 = AtomicU64::new(0);
static USAGE_REFRESH: AtomicU64 = AtomicU64::new(0);
static MODELS_REFRESH: AtomicU64 = AtomicU64::new(0);
static LOGIN_IN_FLIGHT: AtomicBool = AtomicBool::new(false);
static VERIFY_IN_FLIGHT: AtomicBool = AtomicBool::new(false);
static FILTER_EPOCH: AtomicU64 = AtomicU64::new(0);
static APPLIED_FILTER_EPOCH: AtomicU64 = AtomicU64::new(0);

/// One log-page update produced by the reader thread.
#[derive(Debug, Clone, PartialEq, Eq)]
struct LogUpdate {
    previous: Vec<StreamLine>,
    entries: Vec<StreamLine>,
    appended: Vec<StreamLine>,
    reset: bool,
    trimmed: usize,
    failure: Option<String>,
}

impl LogUpdate {
    fn failed(reason: String) -> Self {
        Self {
            previous: Vec::new(),
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
        Tone::Neutral => 3,
    }
}

fn theme_mode_index(theme: ThemeMode) -> i32 {
    match theme {
        ThemeMode::System => 0,
        ThemeMode::Light => 1,
        ThemeMode::Dark => 2,
    }
}

fn theme_mode_from_index(index: i32) -> ThemeMode {
    match index {
        1 => ThemeMode::Light,
        2 => ThemeMode::Dark,
        _ => ThemeMode::System,
    }
}

fn theme_label(theme: ThemeMode) -> &'static str {
    match theme {
        ThemeMode::System => "跟随系统",
        ThemeMode::Light => "浅色",
        ThemeMode::Dark => "深色",
    }
}

fn sidebar_label(mode: SidebarMode) -> &'static str {
    match mode {
        SidebarMode::Expanded => "侧栏展开",
        SidebarMode::IconsOnly => "侧栏仅图标",
    }
}

fn motion_label(preference: MotionPreference) -> &'static str {
    match preference {
        MotionPreference::System => "动效跟随系统",
        MotionPreference::Reduced => "减少动效",
    }
}

fn clock(at: Option<i64>) -> String {
    at.and_then(|at| chrono::Local.timestamp_millis_opt(at).single())
        .map(|time| time.format("%H:%M:%S").to_string())
        .unwrap_or_else(|| "—".to_string())
}

fn clock_now() -> String {
    clock(Some(chrono::Local::now().timestamp_millis()))
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
    window.set_account_input_invalid(false);
    window.set_account_input_error("".into());
    window.set_password_input_invalid(false);
    window.set_password_input_error("".into());
    window.set_show_intro(false);
    window.invoke_navigate_to(2);
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

fn log_row(line: &StreamLine) -> LogRow {
    let time = format_log_time(line.entry.at, chrono::Local::now());
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
        at: time.label.into(),
        level: level.into(),
        source: "chat2api".into(),
        text: line.entry.text.clone().into(),
        detail: format!("{}  {level}\n\n{}", time.precise, line.detail).into(),
    }
}

/// One history row already carries the gateway's level and source labels.
fn history_row(row: &log_history::HistoryRow) -> LogRow {
    let time = format_log_time(row.at, chrono::Local::now());
    LogRow {
        id: row.key.clone().into(),
        at: time.label.into(),
        level: row.level.clone().into(),
        source: row.source.clone().into(),
        text: row.text.clone().into(),
        detail: format!(
            "{}  {} · {}\n\n{}",
            time.precise, row.level, row.source, row.detail
        )
        .into(),
    }
}

fn apply_overview(window: &MainWindow, overview: &Overview, account: &AccountView) {
    window.set_gateway_value(overview.gateway.value.clone().into());
    window.set_gateway_detail(overview.gateway.detail.clone().into());
    window.set_gateway_tone(status_tone(overview.gateway.tone));
    // A completed probe is a settled state; only the first read pulses.
    window.set_gateway_pulse(false);
    window.set_supervision_value(overview.supervision.value.clone().into());
    window.set_supervision_detail(overview.supervision.detail.clone().into());
    window.set_supervision_tone(status_tone(overview.supervision.tone));
    window.set_upstream_value(overview.upstream.value.clone().into());
    window.set_upstream_detail(overview.upstream.detail.clone().into());
    window.set_upstream_tone(status_tone(overview.upstream.tone));
    window.set_upstream_pulse(false);
    apply_account(window, account);
    window.set_status_line(overview::status_line(overview).into());
    window.set_status_tone(if overview.failures.is_empty() { 0 } else { 2 });
    if overview.failures.is_empty() {
        let now = clock_now();
        window.set_last_update(now.clone().into());
        window.set_overview_updated_at(now.into());
    }
}

fn read_live_overview(
    probe: &HttpProbe,
    auth_admin: &HttpAuthAdmin,
    token_path: &Path,
    base: &str,
    supervision: overview::Card,
    saved: &LoadOutcome,
) -> (Overview, AccountView, AuthRead) {
    let overview = overview::read_overview(probe, base, supervision);
    let auth = match load_control_token(token_path) {
        Ok(token) => match auth_admin.read_status(base, &token) {
            Ok(status) => AuthRead::Status(status),
            Err(error) => AuthRead::Unavailable(error),
        },
        Err(error) => AuthRead::Unavailable(error),
    };
    let account = account_view(saved, Some(&auth));
    (overview, account, auth)
}

/// One UM service read: the gateway answers only while the supervisor says it is
/// running; otherwise the card points at the gateway start entry.
fn service_view(
    gateway: &GatewayStatus,
    api: &um_service::HttpServiceApi,
    token_path: &Path,
    base: &str,
) -> um_service::ServiceView {
    match gateway.state {
        GatewayState::Running { .. } => match load_control_token(token_path) {
            Ok(token) => um_service::read(api, base, token.expose()),
            Err(error) => um_service::ServiceView::failure(error),
        },
        GatewayState::Starting => {
            um_service::ServiceView::gateway_starting(&gateway.state.detail())
        }
        _ => um_service::ServiceView::gateway_unavailable(&gateway.state.detail()),
    }
}

fn apply_logs(window: &MainWindow, update: &LogUpdate) {
    if window.get_log_mode() != 0 {
        return;
    }
    if let Some(reason) = &update.failure {
        window.set_log_status(format!("刷新失败：{reason}").into());
        window.set_log_tone(status_tone(Tone::Bad));
        window.set_log_refresh_pending(false);
        window.set_log_refreshed(false);
        return;
    }
    let acknowledge_refresh = window.get_log_refresh_pending();

    // The live layer is the unfiltered tail; the history filters never reach it.
    let epoch = FILTER_EPOCH.load(Ordering::SeqCst);
    let filter_changed = APPLIED_FILTER_EPOCH.swap(epoch, Ordering::SeqCst) != epoch;
    let follow = window.get_log_follow();
    let model = window.get_logs();
    let Some(model) = model.as_any().downcast_ref::<VecModel<LogRow>>() else {
        return;
    };
    let plan = plan_log_page(
        &update.previous,
        &update.entries,
        &update.appended,
        update.reset,
        update.trimmed,
        filter_changed,
        follow,
        window.get_log_scroll_y(),
        window.get_new_log_count(),
        LevelFilter::All,
        "",
    );
    if plan.rebuild {
        model.set_vec(plan.rows.iter().map(log_row).collect::<Vec<_>>());
    } else if plan.added > 0 {
        model.extend(plan.rows.iter().skip(model.row_count()).map(log_row));
    }
    if let Some(scroll_y) = plan.scroll_y {
        window.set_log_scroll_y(scroll_y);
    }
    window.set_new_log_count(plan.new_count);

    let total = update.entries.len();
    let visible = model.row_count();
    let status = if total == 0 {
        "本实例暂无日志记录".to_string()
    } else if visible == total {
        format!("实时 · 最近 {total} 条")
    } else {
        format!("实时 · 显示 {visible} / {total} 条")
    };
    window.set_log_total(total as i32);
    window.set_log_status(status.into());
    // A live stream reads as healthy; an empty list stays neutral.
    window.set_log_tone(if total == 0 {
        3
    } else {
        status_tone(Tone::Good)
    });
    window.set_log_refresh_pending(false);
    if acknowledge_refresh {
        window.set_log_refreshed(true);
    }
    window.set_log_history_note("".into());
    window.set_log_version(window.get_log_version().wrapping_add(1));
    window.set_log_updated_at(clock_now().into());
}

#[derive(Default)]
struct HistoryNav {
    rows: Vec<log_history::HistoryRow>,
    cursor: Option<String>,
    generation: u64,
}

fn level_selection(window: &MainWindow) -> LevelSelection {
    LevelSelection::from_flags(
        window.get_log_level_error(),
        window.get_log_level_warn(),
        window.get_log_level_info(),
        window.get_log_level_debug(),
    )
}

fn sync_log_level(window: &MainWindow) {
    window.set_log_filter_level(level_selection(window).log_level().index());
}

fn apply_level_selection(window: &MainWindow, levels: LevelSelection) {
    let (error, warn, info, debug) = levels.flags();
    window.set_log_level_error(error);
    window.set_log_level_warn(warn);
    window.set_log_level_info(info);
    window.set_log_level_debug(debug);
}

fn history_sources(window: &MainWindow) -> Vec<String> {
    let options = window.get_log_source_options();
    (1..options.row_count())
        .filter_map(|index| options.row_data(index))
        .map(|source| source.to_string())
        .collect()
}

fn apply_history_sources(window: &MainWindow, sources: &[String]) {
    let mut options = vec![String::from("全部来源")];
    options.extend(sources.iter().cloned());
    let selected = window.get_log_source().to_string();
    let index = if selected.is_empty() {
        0
    } else {
        options
            .iter()
            .position(|source| source == &selected)
            .map_or(0, |index| index as i32)
    };
    let options = options
        .iter()
        .map(|source| source.as_str().into())
        .collect::<Vec<slint::SharedString>>();
    window.set_log_source_options(ModelRc::from(options.as_slice()));
    window.set_log_source_index(index);
    if index == 0 && !selected.is_empty() {
        window.set_log_source("".into());
    }
}

fn apply_history_failure(window: &MainWindow, nav: &mut HistoryNav, reason: String) {
    let previous = log_history::HistoryView {
        status: String::new(),
        tone: Tone::Neutral,
        rows: nav.rows.clone(),
        sources: history_sources(window),
        earlier: nav.cursor.clone(),
        empty: false,
        note: String::new(),
    };
    let failure = log_history::history_failure(reason, Some(&previous));
    window.set_log_status(failure.status.into());
    window.set_log_tone(status_tone(failure.tone));
    window.set_older_logs(nav.cursor.is_some());
    window.set_log_history_note("".into());
    window.set_log_refresh_pending(false);
    window.set_log_refreshed(false);
}

fn load_log_history(
    window: &MainWindow,
    client: &Arc<dyn HistoryClient>,
    base_url: &str,
    token_path: &Path,
    navigation: &Arc<Mutex<HistoryNav>>,
    append: bool,
) {
    if window.get_log_mode() != 1 {
        return;
    }
    sync_log_level(window);
    let cursor = if append {
        navigation
            .lock()
            .expect("log history navigation poisoned")
            .cursor
            .clone()
    } else {
        None
    };
    if append && cursor.is_none() {
        window.set_older_logs(false);
        window.set_log_history_note("没有更早记录".into());
        return;
    }
    let query = HistoryQuery {
        range: HistoryRange::from_index(window.get_log_range()),
        levels: level_selection(window),
        source: window.get_log_source().to_string(),
        keyword: window.get_log_filter_keyword().to_string(),
        cursor,
        limit: log_history::HISTORY_LIMIT,
    };
    let (generation, previous_count) = {
        let mut nav = navigation.lock().expect("log history navigation poisoned");
        if !append {
            nav.rows.clear();
            nav.cursor = None;
        }
        nav.generation = nav.generation.wrapping_add(1);
        (nav.generation, nav.rows.len())
    };
    let base = base_url.to_string();
    let token_path = token_path.to_path_buf();
    let weak = window.as_weak();
    let navigation = Arc::clone(navigation);
    let client = Arc::clone(client);
    window.set_older_logs(false);
    window.set_log_status(
        if append {
            "正在读取更早的日志…"
        } else {
            "正在读取历史日志…"
        }
        .into(),
    );
    window.set_log_tone(3);
    window.set_log_history_note("".into());
    std::thread::spawn(move || {
        let result = load_control_token(&token_path)
            .and_then(|token| client.read_history(&base, &token, &query));
        let _ = slint::invoke_from_event_loop(move || {
            let Some(window) = weak.upgrade() else {
                return;
            };
            let mut nav = navigation.lock().expect("log history navigation poisoned");
            if window.get_log_mode() != 1 || nav.generation != generation {
                return;
            }
            let model = window.get_logs();
            let Some(model) = model.as_any().downcast_ref::<VecModel<LogRow>>() else {
                return;
            };
            let acknowledge_refresh = window.get_log_refresh_pending();
            match result {
                Ok(payload) => match log_history::parse_history_payload(&payload) {
                    Ok(view) if view.tone == Tone::Bad => {
                        apply_history_failure(&window, &mut nav, view.status)
                    }
                    Ok(view) => {
                        let mut view = view;
                        view.rows = log_history::filter_history_rows(&view.rows, &query.keyword);
                        view.empty = view.rows.is_empty();
                        if view.empty {
                            view.status = if query.keyword.trim().is_empty() {
                                "读取范围内没有符合条件的日志".to_string()
                            } else {
                                "本页没有符合关键词的日志".to_string()
                            };
                            view.tone = Tone::Neutral;
                        } else if !query.keyword.trim().is_empty() {
                            view.status = format!("历史 · 本页 {} 条", view.rows.len());
                            view.tone = Tone::Good;
                        }
                        let rows = if append {
                            append_history_rows(&nav.rows, &view)
                        } else {
                            view.rows.clone()
                        };
                        nav.rows = rows.clone();
                        nav.cursor = view.earlier.clone();
                        if append {
                            model.extend(rows.iter().skip(previous_count).map(history_row));
                        } else {
                            model.set_vec(rows.iter().map(history_row).collect::<Vec<_>>());
                            window.set_log_scroll_y(0.0);
                            window.set_open_log_id("".into());
                        }
                        apply_history_sources(&window, &view.sources);
                        window.set_older_logs(nav.cursor.is_some());
                        window.set_log_history_note(if nav.cursor.is_none() {
                            "没有更早记录".into()
                        } else {
                            "".into()
                        });
                        window.set_log_status(
                            if append {
                                format!("历史 · 已加载 {} 条", rows.len())
                            } else {
                                view.status.clone()
                            }
                            .into(),
                        );
                        window.set_log_tone(status_tone(view.tone));
                        window.set_log_total(rows.len() as i32);
                        window.set_log_updated_at(clock_now().into());
                        window.set_log_version(window.get_log_version().wrapping_add(1));
                        window.set_log_refresh_pending(false);
                        if acknowledge_refresh {
                            window.set_log_refreshed(true);
                        }
                    }
                    Err(reason) => apply_history_failure(&window, &mut nav, reason),
                },
                Err(reason) => apply_history_failure(&window, &mut nav, reason),
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
    let points = if matches!(view.coverage, Coverage::Empty) {
        Vec::new()
    } else {
        view.buckets
            .into_iter()
            .map(|bucket| UsagePoint {
                label: bucket
                    .label
                    .get(5..)
                    .filter(|_| bucket.label.len() == 10)
                    .unwrap_or(&bucket.label)
                    .into(),
                detail_label: format!("北京时间 {}", bucket.label).into(),
                token_value: bucket.tokens.into(),
                request_value: bucket.requests.into(),
                // The axis reads in 亿 so nine-digit labels do not crowd the
                // plot; the exact total stays in the card and the selection line.
                token_count: bucket.token_count.map_or(f32::NAN, |value| value as f32 / 100_000_000.0),
                request_count: bucket.request_count.map_or(f32::NAN, |value| value as f32),
            })
            .collect::<Vec<_>>()
    };
    window.set_token_total(view.token_total.into());
    window.set_request_total(view.request_total.into());
    let points = ModelRc::from(points.as_slice());
    window.set_usage_labels(ModelRc::new(points.clone().map(|point| point.label)));
    window.set_usage_token_counts(ModelRc::new(points.clone().map(|point| point.token_count)));
    window.set_usage_request_counts(ModelRc::new(
        points.clone().map(|point| point.request_count),
    ));
    window.set_usage_points(points);
    window.set_usage_status(view.message.into());
    window.set_usage_tone(coverage_tone(view.coverage));
    window.set_usage_loading(false);
    if view.coverage != Coverage::Unavailable {
        window.set_overview_updated_at(clock_now().into());
    }
}

fn chart_dataset(label: &str, values: Vec<f32>) -> ChartDataset {
    ChartDataset {
        label: label.into(),
        series_type: ChartSeriesType::Bar,
        values: ModelRc::from(values.as_slice()),
        points: ModelRc::default(),
        color: slint::Color::from_argb_u8(0, 0, 0, 0),
        hidden: false,
        fill: false,
        tension: 0.0,
        stepped: false,
        right_axis: false,
    }
}

fn model_series(page: &usage::RequestPage, metric: u8) -> Vec<ChartDataset> {
    page.chart
        .models
        .iter()
        .map(|model| {
            let values = page
                .chart
                .points
                .iter()
                .map(|point| {
                    point
                        .models
                        .iter()
                        .find(|row| &row.model == model)
                        .map_or(f32::NAN, |row| match metric {
                            1 => row.requests.map_or(f32::NAN, |value| value as f32),
                            2 => row.speed.map_or(f32::NAN, |value| value as f32),
                            _ => row.tokens.map_or(f32::NAN, |value| value as f32),
                        })
                })
                .collect::<Vec<_>>();
            chart_dataset(model, values)
        })
        .collect()
}

fn request_row_ui(row: &usage::RequestRow) -> RequestRow {
    RequestRow {
        id: row.key.clone().into(),
        at: row.at.clone().into(),
        model: row.model.clone().into(),
        outcome: row.result.clone().into(),
        tone: match row.result.as_str() {
            "成功" => status_tone(Tone::Good),
            "失败" | "截断" => status_tone(Tone::Bad),
            "客户端断开" => status_tone(Tone::Warn),
            _ => status_tone(Tone::Neutral),
        },
        summary: format!(
            "耗时 {} · ID {}",
            row.duration,
            row.request_id.as_deref().unwrap_or("—")
        )
        .into(),
        first_token: row.first_token.clone().into(),
        detail: "".into(),
    }
}

fn apply_request_rows(window: &MainWindow, rows: &[usage::RequestRow]) {
    let rows = rows.iter().map(request_row_ui).collect::<Vec<_>>();
    window.set_requests(ModelRc::from(rows.as_slice()));
}

fn apply_request_page(window: &MainWindow, page: &usage::RequestPage) {
    let mut models = vec![String::from("全部模型")];
    models.extend(page.models.iter().cloned());
    let selected = window.get_request_model_filter().to_string();
    let model_index = if selected.is_empty() {
        0
    } else {
        models
            .iter()
            .position(|model| model == &selected)
            .map_or(0, |index| index as i32)
    };
    let model_options = models
        .iter()
        .map(|model| model.as_str().into())
        .collect::<Vec<slint::SharedString>>();
    window.set_request_model_options(ModelRc::from(model_options.as_slice()));
    window.set_request_model_index(model_index);
    apply_request_rows(window, &page.rows);
    let count = page.rows.len();
    apply_request_analytics(window, page);
    if count == 0 || !page.message.is_empty() {
        window.set_request_status(page.message.clone().into());
    } else {
        window.set_request_status(format!("北京时间 · 本页 {count} 条").into());
    }
    window.set_request_tone(coverage_tone(page.coverage));
    window.set_older_requests(page.earlier.is_some());
    if page.coverage != Coverage::Unavailable {
        window.set_request_updated_at(clock_now().into());
    }
}

fn apply_request_analytics(window: &MainWindow, page: &usage::RequestPage) {
    let cards = page.summary.cards();
    // The compact request page folds the five cards into two readable lines;
    // the break lands between whole metrics instead of inside a unit.
    let summary = cards
        .iter()
        .map(|card| format!("{} {}", card.label, card.value))
        .collect::<Vec<_>>();
    window.set_request_summary(format!("{}
{}", summary[..3].join(" · "), summary[3..].join(" · ")).into());
    let metrics = cards
        .into_iter()
        .map(|card| RequestMetric {
            label: card.label.into(),
            value: card.value.into(),
            detail: card.detail.into(),
            detail_tone: card.detail_tone,
        })
        .collect::<Vec<_>>();
    window.set_request_metrics(ModelRc::from(metrics.as_slice()));
    window.set_request_chart_labels(ModelRc::from(
        page.chart
            .points
            .iter()
            .map(|point| point.label.clone().into())
            .collect::<Vec<slint::SharedString>>()
            .as_slice(),
    ));
    window.set_request_chart_token_counts(ModelRc::from(
        page.chart
            .points
            .iter()
            // The request chart plots the same 亿 scale as the overview.
            .map(|point| point.token_count.map_or(f32::NAN, |value| value as f32 / 100_000_000.0))
            .collect::<Vec<_>>()
            .as_slice(),
    ));
    window.set_request_chart_request_counts(ModelRc::from(
        page.chart
            .points
            .iter()
            .map(|point| point.request_count.map_or(f32::NAN, |value| value as f32))
            .collect::<Vec<_>>()
            .as_slice(),
    ));
    window.set_request_chart_speed_counts(ModelRc::from(
        page.chart
            .points
            .iter()
            .map(|point| point.speed_count.map_or(f32::NAN, |value| value as f32))
            .collect::<Vec<_>>()
            .as_slice(),
    ));
    window.set_request_chart_token_series(ModelRc::from(model_series(page, 0).as_slice()));
    window.set_request_chart_request_series(ModelRc::from(model_series(page, 1).as_slice()));
    window.set_request_chart_speed_series(ModelRc::from(model_series(page, 2).as_slice()));
    let model_rows = page
        .model_speeds
        .iter()
        .map(|row| ModelSpeedRow {
            model: row.model.clone().into(),
            requests: row.requests.clone().into(),
            output: row.output.clone().into(),
            speed: row.speed.clone().into(),
            first_token: row.first_token.clone().into(),
        })
        .collect::<Vec<_>>();
    window.set_request_model_speeds(ModelRc::from(model_rows.as_slice()));
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RequestLoad {
    Query,
    Page,
    Refresh,
    Earlier,
}

fn load_requests(
    window: &MainWindow,
    ledger: &Path,
    nav: &Arc<Mutex<usage::RequestNav>>,
    direction: i32,
    mode: RequestLoad,
) {
    let refresh = mode == RequestLoad::Refresh;
    let earlier = mode == RequestLoad::Earlier;
    let range = Range::from_index(window.get_request_period());
    let current_time = chrono::Utc::now().timestamp_millis();
    let (cursor, now, generation) = {
        let mut nav = nav.lock().expect("request navigation poisoned");
        let cursor = match mode {
            RequestLoad::Refresh => nav.refresh(current_time, range),
            RequestLoad::Query => nav.query(current_time),
            RequestLoad::Page => nav.page(direction),
            RequestLoad::Earlier => match nav.begin_earlier() {
                usage::EarlierLoad::Cursor(cursor) => Some(Some(cursor)),
                usage::EarlierLoad::NoMore => {
                    drop(nav);
                    window.set_request_status("没有更早记录".into());
                    window.set_request_tone(3);
                    window.set_has_earlier_records(false);
                    return;
                }
                usage::EarlierLoad::Busy => return,
            },
        };
        let Some(cursor) = cursor else {
            return;
        };
        (cursor, nav.now, nav.generation)
    };
    let result = window.get_request_result();
    let keyword = window.get_request_keyword().to_string();
    let model = window.get_request_model_filter().to_string();
    let scroll = window.get_request_scroll_y();
    let weak = window.as_weak();
    let ledger = ledger.to_path_buf();
    let nav = Arc::clone(nav);
    if earlier {
        // Loading earlier appends history: the displayed rows, the selected
        // detail and the scroll position stay where they are.
        window.set_request_status("正在加载更早的记录…".into());
        window.set_request_tone(3);
    } else if !refresh {
        window.set_selected_request("".into());
        // Filter changes keep the reader where they were; only a page jump
        // starts at the top of the new page.
        if mode == RequestLoad::Page {
            window.set_request_scroll_y(0.0);
        }
        window.set_request_status("正在读取请求记录…".into());
        window.set_request_tone(3);
        window.set_older_requests(false);
        window.set_has_earlier_records(false);
        window.set_newer_requests(false);
    }
    std::thread::spawn(move || {
        let model = if model.is_empty() { None } else { Some(model) };
        let page = usage::read_requests(
            &ledger,
            range,
            model.as_deref(),
            result,
            &keyword,
            cursor.as_deref(),
            now,
        );
        let _ = slint::invoke_from_event_loop(move || {
            let Some(window) = weak.upgrade() else { return; };
            let mut nav = nav.lock().expect("request navigation poisoned");
            if nav.generation != generation { return; }
            if earlier {
                match nav.finish_earlier(page) {
                    usage::EarlierOutcome::Appended { added } => {
                        apply_request_rows(&window, &nav.rows);
                        // Appending changes the model; put the reader back at
                        // the row they were reading.
                        window.set_request_scroll_y(scroll);
                        window.set_has_earlier_records(nav.append_cursor.is_some());
                        window.set_older_requests(nav.earlier.is_some());
                        window.set_request_status(
                            format!("已加载更早的 {added} 条记录 · 共 {} 条", nav.rows.len())
                                .into(),
                        );
                        window.set_request_tone(0);
                        window.set_request_updated_at(clock_now().into());
                    }
                    usage::EarlierOutcome::NoMore => {
                        window.set_has_earlier_records(false);
                        window.set_request_status("没有更早记录".into());
                        window.set_request_tone(3);
                    }
                    usage::EarlierOutcome::Failed(message) => {
                        window.set_has_earlier_records(nav.append_cursor.is_some());
                        window.set_request_status(format!("加载更早失败：{message}").into());
                        window.set_request_tone(2);
                    }
                }
                return;
            }
            let page = if refresh {
                let previous = usage::RequestPage {
                    rows: nav.rows.clone(),
                    earlier: nav.earlier.clone(),
                    coverage: Coverage::Complete,
                    message: String::new(),
                    summary: nav.summary.clone(),
                    model_speeds: nav.model_speeds.clone(),
                    chart: nav.chart.clone(),
                    models: nav.models.clone(),
                };
                usage::keep_previous_page_on_failure(&previous, page)
            } else {
                page
            };
            apply_request_page(&window, &page);
            window.set_newer_requests(nav.pages.len() > 1);
            // A failed refresh kept the appended rows and their 加载更早 cursor;
            // a successful page read restarts 加载更早 from this page.
            nav.apply_page(&page, !refresh || page.coverage != Coverage::Unavailable);
            window.set_older_requests(nav.earlier.is_some());
            window.set_has_earlier_records(nav.append_cursor.is_some());
            if refresh {
                // Replacing the list model can move the viewport; the refresh
                // must leave the reader where they were.
                window.set_request_scroll_y(scroll);
            }
        });
    });
}

fn show_request(
    window: &MainWindow,
    log: &Path,
    nav: &Mutex<usage::RequestNav>,
    key: &str,
) {
    let nav = nav.lock().expect("request navigation poisoned");
    let Some(row) = nav.rows.iter().find(|row| row.key == key).cloned() else {
        return;
    };
    let key = row.key.clone();
    let weak = window.as_weak();
    let log = log.to_path_buf();
    window.set_selected_request(key.clone().into());
    window.set_request_detail_title("请求详情".into());
    window.set_request_detail(usage::request_detail(&row, None).into());
    std::thread::spawn(move || {
        let logs = usage::read_request_logs(
            &log,
            row.request_id.as_deref(),
            chrono::Utc::now().timestamp_millis(),
        );
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
        )
        .is_ok()
            && allowed != 0
    }
}

fn hide_now(window: &MainWindow) {
    PAUSED.store(true, Ordering::SeqCst);
    window.set_refresh_paused(true);
    let _ = window.hide();
}

/// Persists every page setting the window currently shows. A failed write keeps
/// the captured values in memory and reports the reason on the settings status line.
fn persist_window_settings(
    window: &MainWindow,
    path: Option<&Path>,
    config: &Mutex<DesktopConfig>,
    applied: &str,
) {
    let mut config = config
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    match config::save_window_settings(path, &mut config, window) {
        Ok(()) => {
            window.set_settings_status(applied.into());
            window.set_settings_tone(status_tone(Tone::Good));
        }
        Err(message) => {
            window.set_settings_status(message.into());
            window.set_settings_tone(status_tone(Tone::Warn));
        }
    }
}

fn show_now(window: &MainWindow) {
    let _ = window.show();
    window.window().set_minimized(false);
    PAUSED.store(false, Ordering::SeqCst);
    // Clearing the pause immediately refreshes the visible page and restarts the
    // auto-refresh countdown.
    window.set_refresh_paused(false);
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
    window.set_status_tone(status_tone(Tone::Warn));
    std::thread::spawn(move || {
        let stopped = {
            let mut guard = supervisor.lock().expect("gateway supervisor poisoned");
            guard
                .as_mut()
                .map(|supervisor| supervisor.stop(StopReason::UserRequested))
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
                        window.set_status_tone(status_tone(Tone::Bad));
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
    slintplot::register!(window);
    let config_path = config::default_config_path();
    let (loaded_config, settings_status, settings_tone) = match &config_path {
        Ok(path) => {
            let (outcome, warnings) = config::load_from_with_warnings(path);
            match outcome {
                ConfigLoadOutcome::Missing => (
                    DesktopConfig::default(),
                    "使用默认端口 7999（尚未保存设置）；启动时接入已有实例或拉起本实例网关"
                        .to_string(),
                    Tone::Neutral,
                ),
                ConfigLoadOutcome::Loaded(config) if warnings.is_empty() => (
                    config,
                    format!(
                        "已加载配置：{}；启动时接入已有实例或拉起本实例网关",
                        path.display()
                    ),
                    Tone::Good,
                ),
                ConfigLoadOutcome::Loaded(config) => (
                    config,
                    format!(
                        "已加载配置：{}；以下设置已回默认：{}",
                        path.display(),
                        warnings.join("；")
                    ),
                    Tone::Warn,
                ),
                ConfigLoadOutcome::Invalid(reason) => (
                    DesktopConfig::default(),
                    format!("配置不可用：{reason}；使用默认端口 7999"),
                    Tone::Bad,
                ),
            }
        }
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
    window.set_theme_mode(theme_mode_index(loaded_config.theme));
    config::apply_settings(&window, &loaded_config, allow_motion());
    window.set_log_mode(
        if matches!(loaded_config.settings.log_range, config::LogRange::Live) {
            0
        } else {
            1
        },
    );
    window.set_log_range(HistoryRange::from_log_range(loaded_config.settings.log_range).index());
    apply_level_selection(
        &window,
        LevelSelection::from_log_level(loaded_config.settings.log_level),
    );
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
    let verify_state: Arc<Mutex<Option<VerifyOutcome>>> = Arc::new(Mutex::new(None));
    let account_form = Rc::new(RefCell::new(AccountForm::new(None)));
    let initial_account = account_view(&saved_credentials, None);
    apply_account(&window, &initial_account);
    account::apply_verify(&window, &account::verify_view(None));

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

    // The maintenance menu starts closed while the supervisor is still
    // starting; the overview reader refreshes it with the real state.
    {
        let gateway = gateway_status
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        maintenance::apply_menu(
            &window,
            &maintenance::menu(&maintenance::MenuState::new(&gateway.state, None, None)),
        );
    }

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
            // The first read may have landed while the supervisor was still
            // starting; read the service card again now that the state is known.
            OVERVIEW_REFRESH.fetch_add(1, Ordering::SeqCst);
        });
    }

    let probe = HttpProbe::new();
    let auth_admin = Arc::new(HttpAuthAdmin::new());
    let service_api = Arc::new(um_service::HttpServiceApi::new());
    let service_state: Arc<Mutex<Option<um_service::ServiceView>>> = Arc::new(Mutex::new(None));
    let service_log = paths.service_log.clone();
    let control_token_path = paths.control_token.clone();
    let upstream_faults_path = paths.upstream_faults.clone();
    // The settings page's UM model catalog. Every read is strict (`fresh=1`):
    // it runs when the gateway first becomes running, after a restart or port
    // change, and on every manual refresh. A failed read keeps the previous rows
    // and marks them stale; a legal empty catalog clears the rows.
    let catalog_state: Arc<Mutex<Option<models::CatalogView>>> = Arc::new(Mutex::new(None));
    {
        let weak = window.as_weak();
        let gateway_status = Arc::clone(&gateway_status);
        let current_port = Arc::clone(&current_port);
        let token_path = paths.control_token.clone();
        let state = Arc::clone(&catalog_state);
        std::thread::spawn(move || {
            let reader = models::HttpCatalogReader::new();
            let mut last_epoch = 0u64;
            let mut last_port: Option<u16> = None;
            let mut was_running = false;
            while !QUIT.load(Ordering::SeqCst) {
                let epoch = MODELS_REFRESH.load(Ordering::SeqCst);
                let gateway = gateway_status
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let running = matches!(gateway.state, GatewayState::Running { .. });
                let port = current_port.load(Ordering::SeqCst);
                let refresh = epoch != last_epoch;
                let started = running && (!was_running || last_port != Some(port));
                if !refresh && !started {
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                }
                last_epoch = epoch;
                last_port = Some(port);
                was_running = running;
                let previous = state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let view = if !running {
                    models::catalog_failure(
                        format!("本实例网关未就绪：{}", gateway.state.detail()),
                        previous.as_ref(),
                    )
                } else {
                    match load_control_token(&token_path) {
                        Ok(token) => models::refresh_catalog(
                            &reader,
                            &format!("http://127.0.0.1:{port}"),
                            &token,
                            previous.as_ref(),
                        ),
                        Err(error) => models::catalog_failure(error, previous.as_ref()),
                    }
                };
                *state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(view.clone());
                let weak = weak.clone();
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(window) = weak.upgrade() {
                        models::apply_catalog(&window, &view);
                    }
                });
                std::thread::sleep(Duration::from_millis(100));
            }
        });
    }
    let weak_models = window.as_weak();
    window.on_refresh_um_models(move || {
        if let Some(window) = weak_models.upgrade() {
            window.set_um_model_loading(true);
            window.set_um_model_status("正在刷新 UM 模型目录…".into());
            window.set_um_model_tone(3);
        }
        MODELS_REFRESH.fetch_add(1, Ordering::SeqCst);
    });
    let weak_models = window.as_weak();
    let open_catalog = Arc::clone(&catalog_state);
    window.on_open_model(move |id| {
        let Some(window) = weak_models.upgrade() else {
            return;
        };
        let state = open_catalog
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        models::open_model(&window, state.as_ref(), id.as_str());
    });

    // Context probes (#154): the gateway owns the queue and the per-model
    // results, so the App polls its admin face and only keeps the batch it
    // submitted. The poll keeps the previous rows on a failed read; the status
    // carries the reason. Submit and stop are immediate loopback actions.
    let probe_batch: Arc<Mutex<Option<Vec<String>>>> = Arc::new(Mutex::new(None));
    let probe_client = Arc::new(models::HttpProbeClient::new());
    // The overview's recent-issues card reads the last probe snapshot; a failed
    // read keeps its reason in `ProbeView` and is never folded into zero.
    let probe_state: Arc<Mutex<Option<models::ProbeView>>> = Arc::new(Mutex::new(None));
    {
        let weak = window.as_weak();
        let gateway_status = Arc::clone(&gateway_status);
        let current_port = Arc::clone(&current_port);
        let token_path = paths.control_token.clone();
        let catalog_state = Arc::clone(&catalog_state);
        let batch_state = Arc::clone(&probe_batch);
        let probe_state = Arc::clone(&probe_state);
        std::thread::spawn(move || {
            let client = models::HttpProbeClient::new();
            let mut last_view: Option<models::ProbeView> = None;
            while !QUIT.load(Ordering::SeqCst) {
                let gateway = gateway_status
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                if !matches!(gateway.state, GatewayState::Running { .. }) {
                    std::thread::sleep(Duration::from_millis(300));
                    continue;
                }
                let base = format!("http://127.0.0.1:{}", current_port.load(Ordering::SeqCst));
                let catalog = catalog_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let batch = batch_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let read = match load_control_token(&token_path) {
                    Ok(token) => client.read_probes(&base, &token),
                    Err(error) => Err(error),
                };
                let view = match read {
                    Ok(payload) => {
                        match models::parse_probe_snapshot(
                            &payload,
                            batch.as_deref(),
                            catalog.as_ref(),
                        ) {
                            Ok(view) => {
                                last_view = Some(view.clone());
                                view
                            }
                            Err(reason) => models::probe_failure(reason, last_view.as_ref()),
                        }
                    }
                    Err(reason) => models::probe_failure(reason, last_view.as_ref()),
                };
                *probe_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(view.clone());
                let weak = weak.clone();
                let _ = slint::invoke_from_event_loop(move || {
                    if let Some(window) = weak.upgrade() {
                        models::apply_probe(&window, &view);
                    }
                });
                std::thread::sleep(Duration::from_millis(400));
            }
        });
    }
    let weak_probe = window.as_weak();
    window.on_toggle_probe_model(move |id| {
        if let Some(window) = weak_probe.upgrade() {
            models::toggle_probe_model(&window, id.as_str());
        }
    });
    let weak_probe = window.as_weak();
    let probe_catalog = Arc::clone(&catalog_state);
    window.on_select_all_probes(move || {
        if let Some(window) = weak_probe.upgrade() {
            let catalog = probe_catalog
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .clone();
            models::select_all_probes(&window, catalog.as_ref());
        }
    });
    let weak_probe = window.as_weak();
    window.on_clear_probe_selection(move || {
        if let Some(window) = weak_probe.upgrade() {
            models::clear_probe_selection(&window);
        }
    });
    let weak_probe = window.as_weak();
    let probe_client_run = Arc::clone(&probe_client);
    let probe_port_run = Arc::clone(&current_port);
    let probe_token_run = paths.control_token.clone();
    let probe_batch_run = Arc::clone(&probe_batch);
    window.on_run_context_probe(move || {
        let Some(window) = weak_probe.upgrade() else {
            return;
        };
        if window.get_probe_loading() || window.get_probe_running() {
            return;
        }
        let base = format!("http://127.0.0.1:{}", probe_port_run.load(Ordering::SeqCst));
        let token = match load_control_token(&probe_token_run) {
            Ok(token) => token,
            Err(error) => {
                window.set_probe_status(format!("上下文探测启动失败：{error}").into());
                window.set_probe_tone(status_tone(Tone::Bad));
                return;
            }
        };
        let selected: Vec<String> = window
            .get_probe_selected()
            .iter()
            .map(|id| id.to_string())
            .collect();
        window.set_probe_loading(true);
        window.set_probe_status("正在提交上下文探测…".into());
        window.set_probe_tone(status_tone(Tone::Neutral));
        let result = models::run_context_probe(&window, &*probe_client_run, &base, &token);
        window.set_probe_loading(false);
        if result.is_ok() {
            *probe_batch_run
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(selected);
        }
    });
    let weak_probe = window.as_weak();
    let probe_client_stop = Arc::clone(&probe_client);
    let probe_port_stop = Arc::clone(&current_port);
    let probe_token_stop = paths.control_token.clone();
    window.on_stop_context_probe(move || {
        let Some(window) = weak_probe.upgrade() else {
            return;
        };
        let base = format!(
            "http://127.0.0.1:{}",
            probe_port_stop.load(Ordering::SeqCst)
        );
        let token = match load_control_token(&probe_token_stop) {
            Ok(token) => token,
            Err(error) => {
                window.set_probe_status(format!("停止失败：{error}").into());
                window.set_probe_tone(status_tone(Tone::Bad));
                return;
            }
        };
        let _ = models::stop_context_probe(&window, &*probe_client_stop, &base, &token);
    });
    {
        let weak = window.as_weak();
        let gateway_status = Arc::clone(&gateway_status);
        let current_port = Arc::clone(&current_port);
        let saved_state = Arc::clone(&saved_state);
        let auth_state = Arc::clone(&auth_state);
        let auth_admin = Arc::clone(&auth_admin);
        let service_api = Arc::clone(&service_api);
        let service_state = Arc::clone(&service_state);
        let catalog_state = Arc::clone(&catalog_state);
        let probe_state = Arc::clone(&probe_state);
        let usage_log = paths.usage_log.clone();
        let login_reader = Arc::new(recent_issues::HttpLoginRoundsReader::new());
        std::thread::spawn(move || {
            let mut has_success = false;
            let mut last_failure: Option<String> = None;
            let mut last_faults: Option<overview::FaultHistory> = None;
            let mut last_service: Option<um_service::ServiceView> = None;
            let mut last_menu: Option<maintenance::Menu> = None;
            let mut last_issues: Option<recent_issues::RecentIssues> = None;
            let mut poller = overview::OverviewPoller::new();
            while !QUIT.load(Ordering::SeqCst) {
                let paused = PAUSED.load(Ordering::SeqCst);
                let refresh = OVERVIEW_REFRESH.load(Ordering::SeqCst);
                if !poller.should_read(paused, refresh) {
                    // The Slint auto-refresh timer owns the cadence; this
                    // reader only wakes for a new epoch or a resume.
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                }
                let base = format!("http://127.0.0.1:{}", current_port.load(Ordering::SeqCst));
                let gateway = gateway_status
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let supervision = gateway.card();
                let service = service_view(&gateway, &service_api, &control_token_path, &base);
                let saved = saved_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let (overview, account, auth) = read_live_overview(
                    &probe,
                    &auth_admin,
                    &control_token_path,
                    &base,
                    supervision,
                    &saved,
                );
                if PAUSED.load(Ordering::SeqCst) {
                    // The window was hidden or minimized while the probe was
                    // in flight; the resume pass reads again before applying.
                    continue;
                }
                let faults = overview::read_fault_history(&upstream_faults_path);
                if last_faults.as_ref() != Some(&faults) {
                    last_faults = Some(faults.clone());
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            overview::apply_fault_history(&window, &faults);
                        }
                    });
                }
                // The menu only depends on the gateway/service/auth facts; a
                // changed row set is applied once, so a refresh never resets an
                // armed confirmation the user is still looking at.
                let menu = maintenance::menu(&maintenance::MenuState::new(
                    &gateway.state,
                    Some(&service),
                    Some(&auth),
                ));
                if last_menu.as_ref() != Some(&menu) {
                    last_menu = Some(menu.clone());
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            maintenance::apply_menu(&window, &menu);
                        }
                    });
                }
                if last_service.as_ref() != Some(&service) {
                    last_service = Some(service.clone());
                    *service_state
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(service.clone());
                    let weak = weak.clone();
                    let applied = service.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            um_service::apply_service(&window, &applied);
                        }
                    });
                }
                // The recent-issues card reads the ledger, the login-round
                // history and the read faces above. A failed read keeps its
                // reason and is never folded into zero; a legal empty read
                // becomes 近期无问题.
                let catalog = catalog_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let probe_view = probe_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                let login = if matches!(&gateway.state, GatewayState::Running { .. }) {
                    match load_control_token(&control_token_path) {
                        Ok(token) => {
                            recent_issues::read_login_rounds(&*login_reader, &base, token.expose())
                        }
                        Err(error) => recent_issues::LoginRounds::Unavailable(error),
                    }
                } else {
                    recent_issues::LoginRounds::Unavailable(format!(
                        "本实例网关未运行：{}",
                        gateway.state.detail()
                    ))
                };
                let issues = recent_issues::read_recent_issues(
                    &usage_log,
                    chrono::Utc::now().timestamp_millis(),
                    &login,
                    Some(&service),
                    catalog.as_ref(),
                    probe_view.as_ref(),
                );
                if last_issues.as_ref() != Some(&issues) {
                    last_issues = Some(issues.clone());
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            recent_issues::apply_recent_issues(&window, &issues);
                        }
                    });
                }
                *auth_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(auth);
                if overview.failures.is_empty() {
                    last_failure = None;
                    has_success = true;
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            apply_overview(&window, &overview, &account);
                        }
                    });
                } else {
                    // Keep the last good cards; the status line carries the
                    // refresh failure and the chart keeps its previous points.
                    let line = overview::status_line(&overview);
                    if last_failure.as_deref() != Some(line.as_str()) {
                        last_failure = Some(line.clone());
                        let weak = weak.clone();
                        if has_success {
                            let _ = slint::invoke_from_event_loop(move || {
                                if let Some(window) = weak.upgrade() {
                                    window.set_status_line(line.into());
                                    window.set_status_tone(2);
                                }
                            });
                        } else {
                            let _ = slint::invoke_from_event_loop(move || {
                                if let Some(window) = weak.upgrade() {
                                    apply_overview(&window, &overview, &account);
                                }
                            });
                        }
                    }
                }
                std::thread::sleep(Duration::from_millis(100));
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
                        let previous: Vec<_> = opened.lines().iter().cloned().collect();
                        match opened.poll() {
                            Ok(outcome)
                                if refresh
                                    || outcome.reset
                                    || !outcome.appended.is_empty()
                                    || last_failure.is_some() =>
                            {
                                Some(LogUpdate {
                                previous,
                                entries: opened.lines().iter().cloned().collect(),
                                appended: outcome.appended,
                                reset: outcome.reset || last_failure.is_some(),
                                trimmed: outcome.trimmed,
                                failure: None,
                                })
                            }
                            Ok(_) => None,
                            Err(reason) if refresh || last_failure.as_ref() != Some(&reason) => {
                                Some(LogUpdate::failed(reason))
                            }
                            Err(_) => None,
                        }
                    } else {
                        match LogStream::open(&service_log) {
                            Ok(opened) => {
                                let entries = opened.lines().iter().cloned().collect();
                                stream = Some(opened);
                                Some(LogUpdate {
                                    previous: Vec::new(),
                                    entries,
                                    appended: Vec::new(),
                                    reset: true,
                                    trimmed: 0,
                                    failure: None,
                                })
                            }
                            Err(reason) if refresh || last_failure.as_ref() != Some(&reason) => {
                                Some(LogUpdate::failed(reason))
                            }
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

    // The settings page offers one recovery action for a down gateway; it goes
    // through the same supervisor restart the credential and port changes use.
    let weak_restart_gateway = window.as_weak();
    let restart_gateway_supervisor = Arc::clone(&gateway_supervisor);
    let restart_gateway_status = Arc::clone(&gateway_status);
    window.on_restart_gateway(move || {
        let Some(window) = weak_restart_gateway.upgrade() else {
            return;
        };
        if window.get_gateway_restarting()
            || window.get_port_busy()
            || QUIT_REQUESTED.load(Ordering::SeqCst)
        {
            return;
        }
        window.set_gateway_restarting(true);
        window.set_settings_status("正在重启本实例网关…".into());
        window.set_settings_tone(status_tone(Tone::Warn));
        let supervisor = Arc::clone(&restart_gateway_supervisor);
        let status = Arc::clone(&restart_gateway_status);
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let restarted = {
                let mut guard = supervisor
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                let restarted = guard.as_mut().map(|supervisor| {
                    supervisor.restart_with_cancel(StopReason::UserRequested, &|| {
                        QUIT_REQUESTED.load(Ordering::SeqCst)
                    })
                });
                if let Some(restarted) = &restarted {
                    *status.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) =
                        restarted.clone();
                }
                restarted
            };
            let (message, tone) = match restarted.as_ref().map(|status| &status.state) {
                Some(GatewayState::Running { .. }) => ("网关已重启".to_string(), Tone::Good),
                Some(state) => (format!("重启失败：{}", state.detail()), Tone::Bad),
                None => ("本实例网关未接入，无法重启".to_string(), Tone::Bad),
            };
            if matches!(
                restarted.as_ref().map(|status| &status.state),
                Some(GatewayState::Running { .. })
            ) {
                // The service card now has a gateway to read again.
                OVERVIEW_REFRESH.fetch_add(1, Ordering::SeqCst);
                USAGE_REFRESH.fetch_add(1, Ordering::SeqCst);
            }
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    window.set_gateway_restarting(false);
                    if !QUIT_REQUESTED.load(Ordering::SeqCst) {
                        window.set_settings_status(message.into());
                        window.set_settings_tone(status_tone(tone));
                    }
                }
            });
        });
    });

    // The UM service cell starts the service through the gateway control route
    // and re-reads the snapshot; a failure keeps the stopped state and reason.
    let weak_start_service = window.as_weak();
    let start_service_api = Arc::clone(&service_api);
    let start_service_port = Arc::clone(&current_port);
    let start_service_token = paths.control_token.clone();
    let start_service_status = Arc::clone(&gateway_status);
    let start_service_state = Arc::clone(&service_state);
    window.on_start_um_service(move || {
        let Some(window) = weak_start_service.upgrade() else {
            return;
        };
        if window.get_um_service_busy() || QUIT_REQUESTED.load(Ordering::SeqCst) {
            return;
        }
        let gateway = start_service_status
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        if !matches!(gateway.state, GatewayState::Running { .. }) {
            window.set_um_service_start_status("网关未运行，先启动网关".into());
            window.set_um_service_start_tone(status_tone(Tone::Warn));
            return;
        }
        let token = match load_control_token(&start_service_token) {
            Ok(token) => token,
            Err(error) => {
                window.set_um_service_start_status(format!("启动失败：{error}").into());
                window.set_um_service_start_tone(status_tone(Tone::Bad));
                return;
            }
        };
        let previous = start_service_state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
            .unwrap_or_else(|| um_service::ServiceView::failure("尚未读取 UM 服务状态"));
        window.set_um_service_busy(true);
        window.set_um_service_start_status("正在启动 UM 服务…".into());
        window.set_um_service_start_tone(status_tone(Tone::Warn));
        let api = Arc::clone(&start_service_api);
        let base = format!(
            "http://127.0.0.1:{}",
            start_service_port.load(Ordering::SeqCst)
        );
        let weak = window.as_weak();
        let state = Arc::clone(&start_service_state);
        std::thread::spawn(move || {
            let result = um_service::start_and_refresh(&*api, &base, token.expose(), &previous);
            let service = result.service.clone();
            let refreshed = result.refreshed;
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    window.set_um_service_busy(false);
                    if !QUIT_REQUESTED.load(Ordering::SeqCst) {
                        um_service::apply_start_result(&window, &result);
                        if refreshed {
                            *state
                                .lock()
                                .unwrap_or_else(|poisoned| poisoned.into_inner()) =
                                Some(service);
                        }
                    }
                }
            });
        });
    });

    // The maintenance menu re-checks the same availability its rows show, then
    // runs the action through the seam each card already owns. Verify, on-demand
    // login and restart delegate to their own cards so there is one data path;
    // start/stop use the existing supervisor and its drain semantics.
    let weak_maintenance = window.as_weak();
    let maintenance_supervisor = Arc::clone(&gateway_supervisor);
    let maintenance_gateway = Arc::clone(&gateway_status);
    let maintenance_service = Arc::clone(&service_state);
    let maintenance_auth = Arc::clone(&auth_state);
    window.on_maintenance_act(move |index| {
        let Some(window) = weak_maintenance.upgrade() else {
            return;
        };
        if window.get_maintenance_action_busy() || QUIT_REQUESTED.load(Ordering::SeqCst) {
            return;
        }
        let Some(action) = maintenance::Action::from_index(index) else {
            return;
        };
        let gateway = maintenance_gateway
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        let service = maintenance_service
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        let auth = maintenance_auth
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        let state = maintenance::MenuState::new(&gateway.state, service.as_ref(), auth.as_ref());
        let Some(row) = maintenance::menu(&state)
            .items
            .into_iter()
            .find(|row| row.action == action)
        else {
            return;
        };
        if !row.enabled {
            window.set_maintenance_status(row.reason.into());
            window.set_maintenance_status_tone(status_tone(Tone::Warn));
            return;
        }
        match action {
            maintenance::Action::Verify => {
                let (status, tone) = maintenance::delegated_status(action);
                window.set_maintenance_status(status.into());
                window.set_maintenance_status_tone(status_tone(tone));
                window.invoke_verify_login();
            }
            maintenance::Action::Login => {
                let (status, tone) = maintenance::delegated_status(action);
                window.set_maintenance_status(status.into());
                window.set_maintenance_status_tone(status_tone(tone));
                window.invoke_continue_login();
            }
            maintenance::Action::Restart => {
                let (status, tone) = maintenance::delegated_status(action);
                window.set_maintenance_status(status.into());
                window.set_maintenance_status_tone(status_tone(tone));
                window.invoke_restart_gateway();
            }
            maintenance::Action::Start | maintenance::Action::Stop => {
                window.set_maintenance_action_busy(true);
                window.set_maintenance_status(format!("正在{}本实例网关…", row.label).into());
                window.set_maintenance_status_tone(status_tone(Tone::Warn));
                let supervisor = Arc::clone(&maintenance_supervisor);
                let status_slot = Arc::clone(&maintenance_gateway);
                let weak = window.as_weak();
                std::thread::spawn(move || {
                    let (result, running) = {
                        let mut guard = supervisor
                            .lock()
                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                        match guard.as_mut() {
                            Some(supervisor) => {
                                let status = match action {
                                    maintenance::Action::Start => supervisor.start_or_attach(),
                                    maintenance::Action::Stop => {
                                        supervisor.stop(StopReason::UserRequested)
                                    }
                                    _ => unreachable!(),
                                };
                                *status_slot
                                    .lock()
                                    .unwrap_or_else(|poisoned| poisoned.into_inner()) =
                                    status.clone();
                                let outcome = match (&status.state, action) {
                                    (GatewayState::Running { .. }, maintenance::Action::Start) => {
                                        Ok(format!("本实例网关运行于 {}", status.base_url))
                                    }
                                    (GatewayState::Stopped, maintenance::Action::Stop) => {
                                        Ok("本实例网关已停止".to_string())
                                    }
                                    _ => Err(status.state.detail()),
                                };
                                (
                                    maintenance::action_result(action, outcome),
                                    matches!(status.state, GatewayState::Running { .. }),
                                )
                            }
                            None => (
                                maintenance::action_result(
                                    action,
                                    Err("本实例网关未接入，无法执行维护动作".to_string()),
                                ),
                                false,
                            ),
                        }
                    };
                    if running {
                        OVERVIEW_REFRESH.fetch_add(1, Ordering::SeqCst);
                        USAGE_REFRESH.fetch_add(1, Ordering::SeqCst);
                    }
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            window.set_maintenance_action_busy(false);
                            if !QUIT_REQUESTED.load(Ordering::SeqCst) {
                                maintenance::apply_result(&window, &result);
                            }
                        }
                    });
                });
            }
        }
    });

    let request_nav = Arc::new(Mutex::new(usage::RequestNav::default()));
    let ledger = paths.usage_log.clone();
    let request_ledger = ledger.clone();
    let request_page_nav = Arc::clone(&request_nav);
    let weak_request = window.as_weak();
    window.on_query_requests(move |direction| {
        if let Some(window) = weak_request.upgrade() {
            let mode = if direction == 0 {
                RequestLoad::Query
            } else {
                RequestLoad::Page
            };
            load_requests(&window, &request_ledger, &request_page_nav, direction, mode);
        }
    });
    let request_earlier_ledger = ledger.clone();
    let request_earlier_nav = Arc::clone(&request_nav);
    let weak_request = window.as_weak();
    window.on_load_earlier_requests(move || {
        if let Some(window) = weak_request.upgrade() {
            load_requests(
                &window,
                &request_earlier_ledger,
                &request_earlier_nav,
                0,
                RequestLoad::Earlier,
            );
        }
    });
    let request_refresh_ledger = ledger.clone();
    let request_refresh_nav = Arc::clone(&request_nav);
    let weak_request = window.as_weak();
    window.on_refresh_requests(move || {
        if let Some(window) = weak_request.upgrade() {
            load_requests(
                &window,
                &request_refresh_ledger,
                &request_refresh_nav,
                0,
                RequestLoad::Refresh,
            );
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
            // Bumping the refresh epoch makes the reader re-read even when the
            // selected range did not change (Ctrl+R).
            USAGE_REFRESH.fetch_add(1, Ordering::SeqCst);
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
            let mut last_period = None;
            let mut last_view: Option<UsageView> = None;
            while !QUIT.load(Ordering::SeqCst) {
                if !PAUSED.load(Ordering::SeqCst) {
                    let period = usage_range.load(Ordering::SeqCst);
                    let refresh = USAGE_REFRESH.load(Ordering::SeqCst);
                    let metadata =
                        std::fs::metadata(&ledger).map(|meta| (meta.len(), meta.modified().ok()));
                    let day = chrono::Utc::now()
                        .with_timezone(
                            &chrono::FixedOffset::east_opt(8 * 3600).expect("Beijing offset"),
                        )
                        .date_naive();
                    let stamp = (
                        period,
                        refresh,
                        metadata.as_ref().copied().map_err(|error| error.kind()),
                        day,
                    );
                    if last.as_ref() != Some(&stamp) {
                        last = Some(stamp);
                        if last_period != Some(period) {
                            // A new range is a new query, not a refresh: an
                            // unreadable ledger must not show the old range.
                            last_period = Some(period);
                            last_view = None;
                        }
                        let view = usage::read_usage(
                            &ledger,
                            Range::from_index(period),
                            chrono::Utc::now().timestamp_millis(),
                        );
                        let view = match &last_view {
                            Some(previous) => usage::keep_previous_on_failure(previous, view),
                            None => view,
                        };
                        last_view = Some(view.clone());
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
    window.on_navigate(move |page| {
        if let Some(window) = weak_navigate.upgrade() {
            window.set_page(page);
            if page == 3 {
                REFRESH_LOGS.store(true, Ordering::SeqCst);
            } else if page == 1 {
                // Re-entering the request page re-reads the ledger; the
                // generation guard in `load_requests` drops stale results.
                window.invoke_query_requests(0);
            }
        }
    });

    let weak_refresh = window.as_weak();
    window.on_refresh_logs(move || {
        if let Some(window) = weak_refresh.upgrade() {
            // Acknowledge the click before the reader's next 160 ms poll lands;
            // apply_logs restores the real count and flips the button to 已刷新.
            window.set_log_refreshed(false);
            window.set_log_refresh_pending(true);
            window.set_log_status("正在刷新…".into());
            window.set_log_tone(3);
        }
        REFRESH_LOGS.store(true, Ordering::SeqCst);
    });

    // Manual overview refresh forces a read even when the health response is
    // unchanged, and refreshes the usage chart with the same action.
    window.on_refresh_overview(|| {
        OVERVIEW_REFRESH.fetch_add(1, Ordering::SeqCst);
        USAGE_REFRESH.fetch_add(1, Ordering::SeqCst);
    });

    let weak_copy = window.as_weak();
    window.on_copy_text(move |text| {
        let Some(window) = weak_copy.upgrade() else {
            return;
        };
        match clipboard::write_text(text.as_str()) {
            Ok(()) => {
                window.set_copy_ok(true);
                window.set_copy_error("".into());
            }
            Err(reason) => {
                window.set_copy_ok(false);
                window.set_copy_error(reason.into());
            }
        }
    });

    // Ctrl+R dispatches to the page-specific refresh callback in the UI.
    let weak_refresh_page = window.as_weak();
    window.on_refresh_page(move || {
        if let Some(window) = weak_refresh_page.upgrade() {
            window.invoke_refresh_current_page();
        }
    });

    // Settings refresh re-checks the gateway and account status; a failure keeps
    // the previous settings data and only reports the refresh failure.
    let weak_settings = window.as_weak();
    let settings_auth_admin = Arc::clone(&auth_admin);
    let settings_gateway_status = Arc::clone(&gateway_status);
    let settings_saved = Arc::clone(&saved_state);
    let settings_auth_state = Arc::clone(&auth_state);
    let settings_token_path = paths.control_token.clone();
    let settings_port = Arc::clone(&current_port);
    window.on_refresh_settings(move || {
        let Some(window) = weak_settings.upgrade() else {
            return;
        };
        window.set_settings_status("正在重新检查网关与账号状态…".into());
        window.set_settings_tone(status_tone(Tone::Warn));
        let probe = HttpProbe::new();
        let auth_admin = Arc::clone(&settings_auth_admin);
        let gateway_status = Arc::clone(&settings_gateway_status);
        let saved_state = Arc::clone(&settings_saved);
        let auth_state = Arc::clone(&settings_auth_state);
        let token_path = settings_token_path.clone();
        let port = settings_port.load(Ordering::SeqCst);
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let base = format!("http://127.0.0.1:{port}");
            let supervision = gateway_status
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .card();
            let saved = saved_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .clone();
            let (overview, account, auth) =
                read_live_overview(&probe, &auth_admin, &token_path, &base, supervision, &saved);
            let mut failures = overview.failures.clone();
            if let AuthRead::Unavailable(reason) = &auth {
                failures.push(format!("UM 认证 {reason}"));
            }
            let success = failures.is_empty();
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    if success {
                        *auth_state
                            .lock()
                            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(auth.clone());
                        apply_overview(&window, &overview, &account);
                        window.set_settings_status("已重新检查网关与账号状态".into());
                        window.set_settings_tone(status_tone(Tone::Good));
                        window.set_settings_updated_at(clock_now().into());
                    } else {
                        window.set_settings_status(
                            format!("刷新失败：{}", failures.join("；")).into(),
                        );
                        window.set_settings_tone(status_tone(Tone::Bad));
                    }
                }
            });
        });
    });

    let history_client: Arc<dyn HistoryClient> = Arc::new(HttpHistoryClient::new());
    let history_navigation = Arc::new(Mutex::new(HistoryNav::default()));
    let history_token = paths.control_token.clone();

    let weak_mode = window.as_weak();
    let mode_client = Arc::clone(&history_client);
    let mode_navigation = Arc::clone(&history_navigation);
    let mode_token = history_token.clone();
    let mode_port = Arc::clone(&current_port);
    window.on_select_log_mode(move |index| {
        let Some(window) = weak_mode.upgrade() else {
            return;
        };
        window.set_log_mode(index);
        if index == 0 {
            window.set_log_period(0);
            window.set_log_history_note("".into());
            window.set_log_follow(true);
            FILTER_EPOCH.fetch_add(1, Ordering::SeqCst);
            REFRESH_LOGS.store(true, Ordering::SeqCst);
            window.set_log_status("正在读取实时日志…".into());
            window.set_log_tone(3);
            return;
        }
        window.set_log_period(
            HistoryRange::from_index(window.get_log_range())
                .log_range()
                .index(),
        );
        let base = format!("http://127.0.0.1:{}", mode_port.load(Ordering::SeqCst));
        load_log_history(
            &window,
            &mode_client,
            &base,
            &mode_token,
            &mode_navigation,
            false,
        );
    });

    let weak_range = window.as_weak();
    let range_client = Arc::clone(&history_client);
    let range_navigation = Arc::clone(&history_navigation);
    let range_token = history_token.clone();
    let range_port = Arc::clone(&current_port);
    window.on_select_log_range(move |index| {
        let Some(window) = weak_range.upgrade() else {
            return;
        };
        window.set_log_range(index);
        window.set_log_period(HistoryRange::from_index(index).log_range().index());
        let base = format!("http://127.0.0.1:{}", range_port.load(Ordering::SeqCst));
        load_log_history(
            &window,
            &range_client,
            &base,
            &range_token,
            &range_navigation,
            false,
        );
    });

    let weak_level = window.as_weak();
    let level_client = Arc::clone(&history_client);
    let level_navigation = Arc::clone(&history_navigation);
    let level_token = history_token.clone();
    let level_port = Arc::clone(&current_port);
    window.on_toggle_log_level(move |_| {
        let Some(window) = weak_level.upgrade() else {
            return;
        };
        sync_log_level(&window);
        if window.get_log_mode() != 1 {
            return;
        }
        let base = format!("http://127.0.0.1:{}", level_port.load(Ordering::SeqCst));
        load_log_history(
            &window,
            &level_client,
            &base,
            &level_token,
            &level_navigation,
            false,
        );
    });

    let weak_sync = window.as_weak();
    window.on_sync_log_level(move || {
        if let Some(window) = weak_sync.upgrade() {
            sync_log_level(&window);
        }
    });

    let weak_query = window.as_weak();
    let query_client = Arc::clone(&history_client);
    let query_navigation = Arc::clone(&history_navigation);
    let query_token = history_token.clone();
    let query_port = Arc::clone(&current_port);
    window.on_query_log_history(move || {
        let Some(window) = weak_query.upgrade() else {
            return;
        };
        if window.get_refreshing() {
            window.set_log_refresh_pending(true);
        }
        let base = format!("http://127.0.0.1:{}", query_port.load(Ordering::SeqCst));
        load_log_history(
            &window,
            &query_client,
            &base,
            &query_token,
            &query_navigation,
            false,
        );
    });

    let weak_earlier = window.as_weak();
    let earlier_client = Arc::clone(&history_client);
    let earlier_navigation = Arc::clone(&history_navigation);
    let earlier_token = history_token.clone();
    let earlier_port = Arc::clone(&current_port);
    window.on_load_earlier_logs(move || {
        let Some(window) = weak_earlier.upgrade() else {
            return;
        };
        let base = format!("http://127.0.0.1:{}", earlier_port.load(Ordering::SeqCst));
        load_log_history(
            &window,
            &earlier_client,
            &base,
            &earlier_token,
            &earlier_navigation,
            true,
        );
    });

    let weak_skip = window.as_weak();
    let skip_config = Arc::clone(&current_config);
    let skip_path = config_path.as_ref().ok().cloned();
    window.on_skip_login(move || {
        let Some(window) = weak_skip.upgrade() else {
            return;
        };
        window.set_show_intro(false);
        let Some(path) = &skip_path else { return };
        let mut config = skip_config.lock().expect("desktop config poisoned");
        config.intro_dismissed = true;
        if let ConfigLoadOutcome::Invalid(reason) = config::load_from(path) {
            window.set_settings_status(
                format!("无法保存首次登录偏好：配置文件无效：{reason}").into(),
            );
            window.set_settings_tone(status_tone(Tone::Bad));
            return;
        }
        if let Err(error) = config::save_to(path, &config) {
            window.set_settings_status(format!("无法保存首次登录偏好：{error}").into());
            window.set_settings_tone(status_tone(Tone::Bad));
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
                    let account_error = error == FormError::AccountRequired;
                    window.set_account_input_invalid(account_error);
                    window.set_account_input_error(if account_error {
                        error.message().into()
                    } else {
                        "".into()
                    });
                    window.set_password_input_invalid(!account_error);
                    window.set_password_input_error(if account_error {
                        "".into()
                    } else {
                        error.message().into()
                    });
                    window.set_credential_status("".into());
                    window.set_credential_tone(3);
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
        window.set_account_input_invalid(false);
        window.set_account_input_error("".into());
        window.set_password_input_invalid(false);
        window.set_password_input_error("".into());
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
    let verify_continue = Arc::clone(&verify_state);
    let current_port_continue = Arc::clone(&current_port);
    window.on_continue_login(move || {
        let Some(window) = weak_continue.upgrade() else {
            return;
        };
        if LOGIN_IN_FLIGHT.swap(true, Ordering::SeqCst) {
            return;
        }
        window.set_account_busy(true);
        window.set_login_status("正在核验当前凭据…".into());
        window.set_login_tone(status_tone(Tone::Warn));
        let base = format!(
            "http://127.0.0.1:{}",
            current_port_continue.load(Ordering::SeqCst)
        );
        let token_path = token_path_continue.clone();
        let auth_admin = Arc::clone(&auth_admin_continue);
        let saved_state = Arc::clone(&saved_continue);
        let auth_state = Arc::clone(&auth_continue);
        let verify_state = Arc::clone(&verify_continue);
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let current_status = auth_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .as_ref()
                .and_then(|auth| match auth {
                    AuthRead::Status(status) => Some(status.clone()),
                    AuthRead::Unavailable(_) => None,
                });
            let outcome = match load_control_token(&token_path) {
                Ok(token) => {
                    account::on_demand_login(&*auth_admin, &base, &token, current_status.as_ref())
                }
                Err(error) => OnDemandLogin::VerifyUnavailable(error),
            };
            LOGIN_IN_FLIGHT.store(false, Ordering::SeqCst);
            let previous_auth = auth_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .clone();
            let auth = match &outcome {
                OnDemandLogin::LoginStarted { status, .. } => {
                    Some(AuthRead::Status(status.clone()))
                }
                OnDemandLogin::AlreadyValid {
                    status: Some(status),
                    ..
                } => Some(AuthRead::Status(status.clone())),
                _ => previous_auth,
            };
            if let Some(auth) = &auth {
                *auth_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(auth.clone());
            }
            // A failed verify keeps its own reason in the card; a manual
            // takeover that never ran a verify leaves the card untouched.
            let stored = match &outcome {
                OnDemandLogin::VerifyUnavailable(error) => {
                    Some(VerifyOutcome::Unavailable(error.clone()))
                }
                _ => outcome.report().cloned().map(VerifyOutcome::Report),
            };
            if let Some(stored) = stored {
                *verify_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(stored);
            }
            let verify_view = {
                let stored = verify_state
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                account::verify_view(stored.as_ref())
            };
            let saved = saved_state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .clone();
            let view = account_view(&saved, auth.as_ref());
            let (message, tone) = account::on_demand_message(&outcome);
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    window.set_account_busy(false);
                    apply_account(&window, &view);
                    account::apply_verify(&window, &verify_view);
                    window.set_login_status(message.into());
                    window.set_login_tone(status_tone(tone));
                }
            });
        });
    });

    let weak_verify = window.as_weak();
    let verify_admin = Arc::clone(&auth_admin);
    let verify_token_path = paths.control_token.clone();
    let verify_store = Arc::clone(&verify_state);
    let verify_port = Arc::clone(&current_port);
    window.on_verify_login(move || {
        let Some(window) = weak_verify.upgrade() else {
            return;
        };
        if VERIFY_IN_FLIGHT.swap(true, Ordering::SeqCst) {
            return;
        }
        window.set_verify_busy(true);
        let base = format!("http://127.0.0.1:{}", verify_port.load(Ordering::SeqCst));
        let token_path = verify_token_path.clone();
        let auth_admin = Arc::clone(&verify_admin);
        let verify_store = Arc::clone(&verify_store);
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let outcome = match load_control_token(&token_path) {
                Ok(token) => match auth_admin.verify(&base, &token) {
                    Ok(report) => VerifyOutcome::Report(report),
                    Err(error) => VerifyOutcome::Unavailable(error),
                },
                Err(error) => VerifyOutcome::Unavailable(error),
            };
            VERIFY_IN_FLIGHT.store(false, Ordering::SeqCst);
            *verify_store
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(outcome.clone());
            let view = account::verify_view(Some(&outcome));
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    window.set_verify_busy(false);
                    account::apply_verify(&window, &view);
                }
            });
        });
    });

    let theme_path = config_path.as_ref().ok().cloned();
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
        if window.get_gateway_restarting() || window.get_port_busy() {
            return;
        }
        let port = match config::validate_port(&text) {
            Ok(port) => port,
            Err(error) => {
                window.set_port_input_invalid(true);
                window.set_port_input_error(format!("端口无效：{error}").into());
                window.set_settings_status("".into());
                return;
            }
        };
        window.set_port_input_invalid(false);
        window.set_port_input_error("".into());
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
                    let next = DesktopConfig {
                        model_api_port: port,
                        ..config.clone()
                    };
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
                    format!("未切换：{reason}；已恢复 127.0.0.1:{from}"),
                    Tone::Bad,
                ),
                PortSwitch::RestoreFailed {
                    from,
                    to,
                    reason,
                    restore_error,
                } => (
                    format!(
                        "切换到 {to} 失败：{reason}；恢复原端口 127.0.0.1:{from} 也失败：{restore_error}"
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

    let theme_config = Arc::clone(&current_config);
    let theme_path_for_handler = theme_path.clone();
    let weak_theme = window.as_weak();
    window.on_theme_mode_changed(move |index| {
        let Some(window) = weak_theme.upgrade() else {
            return;
        };
        let theme = theme_mode_from_index(index);
        window.set_theme_mode(theme_mode_index(theme));
        let mut config = theme_config
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match config::apply_theme(theme_path_for_handler.as_deref(), &mut config, theme) {
            Ok(()) => {
                window.set_settings_status(format!("外观：{}", theme_label(theme)).into());
                window.set_settings_tone(status_tone(Tone::Good));
            }
            Err(message) => {
                window.set_settings_status(message.into());
                window.set_settings_tone(status_tone(Tone::Warn));
            }
        }
    });

    // Sidebar, motion and transparency are user settings with their own visible
    // controls; each choice reaches the window first and then the saved config.
    let sidebar_config = Arc::clone(&current_config);
    let sidebar_path = theme_path.clone();
    let weak_sidebar = window.as_weak();
    window.on_sidebar_mode_changed(move |index| {
        let Some(window) = weak_sidebar.upgrade() else {
            return;
        };
        let mode = SidebarMode::from_index(index);
        window.set_sidebar_mode(mode.index());
        let mut config = sidebar_config
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match config::apply_sidebar(sidebar_path.as_deref(), &mut config, mode) {
            Ok(()) => {
                window.set_settings_status(format!("外观：{}", sidebar_label(mode)).into());
                window.set_settings_tone(status_tone(Tone::Good));
            }
            Err(message) => {
                window.set_settings_status(message.into());
                window.set_settings_tone(status_tone(Tone::Warn));
            }
        }
    });

    let motion_config = Arc::clone(&current_config);
    let motion_path = theme_path.clone();
    let weak_motion = window.as_weak();
    window.on_motion_mode_changed(move |index| {
        let Some(window) = weak_motion.upgrade() else {
            return;
        };
        let preference = MotionPreference::from_index(index);
        window.set_motion_mode(preference.index());
        let mut config = motion_config
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match config::apply_motion(motion_path.as_deref(), &mut config, preference) {
            Ok(()) => {
                window.set_settings_status(format!("外观：{}", motion_label(preference)).into());
                window.set_settings_tone(status_tone(Tone::Good));
            }
            Err(message) => {
                window.set_settings_status(message.into());
                window.set_settings_tone(status_tone(Tone::Warn));
            }
        }
    });

    let transparency_config = Arc::clone(&current_config);
    let transparency_path = theme_path.clone();
    let weak_transparency = window.as_weak();
    window.on_transparency_changed(move |reduce| {
        let Some(window) = weak_transparency.upgrade() else {
            return;
        };
        window.set_reduce_transparency(reduce);
        let mut config = transparency_config
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match config::apply_reduce_transparency(transparency_path.as_deref(), &mut config, reduce) {
            Ok(()) => {
                let status = if reduce {
                    "外观：降低透明度已保存（当前界面无透明度效果）"
                } else {
                    "外观：透明度已恢复"
                };
                window.set_settings_status(status.into());
                window.set_settings_tone(status_tone(Tone::Good));
            }
            Err(message) => {
                window.set_settings_status(message.into());
                window.set_settings_tone(status_tone(Tone::Warn));
            }
        }
    });

    // Page filters and the auto-refresh interval are user settings; the UI
    // reports every change through one callback so a restart opens the same view.
    let settings_path = theme_path.clone();
    let settings_config = Arc::clone(&current_config);
    let weak_settings = window.as_weak();
    window.on_settings_changed(move || {
        if let Some(window) = weak_settings.upgrade() {
            persist_window_settings(
                &window,
                settings_path.as_deref(),
                &settings_config,
                "设置已更新",
            );
        }
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
        // The single-instance shortcut restores the Win32 window directly, without
        // the tray callback. Detect that the window is visible again while the log
        // reader is paused, and resume with the same backfill the tray uses.
        let minimized = window.window().is_minimized();
        if minimized && !PAUSED.load(Ordering::SeqCst) {
            PAUSED.store(true, Ordering::SeqCst);
            window.set_refresh_paused(true);
        }
        if PAUSED.load(Ordering::SeqCst) && !minimized && window.window().is_visible() {
            show_now(&window);
        }
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
    // The window class icon is not the executable icon; apply the local console
    // cover once the event loop has created the native window.
    slint::Timer::single_shot(Duration::from_millis(250), window_icon::apply);
    // The caption belongs to DWM, not Slint; follow the one derived value after
    // the native window exists and keep following it when the system theme flips.
    window.on_theme_dark_changed(|dark| {
        if let Err(error) = dwm::apply_dark_title_bar(instance::WINDOW_TITLE, dark) {
            eprintln!("um-desktop: {error}");
        }
    });
    // The native window exists only once the event loop runs; the first DWM
    // apply waits for it instead of reporting a missing window at startup.
    let initial_dark = window.get_theme_dark();
    slint::Timer::single_shot(Duration::from_millis(250), move || {
        if let Err(error) = dwm::apply_dark_title_bar(instance::WINDOW_TITLE, initial_dark) {
            eprintln!("um-desktop: {error}");
        }
    });
    slint::run_event_loop_until_quit()?;
    Ok(())
}

#[cfg(not(windows))]
fn main() {
    eprintln!("um-desktop: 桌面版只在 Windows 上提供");
}
