//! Slint-level behaviour for the overview UM service card.
//!
//! The card reads the app instance's gateway `/admin/um/services`; a failed read
//! keeps its reason and never invents a PID. The gateway is the UM service itself,
//! so the card has no second start action: a stopped gateway uses the gateway
//! start entry instead.

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
}

impl FakeApi {
    fn with(gets: Vec<Result<Value, String>>) -> Self {
        Self {
            gets: RefCell::new(VecDeque::from(gets)),
            get_urls: RefCell::new(Vec::new()),
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
            "detail": "本机网关正在运行",
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
            "detail": "本机网关已停止",
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
    assert!(!window.get_um_service_gateway_missing());
    assert!(window.get_um_service_checked_at().starts_with("核对于"));
    assert!(
        !window.get_um_service_start_gateway_visible(),
        "a running gateway must not offer the gateway start entry"
    );
}

#[test]
fn the_card_reads_the_gateway_um_services_route() {
    let api = FakeApi::with(vec![Ok(running_payload(7999, 4321))]);
    let view = um_desktop::um_service::read(&api, "http://127.0.0.1:7999", "control-token");

    assert_eq!(
        api.get_urls.borrow().as_slice(),
        ["http://127.0.0.1:7999/admin/um/services"]
    );
    assert_eq!(view.state, "运行中");
}

#[test]
fn a_stopped_service_has_no_second_start_action() {
    let window = service_window();
    let stopped = um_desktop::um_service::parse_services(&stopped_payload(7999))
        .expect("stopped service view");
    um_desktop::um_service::apply_service(&window, &stopped);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));

    assert_eq!(window.get_um_service_state(), "未运行");
    assert_eq!(window.get_um_service_tone(), 2);
    assert!(
        !window.get_um_service_gateway_missing(),
        "a read stopped snapshot is not a missing gateway"
    );
    assert!(
        !window.get_um_service_start_gateway_visible(),
        "a stopped read must not offer a start action; the gateway is the service"
    );
}

#[test]
fn a_failed_read_shows_the_reason_and_never_a_pid() {
    let window = service_window();
    let api = FakeApi::with(vec![Err(
        "http://127.0.0.1:7999/admin/um/services: 无法连接本机网关（连接被拒绝）".to_string(),
    )]);
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
    assert!(!window.get_um_service_gateway_missing());
    assert!(!window.get_um_service_start_gateway_visible());
    assert_eq!(window.get_um_service_checked_at(), "核对时间未知");
}

#[test]
fn a_missing_gateway_points_to_the_existing_gateway_start() {
    let window = service_window();
    let view = um_desktop::um_service::ServiceView::gateway_unavailable("本机网关未运行");
    um_desktop::um_service::apply_service(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));

    assert_eq!(window.get_um_service_state(), "不可用");
    assert_eq!(window.get_um_service_tone(), 2);
    assert!(
        window
            .get_um_service_detail()
            .contains("本机网关未运行，先启动本机网关"),
        "{:?}",
        window.get_um_service_detail()
    );
    assert!(
        window
            .get_um_service_gateway_detail()
            .contains("本机网关未运行"),
        "{:?}",
        window.get_um_service_gateway_detail()
    );
    assert!(window.get_um_service_gateway_missing());
    assert!(window.get_um_service_can_start_gateway());
    assert!(window.get_um_service_start_gateway_visible());
    assert_eq!(window.get_um_service_start_gateway_label(), "启动本机网关");

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
