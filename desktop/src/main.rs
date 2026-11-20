#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Slint desktop shell for the local console (issue #123).
//!
//! First open skips the UM login, reads the existing 8000/8001 read-only health
//! and log faces, saves the future model API port and keeps the window in the
//! tray. It never starts/stops the gateway (#124), logs in or reads credentials
//! (#125), and it opens no TCP listener.

mod tray;

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use chrono::TimeZone;
use slint::{CloseRequestResponse, ComponentHandle, ModelRc, Timer, TimerMode, VecModel};
use um_desktop::config::{self, DesktopConfig, LoadOutcome};
use um_desktop::instance::{self, InstanceError};
use um_desktop::logs::{self, LogsView};
use um_desktop::overview::{self, HttpProbe, Overview, Tone};
use um_desktop::shell::{ShellAction, ShellEvent, ShellState};

slint::include_modules!();

static PAUSED: AtomicBool = AtomicBool::new(false);
static QUIT: AtomicBool = AtomicBool::new(false);
static REFRESH_LOGS: AtomicBool = AtomicBool::new(false);

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

fn env_url(name: &str, fallback: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

fn apply(window: &MainWindow, overview: &Overview, logs: &LogsView) {
    window.set_gateway_value(overview.gateway.value.clone().into());
    window.set_gateway_detail(overview.gateway.detail.clone().into());
    window.set_gateway_tone(tone_color(overview.gateway.tone));
    window.set_console_value(overview.console.value.clone().into());
    window.set_console_detail(overview.console.detail.clone().into());
    window.set_console_tone(tone_color(overview.console.tone));
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

    let (log_status, log_tone) = logs::logs_status(logs);
    window.set_log_status(log_status.into());
    window.set_log_tone(tone_color(log_tone));
    if let LogsView::Loaded(entries) = logs {
        let rows = entries
            .iter()
            .map(|entry| LogRow {
                at: clock(entry.at).into(),
                level: entry.level.to_uppercase().into(),
                text: entry.text.clone().into(),
            })
            .collect::<Vec<_>>();
        window.set_logs(ModelRc::from(Rc::new(VecModel::from(rows))));
    } else {
        window.set_logs(ModelRc::from(Rc::new(VecModel::<LogRow>::default())));
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
                "使用默认端口 7999（尚未保存设置）".to_string(),
                Tone::Warn,
            ),
            LoadOutcome::Loaded(config) => (
                config,
                format!("已加载配置：{}", path.display()),
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
    let current_config = Rc::new(RefCell::new(loaded_config.clone()));
    window.set_port_input(loaded_config.model_api_port.to_string().into());
    window.set_config_path(
        config_path
            .as_ref()
            .map(|path| path.display().to_string())
            .unwrap_or_else(|_| "（未定位）".to_string())
            .into(),
    );
    window.set_settings_status(settings_status.into());
    window.set_settings_tone(tone_color(settings_tone));

    let gateway = env_url("UM_DESKTOP_GATEWAY_URL", "http://127.0.0.1:8000");
    let console = env_url("UM_DESKTOP_CONSOLE_URL", "http://127.0.0.1:8001");
    let probe = HttpProbe::new();
    {
        let weak = window.as_weak();
        std::thread::spawn(move || {
            let mut last_overview: Option<Overview> = None;
            let mut last_logs: Option<LogsView> = None;
            loop {
                if QUIT.load(Ordering::SeqCst) {
                    break;
                }
                if PAUSED.load(Ordering::SeqCst) {
                    std::thread::sleep(Duration::from_millis(200));
                    continue;
                }
                let overview = overview::read_overview(&probe, &gateway, &console);
                let logs = logs::read_logs(&probe, &console);
                let changed = last_overview.as_ref() != Some(&overview)
                    || last_logs.as_ref() != Some(&logs);
                if changed {
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

    let shell_quit = shell.clone();
    window.on_quit_requested(move || {
        if shell_quit.borrow_mut().apply(ShellEvent::TrayQuit) == ShellAction::Quit {
            QUIT.store(true, Ordering::SeqCst);
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

    let weak_account = window.as_weak();
    window.on_configure_account(move || {
        if let Some(window) = weak_account.upgrade() {
            window.set_account_note(
                "UM 账号登录将在 #125 接入；当前版本可先查看状态、设置端口和检查日志。".into(),
            );
        }
    });

    let save_path = config_path.ok();
    let save_config = current_config.clone();
    let weak_save = window.as_weak();
    window.on_save_port(move |text| {
        let Some(window) = weak_save.upgrade() else {
            return;
        };
        let Some(path) = save_path.as_ref() else {
            window.set_settings_status("无法保存：LOCALAPPDATA 未设置".into());
            window.set_settings_tone(tone_color(Tone::Bad));
            return;
        };
        match config::validate_port(&text) {
            Ok(port) => {
                let next = DesktopConfig {
                    model_api_port: port,
                };
                match config::save_to(path, &next) {
                    Ok(()) => {
                        *save_config.borrow_mut() = next;
                        window.set_port_input(port.to_string().into());
                        window.set_settings_status(
                            format!("已保存 127.0.0.1:{port}（端口切换在 #124）").into(),
                        );
                        window.set_settings_tone(tone_color(Tone::Good));
                    }
                    Err(error) => {
                        window.set_settings_status(format!("保存失败：{error}").into());
                        window.set_settings_tone(tone_color(Tone::Bad));
                    }
                }
            }
            Err(error) => {
                window.set_settings_status(format!("端口无效：{error}").into());
                window.set_settings_tone(tone_color(Tone::Bad));
            }
        }
    });

    let timer = Timer::default();
    let weak_timer = window.as_weak();
    let shell_timer = shell.clone();
    let mut tray = tray;
    timer.start(TimerMode::Repeated, Duration::from_millis(120), move || {
        let Some(window) = weak_timer.upgrade() else {
            return;
        };
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
                    if shell_timer.borrow_mut().apply(ShellEvent::TrayQuit)
                        == ShellAction::Quit
                    {
                        QUIT.store(true, Ordering::SeqCst);
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
