//! Slint-level behaviour for the overview UM service card.
//!
//! The card reads the app instance's gateway `/admin/um/services`; a failed read
//! keeps its reason and never invents a PID, and a failed start stays stopped.

use std::cell::RefCell;
use std::collections::VecDeque;
use std::time::Duration;

use serde_json::{json, Value};
use slint::platform::PointerEventButton;
use slint::{ComponentHandle, LogicalPosition, LogicalSize};
use um_desktop::ui::*;

struct FakeApi {
    gets: RefCell<VecDeque<Result<Value, String>>>,
    get_urls: RefCell<Vec<String>>,
    post_result: Result<Value, String>,
    posted: RefCell<Vec<String>>,
}

impl FakeApi {
    fn with(gets: Vec<Result<Value, String>>, post_result: Result<Value, String>) -> Self {
        Self {
            gets: RefCell::new(VecDeque::from(gets)),
            get_urls: RefCell::new(Vec::new()),
            post_result,
            posted: RefCell::new(Vec::new()),
        }
    }
}

impl um_desktop::um_service::ServiceApi for FakeApi {
    fn get_json(&self, url: &str, _token: &str) -> Result<Value, String> {
        self.get_urls.borrow_mut().push(url.to_string());
        self.gets
            .borrow_mut()
            .pop_front()
            .expect("a queued GET result")
    }

    fn post_json(&self, url: &str, _token: &str, _body: &str) -> Result<Value, String> {
        self.posted.borrow_mut().push(url.to_string());
        self.post_result.clone()
    }
}

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn service_window() -> MainWindow {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(0);
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    window
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

fn running_payload(port: u16, pid: u32) -> Value {
    json!({
        "services": [{
            "key": "chat2api",
            "displayName": "UM 网关",
            "port": port,
            "state": "Running",
            "healthy": true,
            "detail": "本实例网关正在运行",
            "processIds": [pid],
        }],
        "checkedAt": 1_700_000_000_000i64,
        "cached": false,
        "cacheAgeSeconds": Value::Null,
    })
}

fn stopped_payload(port: u16) -> Value {
    json!({
        "services": [{
            "key": "chat2api",
            "displayName": "UM 网关",
            "port": port,
            "state": "Stopped",
            "healthy": false,
            "detail": "本实例网关已停止",
            "processIds": [],
        }],
        "checkedAt": 1_700_000_000_000i64,
        "cached": false,
        "cacheAgeSeconds": Value::Null,
    })
}

#[test]
fn a_running_service_shows_port_pid_and_state() {
    let window = service_window();
    let view = um_desktop::um_service::parse_services(&running_payload(7999, 4321))
        .expect("running service view");
    um_desktop::um_service::apply_service(&window, &view);

    assert_eq!(window.get_um_service_state(), "运行中");
    assert_eq!(window.get_um_service_tone(), 0);
    assert!(
        window.get_um_service_detail().contains("端口 7999"),
        "{:?}",
        window.get_um_service_detail()
    );
    assert!(
        window.get_um_service_detail().contains("PID 4321"),
        "{:?}",
        window.get_um_service_detail()
    );
    assert!(!window.get_um_service_can_start());
    assert!(!window.get_um_service_gateway_missing());
    assert!(window.get_um_service_checked_at().starts_with("核对于"));
    assert!(!window.get_um_service_start_visible());
}

#[test]
fn the_card_reads_the_gateway_um_services_route() {
    let api = FakeApi::with(vec![Ok(running_payload(7999, 4321))], Ok(Value::Null));
    let view = um_desktop::um_service::read(&api, "http://127.0.0.1:7999", "control-token");

    assert_eq!(
        api.get_urls.borrow().as_slice(),
        ["http://127.0.0.1:7999/admin/um/services"]
    );
    assert_eq!(view.state, "运行中");
}

#[test]
fn a_stopped_service_offers_start_and_clicking_starts_it() {
    let window = service_window();
    let stopped = um_desktop::um_service::parse_services(&stopped_payload(7999))
        .expect("stopped service view");
    um_desktop::um_service::apply_service(&window, &stopped);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    assert_eq!(window.get_um_service_state(), "未运行");
    assert_eq!(window.get_um_service_tone(), 2);
    assert!(window.get_um_service_can_start());
    assert!(window.get_um_service_start_visible());
    assert_eq!(window.get_um_service_start_label(), "启动 UM 服务");

    let api = std::rc::Rc::new(FakeApi::with(
        vec![Ok(running_payload(7999, 4321))],
        Ok(json!({"status": "already_running", "started": false})),
    ));
    let weak = window.as_weak();
    let api_for_start = api.clone();
    let previous = stopped.clone();
    window.on_start_um_service(move || {
        let result = um_desktop::um_service::start_and_refresh(
            &*api_for_start,
            "http://127.0.0.1:7999",
            "control-token",
            &previous,
        );
        if let Some(window) = weak.upgrade() {
            um_desktop::um_service::apply_start_result(&window, &result);
        }
    });

    click(
        &window,
        window.get_um_service_start_x() + window.get_um_service_start_width() / 2.0,
        window.get_um_service_start_y() + window.get_um_service_start_height() / 2.0,
    );

    assert_eq!(
        api.posted.borrow().as_slice(),
        ["http://127.0.0.1:7999/admin/um/services/start"]
    );
    assert_eq!(window.get_um_service_state(), "运行中");
    assert!(window.get_um_service_detail().contains("PID 4321"));
    assert!(!window.get_um_service_can_start());
    assert!(!window.get_um_service_start_visible());
    assert_eq!(window.get_um_service_start_status(), "已启动");
    assert_eq!(window.get_um_service_start_tone(), 0);
}

#[test]
fn a_failed_read_shows_the_reason_and_never_a_pid() {
    let window = service_window();
    let api = FakeApi::with(
        vec![Err(
            "http://127.0.0.1:7999/admin/um/services: 无法连接本实例网关（连接被拒绝）".to_string(),
        )],
        Ok(Value::Null),
    );
    let view = um_desktop::um_service::read(&api, "http://127.0.0.1:7999", "control-token");
    um_desktop::um_service::apply_service(&window, &view);

    assert_eq!(window.get_um_service_state(), "读取失败");
    assert_eq!(window.get_um_service_tone(), 2);
    assert!(
        window.get_um_service_detail().contains("连接被拒绝"),
        "{:?}",
        window.get_um_service_detail()
    );
    assert!(
        !window.get_um_service_detail().contains("PID"),
        "a failed read must not invent a PID: {:?}",
        window.get_um_service_detail()
    );
    assert!(!window.get_um_service_can_start());
    assert!(!window.get_um_service_gateway_missing());
    assert!(!window.get_um_service_start_visible());
    assert_eq!(window.get_um_service_checked_at(), "核对时间未知");
}

#[test]
fn a_failed_start_shows_the_reason_and_stays_stopped() {
    let window = service_window();
    let stopped = um_desktop::um_service::parse_services(&stopped_payload(7999))
        .expect("stopped service view");
    let api = FakeApi::with(
        Vec::new(),
        Err(
            "http://127.0.0.1:7999/admin/um/services/start: 本实例网关返回 HTTP 503（service_start_failed）"
                .to_string(),
        ),
    );
    let result = um_desktop::um_service::start_and_refresh(
        &api,
        "http://127.0.0.1:7999",
        "control-token",
        &stopped,
    );
    um_desktop::um_service::apply_start_result(&window, &result);

    assert!(!result.refreshed);
    assert_eq!(window.get_um_service_state(), "未运行");
    assert_eq!(window.get_um_service_tone(), 2);
    assert!(
        window.get_um_service_start_status().contains("启动失败"),
        "{:?}",
        window.get_um_service_start_status()
    );
    assert!(
        window.get_um_service_start_status().contains("503"),
        "{:?}",
        window.get_um_service_start_status()
    );
    assert_eq!(window.get_um_service_start_tone(), 2);
    assert!(window.get_um_service_can_start());
    assert!(window.get_um_service_start_visible());
}

#[test]
fn a_start_that_leaves_the_service_stopped_is_not_reported_as_started() {
    let window = service_window();
    let stopped = um_desktop::um_service::parse_services(&stopped_payload(7999))
        .expect("stopped service view");
    let api = FakeApi::with(
        vec![Ok(stopped_payload(7999))],
        Ok(json!({"status": "already_running", "started": false})),
    );
    let result = um_desktop::um_service::start_and_refresh(
        &api,
        "http://127.0.0.1:7999",
        "control-token",
        &stopped,
    );
    um_desktop::um_service::apply_start_result(&window, &result);

    assert!(result.refreshed);
    assert_eq!(window.get_um_service_state(), "未运行");
    assert!(
        window.get_um_service_start_status().contains("启动后状态"),
        "{:?}",
        window.get_um_service_start_status()
    );
    assert_eq!(window.get_um_service_start_tone(), 1);
    assert!(window.get_um_service_can_start());
}

#[test]
fn a_start_without_a_status_read_is_not_reported_as_running() {
    let window = service_window();
    let stopped = um_desktop::um_service::parse_services(&stopped_payload(7999))
        .expect("stopped service view");
    let api = FakeApi::with(
        vec![Err("状态读取超时".to_string())],
        Ok(json!({"status": "already_running", "started": false})),
    );
    let result = um_desktop::um_service::start_and_refresh(
        &api,
        "http://127.0.0.1:7999",
        "control-token",
        &stopped,
    );
    um_desktop::um_service::apply_start_result(&window, &result);

    assert!(!result.refreshed);
    assert_eq!(window.get_um_service_state(), "未运行");
    assert!(
        window
            .get_um_service_start_status()
            .contains("状态刷新失败"),
        "{:?}",
        window.get_um_service_start_status()
    );
    assert!(
        window
            .get_um_service_start_status()
            .contains("状态读取超时"),
        "{:?}",
        window.get_um_service_start_status()
    );
    assert_eq!(window.get_um_service_start_tone(), 1);
    assert!(window.get_um_service_can_start());
}

#[test]
fn a_missing_gateway_points_to_the_existing_gateway_start() {
    let window = service_window();
    let view = um_desktop::um_service::ServiceView::gateway_unavailable("本实例网关未运行");
    um_desktop::um_service::apply_service(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));

    assert_eq!(window.get_um_service_state(), "不可用");
    assert_eq!(window.get_um_service_tone(), 2);
    assert!(
        window
            .get_um_service_detail()
            .contains("网关未运行，先启动网关"),
        "{:?}",
        window.get_um_service_detail()
    );
    assert!(
        window
            .get_um_service_gateway_detail()
            .contains("本实例网关未运行"),
        "{:?}",
        window.get_um_service_gateway_detail()
    );
    assert!(!window.get_um_service_can_start());
    assert!(window.get_um_service_gateway_missing());
    assert!(window.get_um_service_can_start_gateway());
    assert!(!window.get_um_service_start_visible());
    assert!(window.get_um_service_start_gateway_visible());
    assert_eq!(window.get_um_service_start_gateway_label(), "启动网关");

    let invoked = std::rc::Rc::new(std::cell::Cell::new(false));
    let flag = invoked.clone();
    window.on_restart_gateway(move || {
        flag.set(true);
    });
    click(
        &window,
        window.get_um_service_start_gateway_x() + window.get_um_service_start_gateway_width() / 2.0,
        window.get_um_service_start_gateway_y()
            + window.get_um_service_start_gateway_height() / 2.0,
    );
    assert!(
        invoked.get(),
        "the gateway entry must run the existing restart"
    );
}
