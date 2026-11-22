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
    FormError, HttpAuthAdmin, LoadOutcome,
};
use um_desktop::clipboard;
use um_desktop::config::{self, DesktopConfig, LoadOutcome as ConfigLoadOutcome, ThemeMode};
use um_desktop::dwm;
use um_desktop::gateway::{
    load_control_token, resolve_gateway_binary, GatewayState, GatewayStatus, GatewaySupervisor,
    HttpGateway, InstancePaths, PortSwitch, PortSwitchProgress, StopReason, SystemLauncher,
    ThreadSleeper,
};
use um_desktop::instance::{self, InstanceError};
use um_desktop::logstream::{
    format_log_time, plan_log_page, read_history, LevelFilter, LogStream, StreamLine,
};
use um_desktop::overview::{self, HttpProbe, Overview, Tone};
use um_desktop::shell::{ShellAction, ShellEvent, ShellState};
use um_desktop::ui::*;
use um_desktop::usage::{self, Coverage, Range, UsageView};

static PAUSED: AtomicBool = AtomicBool::new(false);
static QUIT_REQUESTED: AtomicBool = AtomicBool::new(false);
static QUIT: AtomicBool = AtomicBool::new(false);
static REFRESH_LOGS: AtomicBool = AtomicBool::new(false);
static OVERVIEW_REFRESH: AtomicU64 = AtomicU64::new(0);
static USAGE_REFRESH: AtomicU64 = AtomicU64::new(0);
static LOGIN_IN_FLIGHT: AtomicBool = AtomicBool::new(false);
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
        text: line.entry.text.clone().into(),
        detail: format!("{}  {level}\n\n{}", time.precise, line.detail).into(),
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

fn apply_logs(window: &MainWindow, update: &LogUpdate) {
    if window.get_log_period() != 0 {
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

    let level = LevelFilter::from_index(window.get_log_filter_level());
    let keyword = window.get_log_filter_keyword().trim().to_lowercase();
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
        level,
        &keyword,
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
    } else if visible == 0 {
        format!("最新 {total} 条中没有匹配的日志")
    } else if visible == total {
        format!("实时 · 最近 {total} 条")
    } else {
        format!("实时 · 显示 {visible} / {total} 条")
    };
    window.set_log_total(total as i32);
    window.set_log_status(status.into());
    // A live stream reads as healthy; an empty or filtered list stays neutral.
    window.set_log_tone(if total == 0 { 3 } else { status_tone(Tone::Good) });
    window.set_log_refresh_pending(false);
    if acknowledge_refresh {
        window.set_log_refreshed(true);
    }
    window.set_log_version(window.get_log_version().wrapping_add(1));
    window.set_log_updated_at(clock_now().into());
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
        let days = match period {
            2 => 6,
            3 => 29,
            _ => 0,
        };
        let since = now
            .date_naive()
            .checked_sub_days(chrono::Days::new(days))
            .and_then(|date| date.and_hms_opt(0, 0, 0))
            .and_then(|time| time.and_local_timezone(chrono::Local).earliest())
            .map_or_else(
                || (now - chrono::Duration::days(days as i64 + 1)).timestamp_millis(),
                |time| time.timestamp_millis(),
            );
        let result = read_history(&path, before, since, level, &keyword);
        let _ = slint::invoke_from_event_loop(move || {
            let Some(window) = weak.upgrade() else { return };
            let mut nav = navigation.lock().expect("log history navigation poisoned");
            if window.get_log_period() != period || nav.generation != generation {
                return;
            }
            let model = window.get_logs();
            let Some(model) = model.as_any().downcast_ref::<VecModel<LogRow>>() else {
                return;
            };
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
                    window.set_log_tone(3);
                    window.set_log_updated_at(clock_now().into());
                }
                Err(error) => {
                    nav.older = None;
                    window.set_older_logs(false);
                    window.set_newer_logs(nav.pages.len() > 1);
                    window.set_log_status(format!("刷新失败：历史日志读取失败：{error}").into());
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
}

fn load_requests(
    window: &MainWindow,
    ledger: &Path,
    nav: &Arc<Mutex<usage::RequestNav>>,
    direction: i32,
    mode: RequestLoad,
) {
    let refresh = mode == RequestLoad::Refresh;
    let range = Range::from_index(window.get_request_period());
    let current_time = chrono::Utc::now().timestamp_millis();
    let (cursor, now, generation) = {
        let mut nav = nav.lock().expect("request navigation poisoned");
        let cursor = match mode {
            RequestLoad::Refresh => nav.refresh(current_time, range),
            RequestLoad::Query => nav.query(current_time),
            RequestLoad::Page => nav.page(direction),
        };
        let Some(cursor) = cursor else {
            return;
        };
        (cursor, nav.now, nav.generation)
    };
    let result = window.get_request_result();
    let keyword = window.get_request_keyword().to_string();
    let scroll = window.get_request_scroll_y();
    let weak = window.as_weak();
    let ledger = ledger.to_path_buf();
    let nav = Arc::clone(nav);
    if !refresh {
        window.set_selected_request("".into());
        window.set_request_scroll_y(0.0);
        window.set_request_status("正在读取请求记录…".into());
        window.set_request_tone(3);
        window.set_older_requests(false);
        window.set_newer_requests(false);
    }
    std::thread::spawn(move || {
        let page = usage::read_requests(&ledger, range, result, &keyword, cursor.as_deref(), now);
        let _ = slint::invoke_from_event_loop(move || {
            let Some(window) = weak.upgrade() else { return; };
            let mut nav = nav.lock().expect("request navigation poisoned");
            if nav.generation != generation { return; }
            let page = if refresh {
                let previous = usage::RequestPage {
                    rows: nav.rows.clone(),
                    earlier: nav.earlier.clone(),
                    coverage: Coverage::Complete,
                    message: String::new(),
                    summary: nav.summary.clone(),
                    model_speeds: nav.model_speeds.clone(),
                    chart: nav.chart.clone(),
                };
                usage::keep_previous_page_on_failure(&previous, page)
            } else {
                page
            };
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
                summary: format!(
                    "耗时 {} · ID {}",
                    row.duration,
                    row.request_id.as_deref().unwrap_or("—")
                )
                .into(),
                first_token: row.first_token.clone().into(),
                detail: "".into(),
            }).collect::<Vec<_>>();
            let count = rows.len();
            window.set_requests(ModelRc::from(rows.as_slice()));
            apply_request_analytics(&window, &page);
            if count == 0 || !page.message.is_empty() {
                window.set_request_status(page.message.into());
            } else {
                window.set_request_status(format!("北京时间 · 本页 {count} 条").into());
            }
            window.set_request_tone(coverage_tone(page.coverage));
            window.set_older_requests(page.earlier.is_some());
            window.set_newer_requests(nav.pages.len() > 1);
            if page.coverage != Coverage::Unavailable {
                window.set_request_updated_at(clock_now().into());
            }
            nav.summary = page.summary.clone();
            nav.model_speeds = page.model_speeds.clone();
            nav.chart = page.chart.clone();
            nav.rows = page.rows;
            nav.earlier = page.earlier;
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
    let upstream_faults_path = paths.upstream_faults.clone();
    {
        let weak = window.as_weak();
        let gateway_status = Arc::clone(&gateway_status);
        let current_port = Arc::clone(&current_port);
        let saved_state = Arc::clone(&saved_state);
        let auth_state = Arc::clone(&auth_state);
        let auth_admin = Arc::clone(&auth_admin);
        std::thread::spawn(move || {
            let mut has_success = false;
            let mut last_failure: Option<String> = None;
            let mut last_faults: Option<overview::FaultHistory> = None;
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
                let supervision = gateway_status
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .card();
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
                AuthRead::Unavailable(reason) => (format!("无法打开可见登录：{reason}"), Tone::Bad),
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
