//! Request-detail fields: HTTP status, error code and message, cache read,
//! reasoning tokens, first token, speed basis, retry steps and the linked logs.
//!
//! The tests drive the same public seam the window uses: `read_requests` turns the
//! ledger into rows and `request_detail` renders the expanded text. Missing
//! readings must say 不可用 with the ledger's reason instead of folding to zero.

mod common;

use std::path::Path;
use std::time::Duration;

use chrono::DateTime;
use serde_json::{json, Value};
use slint::{ComponentHandle, LogicalSize};
use um_desktop::usage::{read_requests, request_detail, Range};

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

fn ledger_row(at: &str, status: &str, request_id: Option<&str>) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": "fixture-model",
        "stream": true,
        "status": status,
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_input_tokens": 12,
        "cache_report_status": "reported",
        "reasoning_tokens": 5,
        "total_tokens": 15,
        "estimated": false,
        "duration_ms": 1000,
        "first_token_ms": 120,
        "usage_source": "upstream",
        "protocol": "chat",
        "key_id": null,
        "request_id": request_id,
        "attribution": "absent",
    })
}

fn retry_row(at: &str, status: &str, request_id: &str) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": "fixture-model",
        "status": status,
        "request_id": request_id,
    })
}

fn detail_for(ledger: &Path, status: &str, request_id: Option<&str>) -> String {
    let now = time("2026-11-20T19:00:00+08:00");
    write_rows(
        ledger,
        &[ledger_row("2026-11-20T00:01:00+08:00", status, request_id)],
    );
    let page = read_requests(ledger, Range::Today, None, 0, "", None, now);
    let row = page.rows.first().expect("one ledger row");
    request_detail(row, None)
}

#[test]
fn detail_shows_http_status_and_keeps_error_code_separate_from_the_message() {
    let tmp = common::TempDir::new("request-detail-status");
    let ledger = tmp.path().join("usage.jsonl");

    let success = detail_for(&ledger, "ok", Some(REQUEST));
    assert!(success.contains("HTTP 状态  200"), "{success}");
    assert!(success.contains("错误  无"), "{success}");

    let failed = detail_for(&ledger, "upstream_unavailable", Some(REQUEST));
    assert!(
        failed.contains("HTTP 状态  不可用（连接在响应前结束或没有记录）"),
        "{failed}"
    );
    assert!(
        failed.contains("错误代码  upstream_unavailable"),
        "{failed}"
    );
    assert!(failed.contains("错误信息  上游不可用"), "{failed}");
    assert!(
        !failed.contains("错误信息  upstream_unavailable"),
        "the error code must not stand in for the message: {failed}"
    );
    assert!(
        !failed.contains("错误代码  上游不可用"),
        "the message must not stand in for the code: {failed}"
    );
}

#[test]
fn missing_readings_say_unavailable_instead_of_zero() {
    let tmp = common::TempDir::new("request-detail-missing");
    let ledger = tmp.path().join("usage.jsonl");
    let missing = json!({
        "time": "2026-11-20T00:01:00+08:00",
        "service": "chat2api",
        "model": "fixture-model",
        "stream": true,
        "status": "client_disconnected",
        "input_tokens": 10,
        "duration_ms": 900,
        "usage_source": "upstream",
        "protocol": "chat",
        "request_id": null,
    });
    write_rows(&ledger, &[missing]);
    let now = time("2026-11-20T19:00:00+08:00");
    let page = read_requests(&ledger, Range::Today, None, 0, "", None, now);
    let row = page.rows.first().expect("one ledger row");
    let detail = request_detail(row, None);

    assert!(
        detail.contains("HTTP 状态  不可用（连接在响应前结束或没有记录）"),
        "{detail}"
    );
    assert!(
        detail.contains("缓存读取  不可用（旧记录没有缓存报告状态（Q11 unknown））"),
        "{detail}"
    );
    assert!(
        detail.contains("思考 Token  不可用（旧记录缺少思考 Token 字段；已计入输出，不重复相加）"),
        "{detail}"
    );
    assert!(
        detail.contains("首字耗时  不可用（账本没有记录首字耗时，不推算）"),
        "{detail}"
    );
    assert!(
        detail.contains("速度  不可用（缺输出 Token 或正耗时，无法计算）"),
        "{detail}"
    );
    assert!(
        detail.contains("重试步骤  不可用（缺少 request_id，无法关联物理重试）"),
        "{detail}"
    );
    for zero in [
        "缓存读取  0",
        "思考 Token  0",
        "首字耗时  0 ms",
        "速度  0.0",
    ] {
        assert!(
            !detail.contains(zero),
            "missing {zero:?} folded to zero: {detail}"
        );
    }
}

#[test]
fn missing_input_and_output_tokens_say_unavailable_with_the_ledger_reason() {
    let tmp = common::TempDir::new("request-detail-missing-tokens");
    let ledger = tmp.path().join("usage.jsonl");
    let missing = json!({
        "time": "2026-11-20T00:01:00+08:00",
        "service": "chat2api",
        "model": "fixture-model",
        "stream": true,
        "status": "ok",
        "duration_ms": 900,
        "usage_source": "none",
        "protocol": "chat",
        "request_id": null,
    });
    write_rows(&ledger, &[missing]);
    let now = time("2026-11-20T19:00:00+08:00");
    let page = read_requests(&ledger, Range::Today, None, 0, "", None, now);
    let row = page.rows.first().expect("one ledger row");
    let detail = request_detail(row, None);

    assert!(
        detail.contains("输入  不可用（上游没有报告 input Token（写端记为 0 表示未知））"),
        "{detail}"
    );
    assert!(
        detail.contains("输出  不可用（上游没有报告 output Token（写端记为 0 表示未知））"),
        "{detail}"
    );
    assert!(!detail.contains("输入  —"), "{detail}");
    assert!(!detail.contains("输出  —"), "{detail}");
}

#[test]
fn retry_steps_expand_with_each_retry_and_logs_keep_only_this_request() {
    let tmp = common::TempDir::new("request-detail-retries");
    let ledger = tmp.path().join("usage.jsonl");
    let log = tmp.path().join("gateway.log");
    write_rows(
        &ledger,
        &[
            retry_row(
                "2026-11-20T00:00:58+08:00",
                "upstream_transient_retried",
                REQUEST,
            ),
            ledger_row("2026-11-20T00:01:00+08:00", "ok", Some(REQUEST)),
            ledger_row("2026-11-20T00:01:02+08:00", "ok", Some(OTHER_REQUEST)),
        ],
    );
    write_rows(
        &log,
        &[
            json!({
                "time": "2026-11-20T00:01:00+08:00",
                "request_id": REQUEST,
                "event": "request_summary",
                "protocol": "chat",
                "final": "ok",
                "tries": 2,
            }),
            json!({
                "time": "2026-11-20T00:01:01+08:00",
                "request_id": REQUEST,
                "event": "upstream_retry",
                "error": "password=hunter2 api_key=sk-secret authorization=Bearer top-secret",
            }),
            json!({
                "time": "2026-11-20T00:01:02+08:00",
                "request_id": OTHER_REQUEST,
                "event": "request_summary",
                "protocol": "chat",
                "final": "upstream_unavailable",
                "tries": 2,
            }),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    let page = read_requests(&ledger, Range::Today, None, 0, "", None, now);
    assert_eq!(page.rows.len(), 2, "a retry is not a separate request row");
    let row = page
        .rows
        .iter()
        .find(|row| row.request_id.as_deref() == Some(REQUEST))
        .expect("the logical request");

    // Collapsed, the row only names the retry count; the steps live in the
    // expanded detail.
    assert!(row.detail.contains("物理重试 1 次"), "{}", row.detail);
    assert!(!row.detail.contains("重试步骤"), "{}", row.detail);

    let logs = um_desktop::usage::read_request_logs(&log, row.request_id.as_deref(), now);
    assert_eq!(logs.lines.len(), 2, "only this request ID's events");
    let detail = request_detail(row, Some(&logs));
    assert!(detail.contains("重试步骤"), "{detail}");
    assert!(
        detail.contains(
            "- 11-20 00:00:58  物理重试记录：upstream_transient_retried；本次记录是估算下界"
        ),
        "{detail}"
    );
    assert!(detail.contains("request_summary"), "{detail}");
    assert!(detail.contains("upstream_retry"), "{detail}");
    assert!(
        !detail.contains("upstream_unavailable"),
        "another request's event leaked into the detail: {detail}"
    );
    for secret in ["hunter2", "sk-secret", "top-secret"] {
        assert!(!detail.contains(secret), "{secret} leaked: {detail}");
    }
}

#[test]
fn the_detail_panel_renders_the_ledger_fields_in_its_visible_text() {
    init_backend();
    let window = main_ui::MainWindow::new().expect("create MainWindow");
    main_ui::register_chart(&window);
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.set_page(1);
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    let tmp = common::TempDir::new("request-detail-panel");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            retry_row(
                "2026-11-20T00:00:58+08:00",
                "upstream_transient_retried",
                REQUEST,
            ),
            ledger_row("2026-11-20T00:01:00+08:00", "ok", Some(REQUEST)),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    let page = read_requests(&ledger, Range::Today, None, 0, "", None, now);
    let row = page
        .rows
        .iter()
        .find(|row| row.request_id.as_deref() == Some(REQUEST))
        .expect("the logical request");
    let detail = request_detail(row, None);

    window.set_selected_request(row.key.clone().into());
    window.set_request_detail(detail.clone().into());
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    let shown = window.get_request_detail_shown();
    assert_eq!(shown.as_str(), detail.as_str());
    for expected in [
        "HTTP 状态  200",
        "错误  无",
        "缓存读取  ≥ 12 Token（已含在输入中）",
        "思考 Token  ≥ 5（已计入输出）",
        "首字耗时  120 ms",
        "速度  5.0 Token/秒",
        "速度口径  整次平均，含等待",
        "重试步骤",
        "- 11-20 00:00:58  物理重试记录：upstream_transient_retried；本次记录是估算下界",
    ] {
        assert!(shown.contains(expected), "missing {expected:?} in {shown}");
    }
}
