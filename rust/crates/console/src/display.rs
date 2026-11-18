//! S5b: the display-session / display-state read faces.
//!
//! The production entry wires the S7 display relay's `DisplayConfig::availability`
//! through `ShareReadSeams`; an enabled relay is reported as available, and only
//! a disabled/unassembled relay is `display_disabled` (or its config error).
//! The display state maps the instance's own auth observation exactly like
//! `usagesrv.py::_console_display_state_of`.

use serde_json::{json, Map, Value};
use um_gateway::login::LOGIN_TERMINAL_MANUAL_REASONS;

const MANUAL_TAKEOVER_PREFIXES: [&str; 2] = ["manual_takeover_required:", "manual_takeover:"];

fn default_operator() -> Value {
    json!({
        "owned_by_me": false,
        "owner_present": false,
        "generation": 0,
        "stale": false,
    })
}

fn terminal_manual_reason(error: Option<&str>, primary: Option<&str>) -> Option<String> {
    for value in [error, primary].into_iter().flatten() {
        let mut reason = value;
        for prefix in MANUAL_TAKEOVER_PREFIXES {
            if let Some(stripped) = reason.strip_prefix(prefix) {
                reason = stripped;
                break;
            }
        }
        if LOGIN_TERMINAL_MANUAL_REASONS.contains(&reason) {
            return Some(reason.to_string());
        }
    }
    None
}

fn display_availability(seam: Option<Value>) -> Value {
    match seam {
        None => json!({"available": false, "reason": "display_disabled"}),
        Some(availability) => {
            if availability["available"].as_bool() == Some(true) {
                json!({"available": true, "reason": Value::Null})
            } else {
                let reason = availability["reason"]
                    .as_str()
                    .filter(|reason| !reason.is_empty())
                    .unwrap_or("display_disabled");
                json!({"available": false, "reason": reason})
            }
        }
    }
}

pub fn display_session(seam: Option<Value>) -> Value {
    let display = display_availability(seam);
    let available = display["available"].as_bool() == Some(true);
    json!({
        "available": available,
        "url": if available {
            json!("/display/")
        } else {
            Value::Null
        },
        "reason": if available {
            json!("")
        } else {
            display["reason"].clone()
        },
        "expires_at": Value::Null,
        "mode": "novnc",
        "operator": default_operator(),
    })
}

fn read_error(auth: &Map<String, Value>) -> Option<Value> {
    let error = auth.get("read_error")?.as_object()?;
    let code = error
        .get("code")
        .and_then(Value::as_str)
        .filter(|code| !code.is_empty())
        .unwrap_or("unknown");
    let mut detail = format!("认证状态读取失败（{code}）");
    if let Some(message) = error.get("message").and_then(Value::as_str) {
        if !message.is_empty() {
            detail.push('：');
            detail.push_str(message);
        }
    }
    Some(json!({
        "code": code,
        "message": error.get("message").cloned().unwrap_or(Value::Null),
        "detail": detail,
    }))
}

fn display_state_of(
    auth: &Map<String, Value>,
    display_available: bool,
    display_reason: &str,
) -> (String, String, Option<String>) {
    let state = auth.get("state").and_then(Value::as_str);
    let error = auth.get("error").and_then(Value::as_str);
    let primary = auth.get("primary").and_then(Value::as_str);
    let manual_reason = terminal_manual_reason(error, primary);
    let reason = manual_reason.clone().or_else(|| error.map(str::to_string));
    let prefixed_manual = error
        .map(|error| {
            MANUAL_TAKEOVER_PREFIXES
                .iter()
                .any(|prefix| error.starts_with(prefix))
        })
        .unwrap_or(false);
    let read_error = read_error(auth);
    match state {
        Some("valid") => ("valid".to_string(), "登录有效".to_string(), None),
        Some("cancelled") => ("cancelled".to_string(), "本轮登录已取消".to_string(), None),
        Some("timed_out") => ("timed_out".to_string(), "登录窗口已超时".to_string(), None),
        Some("invalid") | Some("failed") => {
            if reason.as_deref() == Some("timeout") {
                return (
                    "timed_out".to_string(),
                    "登录窗口已超时".to_string(),
                    error.map(str::to_string),
                );
            }
            if let Some(manual) = manual_reason {
                let detail = match (error, primary) {
                    (Some(error), Some(primary)) if !primary.is_empty() && primary != error => {
                        Some(format!("{error}；原始原因：{primary}"))
                    }
                    (Some(error), _) => Some(error.to_string()),
                    (None, Some(primary)) => Some(primary.to_string()),
                    (None, None) => Some(manual),
                };
                return ("failed".to_string(), "需要人工接管".to_string(), detail);
            }
            if let (Some(error), Some(primary)) = (error, primary) {
                if !primary.is_empty() && primary != error {
                    return (
                        "failed".to_string(),
                        "登录失败".to_string(),
                        Some(format!("{error}；原始原因：{primary}")),
                    );
                }
            }
            (
                "failed".to_string(),
                "登录失败".to_string(),
                error.map(str::to_string),
            )
        }
        Some("logging_in") | Some("needs_login") => {
            if manual_reason.is_some() && !prefixed_manual {
                return (
                    "failed".to_string(),
                    "需要人工接管".to_string(),
                    error.map(str::to_string),
                );
            }
            if reason.as_deref() == Some("duo_prompt_waiting") {
                // Python `usagesrv.py`: the Duo push wait is its own state, not
                // the generic MFA takeover state.
                return (
                    "waiting_duo".to_string(),
                    "等待本人确认 Duo 推送".to_string(),
                    error.map(str::to_string),
                );
            }
            if state == Some("needs_login") && manual_reason.is_none() {
                // The gateway needs a credential but no login round is running
                // and no manual reason was published: the display has not
                // started a login window. A read error never reaches this arm
                // (state is None there), so an unknown observation cannot be
                // folded into `not_started`.
                return (
                    "not_started".to_string(),
                    "未开始登录".to_string(),
                    error.map(str::to_string),
                );
            }
            if reason
                .as_deref()
                .map(|reason| {
                    reason == "duo_prompt_waiting" || LOGIN_TERMINAL_MANUAL_REASONS.contains(&reason)
                })
                .unwrap_or(false)
            {
                return (
                    "waiting_mfa".to_string(),
                    "等待 Duo 登录（本人操作）".to_string(),
                    error.map(str::to_string),
                );
            }
            (
                "logging_in".to_string(),
                "正在登录".to_string(),
                error.map(str::to_string),
            )
        }
        None => {
            if let Some(read_error) = read_error {
                let detail = read_error["detail"].as_str().unwrap_or_default();
                if !display_available {
                    return (
                        "display_disconnected".to_string(),
                        "显示连接中断".to_string(),
                        Some(format!("{detail}；显示不可用：{display_reason}")),
                    );
                }
                return (
                    "not_started".to_string(),
                    "未开始登录".to_string(),
                    Some(detail.to_string()),
                );
            }
            if !display_available {
                return (
                    "display_disconnected".to_string(),
                    "显示连接中断".to_string(),
                    Some(display_reason.to_string()),
                );
            }
            ("not_started".to_string(), "未开始登录".to_string(), None)
        }
        _ => (
            "unknown".to_string(),
            "状态未知".to_string(),
            error.map(str::to_string),
        ),
    }
}

pub fn display_state(seam: Option<Value>, availability: Option<Value>) -> Value {
    let display = display_availability(availability);
    let auth = seam.unwrap_or_else(|| {
        json!({
            "state": null,
            "label": null,
            "error": null,
            "primary": null,
            "observed_at": null,
            "login_window_deadline_epoch": null,
            "read_error": {
                "code": "auth_observation_failed",
                "message": "unwired",
            },
        })
    });
    let auth = auth.as_object().cloned().unwrap_or_default();
    let (state, label, detail) = display_state_of(
        &auth,
        display["available"].as_bool() == Some(true),
        display["reason"]
            .as_str()
            .unwrap_or("display_proxy_unavailable"),
    );
    json!({
        "available": true,
        "state": state,
        "label": label,
        "detail": detail,
        "observed_at": auth.get("observed_at").cloned().unwrap_or(Value::Null),
        "login_window_deadline_epoch": auth
            .get("login_window_deadline_epoch")
            .cloned()
            .unwrap_or(Value::Null),
        "auth_error": auth.get("read_error").cloned().unwrap_or(Value::Null),
        "auth_primary": auth.get("primary").cloned().unwrap_or(Value::Null),
        "operator": default_operator(),
        "display": display,
    })
}

/// Test seam: the display-state payload from one auth observation.
pub fn display_state_for_tests(seam: Option<Value>, availability: Option<Value>) -> Value {
    display_state(seam, availability)
}
