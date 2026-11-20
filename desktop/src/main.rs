#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Slint desktop shell for the local console (issues #123/#124).
//!
//! First open skips the UM login, reads the app instance's own gateway health and
//! log file, saves the model API port and keeps the window in the tray. The app
//! attaches to an already-running instance or starts exactly one managed gateway
//! child on the configured loopback port. It never touches the existing 8000/8001
//! pair, logs in or reads credentials (#125), or opens its own TCP listener.

mod tray;

use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, AtomicU16, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::TimeZone;
use slint::{CloseRequestResponse, ComponentHandle, Model, ModelRc, Timer, TimerMode, VecModel};
use um_desktop::config::{self, DesktopConfig, LoadOutcome};
use um_desktop::gateway::{
    quit_app, resolve_gateway_binary, GatewayState, GatewayStatus, GatewaySupervisor, HttpGateway,
    InstancePaths, PortSwitch, SystemLauncher, ThreadSleeper,
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

fn apply(window: &MainWindow, overview: &Overview, logs: &LogUpdate) {
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
    window.set_account_label(overview.account_label.clone().into());
    window.set_account_detail(overview.account_detail.clone().into());
    window.set_account_tone(tone_color(overview.account_tone));
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
            LoadOutcome::Missing => (
                DesktopConfig::default(),
                "使用默认端口 7999（尚未保存设置）；启动时接入已有实例或拉起本实例网关".to_string(),
                Tone::Warn,
            ),
            LoadOutcome::Loaded(config) => (
                config,
                format!(
                    "已加载配置：{}；启动时接入已有实例或拉起本实例网关",
                    path.display()
                ),
                Tone::Good,
            ),
            LoadOutcome::Invalid(reason) => (
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

    let program = resolve_gateway_binary(gateway_bin_override(), &app_dir());
    let gateway_supervisor = Arc::new(Mutex::new(Some(GatewaySupervisor::new(
        loaded_config.model_api_port,
        paths.clone(),
        program,
        Arc::new(HttpGateway::new()),
        Box::new(SystemLauncher),
        Box::new(ThreadSleeper),
    ))));
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
    let service_log = paths.service_log.clone();
    {
        let weak = window.as_weak();
        let gateway_status = Arc::clone(&gateway_status);
        let current_port = Arc::clone(&current_port);
        std::thread::spawn(move || {
            let mut last_overview: Option<Overview> = None;
            let mut last_logs: Option<LogUpdate> = None;
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
                let filter_epoch = FILTER_EPOCH.load(Ordering::SeqCst);
                let changed = last_overview.as_ref() != Some(&overview)
                    || last_logs.as_ref() != Some(&logs)
                    || filter_epoch != last_filter_epoch;
                if changed {
                    last_filter_epoch = filter_epoch;
                    last_overview = Some(overview.clone());
                    last_logs = Some(logs.clone());
                    let weak = weak.clone();
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            apply(&window, &overview, &logs);
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

    let weak_account = window.as_weak();
    window.on_configure_account(move || {
        if let Some(window) = weak_account.upgrade() {
            window.set_account_note(
                "UM 账号登录将在 #125 接入；当前版本可查看状态、设置端口和检查日志。".into(),
            );
        }
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
    let mut tray = tray;
    let mut last_paused = false;
    timer.start(TimerMode::Repeated, Duration::from_millis(120), move || {
        let Some(window) = weak_timer.upgrade() else {
            return;
        };
        let paused = PAUSED.load(Ordering::SeqCst);
        if paused != last_paused {
            last_paused = paused;
            if let Some(active) = tray.as_ref() {
                active.set_tooltip(if paused {
                    "本机控制台 · 日志已暂停"
                } else {
                    "本机控制台"
                });
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
