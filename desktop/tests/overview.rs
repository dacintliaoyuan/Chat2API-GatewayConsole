use std::cell::RefCell;

use serde_json::{json, Value};
use um_desktop::overview::{
    read_fault_history, read_overview, status_line, Card, Overview, OverviewPoller, Probe, Tone,
};

mod common;
use common::TempDir;

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

#[test]
fn the_overview_reader_reads_only_on_a_new_epoch_and_after_resume() {
    let mut poller = OverviewPoller::new();
    assert!(
        poller.should_read(false, 0),
        "the initial read still loads the cards"
    );
    assert!(
        !poller.should_read(false, 0),
        "the reader never polls on its own once the first read landed"
    );
    assert!(
        !poller.should_read(true, 0),
        "a hidden or minimized window pauses the reader"
    );
    assert!(
        !poller.should_read(true, 1),
        "an epoch queued while hidden must not apply overview data"
    );
    assert!(
        poller.should_read(false, 1),
        "showing the window reads immediately even if the epoch already moved"
    );
    assert!(
        !poller.should_read(false, 1),
        "the resume read happens once"
    );
    assert!(
        poller.should_read(false, 2),
        "a manual refresh or an auto-refresh tick reads once"
    );
}

#[test]
fn a_missing_fault_record_is_the_empty_state_not_an_empty_table() {
    let dir = TempDir::new("faults-empty");
    let history = read_fault_history(&dir.path().join("upstream-faults.json"));
    assert_eq!(history.status, "当前无上游故障");
    assert_eq!(history.tone, Tone::Good);
    assert_eq!(history.check, "尚无上游核验");
    assert!(history.active.is_none());
    assert!(history.history.is_empty());
}

#[test]
fn an_active_fault_shows_the_reason_failure_count_and_next_check() {
    let dir = TempDir::new("faults-active");
    let path = dir.path().join("upstream-faults.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&json!({
            "schema_version": 1,
            "active": {
                "id": "fault-1",
                "started_epoch": 1_700_000_000.0,
                "last_epoch": 1_700_000_060.0,
                "source": "background_probe",
                "failures": 2,
                "classification": "upstream_5xx",
                "upstream_status": 503,
                "summary": "no healthy upstream",
            },
            "history": [],
            "last_check": {
                "epoch": 1_700_000_060.0,
                "source": "background_probe",
                "result": "upstream_5xx",
                "upstream_status": 503,
            },
            "next_check_epoch": 1_700_000_120.0,
        }))
        .expect("record json"),
    )
    .expect("write record");

    let history = read_fault_history(&path);
    assert_eq!(history.status, "当前有上游故障");
    assert_eq!(history.tone, Tone::Bad);
    let active = history.active.expect("active fault");
    assert!(active.title.contains("上游 5xx"), "{:?}", active.title);
    assert!(active.detail.contains("HTTP 503"), "{:?}", active.detail);
    assert!(
        active.detail.contains("no healthy upstream"),
        "{:?}",
        active.detail
    );
    assert!(active.detail.contains("失败 2 次"), "{:?}", active.detail);
    assert!(active.detail.contains("尚未恢复"), "{:?}", active.detail);
    assert!(active.evidence.is_empty());
    assert!(history.check.contains("最近核验"), "{:?}", history.check);
    assert!(history.check.contains("下次复查"), "{:?}", history.check);
    assert!(history.history.is_empty());
}

#[test]
fn recovered_fault_history_keeps_its_recovery_evidence() {
    let dir = TempDir::new("faults-history");
    let path = dir.path().join("upstream-faults.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&json!({
            "schema_version": 1,
            "active": Value::Null,
            "history": [{
                "id": "fault-0",
                "started_epoch": 1_699_999_900.0,
                "last_epoch": 1_699_999_905.0,
                "source": "business_request",
                "failures": 1,
                "classification": "timeout",
                "upstream_status": Value::Null,
                "summary": Value::Null,
                "recovered_epoch": 1_699_999_910.0,
                "recovery_source": "business_request",
                "recovery_status": 200,
                "recovery_detail": "chat_completion",
            }],
            "last_check": {
                "epoch": 1_699_999_910.0,
                "source": "business_request",
                "result": "valid",
                "upstream_status": 200,
            },
            "next_check_epoch": Value::Null,
        }))
        .expect("record json"),
    )
    .expect("write record");

    let history = read_fault_history(&path);
    assert_eq!(history.status, "当前无上游故障");
    assert!(history.active.is_none());
    assert_eq!(history.history.len(), 1);
    let row = &history.history[0];
    assert!(row.title.contains("已恢复"), "{:?}", row.title);
    assert!(row.title.contains("超时"), "{:?}", row.title);
    assert!(row.detail.contains("失败 1 次"), "{:?}", row.detail);
    assert!(
        row.detail.contains("恢复于") || row.detail.contains(" – "),
        "{:?}",
        row.detail
    );
    assert!(row.evidence.contains("恢复证据"), "{:?}", row.evidence);
    assert!(row.evidence.contains("业务请求"), "{:?}", row.evidence);
    assert!(row.evidence.contains("HTTP 200"), "{:?}", row.evidence);
    assert!(
        row.evidence.contains("chat_completion"),
        "{:?}",
        row.evidence
    );
}

#[test]
fn an_unreadable_fault_record_keeps_its_reason_and_never_claims_no_fault() {
    let dir = TempDir::new("faults-unreadable");
    let path = dir.path().join("upstream-faults.json");
    std::fs::write(&path, [0xff, 0xfe, 0x00]).expect("write unreadable record");
    let history = read_fault_history(&path);
    assert!(
        history.status.contains("读取上游故障记录失败"),
        "{:?}",
        history.status
    );
    assert_ne!(history.status, "当前无上游故障");
    assert_eq!(history.tone, Tone::Bad);
    assert!(history.active.is_none());
    assert!(history.history.is_empty());
    assert!(history.check.is_empty());

    let malformed = dir.path().join("malformed-upstream-faults.json");
    std::fs::write(&malformed, b"{not json").expect("write malformed record");
    let history = read_fault_history(&malformed);
    assert!(
        history.status.contains("不是有效 JSON"),
        "{:?}",
        history.status
    );
    assert_ne!(history.status, "当前无上游故障");
    assert_eq!(history.tone, Tone::Bad);

    let wrong_shape = dir.path().join("wrong-shape-upstream-faults.json");
    std::fs::write(&wrong_shape, b"[]").expect("write wrong shape record");
    let history = read_fault_history(&wrong_shape);
    assert!(history.status.contains("格式无效"), "{:?}", history.status);
    assert_ne!(history.status, "当前无上游故障");
    assert_eq!(history.tone, Tone::Bad);
}

mod main_ui {
    slint::include_modules!();

    pub fn register_chart(window: &MainWindow) {
        slintplot::register!(window);
    }
}

use slint::{ComponentHandle, Model, ModelRc, VecModel};
use std::rc::Rc;
use um_desktop::overview::{
    apply_fault_history, FaultHistory, FaultRow as HistoryRow, FaultSurface,
};

impl FaultSurface for main_ui::MainWindow {
    fn set_fault_status(&self, status: &str) {
        self.set_upstream_fault_status(status.into());
    }

    fn set_fault_tone(&self, tone: Tone) {
        let tone = match tone {
            Tone::Good => 0,
            Tone::Warn => 1,
            Tone::Bad => 2,
            Tone::Neutral => 3,
        };
        self.set_upstream_fault_tone(tone);
    }

    fn set_fault_check(&self, check: &str) {
        self.set_upstream_fault_check(check.into());
    }

    fn set_fault_active(&self, active: Option<&HistoryRow>) {
        self.set_upstream_fault_active(active.is_some());
        let (title, detail) = match active {
            Some(row) => (row.title.as_str(), row.detail.as_str()),
            None => ("", ""),
        };
        self.set_upstream_fault_active_title(title.into());
        self.set_upstream_fault_active_detail(detail.into());
    }

    fn set_fault_history(&self, rows: &[HistoryRow]) {
        let rows: Vec<main_ui::FaultRow> = rows
            .iter()
            .map(|row| main_ui::FaultRow {
                title: row.title.as_str().into(),
                detail: row.detail.as_str().into(),
                evidence: row.evidence.as_str().into(),
            })
            .collect();
        self.set_upstream_fault_history(ModelRc::from(Rc::new(VecModel::from(rows))));
    }
}

thread_local! {
    static FAULT_INIT: std::sync::Once = std::sync::Once::new();
}

fn fault_window() -> main_ui::MainWindow {
    FAULT_INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = main_ui::MainWindow::new().expect("create MainWindow");
    main_ui::register_chart(&window);
    window.set_page(0);
    window
        .window()
        .set_size(slint::LogicalSize::new(1060.0, 900.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
}

fn fault_history(active: Option<HistoryRow>, history: Vec<HistoryRow>) -> FaultHistory {
    FaultHistory {
        status: if active.is_some() {
            "当前有上游故障".to_string()
        } else {
            "当前无上游故障".to_string()
        },
        tone: if active.is_some() {
            Tone::Bad
        } else {
            Tone::Good
        },
        check: "尚无上游核验".to_string(),
        active,
        history,
    }
}

#[test]
fn the_empty_fault_card_says_no_fault_and_draws_no_table() {
    let window = fault_window();
    apply_fault_history(&window, &fault_history(None, Vec::new()));
    assert_eq!(window.get_upstream_fault_status(), "当前无上游故障");
    assert_eq!(window.get_upstream_fault_tone(), 0);
    assert_eq!(window.get_upstream_fault_check(), "尚无上游核验");
    assert!(!window.get_upstream_fault_active());
    assert_eq!(window.get_upstream_fault_history().row_count(), 0);
    assert!(!window.get_upstream_fault_evidence_toggle_visible());
    assert!(window.get_upstream_fault_card_height() > 0.0);
}

#[test]
fn the_active_fault_card_shows_the_reason_failures_and_next_check() {
    let window = fault_window();
    let active = HistoryRow {
        title: "当前异常 · 上游 5xx".to_string(),
        detail: "上游 5xx HTTP 503 · no healthy upstream · 失败 2 次 · 开始 11-22 10:00:00 · 最近 11-22 10:01:00 · 尚未恢复 · 来源 后台探测".to_string(),
        evidence: String::new(),
    };
    let mut history = fault_history(Some(active), Vec::new());
    history.check =
        "最近核验 11-22 10:01:00 · 上游 5xx HTTP 503 · 后台探测 · 下次复查 11-22 10:03:00"
            .to_string();
    apply_fault_history(&window, &history);
    assert_eq!(window.get_upstream_fault_status(), "当前有上游故障");
    assert_eq!(window.get_upstream_fault_tone(), 2);
    assert!(window.get_upstream_fault_active());
    assert_eq!(
        window.get_upstream_fault_active_title(),
        "当前异常 · 上游 5xx"
    );
    assert!(window
        .get_upstream_fault_active_detail()
        .contains("失败 2 次"));
    assert!(window
        .get_upstream_fault_active_detail()
        .contains("尚未恢复"));
    assert!(window.get_upstream_fault_check().contains("下次复查"));
    assert!(!window.get_upstream_fault_evidence_toggle_visible());
}

fn click(window: &main_ui::MainWindow, x: f32, y: f32) {
    let position = slint::LogicalPosition::new(x, y);
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::PointerMoved { position });
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::PointerPressed {
            position,
            button: slint::platform::PointerEventButton::Left,
        });
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::PointerReleased {
            position,
            button: slint::platform::PointerEventButton::Left,
        });
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
}

#[test]
fn recovered_history_and_its_evidence_expand_from_the_card() {
    let window = fault_window();
    let row = HistoryRow {
        title: "已恢复 · 超时".to_string(),
        detail: "超时 · 失败 1 次 · 开始 11-22 09:00:00 · 最近 11-22 09:00:05 · 恢复于 11-22 09:00:10 · 来源 业务请求".to_string(),
        evidence: "恢复证据 业务请求 HTTP 200 · chat_completion · 恢复于 11-22 09:00:10"
            .to_string(),
    };
    apply_fault_history(&window, &fault_history(None, vec![row]));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    let model = window.get_upstream_fault_history();
    assert_eq!(model.row_count(), 1);
    let first = model.row_data(0).expect("history row");
    assert_eq!(first.title, "已恢复 · 超时");
    assert!(first.detail.contains("失败 1 次"));
    assert!(first.evidence.contains("恢复证据"));
    assert!(first.evidence.contains("HTTP 200"));
    assert!(window.get_upstream_fault_evidence_toggle_visible());
    assert!(!window.get_upstream_fault_evidence_shown());
    let before = window.get_upstream_fault_card_height();
    let x = window.get_upstream_fault_evidence_toggle_x()
        + window.get_upstream_fault_evidence_toggle_width() / 2.0;
    let y = window.get_upstream_fault_evidence_toggle_y()
        + window.get_upstream_fault_evidence_toggle_height() / 2.0;
    click(&window, x, y);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(window.get_show_fault_evidence());
    assert!(window.get_upstream_fault_evidence_shown());
    let after = window.get_upstream_fault_card_height();
    assert!(
        after > before,
        "expanding the evidence must grow the card: {after} <= {before}"
    );
}

#[test]
fn a_malformed_probe_record_is_not_reported_as_never_checked() {
    let dir = TempDir::new("faults-malformed-check");
    let path = dir.path().join("upstream-faults.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&json!({
            "schema_version": 1,
            "active": Value::Null,
            "history": [],
            "last_check": "not-an-object",
            "next_check_epoch": "not-a-time",
        }))
        .expect("record json"),
    )
    .expect("write record");

    let history = read_fault_history(&path);
    assert_eq!(history.status, "当前无上游故障");
    assert!(
        history.check.contains("最近核验记录格式无效"),
        "{:?}",
        history.check
    );
    assert!(
        history.check.contains("下次复查时间格式无效"),
        "{:?}",
        history.check
    );
}
