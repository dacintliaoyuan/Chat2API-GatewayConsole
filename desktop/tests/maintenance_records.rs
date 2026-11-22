//! Maintenance-operation records (#162): three read states plus step expansion.
//!
//! A gateway without a record source says 不适用 and draws no table; a source
//! with rows lists them and expands their steps; a failed read keeps its reason
//! instead of folding into an empty table.

use std::cell::RefCell;
use std::time::Duration;

use serde_json::{json, Value};
use slint::platform::PointerEventButton;
use slint::{ComponentHandle, LogicalPosition, LogicalSize, Model};
use um_desktop::maintenance;
use um_desktop::overview::Tone;
use um_desktop::ui::*;

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn window() -> MainWindow {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(0);
    window.window().set_size(LogicalSize::new(1060.0, 900.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    window
}

fn open_menu(window: &MainWindow) {
    window.set_maintenance_menu_open(true);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
}

fn apply(window: &MainWindow, payload: Value) {
    let view = maintenance::records_from(&payload);
    maintenance::apply_records(window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
}

fn unavailable_payload() -> Value {
    json!({
        "status": "unavailable",
        "error": "maintenance_source_unavailable",
        "reason": "本机实例不运行 8001 控制台，没有 console-state.sqlite3 维护操作记录来源",
        "rows": [],
        "scope": "本机实例维护操作记录：不适用",
        "earlier": Value::Null,
        "coverage": {
            "status": "unavailable",
            "error": "maintenance_source_unavailable",
            "source": "console-state.sqlite3",
            "bounded": true,
            "note": "无来源不是合法空列表；不伪造空表",
        },
    })
}

fn source_payload() -> Value {
    json!({
        "rows": [
            {
                "key": "op-1",
                "action": "account.um_verify",
                "state": "succeeded",
                "at": 1_763_861_405_000i64,
                "finishedAt": 1_763_861_407_500i64,
                "steps": [
                    {"at": 1_763_861_405_500i64, "note": "开始核验"},
                    {"at": 1_763_861_406_200i64, "note": "上游确认登录有效"},
                ],
                "result": "已确认当前登录有效",
            },
            {
                "key": "op-2",
                "action": "account.login",
                "state": "mystery",
                "at": 1_763_861_400_000i64,
                "finishedAt": Value::Null,
                "steps": [],
                "result": "登录窗口打开中",
            },
        ],
        "scope": "原操作记录中的 UM 维护项；只读最近 200 条",
        "earlier": Value::Null,
        "coverage": {"status": "ok"},
    })
}

#[test]
fn a_source_with_records_lists_each_row_with_state_and_elapsed() {
    let window = window();
    open_menu(&window);
    apply(&window, source_payload());

    assert!(!window.get_maintenance_records_not_applicable());
    assert!(window.get_maintenance_table_visible());
    assert_eq!(window.get_maintenance_record_count(), 2);
    assert_eq!(
        window.get_maintenance_records_status(),
        "原操作记录中的 UM 维护项；只读最近 200 条"
    );

    let rows = window.get_maintenance_records();
    let first = rows.row_data(0).expect("first row");
    assert_eq!(first.at.to_string(), "11-23 09:30:05");
    assert_eq!(first.action.to_string(), "核验登录");
    assert_eq!(first.state.to_string(), "成功");
    assert_eq!(first.state_tone, 0);
    assert_eq!(first.result.to_string(), "已确认当前登录有效");
    assert_eq!(first.took.to_string(), "2.5 秒");

    let second = rows.row_data(1).expect("second row");
    assert_eq!(second.at.to_string(), "11-23 09:30:00");
    assert_eq!(second.action.to_string(), "检查并按需登录");
    assert_eq!(
        second.state.to_string(),
        "未知",
        "an unknown state is labeled 未知, never blank"
    );
    assert_eq!(second.state_tone, 3);
    assert_eq!(second.took.to_string(), "—");
}

fn click(window: &MainWindow, x: f32, y: f32) {
    let position = LogicalPosition::new(x, y);
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::PointerMoved { position });
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::PointerPressed {
            position,
            button: PointerEventButton::Left,
        });
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::PointerReleased {
            position,
            button: PointerEventButton::Left,
        });
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
}

fn click_first_record(window: &MainWindow) {
    let x = window.get_maintenance_records_x() + window.get_maintenance_records_width() / 2.0;
    let y = window.get_maintenance_records_y() + window.get_maintenance_record_row_height() / 2.0;
    click(window, x, y);
}

#[test]
fn clicking_a_record_expands_its_steps_and_clicking_again_collapses_them() {
    let window = window();
    open_menu(&window);
    apply(&window, source_payload());
    assert_eq!(window.get_maintenance_record_expanded(), -1);
    assert_eq!(window.get_maintenance_record_step_count(), 0);

    click_first_record(&window);
    assert_eq!(window.get_maintenance_record_expanded(), 0);
    assert_eq!(window.get_maintenance_record_step_count(), 2);
    assert_eq!(
        window.get_maintenance_record_step_first().to_string(),
        "09:30:05 开始核验"
    );
    assert_eq!(
        window.get_maintenance_record_step_second().to_string(),
        "09:30:06 上游确认登录有效"
    );

    click_first_record(&window);
    assert_eq!(window.get_maintenance_record_expanded(), -1);
    assert_eq!(window.get_maintenance_record_step_count(), 0);
}

struct FakeApi {
    result: Result<Value, String>,
    calls: RefCell<Vec<(String, String)>>,
}

impl FakeApi {
    fn ok(payload: Value) -> Self {
        Self {
            result: Ok(payload),
            calls: RefCell::new(Vec::new()),
        }
    }

    fn error(reason: &str) -> Self {
        Self {
            result: Err(reason.to_string()),
            calls: RefCell::new(Vec::new()),
        }
    }
}

impl maintenance::RecordsApi for FakeApi {
    fn get_json(&self, url: &str, token: &str) -> Result<Value, String> {
        self.calls
            .borrow_mut()
            .push((url.to_string(), token.to_string()));
        self.result.clone()
    }
}

#[test]
fn the_read_targets_the_gateway_maintenance_endpoint_with_the_control_token() {
    let api = FakeApi::ok(unavailable_payload());
    let view = maintenance::read_records(&api, "http://127.0.0.1:7999", "secret-token");

    assert_eq!(
        api.calls.borrow().as_slice(),
        [(
            "http://127.0.0.1:7999/admin/um/maintenance".to_string(),
            "secret-token".to_string()
        )]
    );
    assert!(view.not_applicable);
    assert_eq!(
        view.status,
        "不适用（本机实例无维护记录来源）"
    );
}

#[test]
fn a_failed_read_keeps_its_reason_and_is_never_an_empty_table() {
    let window = window();
    open_menu(&window);
    let api = FakeApi::error("http://127.0.0.1:7999/admin/um/maintenance: connection refused");
    let view = maintenance::read_records(&api, "http://127.0.0.1:7999", "secret-token");

    assert!(view.failed);
    assert!(!view.not_applicable);
    assert!(!view.empty);
    assert_eq!(view.tone, Tone::Bad);
    assert!(view.status.contains("读取失败"), "{}", view.status);
    assert!(
        view.status.contains("connection refused"),
        "the read's own reason stays visible: {}",
        view.status
    );
    assert!(view.rows.is_empty());

    maintenance::apply_records(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    assert!(!window.get_maintenance_table_visible());
    assert_eq!(window.get_maintenance_record_count(), 0);
    assert_eq!(
        window.get_maintenance_records_status(),
        view.status
    );
    assert_eq!(window.get_maintenance_records_tone(), 2);
}

#[test]
fn a_malformed_payload_is_a_read_failure_not_an_empty_table() {
    let view = maintenance::records_from(&json!({"rows": "nope"}));
    assert!(view.failed);
    assert!(!view.not_applicable);
    assert!(!view.empty);
    assert_eq!(view.tone, Tone::Bad);
    assert!(view.status.contains("格式无效"), "{}", view.status);
    assert!(view.rows.is_empty());
}

#[test]
fn a_source_with_no_records_in_range_says_so_without_a_table() {
    let window = window();
    open_menu(&window);
    let view = maintenance::records_from(&json!({
        "rows": [],
        "scope": "原操作记录中的 UM 维护项；只读最近 200 条",
        "earlier": Value::Null,
        "coverage": {"status": "ok"},
    }));

    assert!(!view.not_applicable);
    assert!(!view.failed);
    assert!(view.empty);
    assert_eq!(view.status, "读取范围内没有维护操作记录。");

    maintenance::apply_records(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    assert!(!window.get_maintenance_table_visible());
    assert_eq!(window.get_maintenance_record_count(), 0);
}

#[test]
fn a_gateway_without_a_record_source_says_not_applicable_and_draws_no_table() {
    let window = window();
    open_menu(&window);
    apply(&window, unavailable_payload());

    assert!(window.get_maintenance_records_not_applicable());
    assert_eq!(
        window.get_maintenance_records_status(),
        "不适用（本机实例无维护记录来源）"
    );
    assert!(!window.get_maintenance_table_visible());
    assert_eq!(window.get_maintenance_record_count(), 0);
    assert!(
        window
            .get_maintenance_records_detail()
            .contains("console-state.sqlite3"),
        "the gateway's concrete reason stays visible: {}",
        window.get_maintenance_records_detail()
    );
}
