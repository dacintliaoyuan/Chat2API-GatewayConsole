//! Issue #109 prototype: Tauri 2 shell over a minimal TypeScript status/log page.
//!
//! The Rust side polls the existing local 8000/8001 (read-only), owns the tray
//! and hides the window on close. The page only renders snapshots and records
//! measurement markers; it never logs in or changes a service.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod metrics;

use std::time::Duration;

use serde::Serialize;
use serde_json::{json, Value};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, WindowEvent};

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct StatusView {
    value: String,
    detail: String,
    tone: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct LogRow {
    at: i64,
    level: String,
    text: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct Snapshot {
    gateway: StatusView,
    console: StatusView,
    auth: StatusView,
    upstream: StatusView,
    logs: Vec<LogRow>,
    log_error: Option<String>,
    status_error: Option<String>,
}

fn base(name: &str, fallback: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

fn env_ms(name: &str) -> Option<i64> {
    std::env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| *value > 0)
}

fn fetch_json(agent: &ureq::Agent, url: &str) -> Result<Value, String> {
    agent
        .get(url)
        .call()
        .map_err(|error| format!("{url}: {error}"))?
        .into_json::<Value>()
        .map_err(|error| format!("{url}: {error}"))
}

fn auth_view(state: &str, label: Option<&str>) -> StatusView {
    let value = match state {
        "valid" => "有效".to_string(),
        "expired" => "已过期".to_string(),
        "missing" | "unconfigured" | "" => "未配置".to_string(),
        other => other.to_string(),
    };
    let tone = match state {
        "valid" => "good",
        "missing" | "unconfigured" | "expired" => "warn",
        _ => "bad",
    };
    StatusView {
        value,
        detail: label.unwrap_or(state).to_string(),
        tone: tone.to_string(),
    }
}

fn fetch_snapshot() -> Snapshot {
    let gateway_base = base("PROTO109_GATEWAY_URL", "http://127.0.0.1:8000");
    let console_base = base("PROTO109_CONSOLE_URL", "http://127.0.0.1:8001");
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_millis(2000))
        .build();

    let mut failures = Vec::new();
    let gateway_body = fetch_json(&agent, &format!("{gateway_base}/health"));
    let console_body = fetch_json(&agent, &format!("{console_base}/health"));

    let (gateway, auth, upstream) = match &gateway_body {
        Ok(body) => {
            let state = body["authentication"]["state"].as_str().unwrap_or("unknown");
            let auth = auth_view(state, body["authentication"]["label"].as_str());
            let upstream_state = body["upstream_reachability"]["state"]
                .as_str()
                .unwrap_or("unknown");
            let status = body["upstream_reachability"]["upstream_status"]
                .as_i64()
                .map(|status| format!(" · HTTP {status}"))
                .unwrap_or_default();
            let name = body["upstream"]["name"].as_str().unwrap_or("UM 上游");
            let (value, tone) = match upstream_state {
                "ok" => ("可达", "good"),
                "unknown" => ("未知", "warn"),
                _ => (upstream_state, "bad"),
            };
            (
                StatusView {
                    value: "运行中".to_string(),
                    detail: format!(
                        "{gateway_base} · 模型 {} 个",
                        body["models"].as_i64().unwrap_or(0)
                    ),
                    tone: "good".to_string(),
                },
                auth,
                StatusView {
                    value: value.to_string(),
                    detail: format!("{name}{status}"),
                    tone: tone.to_string(),
                },
            )
        }
        Err(error) => {
            failures.push(format!("8000 {error}"));
            (
                StatusView {
                    value: "不可用".to_string(),
                    detail: error.clone(),
                    tone: "bad".to_string(),
                },
                StatusView {
                    value: "未知".to_string(),
                    detail: "8000 未返回认证状态".to_string(),
                    tone: "warn".to_string(),
                },
                StatusView {
                    value: "未知".to_string(),
                    detail: "8000 未返回上游状态".to_string(),
                    tone: "warn".to_string(),
                },
            )
        }
    };

    let console = match &console_body {
        Ok(body) => StatusView {
            value: "运行中".to_string(),
            detail: format!(
                "{console_base} · {}",
                body["status"].as_str().unwrap_or("?")
            ),
            tone: "good".to_string(),
        },
        Err(error) => {
            failures.push(format!("8001 {error}"));
            StatusView {
                value: "不可用".to_string(),
                detail: error.clone(),
                tone: "bad".to_string(),
            }
        }
    };

    let (logs, log_error) = match fetch_json(
        &agent,
        &format!("{console_base}/api/console/um/logs?window=today&source=service&limit=80"),
    ) {
        Ok(body) => {
            let status = body["status"].as_str().unwrap_or("unknown");
            if status == "ok" {
                let rows = body["lines"]
                    .as_array()
                    .map(|rows| {
                        rows.iter()
                            .map(|row| LogRow {
                                at: row["at"].as_i64().unwrap_or(0),
                                level: row["level"].as_str().unwrap_or("unknown").to_string(),
                                text: row["text"].as_str().unwrap_or("").to_string(),
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                (rows, None)
            } else {
                (
                    Vec::new(),
                    Some(format!(
                        "日志读取状态 {status}: {}",
                        body["error"].as_str().unwrap_or("")
                    )),
                )
            }
        }
        Err(error) => (Vec::new(), Some(error)),
    };

    Snapshot {
        gateway,
        console,
        auth,
        upstream,
        logs,
        log_error,
        status_error: if failures.is_empty() {
            None
        } else {
            Some(failures.join("；"))
        },
    }
}

#[tauri::command]
fn snapshot() -> Snapshot {
    fetch_snapshot()
}

#[tauri::command]
fn record_marker(name: String, at_ms: i64, extra: Option<Value>) {
    metrics::record(&name, at_ms, extra.unwrap_or_else(|| json!({})));
}

fn hide_window(app: &tauri::AppHandle, source: &str) {
    if let Some(window) = app.get_webview_window("main") {
        metrics::record("tray_hide", metrics::now_ms(), json!({ "source": source }));
        let _ = window.hide();
        let _ = window.emit("tray-hidden", ());
    }
}

#[tauri::command]
fn hide_to_tray(app: tauri::AppHandle) {
    hide_window(&app, "button");
}

#[tauri::command]
fn quit_app(app: tauri::AppHandle) {
    metrics::record("tray_quit", metrics::now_ms(), json!({ "source": "button" }));
    app.exit(0);
}

fn show_main(app: &tauri::AppHandle, click_ms: i64) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
        metrics::record("tray_restore_shown", metrics::now_ms(), json!({}));
        let _ = window.emit("tray-restored", click_ms);
    }
}

fn icon_rgba() -> Vec<u8> {
    let mut pixels = Vec::with_capacity(16 * 16 * 4);
    for y in 0..16u8 {
        for x in 0..16u8 {
            let border = x < 2 || y < 2 || x > 13 || y > 13;
            let center = (5..=10).contains(&x) && (5..=10).contains(&y);
            let color = if border {
                [255, 255, 255, 255]
            } else if center {
                [220, 235, 255, 255]
            } else {
                [27, 92, 204, 255]
            };
            pixels.extend_from_slice(&color);
        }
    }
    pixels
}

fn main() {
    metrics::record("process_start", metrics::now_ms(), json!({}));
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            snapshot,
            record_marker,
            hide_to_tray,
            quit_app
        ])
        .setup(|app| {
            let show = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            let icon = tauri::image::Image::new_owned(icon_rgba(), 16, 16);
            TrayIconBuilder::with_id("main")
                .icon(icon)
                .tooltip("本机控制台 109 原型")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        let at = metrics::now_ms();
                        metrics::record("tray_click", at, json!({ "source": "menu" }));
                        show_main(app, at);
                    }
                    "quit" => {
                        metrics::record("tray_quit", metrics::now_ms(), json!({}));
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let at = metrics::now_ms();
                        metrics::record("tray_click", at, json!({ "source": "icon" }));
                        show_main(tray.app_handle(), at);
                    }
                })
                .build(app)?;
            // Measurement-only hook: the same hide/show path as the tray, driven
            // by absolute delays so the report can time restore without a
            // physical click.
            let handle = app.handle().clone();
            let hide_after = env_ms("PROTO109_AUTO_HIDE_MS");
            let restore_after = env_ms("PROTO109_AUTO_RESTORE_MS");
            if hide_after.is_some() || restore_after.is_some() {
                let start = metrics::now_ms();
                std::thread::spawn(move || {
                    if let Some(ms) = hide_after {
                        let wait = (start + ms - metrics::now_ms()).max(0);
                        std::thread::sleep(Duration::from_millis(wait as u64));
                        hide_window(&handle, "auto");
                    }
                    if let Some(ms) = restore_after {
                        let wait = (start + ms - metrics::now_ms()).max(0);
                        std::thread::sleep(Duration::from_millis(wait as u64));
                        let at = metrics::now_ms();
                        metrics::record("tray_click", at, json!({ "source": "auto" }));
                        show_main(&handle, at);
                    }
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                metrics::record("tray_hide", metrics::now_ms(), json!({ "source": "close" }));
                let _ = window.hide();
                let _ = window.emit("tray-hidden", ());
            }
        })
        .run(tauri::generate_context!())
        .expect("run the #109 Tauri prototype");
}
