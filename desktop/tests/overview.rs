use std::cell::RefCell;

use serde_json::{json, Value};
use um_desktop::overview::{read_overview, status_line, Overview, Probe, Tone};

struct FakeProbe {
    gateway: Result<Value, String>,
    console: Result<Value, String>,
    urls: RefCell<Vec<String>>,
}

impl FakeProbe {
    fn new(gateway: Result<Value, String>, console: Result<Value, String>) -> Self {
        Self {
            gateway,
            console,
            urls: RefCell::new(Vec::new()),
        }
    }
}

impl Probe for FakeProbe {
    fn get_json(&self, url: &str) -> Result<Value, String> {
        self.urls.borrow_mut().push(url.to_string());
        if url.starts_with("http://127.0.0.1:8000") {
            self.gateway.clone()
        } else {
            self.console.clone()
        }
    }
}

fn gateway_health(authentication: Value, upstream: Value) -> Value {
    json!({
        "status": "ok",
        "models": 3,
        "authentication": authentication,
        "upstream": {"name": "UM"},
        "upstream_reachability": upstream,
    })
}

fn console_health() -> Value {
    json!({"status": "ok", "service": "usagesrv", "port": 8001})
}

fn read(gateway: Result<Value, String>, console: Result<Value, String>) -> Overview {
    read_overview(
        &FakeProbe::new(gateway, console),
        "http://127.0.0.1:8000",
        "http://127.0.0.1:8001",
    )
}

#[test]
fn first_open_without_credentials_shows_not_logged_in() {
    let overview = read(
        Ok(gateway_health(
            json!({"state": "missing", "label": "未配置"}),
            json!({"state": "unknown"}),
        )),
        Ok(console_health()),
    );
    assert_eq!(overview.account_label, "尚未登录");
    assert_eq!(overview.auth.value, "尚未登录");
    assert_eq!(overview.account_tone, Tone::Warn);
}

#[test]
fn valid_credentials_show_logged_in_with_the_label() {
    let overview = read(
        Ok(gateway_health(
            json!({"state": "valid", "label": "someone@um.edu.mo"}),
            json!({"state": "ok", "upstream_status": 200}),
        )),
        Ok(console_health()),
    );
    assert_eq!(overview.account_label, "已登录");
    assert!(overview.account_detail.contains("someone@um.edu.mo"));
    assert_eq!(overview.account_tone, Tone::Good);
}

#[test]
fn expired_credentials_are_not_reported_as_not_logged_in() {
    let overview = read(
        Ok(gateway_health(
            json!({"state": "expired"}),
            json!({"state": "ok"}),
        )),
        Ok(console_health()),
    );
    assert_eq!(overview.account_label, "登录已过期");
    assert_ne!(overview.account_label, "尚未登录");
    assert_eq!(overview.account_tone, Tone::Warn);
}

#[test]
fn gateway_failure_keeps_the_reason_and_never_becomes_normal() {
    let overview = read(Err("8000: connection refused".to_string()), Ok(console_health()));
    assert_eq!(overview.gateway.value, "不可用");
    assert_eq!(overview.gateway.tone, Tone::Bad);
    assert!(overview.gateway.detail.contains("connection refused"));
    assert_eq!(overview.account_label, "未知");
    assert_ne!(overview.account_label, "尚未登录");
    assert_eq!(overview.auth.value, "未知");
    assert!(overview.auth.detail.contains("8000"));
    assert_eq!(overview.upstream.value, "未知");
    assert_eq!(overview.upstream.tone, Tone::Warn);
    assert!(overview.failures.iter().any(|failure| failure.contains("8000")));
}

#[test]
fn console_failure_keeps_the_reason_and_never_becomes_normal() {
    let overview = read(
        Ok(gateway_health(
            json!({"state": "valid", "label": "a@b"}),
            json!({"state": "ok"}),
        )),
        Err("8001: connection refused".to_string()),
    );
    assert_eq!(overview.console.value, "不可用");
    assert_eq!(overview.console.tone, Tone::Bad);
    assert!(overview.console.detail.contains("connection refused"));
    assert!(overview.failures.iter().any(|failure| failure.contains("8001")));
}

#[test]
fn missing_model_count_is_unknown_not_zero() {
    let overview = read(
        Ok(json!({
            "status": "ok",
            "authentication": {"state": "valid"},
            "upstream_reachability": {"state": "ok"},
        })),
        Ok(console_health()),
    );
    assert!(overview.gateway.detail.contains("未知"));
    assert!(!overview.gateway.detail.contains("模型 0"));
}

#[test]
fn zero_models_is_a_real_zero() {
    let overview = read(
        Ok(json!({
            "status": "ok",
            "models": 0,
            "authentication": {"state": "valid"},
            "upstream_reachability": {"state": "ok"},
        })),
        Ok(console_health()),
    );
    assert!(overview.gateway.detail.contains("模型 0"));
}

#[test]
fn unhealthy_gateway_status_is_not_folded_into_running() {
    let overview = read(
        Ok(json!({
            "status": "degraded",
            "models": 2,
            "authentication": {"state": "valid"},
            "upstream_reachability": {"state": "ok"},
        })),
        Ok(console_health()),
    );
    assert_eq!(overview.gateway.value, "异常");
    assert_eq!(overview.gateway.tone, Tone::Bad);
    assert!(overview.gateway.detail.contains("degraded"));
}

#[test]
fn unknown_upstream_keeps_its_reason() {
    let overview = read(
        Ok(gateway_health(
            json!({"state": "valid"}),
            json!({"state": "unknown", "reason": "probe_timeout"}),
        )),
        Ok(console_health()),
    );
    assert_eq!(overview.upstream.value, "未知");
    assert_eq!(overview.upstream.tone, Tone::Warn);
    assert!(overview.upstream.detail.contains("probe_timeout"));
}

#[test]
fn reachable_upstream_shows_http_status() {
    let overview = read(
        Ok(gateway_health(
            json!({"state": "valid"}),
            json!({"state": "ok", "upstream_status": 200}),
        )),
        Ok(console_health()),
    );
    assert_eq!(overview.upstream.value, "可达");
    assert_eq!(overview.upstream.tone, Tone::Good);
    assert!(overview.upstream.detail.contains("HTTP 200"));
}

#[test]
fn reads_the_health_routes_of_both_services() {
    let probe = FakeProbe::new(
        Ok(gateway_health(
            json!({"state": "valid"}),
            json!({"state": "ok"}),
        )),
        Ok(console_health()),
    );
    let _ = read_overview(&probe, "http://127.0.0.1:8000", "http://127.0.0.1:8001");
    let urls = probe.urls.borrow();
    assert_eq!(
        urls.as_slice(),
        [
            "http://127.0.0.1:8000/health",
            "http://127.0.0.1:8001/health"
        ]
    );
}

#[test]
fn status_line_lists_failures_instead_of_claiming_normal() {
    let overview = read(Err("8000: timeout".to_string()), Err("8001: timeout".to_string()));
    let line = status_line(&overview);
    assert!(line.contains("8000"));
    assert!(line.contains("8001"));
    assert!(!line.contains("状态正常"));
}
