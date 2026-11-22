//! Request-page 加载更早: append older rows under the current filters, keep the
//! displayed rows and scroll position, and say 没有更早记录 when the history ends.
//!
//! The tests drive the same public seam the window uses: `read_requests` reads one
//! page from the ledger and `RequestNav` owns the page anchor and the separate
//! 加载更早 cursor. The page-level 较新/较早 navigation stays independent.

mod common;

use std::cell::Cell;
use std::collections::HashSet;
use std::path::Path;
use std::rc::Rc;
use std::time::Duration;

use chrono::DateTime;
use serde_json::{json, Value};
use slint::platform::{PointerEventButton, WindowEvent};
use slint::{ComponentHandle, LogicalPosition, LogicalSize, Model, ModelRc};
use um_desktop::usage::{
    read_requests, Coverage, EarlierLoad, EarlierOutcome, Range, ReadingView, RequestChart,
    RequestNav, RequestPage, RequestRow, RequestSummary,
};

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
        summary: "耗时 900 ms · ID f314e7d8c7526367baec95ac155b6406".into(),
        first_token: "100 ms".into(),
        detail: "".into(),
    }
}

fn set_rows(window: &main_ui::MainWindow, count: usize) {
    let rows = (0..count).map(request_row).collect::<Vec<_>>();
    window.set_requests(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));
}

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

fn ledger_row(at: &str, request_id: &str) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": "fixture-model",
        "stream": false,
        "status": "ok",
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

fn usage_row(key: &str, at: &str) -> RequestRow {
    RequestRow {
        key: key.into(),
        request_id: None,
        at: at.into(),
        model: "fixture-model".into(),
        result: "成功".into(),
        input: ReadingView {
            value: Some("10".into()),
            note: None,
        },
        output: ReadingView {
            value: Some("2".into()),
            note: None,
        },
        duration: "900 ms".into(),
        first_token: "100 ms".into(),
        detail: String::new(),
        http_status: Some("200".into()),
        error_code: None,
        error_message: None,
        cache_read: Default::default(),
        reasoning: Default::default(),
        first_token_note: None,
        speed: Default::default(),
        speed_basis: "整次平均，含等待".into(),
        retries: Some(Vec::new()),
    }
}

fn page(rows: Vec<RequestRow>, earlier: Option<String>, coverage: Coverage) -> RequestPage {
    RequestPage {
        rows,
        earlier,
        coverage,
        message: String::new(),
        summary: RequestSummary::default(),
        model_speeds: Vec::new(),
        chart: RequestChart::default(),
        models: Vec::new(),
    }
}

fn first_page_and_older(ledger: &Path, now: i64) -> (RequestPage, RequestPage) {
    let first = read_requests(ledger, Range::Today, None, 0, "", None, now);
    let cursor = first.earlier.clone().expect("65 rows need a second page");
    let older = read_requests(
        ledger,
        Range::Today,
        None,
        0,
        "",
        Some(cursor.as_str()),
        now,
    );
    (first, older)
}

#[test]
fn loading_earlier_appends_older_rows_after_the_displayed_ones() {
    let tmp = common::TempDir::new("load-earlier-append");
    let ledger = tmp.path().join("usage.jsonl");
    let rows = (0..65)
        .map(|minute| {
            ledger_row(
                &format!("2026-11-20T{:02}:{:02}:00+08:00", minute / 60, minute % 60),
                &format!("{minute:032x}"),
            )
        })
        .collect::<Vec<_>>();
    write_rows(&ledger, &rows);
    let now = time("2026-11-20T19:00:00+08:00");
    let (first, older) = first_page_and_older(&ledger, now);
    assert_eq!(first.rows.len(), 60);
    assert_eq!(older.rows.len(), 5);

    let mut nav = RequestNav::default();
    assert_eq!(nav.query(now), Some(None));
    nav.rows = first.rows.clone();
    nav.earlier = first.earlier.clone();
    nav.append_cursor = first.earlier.clone();

    let cursor = match nav.begin_earlier() {
        EarlierLoad::Cursor(cursor) => cursor,
        other => panic!("expected a cursor, got {other:?}"),
    };
    assert_eq!(Some(cursor.as_str()), first.earlier.as_deref());
    let mut older = older;
    // A moved ledger can return a row that is already on screen. It must not
    // become a second copy.
    older.rows.insert(0, first.rows.last().unwrap().clone());
    let outcome = nav.finish_earlier(older);

    assert_eq!(outcome, EarlierOutcome::Appended { added: 5 });
    assert_eq!(
        nav.rows.len(),
        65,
        "the older rows are appended, not paged in"
    );
    let keys = nav
        .rows
        .iter()
        .map(|row| row.key.clone())
        .collect::<HashSet<_>>();
    assert_eq!(keys.len(), 65, "the appended rows must not repeat");
    assert!(
        nav.rows.windows(2).all(|pair| pair[0].at >= pair[1].at),
        "the list stays newest first: {:?}",
        nav.rows.iter().map(|row| &row.at).collect::<Vec<_>>()
    );
    assert_eq!(
        nav.earlier, first.earlier,
        "加载更早 never moves the 较早 page anchor"
    );
    assert_eq!(nav.append_cursor, None, "the history ended");
}

#[test]
fn an_empty_older_page_keeps_the_rows_and_reports_no_more() {
    let mut nav = RequestNav::default();
    nav.query(1000);
    nav.rows = vec![
        usage_row("key-a", "11-20 00:02:00"),
        usage_row("key-b", "11-20 00:01:00"),
    ];
    nav.earlier = Some("page-cursor".into());
    nav.append_cursor = Some("page-cursor".into());

    assert_eq!(
        nav.begin_earlier(),
        EarlierLoad::Cursor("page-cursor".into())
    );
    let outcome = nav.finish_earlier(page(Vec::new(), None, Coverage::Empty));

    assert_eq!(outcome, EarlierOutcome::NoMore);
    assert_eq!(
        nav.rows.len(),
        2,
        "no empty list replaces the displayed rows"
    );
    assert_eq!(
        nav.earlier.as_deref(),
        Some("page-cursor"),
        "较早 keeps its own cursor"
    );
    assert_eq!(nav.append_cursor, None, "the history ended");
}

#[test]
fn a_new_query_restarts_loading_earlier_from_its_first_page() {
    let mut nav = RequestNav::default();
    nav.query(1000);
    nav.rows = vec![usage_row("key-a", "11-20 00:01:00")];
    nav.earlier = Some("page-cursor".into());
    nav.append_cursor = Some("appended-cursor".into());

    assert_eq!(
        nav.begin_earlier(),
        EarlierLoad::Cursor("appended-cursor".into())
    );
    assert_eq!(nav.query(2000), Some(None));

    assert_eq!(nav.append_cursor, None, "the new query starts over");
    assert_eq!(nav.earlier, None, "the new query has no page anchor yet");
    assert_eq!(
        nav.begin_earlier(),
        EarlierLoad::NoMore,
        "加载更早 waits for the new first page before continuing"
    );
}

#[test]
fn a_page_read_restarts_loading_earlier_from_that_page() {
    let mut nav = RequestNav::default();
    nav.query(1000);
    nav.rows = vec![
        usage_row("key-a", "11-20 00:02:00"),
        usage_row("key-b", "11-20 00:01:00"),
    ];
    nav.append_cursor = Some("older-than-this-page".into());

    nav.apply_page(
        &page(
            vec![usage_row("key-c", "11-20 00:00:00")],
            Some("page-2".into()),
            Coverage::Complete,
        ),
        true,
    );

    assert_eq!(nav.rows.len(), 1, "the page replaces the displayed rows");
    assert_eq!(nav.earlier.as_deref(), Some("page-2"));
    assert_eq!(
        nav.append_cursor.as_deref(),
        Some("page-2"),
        "加载更早 restarts after the page that is now shown"
    );
}

#[test]
fn loading_earlier_without_a_cursor_says_no_more_without_clearing_the_rows() {
    let mut nav = RequestNav::default();
    nav.query(1000);
    nav.rows = vec![usage_row("key-a", "11-20 00:01:00")];

    assert_eq!(nav.begin_earlier(), EarlierLoad::NoMore);
    assert_eq!(nav.rows.len(), 1);
}

#[test]
fn a_failed_earlier_read_keeps_the_rows_and_the_cursor_for_retry() {
    let mut nav = RequestNav::default();
    nav.query(1000);
    nav.rows = vec![usage_row("key-a", "11-20 00:02:00")];
    nav.append_cursor = Some("next-page".into());
    assert_eq!(nav.begin_earlier(), EarlierLoad::Cursor("next-page".into()));

    let failed = RequestPage {
        rows: Vec::new(),
        earlier: None,
        coverage: Coverage::Unavailable,
        message: "无法读取本实例账本".into(),
        summary: RequestSummary::default(),
        model_speeds: Vec::new(),
        chart: RequestChart::default(),
        models: Vec::new(),
    };
    assert_eq!(
        nav.finish_earlier(failed),
        EarlierOutcome::Failed("无法读取本实例账本".into())
    );
    assert_eq!(nav.rows.len(), 1, "a failed read must not clear the rows");
    assert_eq!(
        nav.append_cursor.as_deref(),
        Some("next-page"),
        "a failed read keeps the cursor so the user can retry"
    );
    assert_eq!(nav.begin_earlier(), EarlierLoad::Cursor("next-page".into()));
}

#[test]
fn the_load_earlier_button_appends_without_resetting_the_scroll_or_page() {
    let window = request_window();
    set_rows(&window, 20);
    window.set_older_requests(true);
    window.set_has_earlier_records(true);
    window.set_selected_request("key-3".into());
    window.set_request_scroll_y(-138.0);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));
    let loads = Rc::new(Cell::new(0));
    window.on_load_earlier_requests({
        let loads = loads.clone();
        move || loads.set(loads.get() + 1)
    });

    click_center(
        &window,
        window.get_request_load_earlier_x(),
        window.get_request_load_earlier_y(),
        window.get_request_load_earlier_width(),
        window.get_request_load_earlier_height(),
    );

    assert_eq!(loads.get(), 1, "the button asks for the next older page");
    assert_eq!(
        window.get_requests().row_count(),
        20,
        "loading earlier must not replace the displayed rows"
    );
    assert!(
        (window.get_request_scroll_y() + 138.0).abs() < 1.0,
        "loading earlier must keep the scroll position"
    );
    assert_eq!(
        window.get_selected_request().as_str(),
        "key-3",
        "loading earlier must keep the open request detail"
    );

    // The app appends the older page, keeps the reader in place and leaves
    // the 较早 page pager alone when the history ends.
    let rows = (0..25).map(request_row).collect::<Vec<_>>();
    window.set_requests(ModelRc::from(rows.as_slice()));
    window.set_has_earlier_records(false);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));
    assert_eq!(window.get_requests().row_count(), 25);
    assert!(
        (window.get_request_scroll_y() + 138.0).abs() < 1.0,
        "the appended rows must not move the scroll position"
    );
    assert!(window.get_request_earlier_hint_visible());
    assert!(
        window.get_request_older_page_enabled(),
        "加载更早 ending must not disable the 较早 page pager"
    );
}

#[test]
fn the_older_page_button_still_starts_a_new_page_at_the_top() {
    let window = request_window();
    set_rows(&window, 20);
    window.set_older_requests(true);
    window.set_request_scroll_y(-138.0);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));
    let queries = Rc::new(Cell::new(0));
    window.on_query_requests({
        let queries = queries.clone();
        move |direction| queries.set(direction)
    });

    click_center(
        &window,
        window.get_request_older_page_x(),
        window.get_request_older_page_y(),
        window.get_request_older_page_width(),
        window.get_request_older_page_height(),
    );

    assert_eq!(queries.get(), 1, "较早 still asks for the next page");
    assert_eq!(
        window.get_request_scroll_y(),
        0.0,
        "较早 starts the new page at its top"
    );
}

#[test]
fn the_no_more_records_hint_keeps_the_displayed_rows() {
    let window = request_window();
    set_rows(&window, 20);
    window.set_older_requests(true);
    window.set_has_earlier_records(false);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(300));

    assert!(window.get_request_earlier_hint_visible());
    assert_eq!(
        window.get_request_earlier_hint_text().as_str(),
        "没有更早记录"
    );
    assert_eq!(
        window.get_requests().row_count(),
        20,
        "no empty list replaces the displayed rows"
    );
    assert!(!window.get_request_load_earlier_enabled());
    assert!(
        window.get_request_older_page_enabled(),
        "the 较早 page pager keeps its own cursor"
    );
}
