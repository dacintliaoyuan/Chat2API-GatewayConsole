//! Slint-level behaviour for the settings page's context probes (#154).
//!
//! The tests exercise the snapshot parser, the batch selection, the run/stop
//! actions and the collapsible history through the visible Slint properties and a
//! fake admin read/write seam. They never assert private helpers.

use std::sync::Mutex;

use serde_json::{json, Value};
use slint::Model;

use um_desktop::gateway::ControlToken;
use um_desktop::models::{
    apply_catalog, apply_probe, clear_probe_selection, parse_catalog, parse_probe_snapshot,
    probe_failure, run_context_probe, select_all_probes, stop_context_probe, toggle_probe_model,
    CatalogView, ProbeClient,
};
use um_desktop::overview::Tone;
use um_desktop::ui::*;

fn token() -> ControlToken {
    ControlToken::parse(&"ab".repeat(32)).expect("token")
}

fn catalog() -> CatalogView {
    parse_catalog(&json!({
        "models": [
            {"id": "fixture-model-a", "name": "Fixture A"},
            {"id": "fixture-model-b", "name": "Fixture B"},
            {"id": "fixture-model-c", "name": "Fixture C"},
        ],
    }))
    .expect("catalog")
}

fn snapshot() -> Value {
    json!({
        "status": "ok",
        "running": true,
        "stop_requested": false,
        "queue": [{"model_id": "fixture-model-b", "status": "pending"}],
        "records": [
            {
                "model_id": "fixture-model-a",
                "status": "running",
                "attempts": 2,
                "last_run": "2026-11-23T10:00:00Z",
                "history": [],
            },
            {
                "model_id": "fixture-model-c",
                "status": "verified",
                "observed_floor": 256000,
                "attempts": 4,
                "last_run": "2026-11-23T10:01:00Z",
                "last_success": "2026-11-23T10:01:00Z",
                "history": [{
                    "status": "verified",
                    "observed_floor": 256000,
                    "last_run": "2026-11-23T10:01:00Z",
                    "reason": "probe_completed",
                }],
            },
            {
                "model_id": "fixture-model-d",
                "status": "failed",
                "observed_ceiling": 64000,
                "attempts": 2,
                "last_run": "2026-11-23T10:02:00Z",
                "last_error": "ContextLimitRefused",
                "history": [{
                    "status": "failed",
                    "observed_ceiling": 64000,
                    "last_error": "ContextLimitRefused",
                    "last_run": "2026-11-23T10:02:00Z",
                }],
            },
        ],
        "steps": [4000, 16000, 64000, 256000],
        "budget": {"max_requests": 20, "max_tokens": 64},
    })
}

fn probe_view() -> um_desktop::models::ProbeView {
    parse_probe_snapshot(
        &snapshot(),
        Some(&[
            "fixture-model-a".to_string(),
            "fixture-model-b".to_string(),
            "fixture-model-c".to_string(),
            "fixture-model-d".to_string(),
        ]),
        Some(&catalog()),
    )
    .expect("probe view")
}

#[test]
fn the_snapshot_reports_queue_progress_budget_and_results() {
    let view = probe_view();
    assert!(
        view.status.contains("正在执行上下文探测"),
        "{}",
        view.status
    );
    assert_eq!(view.progress, "已完成 2/4 · 排队 1");
    assert_eq!(view.budget, "请求预算：最多 20 次 · 每次最多 64 Token");
    assert_eq!(view.rows.len(), 4);

    assert_eq!(view.rows[0].state, "测试中");
    assert_eq!(view.rows[0].name, "Fixture A");
    assert_eq!(view.rows[1].state, "等待中");
    assert_eq!(view.rows[1].summary, "排在后面");
    assert_eq!(view.rows[2].state, "已验证");
    assert!(view.rows[2].summary.contains("≥256,000 Token"));
    assert_eq!(view.rows[3].state, "未通过");
    assert!(view.rows[3].summary.contains("上限 64,000 Token"));
    assert!(
        view.rows[3].summary.contains("ContextLimitRefused"),
        "{}",
        view.rows[3].summary
    );

    assert_eq!(view.history.len(), 2);
    assert_eq!(view.history[0].model, "fixture-model-d");
    assert_eq!(view.history[0].result, "未通过");
    assert!(view.history[0].summary.contains("ContextLimitRefused"));
    assert_eq!(view.history[1].model, "fixture-model-c");
    assert_eq!(view.history[1].result, "已验证");
}

#[test]
fn missing_budget_and_result_fields_are_unavailable_not_zero() {
    let view = parse_probe_snapshot(
        &json!({
            "running": false,
            "queue": [],
            "records": [
                {"model_id": "fixture-model-a", "status": "verified"},
                {"model_id": "fixture-model-b", "status": "failed", "last_error": "HTTP500"},
            ],
            "budget": {"max_requests": null, "max_tokens": null},
        }),
        None,
        None,
    )
    .expect("probe view");

    assert_eq!(view.budget, "请求预算：不可用");
    assert!(!view.budget.contains("0 "), "{}", view.budget);
    assert!(
        view.rows[0].summary.contains("不可用"),
        "{}",
        view.rows[0].summary
    );
    assert!(
        view.rows[1].summary.contains("上限不可用"),
        "{}",
        view.rows[1].summary
    );
    assert!(view.rows[1].summary.contains("HTTP500"));
    assert!(!view.rows[0].summary.contains("0 Token"));
}

#[test]
fn a_stopped_batch_marks_unstarted_models_cancelled() {
    let view = parse_probe_snapshot(
        &json!({
            "running": false,
            "stop_requested": true,
            "queue": [],
            "records": [
                {
                    "model_id": "fixture-model-a",
                    "status": "verified",
                    "observed_floor": 256000,
                    "history": [],
                },
                {
                    "model_id": "fixture-model-b",
                    "status": "cancelled",
                    "last_error": "stopped_by_user",
                    "history": [{"status": "cancelled", "last_error": "stopped_by_user"}],
                },
                {
                    "model_id": "fixture-model-c",
                    "status": "cancelled",
                    "last_error": "stopped_by_user",
                    "history": [{"status": "cancelled", "last_error": "stopped_by_user"}],
                },
            ],
            "budget": {"max_requests": 20, "max_tokens": 64},
        }),
        Some(&[
            "fixture-model-a".to_string(),
            "fixture-model-b".to_string(),
            "fixture-model-c".to_string(),
        ]),
        None,
    )
    .expect("probe view");

    assert!(
        view.status.contains("已请求停止") || view.status.contains("已停止"),
        "{}",
        view.status
    );
    assert_eq!(view.rows[1].state, "已取消");
    assert!(
        view.rows[1].summary.contains("停止后续探测"),
        "{}",
        view.rows[1].summary
    );
    assert_eq!(view.rows[2].state, "已取消");
    assert_eq!(view.progress, "已完成 3/3 · 排队 0");
    assert!(view
        .history
        .iter()
        .any(|row| row.result == "已取消" && row.summary.contains("停止后续探测")));
}

#[test]
fn a_malformed_snapshot_is_an_error_not_an_empty_history() {
    let missing_records = parse_probe_snapshot(&json!({"running": false, "queue": []}), None, None)
        .expect_err("records must be an array");
    assert!(missing_records.contains("格式无效"), "{missing_records}");

    let row_without_id = parse_probe_snapshot(
        &json!({"running": false, "queue": [], "records": [{"status": "verified"}]}),
        None,
        None,
    )
    .expect_err("a probe record needs a model id");
    assert!(row_without_id.contains("缺少模型 ID"), "{row_without_id}");
}

thread_local! {
    static UI_INIT: std::sync::Once = std::sync::Once::new();
}

fn init_ui() {
    UI_INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
}

fn probe_window() -> MainWindow {
    use slint::{ComponentHandle, LogicalSize};

    init_ui();
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(2);
    window.window().set_size(LogicalSize::new(1060.0, 2200.0));
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

#[test]
fn the_probe_card_shows_progress_results_and_a_collapsible_history() {
    let window = probe_window();
    apply_catalog(&window, &catalog());
    apply_probe(&window, &probe_view());

    assert_eq!(window.get_probe_row_count(), 4);
    assert!(window.get_probe_list_visible());
    assert_eq!(window.get_probe_status(), probe_view().status);
    assert_eq!(window.get_probe_progress(), "已完成 2/4 · 排队 1");
    assert_eq!(
        window.get_probe_budget(),
        "请求预算：最多 20 次 · 每次最多 64 Token"
    );
    assert_eq!(window.get_probe_selection_text(), "已选 0 个");
    assert!(!window.get_probe_run_enabled());
    assert!(window.get_probe_stop_visible());
    assert_eq!(window.get_probe_history_count(), 2);
    assert!(!window.get_probe_history_visible());

    let rows = window.get_probe_models();
    let first = rows.row_data(0).expect("first probe row");
    assert_eq!(first.name, "Fixture A");
    assert_eq!(first.state, "测试中");
    let last = rows.row_data(3).expect("last probe row");
    assert_eq!(last.state, "未通过");
    assert!(last.summary.contains("ContextLimitRefused"));

    let x = window.get_probe_history_toggle_x() + window.get_probe_history_toggle_width() / 2.0;
    let y = window.get_probe_history_toggle_y() + window.get_probe_history_toggle_height() / 2.0;
    click(&window, x, y);
    assert!(window.get_probe_history_visible());
    let history = window.get_probe_history();
    assert_eq!(
        history.row_data(0).expect("history row").model,
        "fixture-model-d"
    );
    assert_eq!(history.row_data(0).expect("history row").result, "未通过");
    assert!(history
        .row_data(0)
        .expect("history row")
        .summary
        .contains("ContextLimitRefused"));
}

#[test]
fn clicking_the_row_checkbox_and_the_batch_controls_changes_the_selection() {
    let window = probe_window();
    let view = catalog();
    apply_catalog(&window, &view);
    apply_probe(&window, &probe_view());
    window.set_probe_running(false);
    let weak = window.as_weak();
    window.on_toggle_probe_model(move |id| {
        if let Some(window) = weak.upgrade() {
            toggle_probe_model(&window, id.as_str());
        }
    });
    let weak = window.as_weak();
    let all = view.clone();
    window.on_select_all_probes(move || {
        if let Some(window) = weak.upgrade() {
            select_all_probes(&window, Some(&all));
        }
    });
    let weak = window.as_weak();
    window.on_clear_probe_selection(move || {
        if let Some(window) = weak.upgrade() {
            clear_probe_selection(&window);
        }
    });

    let x = window.get_probe_list_x() + 22.0;
    let y = window.get_probe_list_y() + 22.0;
    click(&window, x, y);
    assert_eq!(
        window
            .get_probe_selected()
            .iter()
            .map(|id| id.to_string())
            .collect::<Vec<_>>(),
        ["fixture-model-a".to_string()]
    );
    assert_eq!(window.get_probe_selection_text(), "已选 1 个");
    assert!(window.get_probe_run_enabled());

    click(&window, x, y);
    assert_eq!(window.get_probe_selected().row_count(), 0);
    assert_eq!(window.get_probe_selection_text(), "已选 0 个");
    assert!(!window.get_probe_run_enabled());

    let x = window.get_probe_select_all_x() + window.get_probe_select_all_width() / 2.0;
    let y = window.get_probe_select_all_y() + window.get_probe_select_all_height() / 2.0;
    click(&window, x, y);
    assert_eq!(window.get_probe_selected().row_count(), 4);
    assert_eq!(window.get_probe_selection_text(), "已选 4 个");

    let x = window.get_probe_clear_x() + window.get_probe_clear_width() / 2.0;
    let y = window.get_probe_clear_y() + window.get_probe_clear_height() / 2.0;
    click(&window, x, y);
    assert_eq!(window.get_probe_selected().row_count(), 0);
    assert_eq!(window.get_probe_selection_text(), "已选 0 个");
}

#[test]
fn the_run_and_stop_buttons_ask_the_shell_for_the_actions() {
    let window = probe_window();
    apply_catalog(&window, &catalog());
    apply_probe(&window, &probe_view());
    window.set_probe_running(false);
    toggle_probe_model(&window, "fixture-model-a");
    assert!(window.get_probe_run_enabled());

    let runs = std::rc::Rc::new(std::cell::Cell::new(0));
    window.on_run_context_probe({
        let runs = runs.clone();
        move || runs.set(runs.get() + 1)
    });
    let x = window.get_probe_run_x() + window.get_probe_run_width() / 2.0;
    let y = window.get_probe_run_y() + window.get_probe_run_height() / 2.0;
    click(&window, x, y);
    assert_eq!(runs.get(), 1);

    window.set_probe_running(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_probe_stop_visible());
    let stops = std::rc::Rc::new(std::cell::Cell::new(0));
    window.on_stop_context_probe({
        let stops = stops.clone();
        move || stops.set(stops.get() + 1)
    });
    let x = window.get_probe_stop_x() + window.get_probe_stop_width() / 2.0;
    let y = window.get_probe_stop_y() + window.get_probe_stop_height() / 2.0;
    click(&window, x, y);
    assert_eq!(stops.get(), 1);
}

#[test]
fn an_empty_history_is_a_legal_empty_state() {
    let window = probe_window();
    let view = parse_probe_snapshot(
        &json!({
            "running": false,
            "queue": [],
            "records": [],
            "budget": {"max_requests": 20, "max_tokens": 64},
        }),
        None,
        None,
    )
    .expect("empty probe snapshot");
    apply_probe(&window, &view);
    assert_eq!(window.get_probe_row_count(), 0);
    assert!(window.get_probe_empty_visible());
    assert!(!window.get_probe_list_visible());
    assert_eq!(window.get_probe_history_count(), 0);
    window.set_probe_history_open(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_probe_history_visible());
    assert!(window.get_probe_history_empty_visible());
    assert!(
        window.get_probe_status().contains("尚未执行上下文探测"),
        "{}",
        window.get_probe_status()
    );
}

struct FakeProbeClient {
    started: Mutex<Vec<Vec<String>>>,
    stopped: Mutex<usize>,
    start_result: Result<Value, String>,
    stop_result: Result<Value, String>,
}

impl FakeProbeClient {
    fn new() -> Self {
        Self {
            started: Mutex::new(Vec::new()),
            stopped: Mutex::new(0),
            start_result: Ok(json!({"status": "started", "queued": 1})),
            stop_result: Ok(json!({"status": "stopping", "stopped": true})),
        }
    }
}

impl ProbeClient for FakeProbeClient {
    fn read_probes(&self, _base_url: &str, _token: &ControlToken) -> Result<Value, String> {
        Ok(snapshot())
    }

    fn start_probes(
        &self,
        _base_url: &str,
        _token: &ControlToken,
        models: &[String],
    ) -> Result<Value, String> {
        self.started
            .lock()
            .expect("started poisoned")
            .push(models.to_vec());
        self.start_result.clone()
    }

    fn stop_probes(&self, _base_url: &str, _token: &ControlToken) -> Result<Value, String> {
        *self.stopped.lock().expect("stopped poisoned") += 1;
        self.stop_result.clone()
    }
}

#[test]
fn running_the_probe_submits_the_selected_models_and_shows_progress() {
    let window = probe_window();
    apply_catalog(&window, &catalog());
    apply_probe(&window, &probe_view());
    toggle_probe_model(&window, "fixture-model-a");

    let client = FakeProbeClient::new();
    let payload =
        run_context_probe(&window, &client, "http://127.0.0.1:7999", &token()).expect("run probes");

    assert_eq!(payload["status"], json!("started"));
    assert_eq!(
        client.started.lock().expect("started poisoned").as_slice(),
        [vec!["fixture-model-a".to_string()]]
    );
    assert_eq!(
        window.get_probe_status(),
        "已提交上下文探测：1 个模型按顺序执行"
    );
    assert!(window.get_probe_running());
    assert_eq!(window.get_probe_progress(), "已完成 0/1 · 排队 1");
    assert!(!window.get_probe_loading());
}

#[test]
fn running_without_a_selection_keeps_the_reason_and_submits_nothing() {
    let window = probe_window();
    apply_catalog(&window, &catalog());
    apply_probe(&window, &probe_view());
    clear_probe_selection(&window);

    let client = FakeProbeClient::new();
    let error = run_context_probe(&window, &client, "http://127.0.0.1:7999", &token())
        .expect_err("an empty selection must not submit");
    assert!(error.contains("请先勾选"), "{error}");
    assert!(client.started.lock().expect("started poisoned").is_empty());
    assert!(
        window.get_probe_status().contains("请先勾选"),
        "{}",
        window.get_probe_status()
    );
    assert_eq!(window.get_probe_tone(), 1);
    assert!(!window.get_probe_run_enabled());
}

#[test]
fn stopping_the_probe_requests_the_stop_and_keeps_the_reason() {
    let window = probe_window();
    apply_catalog(&window, &catalog());
    apply_probe(&window, &probe_view());
    assert!(window.get_probe_stop_visible());

    let client = FakeProbeClient::new();
    let payload = stop_context_probe(&window, &client, "http://127.0.0.1:7999", &token())
        .expect("stop probes");
    assert_eq!(payload["stopped"], json!(true));
    assert_eq!(*client.stopped.lock().expect("stopped poisoned"), 1);
    assert!(
        window.get_probe_status().contains("已请求停止"),
        "{}",
        window.get_probe_status()
    );
    assert!(window.get_probe_stop_requested());
    assert!(!window.get_probe_stop_enabled());
}

#[test]
fn a_failed_stop_keeps_its_reason_instead_of_claiming_success() {
    let window = probe_window();
    apply_catalog(&window, &catalog());
    apply_probe(&window, &probe_view());
    window.set_probe_running(true);
    let mut client = FakeProbeClient::new();
    client.stop_result = Err("connection refused".to_string());

    let error = stop_context_probe(&window, &client, "http://127.0.0.1:7999", &token())
        .expect_err("a failed stop must not claim success");
    assert_eq!(error, "connection refused");
    assert!(
        window
            .get_probe_status()
            .contains("停止失败：connection refused"),
        "{}",
        window.get_probe_status()
    );
    assert_eq!(window.get_probe_tone(), 2);
    assert!(!window.get_probe_stop_requested());
}

#[test]
fn a_failed_snapshot_read_keeps_the_rows_and_keeps_the_reason() {
    let previous = probe_view();
    let failed = probe_failure("connection refused".to_string(), Some(&previous));
    assert_eq!(failed.rows, previous.rows);
    assert_eq!(failed.history, previous.history);
    assert!(failed
        .status
        .contains("读取上下文探测失败：connection refused"));
    assert_eq!(failed.tone, Tone::Bad);
    assert_eq!(failed.budget, previous.budget);
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

    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("fake probe server");
    let address = listener.local_addr().unwrap();
    let (sender, receiver) = channel();
    let status = status.to_string();
    let body = body.to_string();
    let handle = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("probe request");
        let mut buffer = Vec::new();
        let mut chunk = [0u8; 1024];
        loop {
            let read = stream.read(&mut chunk).expect("read request");
            if read == 0 {
                break;
            }
            buffer.extend_from_slice(&chunk[..read]);
            let text = String::from_utf8_lossy(&buffer);
            if let Some(head_end) = text.find("\r\n\r\n") {
                let content_length = text[..head_end]
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length:")
                            .and_then(|value| value.trim().parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                if buffer.len() >= head_end + 4 + content_length {
                    break;
                }
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
fn the_probe_client_reads_the_admin_snapshot_with_the_control_token() {
    use um_desktop::models::HttpProbeClient;

    let (address, request, server) = serve_once(
        "200 OK",
        r#"{"running":false,"queue":[],"records":[],"budget":{"max_requests":20,"max_tokens":64}}"#,
    );
    let client = HttpProbeClient::new();
    let token = token();
    let payload = client
        .read_probes(&format!("http://{address}"), &token)
        .expect("snapshot");
    assert_eq!(payload["running"], json!(false));
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("GET /admin/model-context/probes HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token.expose())),
        "{request}"
    );
    server.join().unwrap();
}

#[test]
fn the_probe_client_posts_the_selected_models() {
    use um_desktop::models::HttpProbeClient;

    let (address, request, server) =
        serve_once("202 Accepted", r#"{"status":"started","queued":1}"#);
    let client = HttpProbeClient::new();
    let token = token();
    let models = vec!["fixture-model-a".to_string()];
    let payload = client
        .start_probes(&format!("http://{address}"), &token, &models)
        .expect("start");
    assert_eq!(payload["status"], json!("started"));
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("POST /admin/model-context/probes HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token.expose())),
        "{request}"
    );
    let body = request.split("\r\n\r\n").nth(1).expect("request body");
    assert!(body.contains("fixture-model-a"), "{body}");
    server.join().unwrap();
}

#[test]
fn the_probe_client_deletes_to_stop_the_batch() {
    use um_desktop::models::HttpProbeClient;

    let (address, request, server) =
        serve_once("200 OK", r#"{"status":"stopping","stopped":true}"#);
    let client = HttpProbeClient::new();
    let token = token();
    let payload = client
        .stop_probes(&format!("http://{address}"), &token)
        .expect("stop");
    assert_eq!(payload["stopped"], json!(true));
    let request = request.recv().expect("request");
    assert!(
        request.starts_with("DELETE /admin/model-context/probes HTTP/1.1\r\n"),
        "{request}"
    );
    assert!(
        request.contains(&format!("X-Admin-Control: {}", token.expose())),
        "{request}"
    );
    server.join().unwrap();
}
