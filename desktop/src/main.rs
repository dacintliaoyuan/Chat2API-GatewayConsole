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

use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::TimeZone;
use slint::{CloseRequestResponse, ComponentHandle, Model, ModelRc, Timer, TimerMode, VecModel};
use um_desktop::account::{
    self, account_view, AccountForm, AccountView, AuthAdmin, AuthRead, AuthState, HttpAuthAdmin,
    LoadOutcome,
};
use um_desktop::config::{self, DesktopConfig, LoadOutcome as ConfigLoadOutcome};
use um_desktop::gateway::{
    load_control_token, quit_app, resolve_gateway_binary, GatewayState, GatewayStatus,
    GatewaySupervisor, HttpGateway, InstancePaths, PortSwitch, StopReason, SystemLauncher,
    ThreadSleeper,
};
use um_desktop::instance::{self, InstanceError};
use um_desktop::logs::LogEntry;
use um_desktop::logstream::{filter_entries, LevelFilter, LogStream};
use um_desktop::overview::{self, HttpProbe, Overview, Tone};
use um_desktop::shell::{ShellAction, ShellEvent, ShellState};

slint::include_modules!();

static PAUSED: AtomicBool = AtomicBool::new(false);
static QUIT: AtomicBool = AtomicBool::new(false);
static REFRESH_LOGS: AtomicBool = AtomicBool::new(false);
static LOGIN_IN_FLIGHT: AtomicBool = AtomicBool::new(false);
static FILTER_EPOCH: AtomicU64 = AtomicU64::new(0);
static APPLIED_FILTER_EPOCH: AtomicU64 = AtomicU64::new(0);

/// One log-page update produced by the reader thread.
#[derive(Debug, Clone, PartialEq, Eq)]
struct LogUpdate {
    entries: Vec<LogEntry>,
    appended: Vec<LogEntry>,
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

fn tone_color(tone: Tone) -> slint::Color {
    match tone {
        Tone::Good => slint::Color::from_rgb_u8(23, 106, 60),
        Tone::Warn => slint::Color::from_rgb_u8(139, 76, 12),
        Tone::Bad => slint::Color::from_rgb_u8(180, 35, 24),
    }
}

fn clock(at: Option<i64>) -> String {
    at.and_then(|at| chrono::Local.timestamp_millis_opt(at).single())
        .map(|time| time.format("%H:%M:%S").to_string())
        .unwrap_or_else(|| "—".to_string())
}

fn muted_color() -> slint::Color {
    slint::Color::from_rgb_u8(81, 93, 112)
}

fn apply_account(window: &MainWindow, view: &AccountView) {
    window.set_account_saved(view.saved_account.clone().into());
    window.set_account_saved_detail(view.saved_detail.clone().into());
    window.set_account_state(view.state_value.clone().into());
    window.set_account_state_detail(view.state_detail.clone().into());
    window.set_account_state_tone(tone_color(view.state_tone));
    window.set_account_can_clear(view.can_clear);
    window.set_account_can_continue(view.can_continue_login);
    window.set_account_label(view.overview_label.clone().into());
    window.set_account_detail(view.overview_detail.clone().into());
    window.set_account_tone(tone_color(view.overview_tone));
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
    window.set_account_form_visible(true);
    let (status, tone) = match saved {
        LoadOutcome::Unusable(reason) => (
            format!("凭据不可用：{reason}；请重新录入账号密码"),
            tone_color(Tone::Bad),
        ),
        _ => (String::new(), muted_color()),
    };
    window.set_credential_status(status.into());
    window.set_credential_tone(tone);
    window.set_page(2);
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

fn log_rows(entries: &[LogEntry]) -> Vec<LogRow> {
    entries
        .iter()
        .map(|entry| LogRow {
            at: clock(entry.at).into(),
            level: entry.level.to_uppercase().into(),
            text: entry.text.clone().into(),
        })
        .collect()
}

fn apply(window: &MainWindow, overview: &Overview, logs: &LogUpdate, account: &AccountView) {
    window.set_gateway_value(overview.gateway.value.clone().into());
    window.set_gateway_detail(overview.gateway.detail.clone().into());
    window.set_gateway_tone(tone_color(overview.gateway.tone));
    window.set_supervision_value(overview.supervision.value.clone().into());
    window.set_supervision_detail(overview.supervision.detail.clone().into());
    window.set_supervision_tone(tone_color(overview.supervision.tone));
    window.set_auth_value(overview.auth.value.clone().into());
    window.set_auth_detail(overview.auth.detail.clone().into());
    window.set_auth_tone(tone_color(overview.auth.tone));
    window.set_upstream_value(overview.upstream.value.clone().into());
    window.set_upstream_detail(overview.upstream.detail.clone().into());
    window.set_upstream_tone(tone_color(overview.upstream.tone));
    apply_account(window, account);
    window.set_status_line(overview::status_line(overview).into());
    window.set_last_update(clock(Some(chrono::Local::now().timestamp_millis())).into());

    apply_logs(window, logs);
}

fn apply_logs(window: &MainWindow, update: &LogUpdate) {
    if let Some(reason) = &update.failure {
        window.set_log_status(format!("日志读取失败：{reason}").into());
        window.set_log_tone(tone_color(Tone::Bad));
        return;
    }

    let level = LevelFilter::from_index(window.get_log_filter_level());
    let keyword = window.get_log_filter_keyword().to_string();
    let epoch = FILTER_EPOCH.load(Ordering::SeqCst);
    let filter_changed = APPLIED_FILTER_EPOCH.swap(epoch, Ordering::SeqCst) != epoch;
    let rebuild = update.reset || update.trimmed > 0 || filter_changed;
    let follow = window.get_log_follow();
    let saved_scroll = window.get_log_scroll_y();

    let model = window.get_logs();
    let Some(model) = model.as_any().downcast_ref::<VecModel<LogRow>>() else {
        return;
    };
    let visible = if rebuild {
        let rows = log_rows(&filter_entries(&update.entries, level, &keyword));
        let count = rows.len();
        let previous = model.row_count();
        model.set_vec(rows);
        if follow {
            window.set_new_log_count(0);
        } else if filter_changed {
            window.set_log_scroll_y(0.0);
        } else if update.trimmed > 0 || previous > count {
            let removed = update.trimmed.max(previous.saturating_sub(count));
            window.set_log_scroll_y(saved_scroll + removed as f32 * 25.0);
        } else {
            window.set_log_scroll_y(saved_scroll);
        }
        count
    } else {
        let rows = log_rows(&filter_entries(&update.appended, level, &keyword));
        let count = rows.len();
        if !rows.is_empty() {
            model.extend(rows.iter().cloned());
            if follow {
                window.set_new_log_count(0);
            } else {
                window.set_new_log_count(window.get_new_log_count() + count as i32);
            }
        }
        model.row_count()
    };

    window.set_log_total(update.entries.len() as i32);
    let status = if update.entries.is_empty() {
        "暂无日志记录（本实例）".to_string()
    } else if level == LevelFilter::All && keyword.trim().is_empty() {
        format!("实时增量 · 最近 {} 条", update.entries.len())
    } else {
        format!("实时增量 · 显示 {visible} / 共 {} 条", update.entries.len())
    };
    window.set_log_status(status.into());
    window.set_log_tone(tone_color(if update.entries.is_empty() {
        Tone::Warn
    } else {
        Tone::Good
    }));
    window.set_log_version(window.get_log_version() + 1);
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

fn stop_gateway(supervisor: &SharedSupervisor, status: &SharedStatus) {
    let stopped = {
        let mut guard = supervisor
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match guard.take() {
            Some(mut supervisor) => quit_app(&mut supervisor, &mut || {}),
            None => return,
        }
    };
    *status
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = stopped;
}

fn request_quit(supervisor: &SharedSupervisor, status: &SharedStatus) {
    stop_gateway(supervisor, status);
    QUIT.store(true, Ordering::SeqCst);
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
    window.set_config_path(
        config_path
            .as_ref()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|_| "（未定位）".to_string())
            .into(),
    );
    window.set_instance_path(paths.instance_dir.display().to_string().into());
    window.set_settings_status(settings_status.into());
    window.set_settings_tone(tone_color(settings_tone));
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
            let mut last_overview: Option<Overview> = None;
            let mut last_logs: Option<LogUpdate> = None;
            let mut last_account: Option<AccountView> = None;
            let mut last_filter_epoch = FILTER_EPOCH.load(Ordering::SeqCst);
            let mut stream: Option<LogStream> = None;
            loop {
                if QUIT.load(Ordering::SeqCst) {
                    break;
                }
                if PAUSED.load(Ordering::SeqCst) {
                    std::thread::sleep(Duration::from_millis(200));
                    continue;
                }
                let base = format!("http://127.0.0.1:{}", current_port.load(Ordering::SeqCst));
                let supervision = gateway_status
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .card();
                let overview = overview::read_overview(&probe, &base, supervision);
                let logs = match stream.as_mut() {
                    Some(stream) => match stream.poll() {
                        Ok(outcome) => LogUpdate {
                            entries: stream.entries(),
                            appended: outcome
                                .appended
                                .into_iter()
                                .map(|line| line.entry)
                                .collect(),
                            reset: outcome.reset,
                            trimmed: outcome.trimmed,
                            failure: None,
                        },
                        Err(reason) => LogUpdate::failed(reason),
                    },
                    None => match LogStream::open(&service_log) {
                        Ok(opened) => {
                            let entries = opened.entries();
                            stream = Some(opened);
                            LogUpdate {
                                entries,
                                appended: Vec::new(),
                                reset: true,
                                trimmed: 0,
                                failure: None,
                            }
                        }
                        Err(reason) => LogUpdate::failed(reason),
                    },
                };
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
                let filter_epoch = FILTER_EPOCH.load(Ordering::SeqCst);
                let changed = last_overview.as_ref() != Some(&overview)
                    || last_logs.as_ref() != Some(&logs)
                    || last_account.as_ref() != Some(&account)
                    || filter_epoch != last_filter_epoch;
                if changed {
                    last_filter_epoch = filter_epoch;
                    last_overview = Some(overview.clone());
                    last_logs = Some(logs.clone());
                    last_account = Some(account.clone());
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            apply(&window, &overview, &logs, &account);
                        }
                    });
                }
                for _ in 0..10 {
                    if QUIT.load(Ordering::SeqCst)
                        || !PAUSED.load(Ordering::SeqCst)
                        || REFRESH_LOGS.swap(false, Ordering::SeqCst)
                    {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(200));
                }
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

    let shell = Rc::new(RefCell::new(ShellState::new()));

    let weak_close = window.as_weak();
    let shell_close = shell.clone();
    window.window().on_close_requested(move || {
        shell_close.borrow_mut().apply(ShellEvent::CloseRequested);
        if let Some(window) = weak_close.upgrade() {
            hide_now(&window);
        }
        CloseRequestResponse::HideWindow
    });

    let weak_hide = window.as_weak();
    let shell_hide = shell.clone();
    window.on_hide_requested(move || {
        shell_hide.borrow_mut().apply(ShellEvent::CloseRequested);
        if let Some(window) = weak_hide.upgrade() {
            hide_now(&window);
        }
    });

    let supervisor_quit = Arc::clone(&gateway_supervisor);
    let status_quit = Arc::clone(&gateway_status);
    let weak_quit = window.as_weak();
    window.on_quit_requested(move || {
        request_quit(&supervisor_quit, &status_quit);
        if let Some(window) = weak_quit.upgrade() {
            let _ = window.hide();
        }
    });

    let weak_navigate = window.as_weak();
    window.on_navigate(move |page| {
        if let Some(window) = weak_navigate.upgrade() {
            window.set_page(page);
            if page == 1 {
                REFRESH_LOGS.store(true, Ordering::SeqCst);
            }
        }
    });

    window.on_refresh_logs(|| {
        REFRESH_LOGS.store(true, Ordering::SeqCst);
    });

    window.on_apply_log_filter(|_level, _keyword| {
        FILTER_EPOCH.fetch_add(1, Ordering::SeqCst);
        REFRESH_LOGS.store(true, Ordering::SeqCst);
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
                    window.set_credential_tone(tone_color(Tone::Bad));
                    return;
                }
            }
        };
        let Some(path) = credentials_save_path.clone() else {
            window.set_credential_status("无法保存：LOCALAPPDATA 未设置".into());
            window.set_credential_tone(tone_color(Tone::Bad));
            return;
        };
        window.set_account_form_visible(false);
        window.set_credential_status("正在保存账号密码并应用到本实例网关…".into());
        window.set_credential_tone(tone_color(Tone::Warn));

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
                        window.set_credential_status(format!("保存失败：{error}").into());
                        window.set_credential_tone(tone_color(Tone::Bad));
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
                    window.set_credential_password_input("".into());
                    window.set_password_visible(false);
                    window.set_account_form_visible(false);
                    apply_account(&window, &view);
                    window.set_credential_status(message.into());
                    window.set_credential_tone(tone_color(tone));
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
            window.set_credential_tone(tone_color(Tone::Bad));
            return;
        };
        *form_clear.borrow_mut() = AccountForm::new(None);
        window.set_credential_account_input("".into());
        window.set_credential_password_input("".into());
        window.set_password_visible(false);
        window.set_account_form_visible(false);
        window.set_credential_status("正在清除已保存的账号密码…".into());
        window.set_credential_tone(tone_color(Tone::Warn));

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
                            window.set_credential_tone(tone_color(Tone::Bad));
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
                    window.set_credential_tone(tone_color(tone));
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
        window.set_credential_status("正在打开可见 Edge 登录…".into());
        window.set_credential_tone(tone_color(Tone::Warn));
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
                    AuthState::Valid => ("已登录".to_string(), Tone::Good),
                    AuthState::LoggingIn => (
                        "已打开可见 Edge；请完成 UMPASS/Duo 登录".to_string(),
                        Tone::Warn,
                    ),
                    _ => (format!("登录未完成：{}", view.state_detail), Tone::Bad),
                },
                AuthRead::Unavailable(reason) => {
                    (format!("无法打开可见登录：{reason}"), Tone::Bad)
                }
            };
            let _ = slint::invoke_from_event_loop(move || {
                if let Some(window) = weak.upgrade() {
                    apply_account(&window, &view);
                    window.set_credential_status(message.into());
                    window.set_credential_tone(tone_color(tone));
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
                window.set_settings_tone(tone_color(Tone::Bad));
                return;
            }
        };
        window.set_settings_status(format!("正在切换到 127.0.0.1:{port}…").into());
        window.set_settings_tone(tone_color(Tone::Warn));

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
                    config::save_to(
                        path,
                        &DesktopConfig {
                            model_api_port: port,
                        },
                    )?;
                    *config
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner()) = DesktopConfig {
                        model_api_port: port,
                    };
                    Ok(())
                };
                let outcome = supervisor.switch_port(port, &mut save);
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
                    window.set_port_input(next_port.to_string().into());
                    window.set_settings_status(message.into());
                    window.set_settings_tone(tone_color(tone));
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
                    if shell_timer.borrow_mut().apply(ShellEvent::TrayQuit) == ShellAction::Quit {
                        request_quit(&supervisor_tray, &status_tray);
                    }
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
