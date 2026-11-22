//! Request-page filters: model options from the ledger window, the six result
//! kinds, search over request ID/model/error and the reason an empty page shows.

mod common;

use std::cell::Cell;
use std::path::Path;
use std::rc::Rc;
use std::time::Duration;

use chrono::DateTime;
use serde_json::{json, Value};
use slint::platform::{PointerEventButton, WindowEvent};
use slint::{ComponentHandle, LogicalPosition, LogicalSize, Model, ModelRc, SharedString};
use um_desktop::usage::{read_requests, Coverage, Range};

mod main_ui {
    slint::include_modules!();

    pub fn register_chart(window: &MainWindow) {
        slintplot::register!(window);
    }
}

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn init_backend() {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
}

fn click<T: ComponentHandle>(component: &T, x: f32, y: f32) {
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
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
}

fn click_center<T: ComponentHandle>(component: &T, x: f32, y: f32, width: f32, height: f32) {
    click(component, x + width / 2.0, y + height / 2.0);
}

fn request_window() -> main_ui::MainWindow {
    init_backend();
    let window = main_ui::MainWindow::new().expect("create MainWindow");
    main_ui::register_chart(&window);
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.set_page(1);
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    window
}

fn request_row(index: usize) -> main_ui::RequestRow {
    main_ui::RequestRow {
        id: format!("key-{index}").into(),
        at: format!("11-20 00:{index:02}:00").into(),
        model: "fixture-model".into(),
        outcome: "成功".into(),
        tone: 0,
        summary: "耗时 1200 ms · ID f314e7d8c7526367baec95ac155b6406".into(),
        first_token: "120 ms".into(),
        detail: "".into(),
    }
}

fn click_model_option(window: &main_ui::MainWindow, index: usize) {
    let row_height = window.get_request_model_option_row_height();
    let x = window.get_request_model_option_x() + window.get_request_model_option_width() / 2.0;
    let y = window.get_request_model_option_y() + 4.0 + row_height * (index as f32 + 0.5);
    click(window, x, y);
}

fn click_result_option(window: &main_ui::MainWindow, index: usize) {
    let row_height = window.get_request_result_option_row_height();
    let x = window.get_request_result_option_x() + window.get_request_result_option_width() / 2.0;
    let y = window.get_request_result_option_y() + 4.0 + row_height * (index as f32 + 0.5);
    click(window, x, y);
}

#[test]
fn the_model_dropdown_keeps_catalog_models_that_have_no_request_yet() {
    let catalog = um_desktop::models::parse_catalog(&json!({
        "models": [{"id": "catalog-only"}, {"id": "alpha"}]
    }))
    .expect("catalog");

    let options = um_desktop::models::request_model_options(
        &["ledger-only".to_string(), "alpha".to_string()],
        Some(&catalog),
    );
    assert_eq!(
        options,
        vec![
            "alpha".to_string(),
            "catalog-only".to_string(),
            "ledger-only".to_string()
        ],
        "the dropdown must union the ledger and the catalog without duplicates"
    );

    let ledger_only = um_desktop::models::request_model_options(
        &["ledger-only".to_string(), "alpha".to_string()],
        None,
    );
    assert_eq!(
        ledger_only,
        vec!["alpha".to_string(), "ledger-only".to_string()]
    );
}

#[test]
fn the_filter_row_lists_models_six_results_and_the_search_scope() {
    let window = request_window();
    window.set_request_model_options(ModelRc::from([
        SharedString::from("全部模型"),
        "alpha".into(),
        "beta".into(),
    ]));
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert_eq!(window.get_request_model_options().row_count(), 3);
    assert_eq!(window.get_request_model_current().as_str(), "全部模型");
    assert_eq!(window.get_request_result_options().row_count(), 7);
    assert_eq!(
        window.get_request_result_options().row_data(0),
        Some(SharedString::from("全部结果"))
    );
    assert_eq!(
        window.get_request_result_options().row_data(6),
        Some(SharedString::from("未知"))
    );
    assert_eq!(
        window.get_request_search_placeholder().as_str(),
        "请求 ID、模型或错误"
    );
}

#[test]
fn choosing_a_model_or_result_from_the_dropdown_queries_the_filter() {
    let window = request_window();
    window.set_request_model_options(ModelRc::from([
        SharedString::from("全部模型"),
        "alpha".into(),
        "beta".into(),
    ]));
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    let queries = Rc::new(Cell::new(0));
    window.on_query_requests({
        let queries = queries.clone();
        move |_| queries.set(queries.get() + 1)
    });

    click_center(
        &window,
        window.get_request_model_x(),
        window.get_request_model_y(),
        window.get_request_model_width(),
        window.get_request_model_height(),
    );
    assert!(window.get_request_model_open());
    click_model_option(&window, 1);
    assert!(!window.get_request_model_open());
    assert_eq!(window.get_request_model_filter().as_str(), "alpha");
    assert_eq!(window.get_request_model_current().as_str(), "alpha");
    assert_eq!(queries.get(), 1);

    click_center(
        &window,
        window.get_request_result_x(),
        window.get_request_result_y(),
        window.get_request_result_width(),
        window.get_request_result_height(),
    );
    assert!(window.get_request_result_open());
    click_result_option(&window, 6);
    assert!(!window.get_request_result_open());
    assert_eq!(window.get_request_result(), 6);
    assert_eq!(window.get_request_result_current().as_str(), "未知");
    assert_eq!(queries.get(), 2);
}

#[test]
fn changing_a_filter_keeps_the_displayed_rows_and_scroll_position() {
    let window = request_window();
    let rows = (0..20).map(request_row).collect::<Vec<_>>();
    window.set_requests(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));
    window.set_request_scroll_y(-138.0);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));
    window.set_request_model_options(ModelRc::from([
        SharedString::from("全部模型"),
        "alpha".into(),
        "beta".into(),
    ]));
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));

    click_center(
        &window,
        window.get_request_result_x(),
        window.get_request_result_y(),
        window.get_request_result_width(),
        window.get_request_result_height(),
    );
    click_result_option(&window, 2);
    assert_eq!(window.get_request_result(), 2);
    assert_eq!(window.get_requests().row_count(), 20);
    assert!(
        (window.get_request_scroll_y() + 138.0).abs() < 1.0,
        "the result filter must keep the scroll position"
    );

    window.set_request_scroll_y(-138.0);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));
    click_center(
        &window,
        window.get_request_model_x(),
        window.get_request_model_y(),
        window.get_request_model_width(),
        window.get_request_model_height(),
    );
    click_model_option(&window, 2);
    assert_eq!(window.get_request_model_filter().as_str(), "beta");
    assert_eq!(window.get_requests().row_count(), 20);
    assert!(
        (window.get_request_scroll_y() + 138.0).abs() < 1.0,
        "the model filter must keep the scroll position"
    );
}

const REQUEST: &str = "f314e7d8c7526367baec95ac155b6406";
const OTHER_REQUEST: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

fn time(text: &str) -> i64 {
    DateTime::parse_from_rfc3339(text)
        .unwrap()
        .timestamp_millis()
}

fn write_rows(path: &Path, rows: &[Value]) {
    let lines = rows
        .iter()
        .map(|row| format!("{row}\n"))
        .collect::<String>();
    std::fs::write(path, lines).unwrap();
}

fn ledger_row(at: &str, status: &str, model: &str, request_id: &str) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": model,
        "stream": false,
        "status": status,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 0,
        "cache_report_status": "reported",
        "reasoning_tokens": 0,
        "total_tokens": 12,
        "estimated": false,
        "duration_ms": 900,
        "usage_source": "upstream",
        "protocol": "chat",
        "key_id": null,
        "request_id": request_id,
        "attribution": "absent",
    })
}

#[test]
fn model_options_come_from_the_current_ledger_window_sorted_and_deduped() {
    let tmp = common::TempDir::new("requests-models");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            ledger_row("2026-11-20T00:01:00+08:00", "ok", "zeta", REQUEST),
            ledger_row("2026-11-20T00:02:00+08:00", "ok", "alpha", OTHER_REQUEST),
            ledger_row(
                "2026-11-20T00:03:00+08:00",
                "ok",
                "zeta",
                "22222222222222222222222222222222",
            ),
            ledger_row(
                "2026-11-19T23:59:00+08:00",
                "ok",
                "old-model",
                "33333333333333333333333333333333",
            ),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");

    let today = read_requests(&ledger, Range::Today, None, 0, "", None, now);
    assert_eq!(today.models, vec!["alpha", "zeta"]);

    let all = read_requests(&ledger, Range::All, None, 0, "", None, now);
    assert_eq!(all.models, vec!["alpha", "old-model", "zeta"]);

    let filtered = read_requests(&ledger, Range::Today, Some("zeta"), 0, "", None, now);
    assert_eq!(filtered.rows.len(), 2);
    assert!(filtered.rows.iter().all(|row| row.model == "zeta"));
    assert_eq!(filtered.models, vec!["alpha", "zeta"]);
}

#[test]
fn six_result_filters_follow_the_ledger_status_and_unknown_is_not_folded() {
    let tmp = common::TempDir::new("requests-results");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            ledger_row("2026-11-20T00:01:00+08:00", "ok", "alpha", REQUEST),
            ledger_row(
                "2026-11-20T00:02:00+08:00",
                "upstream_unavailable",
                "alpha",
                OTHER_REQUEST,
            ),
            ledger_row(
                "2026-11-20T00:03:00+08:00",
                "upstream_missing_finish_reason",
                "beta",
                "22222222222222222222222222222222",
            ),
            ledger_row(
                "2026-11-20T00:04:00+08:00",
                "client_disconnected",
                "beta",
                "33333333333333333333333333333333",
            ),
            ledger_row(
                "2026-11-20T00:05:00+08:00",
                "cancelled",
                "gamma",
                "44444444444444444444444444444444",
            ),
            ledger_row(
                "2026-11-20T00:06:00+08:00",
                "mystery_status",
                "gamma",
                "55555555555555555555555555555555",
            ),
            ledger_row(
                "2026-11-20T00:07:00+08:00",
                "",
                "delta",
                "66666666666666666666666666666666",
            ),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");

    let cases = [
        (1, "成功", 1),
        (2, "失败", 1),
        (3, "截断", 1),
        (4, "客户端断开", 1),
        (5, "已取消", 1),
        (6, "未知", 2),
    ];
    for (index, label, count) in cases {
        let page = read_requests(&ledger, Range::Today, None, index, "", None, now);
        assert_eq!(page.rows.len(), count, "{label}");
        assert!(
            page.rows.iter().all(|row| row.result == label),
            "{label}: {:?}",
            page.rows.iter().map(|row| &row.result).collect::<Vec<_>>()
        );
    }

    let failed = read_requests(&ledger, Range::Today, None, 2, "", None, now);
    assert!(failed
        .rows
        .iter()
        .all(|row| row.model != "gamma" && row.model != "delta"));
    let unknown = read_requests(&ledger, Range::Today, None, 6, "", None, now);
    assert!(unknown.rows.iter().any(|row| row.model == "gamma"));
    assert!(unknown.rows.iter().any(|row| row.model == "delta"));
}

#[test]
fn search_matches_request_id_model_and_error_and_empty_results_explain_why() {
    let tmp = common::TempDir::new("requests-search");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            ledger_row("2026-11-20T00:01:00+08:00", "ok", "alpha", REQUEST),
            ledger_row(
                "2026-11-20T00:02:00+08:00",
                "upstream_unavailable",
                "beta",
                OTHER_REQUEST,
            ),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");

    let by_request_id = read_requests(&ledger, Range::Today, None, 0, &REQUEST[..8], None, now);
    assert_eq!(by_request_id.rows.len(), 1);
    assert_eq!(by_request_id.rows[0].request_id.as_deref(), Some(REQUEST));

    let by_model = read_requests(&ledger, Range::Today, None, 0, "BETA", None, now);
    assert_eq!(by_model.rows.len(), 1);
    assert_eq!(by_model.rows[0].model, "beta");

    let by_error = read_requests(&ledger, Range::Today, None, 0, "上游不可用", None, now);
    assert_eq!(by_error.rows.len(), 1);
    assert_eq!(by_error.rows[0].model, "beta");

    let no_match = read_requests(&ledger, Range::Today, None, 0, "gamma", None, now);
    assert!(no_match.rows.is_empty());
    assert_eq!(no_match.coverage, Coverage::Empty);
    assert!(
        no_match.message.contains("没有符合条件的请求"),
        "{}",
        no_match.message
    );

    let empty_ledger = tmp.path().join("empty.jsonl");
    write_rows(
        &empty_ledger,
        &[ledger_row(
            "2026-11-19T23:59:00+08:00",
            "ok",
            "alpha",
            REQUEST,
        )],
    );
    let empty_window = read_requests(&empty_ledger, Range::Today, None, 0, "", None, now);
    assert!(empty_window.rows.is_empty());
    assert_eq!(empty_window.coverage, Coverage::Empty);
    assert!(
        empty_window.message.contains("所选时间没有请求记录"),
        "{}",
        empty_window.message
    );
    assert_ne!(no_match.message, empty_window.message);
}
