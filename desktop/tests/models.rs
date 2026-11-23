//! Slint-level behaviour for the settings page's UM model catalog (#153).
//!
//! The tests exercise the read/refresh state machine and the visible table the
//! settings page draws. They never assert private helpers: only the external
//! catalog rows, the status copy and the empty/stale states the user sees.

use serde_json::{json, Value};

use um_desktop::models::parse_catalog;

#[test]
fn the_catalog_parses_names_context_efforts_and_last_test() {
    let view = parse_catalog(&json!({
        "models": [
            {
                "id": "fixture-model-a",
                "name": "Fixture A",
                "context": 128000,
                "context_known": true,
                "efforts": ["low", "high"],
                "default_effort": "high",
                "last_test": {
                    "status": "verified",
                    "observed_floor": 256000,
                    "last_run": "2026-11-22T10:00:00Z",
                },
            },
            {
                "id": "fixture-model-b",
                "name": "Fixture B",
                "context": null,
                "context_known": false,
                "efforts": [],
                "default_effort": null,
                "last_test": null,
            },
        ],
    }))
    .expect("catalog");

    assert_eq!(view.models.len(), 2);
    let first = &view.models[0];
    assert_eq!(first.id, "fixture-model-a");
    assert_eq!(first.name, "Fixture A");
    assert_eq!(first.context, "128,000 Token");
    assert_eq!(first.efforts, "low · high（默认 high）");
    assert!(first.last_test.contains("已验证"));
    assert!(first.last_test.contains("≥256,000 Token"));
    assert!(first.detail.contains("模型 ID：fixture-model-a"));
    assert!(first.detail.contains("上下文：128,000 Token"));
    assert!(first.detail.contains("思考档位：low · high（默认 high）"));
    assert!(first.detail.contains("最近测试：已验证"));

    let second = &view.models[1];
    assert_eq!(second.id, "fixture-model-b");
    assert_eq!(second.context, "未知");
    assert_eq!(second.efforts, "未知");
    assert_eq!(second.last_test, "未测");
    assert!(second.detail.contains("上下文：未知"));
    assert!(second.detail.contains("思考档位：未知"));
    assert!(second.detail.contains("最近测试：未测"));
}

use std::sync::Mutex;

use um_desktop::gateway::ControlToken;
use um_desktop::models::{refresh_catalog, CatalogReader, CatalogView};
use um_desktop::overview::Tone;
use um_desktop::ui::*;

struct FakeReader {
    result: Result<Value, String>,
    calls: Mutex<Vec<(String, bool)>>,
}

impl FakeReader {
    fn new(result: Result<Value, String>) -> Self {
        Self {
            result,
            calls: Mutex::new(Vec::new()),
        }
    }
}

impl CatalogReader for FakeReader {
    fn read_catalog(
        &self,
        base_url: &str,
        _token: &ControlToken,
        fresh: bool,
    ) -> Result<Value, String> {
        self.calls
            .lock()
            .expect("calls poisoned")
            .push((base_url.to_string(), fresh));
        self.result.clone()
    }
}

fn token() -> ControlToken {
    ControlToken::parse(&"ab".repeat(32)).expect("token")
}

fn catalog(models: Value) -> CatalogView {
    parse_catalog(&json!({ "models": models })).expect("catalog")
}

#[test]
fn a_failed_refresh_keeps_the_previous_rows_and_marks_them_stale() {
    let previous = catalog(json!([
        {"id": "fixture-model-a", "name": "Fixture A", "context": 128000, "efforts": ["low"]},
    ]));
    let reader = FakeReader::new(Err("upstream 503".to_string()));
    let refreshed = refresh_catalog(&reader, "http://127.0.0.1:7999", &token(), Some(&previous));
    assert_eq!(refreshed.models, previous.models);
    assert!(refreshed.stale);
    assert!(
        refreshed.status.contains("刷新失败"),
        "{}",
        refreshed.status
    );
    assert!(refreshed.status.contains("已过期"), "{}", refreshed.status);
    assert_eq!(refreshed.tone, Tone::Bad);
    assert_eq!(
        reader.calls.lock().expect("calls poisoned").as_slice(),
        [("http://127.0.0.1:7999".to_string(), true)]
    );
}

#[test]
fn a_successful_refresh_replaces_the_rows_and_clears_the_stale_mark() {
    let previous = CatalogView {
        status: "刷新失败：upstream 503；下表仍是上次取到的目录，已过期。".to_string(),
        tone: Tone::Bad,
        stale: true,
        empty: false,
        models: catalog(json!([
            {"id": "fixture-model-old", "name": "Old"},
        ]))
        .models,
    };
    let reader = FakeReader::new(Ok(json!({
        "models": [
            {"id": "fixture-model-a", "name": "Fixture A"},
            {"id": "fixture-model-b", "name": "Fixture B"},
        ],
    })));
    let refreshed = refresh_catalog(&reader, "http://127.0.0.1:7999", &token(), Some(&previous));
    assert_eq!(refreshed.models.len(), 2);
    assert_eq!(refreshed.models[0].id, "fixture-model-a");
    assert!(!refreshed.stale);
    assert_eq!(refreshed.tone, Tone::Good);
    assert!(
        refreshed.status.contains("已从 UM 取到最新目录"),
        "{}",
        refreshed.status
    );
    assert!(!refreshed.status.contains("已过期"), "{}", refreshed.status);
}

#[test]
fn an_empty_catalog_replaces_the_previous_rows_and_says_no_models() {
    let previous = catalog(json!([
        {"id": "fixture-model-a", "name": "Fixture A"},
    ]));
    let reader = FakeReader::new(Ok(json!({ "models": [] })));
    let refreshed = refresh_catalog(&reader, "http://127.0.0.1:7999", &token(), Some(&previous));
    assert!(refreshed.models.is_empty());
    assert!(!refreshed.stale);
    assert!(refreshed.status.contains("无模型"), "{}", refreshed.status);
}

#[test]
fn a_failed_first_read_keeps_the_reason_and_has_no_stale_table() {
    let reader = FakeReader::new(Err("connection refused".to_string()));
    let refreshed = refresh_catalog(&reader, "http://127.0.0.1:7999", &token(), None);
    assert!(refreshed.models.is_empty());
    assert!(!refreshed.stale);
    assert_eq!(refreshed.tone, Tone::Bad);
    assert!(
        refreshed.status.contains("刷新失败：connection refused"),
        "{}",
        refreshed.status
    );
    assert!(!refreshed.status.contains("已过期"), "{}", refreshed.status);
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

    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake catalog server");
    let address = listener.local_addr().unwrap();
    let (sender, receiver) = channel();
    let status = status.to_string();
    let body = body.to_string();
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("catalog request");
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
fn the_reader_requests_the_strict_fresh_catalog_with_the_control_token() {
    use um_desktop::models::HttpCatalogReader;

    let (address, request, server) = serve_once(
        "200 OK",
        r#"{"models":[{"id":"fixture-model-a","name":"Fixture A"}]}"#,
    );
    let reader = HttpCatalogReader::new();
    let token = token();
    let payload = reader
        .read_catalog(&format!("http://{address}"), &token, true)
        .expect("catalog");
    assert_eq!(payload["models"].as_array().expect("models").len(), 1);
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("GET /admin/models?fresh=1 HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token.expose())),
        "{request}"
    );
    server.join().unwrap();
}

#[test]
fn the_reader_reports_a_gateway_error_without_relaying_credentials() {
    use um_desktop::models::HttpCatalogReader;

    let (address, _request, server) = serve_once(
        "403 Forbidden",
        r#"{"error":{"code":"control_denied","message":"control_denied_token","password":"super-secret"}}"#,
    );
    let reader = HttpCatalogReader::new();
    let error = reader
        .read_catalog(&format!("http://{address}"), &token(), true)
        .expect_err("403 must fail");
    assert!(error.contains("403"), "{error}");
    assert!(error.contains("control_denied_token"), "{error}");
    assert!(!error.contains("super-secret"), "{error}");
    assert!(!error.contains("password"), "{error}");
    server.join().unwrap();
}

thread_local! {
    static UI_INIT: std::sync::Once = std::sync::Once::new();
}

fn init_ui() {
    UI_INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
}

fn model_window() -> um_desktop::ui::MainWindow {
    use slint::{ComponentHandle, LogicalSize};

    init_ui();
    let window = um_desktop::ui::MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(2);
    window.window().set_size(LogicalSize::new(1060.0, 1200.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
}

fn scroll<T: slint::ComponentHandle>(component: &T, x: f32, y: f32, delta_y: f32) {
    use slint::platform::WindowEvent;
    use slint::LogicalPosition;

    let position = LogicalPosition::new(x, y);
    let window = component.window();
    window.dispatch_event(WindowEvent::PointerMoved { position });
    window.dispatch_event(WindowEvent::PointerScrolled {
        position,
        delta_x: 0.0,
        delta_y,
    });
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
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

#[test]
fn the_settings_catalog_table_shows_names_context_efforts_and_last_test() {
    use slint::Model;
    use um_desktop::models::apply_catalog;

    let window = model_window();
    let view = parse_catalog(&json!({
        "models": [
            {
                "id": "fixture-model-a",
                "name": "Fixture A",
                "context": 128000,
                "efforts": ["low", "high"],
                "default_effort": "high",
                "last_test": {"status": "verified", "observed_floor": 256000},
            },
            {
                "id": "fixture-model-b",
                "name": "Fixture B",
                "context": null,
                "efforts": [],
                "last_test": null,
            },
        ],
    }))
    .expect("catalog");
    apply_catalog(&window, &view);

    assert_eq!(window.get_um_model_status(), view.status);
    assert_eq!(window.get_um_model_tone(), 3);
    assert_eq!(window.get_um_model_row_count(), 2);
    assert!(window.get_um_model_table_visible());
    assert!(!window.get_um_model_empty_visible());
    assert!(!window.get_um_model_stale_visible());
    let rows = window.get_um_models();
    let first = rows.row_data(0).expect("first row");
    assert_eq!(first.id, "fixture-model-a");
    assert_eq!(first.name, "Fixture A");
    assert_eq!(first.context, "128,000 Token");
    assert_eq!(first.efforts, "low · high（默认 high）");
    assert!(first.last_test.contains("已验证"));
    let second = rows.row_data(1).expect("second row");
    assert_eq!(second.context, "未知");
    assert_eq!(second.efforts, "未知");
    assert_eq!(second.last_test, "未测");
}

#[test]
fn an_empty_catalog_shows_no_models_and_drops_the_old_rows() {
    use um_desktop::models::apply_catalog;

    let window = model_window();
    let old = parse_catalog(&json!({
        "models": [{"id": "fixture-model-a", "name": "Fixture A"}],
    }))
    .expect("old catalog");
    apply_catalog(&window, &old);
    assert_eq!(window.get_um_model_row_count(), 1);

    let empty = parse_catalog(&json!({"models": []})).expect("empty catalog");
    apply_catalog(&window, &empty);
    assert_eq!(window.get_um_model_row_count(), 0);
    assert!(window.get_um_model_empty_visible());
    assert!(!window.get_um_model_table_visible());
    assert!(
        window.get_um_model_status().contains("无模型"),
        "{}",
        window.get_um_model_status()
    );
}

#[test]
fn a_failed_refresh_keeps_the_rows_visible_and_marks_them_stale() {
    use um_desktop::models::apply_catalog;

    let window = model_window();
    let previous = parse_catalog(&json!({
        "models": [{"id": "fixture-model-a", "name": "Fixture A"}],
    }))
    .expect("catalog");
    apply_catalog(&window, &previous);
    let stale = refresh_catalog(
        &FakeReader::new(Err("upstream 503".to_string())),
        "http://127.0.0.1:7999",
        &token(),
        Some(&previous),
    );
    apply_catalog(&window, &stale);

    assert_eq!(window.get_um_model_row_count(), 1);
    assert!(window.get_um_model_table_visible());
    assert!(window.get_um_model_stale_visible());
    assert!(window.get_um_model_status().contains("刷新失败"));
    assert!(window.get_um_model_status().contains("已过期"));
    assert_eq!(window.get_um_model_tone(), 2);
}

#[test]
fn the_refresh_button_asks_the_shell_for_a_strict_read() {
    let window = model_window();
    let refreshed = std::rc::Rc::new(std::cell::Cell::new(0));
    window.on_refresh_um_models({
        let refreshed = refreshed.clone();
        move || refreshed.set(refreshed.get() + 1)
    });
    let x = window.get_um_model_refresh_x() + window.get_um_model_refresh_width() / 2.0;
    let mut y = window.get_um_model_refresh_y() + window.get_um_model_refresh_height() / 2.0;
    if y > window.get_window_height() {
        // The settings page now carries the ADR 0012 autostart row; bring the
        // model card's refresh action into view before clicking it.
        scroll(&window, 500.0, 600.0, -200.0);
        y = window.get_um_model_refresh_y() + window.get_um_model_refresh_height() / 2.0;
    }
    click(&window, x, y);
    assert_eq!(refreshed.get(), 1);
    assert!(window.get_um_model_refresh_enabled());
    window.set_um_model_loading(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(!window.get_um_model_refresh_enabled());
}

#[test]
fn clicking_a_model_row_opens_that_model() {
    use um_desktop::models::apply_catalog;

    let window = model_window();
    let view = parse_catalog(&json!({
        "models": [
            {"id": "fixture-model-a", "name": "Fixture A"},
            {"id": "fixture-model-b", "name": "Fixture B"},
        ],
    }))
    .expect("catalog");
    apply_catalog(&window, &view);
    let opened = std::rc::Rc::new(std::cell::RefCell::new(String::new()));
    window.on_open_model({
        let opened = opened.clone();
        move |id| *opened.borrow_mut() = id.to_string()
    });
    // The export card and the taller appearance card push the model catalog
    // down; bring the model list into view before clicking its first row.
    scroll(&window, 600.0, 400.0, -700.0);
    let x = window.get_um_model_list_x() + 20.0;
    let y = window.get_um_model_list_y() + 28.0;
    click(&window, x, y);
    assert_eq!(opened.borrow().as_str(), "fixture-model-a");
}

#[test]
fn the_detail_panel_shows_the_selected_models_facts_and_closes_with_an_empty_catalog() {
    use um_desktop::models::apply_catalog;

    let window = model_window();
    let view = parse_catalog(&json!({
        "models": [{"id": "fixture-model-a", "name": "Fixture A", "context": 128000}],
    }))
    .expect("catalog");
    apply_catalog(&window, &view);
    window.set_um_model_detail_title("Fixture A".into());
    window.set_um_model_detail(view.models[0].detail.clone().into());
    window.set_selected_model("fixture-model-a".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_um_model_detail_visible());
    assert_eq!(window.get_um_model_detail_title(), "Fixture A");
    assert!(window
        .get_um_model_detail_body()
        .contains("上下文：128,000 Token"));

    let empty = parse_catalog(&json!({"models": []})).expect("empty catalog");
    apply_catalog(&window, &empty);
    assert_eq!(window.get_selected_model(), "");
    assert!(!window.get_um_model_detail_visible());
    assert!(window.get_um_model_detail_body().is_empty());
}

#[test]
fn opening_a_model_sets_its_facts_and_clicking_again_closes_them() {
    use um_desktop::models::{apply_catalog, open_model};

    let window = model_window();
    let view = parse_catalog(&json!({
        "models": [{"id": "fixture-model-a", "name": "Fixture A", "context": 128000}],
    }))
    .expect("catalog");
    apply_catalog(&window, &view);

    open_model(&window, Some(&view), "fixture-model-a");
    assert_eq!(window.get_selected_model(), "fixture-model-a");
    assert_eq!(window.get_um_model_detail_title(), "Fixture A");
    assert!(window
        .get_um_model_detail_body()
        .contains("上下文：128,000 Token"));
    assert!(window.get_um_model_detail_visible());

    open_model(&window, Some(&view), "fixture-model-a");
    assert_eq!(window.get_selected_model(), "");
    assert!(window.get_um_model_detail_body().is_empty());
    assert!(!window.get_um_model_detail_visible());
}

#[test]
fn a_malformed_catalog_is_an_error_not_an_empty_table() {
    let missing = parse_catalog(&json!({"models": null})).expect_err("models must be an array");
    assert!(missing.contains("格式无效"), "{missing}");
    let row_without_id =
        parse_catalog(&json!({"models": [{"name": "Fixture"}]})).expect_err("a model needs an id");
    assert!(row_without_id.contains("缺少 ID"), "{row_without_id}");
}
