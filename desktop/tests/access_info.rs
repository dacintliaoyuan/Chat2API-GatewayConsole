//! Slint-level behaviour for the settings page's access info (#152).
//!
//! The tests exercise the gateway's `/admin/connection` read through the visible
//! settings rows: the single address row, the interface type, the hints and the
//! failure/not-running copy. They never assert private helpers.

use std::cell::RefCell;
use std::rc::Rc;

use serde_json::json;
use slint::Model;
use um_desktop::access_info::{
    access_failure, apply_access_info, parse_connection, AccessInfoReader,
};
use um_desktop::ui::*;

thread_local! {
    static UI_INIT: std::sync::Once = std::sync::Once::new();
}

fn init_ui() {
    UI_INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
}

fn settings_window() -> um_desktop::ui::MainWindow {
    use slint::{ComponentHandle, LogicalSize};

    init_ui();
    let window = um_desktop::ui::MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(2);
    window.window().set_size(LogicalSize::new(1060.0, 1400.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
}

fn click<T: slint::ComponentHandle>(component: &T, x: f32, y: f32) {
    use slint::platform::{PointerEventButton, WindowEvent};
    use slint::LogicalPosition;

    let position = LogicalPosition::new(x, y);
    let window = component.window();
    window.dispatch_event(WindowEvent::PointerMoved { position });
    window.dispatch_event(WindowEvent::PointerPressed {
        position,
        button: PointerEventButton::Left,
    });
    window.dispatch_event(WindowEvent::PointerReleased {
        position,
        button: PointerEventButton::Left,
    });
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
}

fn connection() -> serde_json::Value {
    json!({
        "ok": true,
        "service": "chat2api",
        "display_name": "UM 网关",
        "port": 8123,
        "health_path": "/health",
        "base_url": "http://127.0.0.1:8123/v1",
        "protocol": "openai-compatible",
        "protocol_path": "/v1",
        "interface_type": "openai-compatible",
        "runtime_state": "running",
        "runtime_healthy": true,
        "hints": [
            "在客户端里把上面的地址填成 OpenAI 兼容接口的 Base URL。",
            "本机回环实例当前不要求调用密钥。",
        ],
        "reason": "",
    })
}

#[test]
fn the_gateway_connection_fills_the_address_type_and_hints() {
    let window = settings_window();
    let view = parse_connection(&connection()).expect("connection");
    apply_access_info(&window, &view);

    assert_eq!(
        window.get_gateway_address_text(),
        "http://127.0.0.1:8123",
        "接入信息必须复用唯一的地址行并显示网关报告的地址"
    );
    assert_eq!(
        window.get_access_interface_type(),
        "OpenAI 兼容（/v1）",
        "接口类型必须显示网关报告的协议与路径"
    );
    let hints: Vec<String> = window
        .get_access_hints()
        .iter()
        .map(|hint| hint.to_string())
        .collect();
    assert_eq!(hints.len(), 2, "hints 必须逐条显示：{hints:?}");
    assert!(hints[0].contains("Base URL"), "{hints:?}");
    assert!(hints[1].contains("不要求调用密钥"), "{hints:?}");
    assert!(
        window.get_access_status().contains("接入信息"),
        "{}",
        window.get_access_status()
    );
    assert!(window.get_access_address_note().is_empty());
    assert!(window.get_access_address_available());
    assert!(window.get_copy_base_enabled());
    assert!(window.get_copy_v1_enabled());
    assert_eq!(view.full_base_url, "http://127.0.0.1:8123/v1");
    assert!(view.connected, "读取成功才表示网关已确认连接");
}

#[test]
fn copying_the_access_address_receives_the_gateways_full_base_url() {
    let window = settings_window();
    // A stale configured address must not win over the gateway's bound port.
    window.set_active_address("127.0.0.1:8001".into());
    let view = parse_connection(&connection()).expect("connection");
    apply_access_info(&window, &view);

    let copied = Rc::new(RefCell::new(Vec::<String>::new()));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| copied.borrow_mut().push(text.to_string())
    });

    click(
        &window,
        window.get_copy_base_x() + window.get_copy_base_width() / 2.0,
        window.get_copy_base_y() + window.get_copy_base_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().last().map(String::as_str),
        Some("http://127.0.0.1:8123"),
        "复制地址必须收到网关报告的完整 base URL"
    );

    click(
        &window,
        window.get_copy_v1_x() + window.get_copy_v1_width() / 2.0,
        window.get_copy_v1_y() + window.get_copy_v1_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().last().map(String::as_str),
        Some("http://127.0.0.1:8123/v1"),
        "复制 /v1 必须收到网关报告的完整 base URL 加 /v1"
    );
}

#[test]
fn the_access_info_reuses_the_single_address_row_and_puts_the_type_below_it() {
    let window = settings_window();
    let view = parse_connection(&connection()).expect("connection");
    apply_access_info(&window, &view);

    assert_eq!(
        window.get_gateway_address_text(),
        "http://127.0.0.1:8123",
        "地址必须只占现有的一行"
    );
    assert!(
        window.get_access_type_row_y()
            >= window.get_gateway_address_y() + window.get_gateway_address_height() - 1.0,
        "接口类型必须排在唯一的地址行下方"
    );
    assert!(
        window.get_access_hints_y() >= window.get_access_type_row_y() - 1.0,
        "hints 必须排在接口类型下方"
    );
}

#[test]
fn a_failed_read_shows_the_reason_and_no_address() {
    let window = settings_window();
    window.set_active_address("127.0.0.1:8001".into());
    let view = access_failure(
        "http://127.0.0.1:8001: 本实例网关返回 HTTP 503（connection_info_unavailable）".to_string(),
        true,
        8001,
    );
    apply_access_info(&window, &view);

    assert!(
        window.get_access_status().contains("读取接入信息失败"),
        "{}",
        window.get_access_status()
    );
    assert!(
        window.get_access_status().contains("503"),
        "失败原因必须显示在状态行：{}",
        window.get_access_status()
    );
    assert_eq!(
        window.get_gateway_address_text(),
        "地址不可用",
        "读面失败时不能显示伪造的地址"
    );
    assert!(!window.get_access_address_available());
    assert!(!window.get_copy_base_enabled(), "读面失败时复制必须禁用");
    assert!(!window.get_copy_v1_enabled(), "读面失败时复制必须禁用");
    assert!(!view.connected, "读面失败不能假装已连接");
    assert!(
        view.full_base_url.is_empty(),
        "读面失败不能伪造完整 base URL"
    );
    assert!(
        window.get_access_interface_type().contains("不可用"),
        "{}",
        window.get_access_interface_type()
    );
    assert_eq!(
        window.get_access_hints().row_count(),
        0,
        "读面失败时不能伪造 hints"
    );
}

#[test]
fn a_gateway_that_is_not_running_shows_the_configured_address_with_a_note() {
    let window = settings_window();
    let view = access_failure("本实例网关未运行".to_string(), false, 8123);
    apply_access_info(&window, &view);

    assert_eq!(
        window.get_gateway_address_text(),
        "http://127.0.0.1:8123",
        "网关未运行时必须显示当前端口推导的地址"
    );
    assert_eq!(
        window.get_access_address_note_text(),
        "网关未运行，地址为当前配置",
        "推导地址必须明确标注不是已连接"
    );
    assert!(window.get_access_address_note_visible());
    assert!(
        window.get_access_status().contains("网关未运行"),
        "{}",
        window.get_access_status()
    );
    assert!(
        !window.get_access_status().contains("已连接"),
        "未运行的网关不能假装已连接：{}",
        window.get_access_status()
    );
    assert!(window.get_access_address_available());
    assert!(window.get_copy_base_enabled());
    assert!(window.get_copy_v1_enabled());
    assert!(!view.connected, "网关未运行不能假装已连接");
    assert_eq!(view.full_base_url, "http://127.0.0.1:8123/v1");
    assert!(
        window.get_access_interface_type().contains("不可用"),
        "{}",
        window.get_access_interface_type()
    );
    assert_eq!(
        window.get_access_hints().row_count(),
        0,
        "未运行时不能伪造 hints"
    );
}

#[test]
fn a_connection_without_interface_type_or_hints_shows_the_reason() {
    let view = parse_connection(&json!({
        "ok": true,
        "port": 8123,
        "base_url": "http://127.0.0.1:8123/v1",
        "protocol_path": "/v1",
        "reason": "",
    }))
    .expect("connection");

    assert!(
        view.interface_type.contains("未提供"),
        "缺失接口类型必须显示原因：{}",
        view.interface_type
    );
    assert!(
        view.interface_type.contains("接口类型"),
        "缺失原因必须说明缺的是接口类型：{}",
        view.interface_type
    );
    assert_eq!(view.hints.len(), 1, "缺失 hints 必须显示一条原因");
    assert!(
        view.hints[0].contains("未提供"),
        "缺失 hints 必须显示原因：{:?}",
        view.hints
    );
    assert!(
        view.hints[0].contains("接入提示"),
        "缺失原因必须说明缺的是接入提示：{:?}",
        view.hints
    );
}

#[test]
fn a_connection_without_a_base_url_is_an_error_not_a_fabricated_address() {
    let error = parse_connection(&json!({
        "ok": true,
        "port": 8123,
        "protocol": "openai-compatible",
        "protocol_path": "/v1",
        "hints": ["有提示但没有地址"],
    }))
    .expect_err("base_url 必须存在");
    assert!(error.contains("缺少 base_url"), "{error}");
}

fn token() -> um_desktop::gateway::ControlToken {
    um_desktop::gateway::ControlToken::parse(&"ab".repeat(32)).expect("token")
}

fn serve_once(
    status: &str,
    body: &str,
) -> (
    std::net::SocketAddr,
    std::sync::mpsc::Receiver<String>,
    std::thread::JoinHandle<()>,
) {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc::channel;

    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake connection server");
    let address = listener.local_addr().unwrap();
    let (sender, receiver) = channel();
    let status = status.to_string();
    let body = body.to_string();
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("connection request");
        let mut buffer = Vec::new();
        let mut chunk = [0u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            if String::from_utf8_lossy(&buffer).contains("\r\n\r\n") {
                break;
            }
        }
        sender
            .send(String::from_utf8_lossy(&buffer).into_owned())
            .expect("send request");
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream
            .write_all(response.as_bytes())
            .expect("write response");
    });
    (address, receiver, handle)
}

#[test]
fn the_reader_requests_admin_connection_with_the_control_token() {
    use um_desktop::access_info::HttpAccessInfoReader;

    let (address, request, server) = serve_once(
        "200 OK",
        r#"{"port":8123,"base_url":"http://127.0.0.1:8123/v1","protocol":"openai-compatible","protocol_path":"/v1","hints":["在客户端里填 Base URL"]}"#,
    );
    let reader = HttpAccessInfoReader::new();
    let payload = reader
        .read_connection(&format!("http://{address}"), &token())
        .expect("connection");
    assert_eq!(payload["port"], 8123);
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("GET /admin/connection HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token().expose())),
        "{request}"
    );
    server.join().unwrap();
}

#[test]
fn the_reader_reports_a_gateway_error_without_relaying_credentials() {
    use um_desktop::access_info::HttpAccessInfoReader;

    let (address, _request, server) = serve_once(
        "503 Service Unavailable",
        r#"{"error":{"code":"connection_info_unavailable","message":"bound address unknown","password":"super-secret"}}"#,
    );
    let reader = HttpAccessInfoReader::new();
    let error = reader
        .read_connection(&format!("http://{address}"), &token())
        .expect_err("503 must fail");
    assert!(error.contains("503"), "{error}");
    assert!(error.contains("connection_info_unavailable"), "{error}");
    assert!(!error.contains("super-secret"), "{error}");
    assert!(!error.contains("password"), "{error}");
    server.join().unwrap();
}
