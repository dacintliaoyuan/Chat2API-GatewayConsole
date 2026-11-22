//! Issue #109 prototype: Slint desktop window over the existing local 8000/8001.
//!
//! Read-only: it polls the local gateway health and the console's UM log read.
//! It never logs in, writes a ledger or changes a service. Closing the window
//! hides it to the tray; polling pauses while hidden and resumes on restore.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod metrics;
mod tray;

use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::sync::atomic::{AtomicBool, AtomicI64, Ordering};
use std::time::Duration;

use chrono::TimeZone;
use serde_json::{json, Value};
use slint::{CloseRequestResponse, ComponentHandle, Model, ModelRc, Timer, TimerMode, VecModel};

slint::include_modules!();

const POLL_MS: u64 = 250;
const MAX_ROWS: usize = 400;
static PAUSED: AtomicBool = AtomicBool::new(false);
static QUIT: AtomicBool = AtomicBool::new(false);
static FIRST_READY: AtomicBool = AtomicBool::new(false);
static AUTO_HIDE_AT: AtomicI64 = AtomicI64::new(0);
static AUTO_RESTORE_AT: AtomicI64 = AtomicI64::new(0);

#[derive(Clone, PartialEq)]
struct Status {
    gateway_value: String,
    gateway_detail: String,
    gateway_tone: Tone,
    console_value: String,
    console_detail: String,
    console_tone: Tone,
    auth_value: String,
    auth_detail: String,
    auth_tone: Tone,
    upstream_value: String,
    upstream_detail: String,
    upstream_tone: Tone,
}

#[derive(Clone, Copy, PartialEq)]
enum Tone {
    Good,
    Bad,
    Warn,
}

struct Fetched {
    status: Status,
    log_error: Option<String>,
    status_error: Option<String>,
}

#[derive(Clone)]
struct LogView {
    at: i64,
    level: String,
    text: String,
}

fn base(name: &str, fallback: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| fallback.to_string())
}

fn fetch_json(agent: &ureq::Agent, url: &str) -> Result<Value, String> {
    agent
        .get(url)
        .call()
        .map_err(|error| format!("{url}: {error}"))?
        .into_json::<Value>()
        .map_err(|error| format!("{url}: {error}"))
}

fn auth_label(state: &str, label: Option<&str>) -> (String, String, Tone) {
    let value = match state {
        "valid" => "有效".to_string(),
        "expired" => "已过期".to_string(),
        "missing" | "unconfigured" | "" => "未配置".to_string(),
        other => other.to_string(),
    };
    let detail = label.unwrap_or(state).to_string();
    let tone = match state {
        "valid" => Tone::Good,
        "missing" | "unconfigured" | "expired" => Tone::Warn,
        _ => Tone::Bad,
    };
    (value, detail, tone)
}

fn fetch_status(agent: &ureq::Agent, gateway: &str, console: &str) -> (Status, Option<String>) {
    let mut failures = Vec::new();
    let gateway_body = fetch_json(agent, &format!("{gateway}/health"));
    let console_body = fetch_json(agent, &format!("{console}/health"));

    let (gateway_value, gateway_detail, gateway_tone) = match &gateway_body {
        Ok(body) => (
            "运行中".to_string(),
            format!("{gateway} · 模型 {} 个", body["models"].as_i64().unwrap_or(0)),
            Tone::Good,
        ),
        Err(error) => {
            failures.push(format!("8000 {error}"));
            ("不可用".to_string(), error.clone(), Tone::Bad)
        }
    };
    let (console_value, console_detail, console_tone) = match &console_body {
        Ok(body) => (
            "运行中".to_string(),
            format!("{console} · {}", body["status"].as_str().unwrap_or("?")),
            Tone::Good,
        ),
        Err(error) => {
            failures.push(format!("8001 {error}"));
            ("不可用".to_string(), error.clone(), Tone::Bad)
        }
    };
    let (auth_value, auth_detail, auth_tone, upstream_value, upstream_detail, upstream_tone) =
        match &gateway_body {
            Ok(body) => {
                let state = body["authentication"]["state"].as_str().unwrap_or("unknown");
                let label = body["authentication"]["label"].as_str();
                let (value, detail, tone) = auth_label(state, label);
                let upstream = body["upstream_reachability"]["state"]
                    .as_str()
                    .unwrap_or("unknown");
                let upstream_status = body["upstream_reachability"]["upstream_status"]
                    .as_i64()
                    .map(|status| format!(" · HTTP {status}"))
                    .unwrap_or_default();
                let name = body["upstream"]["name"].as_str().unwrap_or("UM 上游");
                let (upstream_value, upstream_detail, upstream_tone) = match upstream {
                    "ok" => ("可达".to_string(), format!("{name}{upstream_status}"), Tone::Good),
                    "unknown" => ("未知".to_string(), format!("{name} 探测未定"), Tone::Warn),
                    other => (other.to_string(), format!("{name}{upstream_status}"), Tone::Bad),
                };
                (value, detail, tone, upstream_value, upstream_detail, upstream_tone)
            }
            Err(_) => (
                "未知".to_string(),
                "8000 未返回认证状态".to_string(),
                Tone::Warn,
                "未知".to_string(),
                "8000 未返回上游状态".to_string(),
                Tone::Warn,
            ),
        };
    (
        Status {
            gateway_value,
            gateway_detail,
            gateway_tone,
            console_value,
            console_detail,
            console_tone,
            auth_value,
            auth_detail,
            auth_tone,
            upstream_value,
            upstream_detail,
            upstream_tone,
        },
        if failures.is_empty() {
            None
        } else {
            Some(failures.join("；"))
        },
    )
}

fn fetch_logs(agent: &ureq::Agent, console: &str) -> Result<Vec<LogView>, String> {
    let body = fetch_json(
        agent,
        &format!("{console}/api/console/um/logs?window=today&source=service&limit=80"),
    )?;
    let status = body["status"].as_str().unwrap_or("unknown");
    if status != "ok" {
        return Err(format!(
            "日志读取状态 {status}: {}",
            body["error"].as_str().unwrap_or("")
        ));
    }
    Ok(body["lines"]
        .as_array()
        .map(|rows| {
            rows.iter()
                .map(|row| LogView {
                    at: row["at"].as_i64().unwrap_or(0),
                    level: row["level"].as_str().unwrap_or("unknown").to_string(),
                    text: row["text"].as_str().unwrap_or("").to_string(),
                })
                .collect()
        })
        .unwrap_or_default())
}

fn row_id(row: &LogView) -> u64 {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    row.at.hash(&mut hasher);
    row.level.hash(&mut hasher);
    row.text.hash(&mut hasher);
    hasher.finish()
}

fn marker(text: &str) -> Option<(String, i64)> {
    let mut id = None;
    let mut written = None;
    for token in text.split_whitespace() {
        if let Some(value) = token.strip_prefix("id=") {
            id = Some(
                value
                    .trim_matches(|character: char| {
                        !character.is_ascii_alphanumeric() && character != '-'
                    })
                    .to_string(),
            );
        } else if let Some(value) = token.strip_prefix("t=") {
            written = value
                .trim_matches(|character: char| !character.is_ascii_digit())
                .parse::<i64>()
                .ok();
        }
    }
    match (id, written) {
        (Some(id), Some(written)) if !id.is_empty() => Some((id, written)),
        _ => None,
    }
}

fn clock(at: i64) -> String {
    chrono::Local
        .timestamp_millis_opt(at)
        .single()
        .map(|time| time.format("%H:%M:%S").to_string())
        .unwrap_or_else(|| "—".to_string())
}

fn tone_color(tone: Tone) -> slint::Color {
    match tone {
        Tone::Good => slint::Color::from_rgb_u8(23, 106, 60),
        Tone::Bad => slint::Color::from_rgb_u8(180, 35, 24),
        Tone::Warn => slint::Color::from_rgb_u8(139, 76, 12),
    }
}

fn apply(window: &MainWindow, fetched: Fetched, new_logs: Vec<LogView>, changed: bool, first: bool) {
    let now = metrics::now_ms();
    if changed || first {
        let status = fetched.status;
        window.set_gateway_value(status.gateway_value.into());
        window.set_gateway_detail(status.gateway_detail.into());
        window.set_gateway_tone(tone_color(status.gateway_tone));
        window.set_console_value(status.console_value.into());
        window.set_console_detail(status.console_detail.into());
        window.set_console_tone(tone_color(status.console_tone));
        window.set_auth_value(status.auth_value.into());
        window.set_auth_detail(status.auth_detail.into());
        window.set_auth_tone(tone_color(status.auth_tone));
        window.set_upstream_value(status.upstream_value.into());
        window.set_upstream_detail(status.upstream_detail.into());
        window.set_upstream_tone(tone_color(status.upstream_tone));
    }
    if let Some(error) = &fetched.log_error {
        window.set_poll_status(format!("日志读取失败：{error}").into());
    } else if let Some(error) = &fetched.status_error {
        window.set_poll_status(format!("状态读取失败：{error}").into());
    } else {
        window.set_poll_status(format!("轮询正常 · {POLL_MS} ms · 只读").into());
    }
    window.set_last_update(clock(now).into());
    if !new_logs.is_empty() {
        if let Some(model) = window.get_logs().as_any().downcast_ref::<VecModel<LogRow>>() {
            for row in new_logs.iter().rev() {
                model.insert(
                    0,
                    LogRow {
                        at: clock(row.at).into(),
                        level: row.level.to_uppercase().into(),
                        text: row.text.clone().into(),
                    },
                );
            }
            while model.row_count() > MAX_ROWS {
                model.remove(MAX_ROWS);
            }
            window.set_log_count(model.row_count() as i32);
        }
        let mut marked = Vec::new();
        for row in &new_logs {
            if let Some((id, written)) = marker(&row.text) {
                marked.push((id, written));
            }
        }
        if !marked.is_empty() {
            for (id, written) in &marked {
                metrics::record(
                    "log_visible",
                    now,
                    json!({"id": id, "written_ms": written}),
                );
            }
            let ids: Vec<String> = marked.iter().map(|(id, _)| id.clone()).collect();
            slint::Timer::single_shot(Duration::from_millis(16), move || {
                metrics::record(
                    "log_visible_frame",
                    metrics::now_ms(),
                    json!({"ids": ids}),
                );
            });
        }
    }
    if first && !FIRST_READY.swap(true, Ordering::SeqCst) {
        metrics::record("ui_ready", now, json!({}));
    }
}

fn env_ms(name: &str) -> Option<i64> {
    std::env::var(name)
        .ok()
        .and_then(|value| value.trim().parse::<i64>().ok())
        .filter(|value| *value > 0)
}

/// Same path as a tray activation or the close button: record the click, show the
/// window, resume polling and record the first post-show tick.
fn restore_now(window: &MainWindow, click_ms: i64, source: &str) {
    metrics::record("tray_click", click_ms, json!({"source": source}));
    let _ = window.show();
    window.window().set_minimized(false);
    metrics::record("tray_restore_shown", metrics::now_ms(), json!({}));
    PAUSED.store(false, Ordering::SeqCst);
    slint::Timer::single_shot(Duration::from_millis(30), move || {
        metrics::record("tray_restore_visible", metrics::now_ms(), json!({}));
    });
}

fn hide_now(window: &MainWindow, source: &str) {
    metrics::record("tray_hide", metrics::now_ms(), json!({"source": source}));
    PAUSED.store(true, Ordering::SeqCst);
    let _ = window.hide();
}

fn main() -> Result<(), slint::PlatformError> {
    metrics::record("process_start", metrics::now_ms(), json!({}));

    if let Some(ms) = env_ms("PROTO109_AUTO_HIDE_MS") {
        AUTO_HIDE_AT.store(metrics::now_ms() + ms, Ordering::SeqCst);
    }
    if let Some(ms) = env_ms("PROTO109_AUTO_RESTORE_MS") {
        AUTO_RESTORE_AT.store(metrics::now_ms() + ms, Ordering::SeqCst);
    }

    let window = MainWindow::new()?;
    let model = std::rc::Rc::new(VecModel::<LogRow>::default());
    window.set_logs(ModelRc::from(model.clone()));

    let gateway = base("PROTO109_GATEWAY_URL", "http://127.0.0.1:8000");
    let console = base("PROTO109_CONSOLE_URL", "http://127.0.0.1:8001");
    let poll_ms = std::env::var("PROTO109_POLL_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|value| (100..=5000).contains(value))
        .unwrap_or(POLL_MS);

    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_millis(2000))
        .build();

    {
        let weak = window.as_weak();
        let gateway = gateway.clone();
        let console = console.clone();
        std::thread::spawn(move || {
            let mut last: Option<Status> = None;
            let mut seen: HashMap<u64, usize> = HashMap::new();
            loop {
                if QUIT.load(Ordering::SeqCst) {
                    break;
                }
                if PAUSED.load(Ordering::SeqCst) {
                    std::thread::sleep(Duration::from_millis(200));
                    continue;
                }
                let (status, status_error) = fetch_status(&agent, &gateway, &console);
                let logs = fetch_logs(&agent, &console);
                let mut new_logs = Vec::new();
                let mut log_error = None;
                match logs {
                    Ok(rows) => {
                        let mut counts: HashMap<u64, usize> = HashMap::new();
                        for row in &rows {
                            *counts.entry(row_id(row)).or_insert(0) += 1;
                        }
                        for row in &rows {
                            let id = row_id(row);
                            let have = seen.get(&id).copied().unwrap_or(0);
                            let total = counts[&id];
                            if total > have {
                                new_logs.push(row.clone());
                                counts.insert(id, total - 1);
                                seen.insert(id, total);
                            }
                        }
                    }
                    Err(error) => log_error = Some(error),
                }
                let changed = last.as_ref() != Some(&status);
                let first = last.is_none();
                last = Some(status.clone());
                if changed || first || !new_logs.is_empty() || log_error.is_some() {
                    let weak = weak.clone();
                    let fetched = Fetched {
                        status,
                        log_error,
                        status_error,
                    };
                    let _ = slint::invoke_from_event_loop(move || {
                        if let Some(window) = weak.upgrade() {
                            apply(&window, fetched, new_logs, changed, first);
                        }
                    });
                }
                std::thread::sleep(Duration::from_millis(poll_ms));
            }
        });
    }

    let tray = match tray::install() {
        Ok(tray) => Some(tray),
        Err(error) => {
            eprintln!("proto109: tray unavailable: {error}");
            None
        }
    };

    let weak_close = window.as_weak();
    window.window().on_close_requested(move || {
        if let Some(window) = weak_close.upgrade() {
            hide_now(&window, "close");
        }
        CloseRequestResponse::HideWindow
    });

    let weak_button = window.as_weak();
    window.on_hide_requested(move || {
        if let Some(window) = weak_button.upgrade() {
            hide_now(&window, "button");
        }
    });

    let weak_quit = window.as_weak();
    window.on_quit_requested(move || {
        if let Some(window) = weak_quit.upgrade() {
            let _ = window.hide();
        }
        QUIT.store(true, Ordering::SeqCst);
    });

    let timer = Timer::default();
    let weak_timer = window.as_weak();
    let mut tray = tray;
    timer.start(TimerMode::Repeated, Duration::from_millis(120), move || {
        let Some(window) = weak_timer.upgrade() else {
            return;
        };
        let now = metrics::now_ms();
        let hide_at = AUTO_HIDE_AT.load(Ordering::SeqCst);
        if hide_at != 0 && now >= hide_at {
            AUTO_HIDE_AT.store(0, Ordering::SeqCst);
            hide_now(&window, "auto");
        }
        let restore_at = AUTO_RESTORE_AT.load(Ordering::SeqCst);
        if restore_at != 0 && now >= restore_at {
            AUTO_RESTORE_AT.store(0, Ordering::SeqCst);
            restore_now(&window, now, "auto");
        }
        if let Some(active) = tray.as_ref() {
            match active.pump() {
                Some(tray::SHOW) => {
                    restore_now(&window, tray::CLICK_MS.load(Ordering::SeqCst), "icon");
                }
                Some(tray::QUIT) => {
                    metrics::record("tray_quit", metrics::now_ms(), json!({}));
                    QUIT.store(true, Ordering::SeqCst);
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
    metrics::record("window_shown", metrics::now_ms(), json!({}));
    // A hidden window must keep the loop alive for the tray and the poll thread.
    slint::run_event_loop_until_quit()
}
