use std::cell::RefCell;

use serde_json::{json, Value};
use um_desktop::overview::{read_overview, status_line, Card, Overview, Probe, Tone};

struct FakeProbe {
    gateway: Result<Value, String>,
    urls: RefCell<Vec<String>>,
}

impl FakeProbe {
    fn new(gateway: Result<Value, String>) -> Self {
        Self {
            gateway,
            urls: RefCell::new(Vec::new()),
        }
    }
}

impl Probe for FakeProbe {
    fn get_json(&self, url: &str) -> Result<Value, String> {
        self.urls.borrow_mut().push(url.to_string());
        self.gateway.clone()
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

fn supervision() -> Card {
    Card {
        label: "网关监督",
        value: "运行中".to_string(),
        detail: "App 拉起并监督".to_string(),
        tone: Tone::Good,
    }
}

fn read(gateway: Result<Value, String>) -> Overview {
    read_overview(
        &FakeProbe::new(gateway),
        "http://127.0.0.1:7999",
        supervision(),
    )
}

#[test]
fn first_open_without_credentials_shows_not_logged_in() {
    let overview = read(Ok(gateway_health(
        json!({"state": "missing", "label": "未配置"}),
        json!({"state": "unknown"}),
    )));
    assert_eq!(overview.account_label, "尚未登录");
    assert_eq!(overview.auth.value, "尚未登录");
    assert_eq!(overview.account_tone, Tone::Warn);
}

#[test]
fn valid_credentials_show_logged_in_with_the_label() {
    let overview = read(Ok(gateway_health(
        json!({"state": "valid", "label": "someone@um.edu.mo"}),
        json!({"state": "ok", "upstream_status": 200}),
    )));
    assert_eq!(overview.account_label, "已登录");
    assert!(overview.account_detail.contains("someone@um.edu.mo"));
    assert_eq!(overview.account_tone, Tone::Good);
}

#[test]
fn expired_credentials_are_not_reported_as_not_logged_in() {
    let overview = read(Ok(gateway_health(
        json!({"state": "expired"}),
        json!({"state": "ok"}),
    )));
    assert_eq!(overview.account_label, "登录已过期");
    assert_ne!(overview.account_label, "尚未登录");
    assert_eq!(overview.account_tone, Tone::Warn);
}

#[test]
fn gateway_failure_keeps_the_reason_and_never_becomes_normal() {
    let overview = read(Err("本实例网关: connection refused".to_string()));
    assert_eq!(overview.gateway.value, "不可用");
    assert_eq!(overview.gateway.tone, Tone::Bad);
    assert!(overview.gateway.detail.contains("connection refused"));
    assert_eq!(overview.account_label, "未知");
    assert_ne!(overview.account_label, "尚未登录");
    assert_eq!(overview.auth.value, "未知");
    assert!(overview.auth.detail.contains("本实例网关"));
    assert_eq!(overview.upstream.value, "未知");
    assert_eq!(overview.upstream.tone, Tone::Warn);
    assert!(overview
        .failures
        .iter()
        .any(|failure| failure.contains("本实例网关")));
}

#[test]
fn the_supervision_card_is_supplied_by_the_supervisor() {
    let overview = read(Ok(gateway_health(
        json!({"state": "valid"}),
        json!({"state": "ok"}),
    )));
    assert_eq!(overview.supervision, supervision());
}

#[test]
fn missing_model_count_is_unknown_not_zero() {
    let overview = read(Ok(json!({
        "status": "ok",
        "authentication": {"state": "valid"},
        "upstream_reachability": {"state": "ok"},
    })));
    assert!(overview.gateway.detail.contains("未知"));
    assert!(!overview.gateway.detail.contains("模型 0"));
}

#[test]
fn zero_models_is_a_real_zero() {
    let overview = read(Ok(json!({
        "status": "ok",
        "models": 0,
        "authentication": {"state": "valid"},
        "upstream_reachability": {"state": "ok"},
    })));
    assert!(overview.gateway.detail.contains("模型 0"));
}

#[test]
fn unhealthy_gateway_status_is_not_folded_into_running() {
    let overview = read(Ok(json!({
        "status": "degraded",
        "models": 2,
        "authentication": {"state": "valid"},
        "upstream_reachability": {"state": "ok"},
    })));
    assert_eq!(overview.gateway.value, "异常");
    assert_eq!(overview.gateway.tone, Tone::Bad);
    assert!(overview.gateway.detail.contains("degraded"));
}

#[test]
fn unknown_upstream_keeps_its_reason() {
    let overview = read(Ok(gateway_health(
        json!({"state": "valid"}),
        json!({"state": "unknown", "reason": "probe_timeout"}),
    )));
    assert_eq!(overview.upstream.value, "未知");
    assert_eq!(overview.upstream.tone, Tone::Warn);
    assert!(overview.upstream.detail.contains("probe_timeout"));
}

#[test]
fn reachable_upstream_shows_http_status() {
    let overview = read(Ok(gateway_health(
        json!({"state": "valid"}),
        json!({"state": "ok", "upstream_status": 200}),
    )));
    assert_eq!(overview.upstream.value, "可达");
    assert_eq!(overview.upstream.tone, Tone::Good);
    assert!(overview.upstream.detail.contains("HTTP 200"));
}

#[test]
fn reads_only_the_instance_gateway_health_route() {
    let probe = FakeProbe::new(Ok(gateway_health(
        json!({"state": "valid"}),
        json!({"state": "ok"}),
    )));
    let _ = read_overview(&probe, "http://127.0.0.1:7999", supervision());
    let urls = probe.urls.borrow();
    assert_eq!(urls.as_slice(), ["http://127.0.0.1:7999/health"]);
}

#[test]
fn gateway_um_login_and_upstream_are_reported_independently() {
    let overview = read(Ok(gateway_health(
        json!({"state": "missing", "label": "未配置"}),
        json!({"state": "ok", "upstream_status": 200}),
    )));
    assert_eq!(overview.gateway.value, "运行中");
    assert_eq!(overview.account_label, "尚未登录");
    assert_eq!(overview.upstream.value, "可达");
    assert_eq!(overview.upstream.tone, Tone::Good);
}

#[test]
fn status_line_lists_failures_instead_of_claiming_normal() {
    let overview = read(Err("本实例网关: timeout".to_string()));
    let line = status_line(&overview);
    assert!(line.contains("本实例网关"));
    assert!(!line.contains("状态正常"));
}
