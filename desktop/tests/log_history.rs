//! Server-side run-log history for the log page (#158).
//!
//! The history layer reads the gateway's `/admin/um/logs` face; the live layer
//! keeps tailing the instance's own `gateway.log`. These tests exercise the
//! external behaviour: the five time windows, the multi-select level filter, the
//! gateway source closed set, load-earlier appends and the failure/empty states.

use std::cell::Cell;
use std::rc::Rc;

use chrono::{DateTime, Local};
use serde_json::json;
use slint::platform::{PointerEventButton, WindowEvent};
use slint::{ComponentHandle, LogicalPosition, Model, ModelRc, SharedString};
use um_desktop::log_history::{
    append_history_rows, filter_history_rows, history_failure, level_label, parse_history_payload,
    source_label, HistoryQuery, HistoryRange, HistoryRow, LevelSelection,
};
use um_desktop::overview::Tone;

mod main_ui {
    slint::include_modules!();

    pub fn register_chart(window: &MainWindow) {
        slintplot::register!(window);
    }
}

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn log_window() -> main_ui::MainWindow {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = main_ui::MainWindow::new().expect("create MainWindow");
    main_ui::register_chart(&window);
    window
        .window()
        .set_size(slint::LogicalSize::new(1060.0, 720.0));
    window.set_page(3);
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
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
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
}

fn click_center<T: ComponentHandle>(component: &T, x: f32, y: f32, width: f32, height: f32) {
    click(component, x + width / 2.0, y + height / 2.0);
}

#[test]
fn the_log_page_separates_live_and_history_without_pausing_live_follow() {
    let window = log_window();
    window.set_log_follow(false);
    window.set_new_log_count(3);
    assert_eq!(window.get_log_layer_label().as_str(), "实时");
    assert!(!window.get_log_range_visible());
    assert!(window.get_log_follow_visible());

    window.set_log_mode(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_log_layer_label().as_str(), "历史");
    assert!(window.get_log_range_visible());
    assert!(!window.get_log_follow_visible());
    assert!(
        !window.get_log_follow(),
        "switching to history must not resume live follow"
    );
    assert_eq!(
        window.get_new_log_count(),
        3,
        "switching to history must keep the new-log count"
    );

    let queries = Rc::new(Cell::new(0));
    window.on_query_log_history({
        let queries = Rc::clone(&queries);
        move || queries.set(queries.get() + 1)
    });
    click_center(
        &window,
        window.get_log_level_info_x(),
        window.get_log_level_info_y(),
        window.get_log_level_info_width(),
        window.get_log_level_info_height(),
    );
    assert_eq!(queries.get(), 1);
    assert!(!window.get_log_follow());
    assert_eq!(window.get_new_log_count(), 3);
}

#[test]
fn the_five_ranges_and_gateway_sources_are_the_history_filter_surface() {
    let window = log_window();
    window.set_log_mode(1);
    window.set_log_source_options(ModelRc::from([
        SharedString::from("全部来源"),
        "chat2api".into(),
        "edge".into(),
    ]));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_log_source_options().row_count(), 3);
    assert_eq!(window.get_log_source_current().as_str(), "全部来源");

    let ranges = Rc::new(Cell::new(0));
    window.on_select_log_range({
        let ranges = Rc::clone(&ranges);
        move |index| ranges.set(index)
    });
    let x = window.get_log_range_x() + window.get_log_range_width() * 0.9;
    let y = window.get_log_range_y() + window.get_log_range_height() / 2.0;
    click(&window, x, y);
    assert_eq!(ranges.get(), 4);
    assert_eq!(window.get_log_range(), 4);

    let queries = Rc::new(Cell::new(0));
    window.on_query_log_history({
        let queries = Rc::clone(&queries);
        move || queries.set(queries.get() + 1)
    });
    click_center(
        &window,
        window.get_log_source_x(),
        window.get_log_source_y(),
        window.get_log_source_width(),
        window.get_log_source_height(),
    );
    assert!(window.get_log_source_open());
    let row_height = window.get_log_source_option_row_height();
    let option_x = window.get_log_source_option_x() + window.get_log_source_option_width() / 2.0;
    let option_y = window.get_log_source_option_y() + 4.0 + row_height * 1.5;
    click(&window, option_x, option_y);
    assert!(!window.get_log_source_open());
    assert_eq!(window.get_log_source().as_str(), "chat2api");
    assert_eq!(window.get_log_source_current().as_str(), "chat2api");
    assert_eq!(queries.get(), 1);
}

#[test]
fn loading_earlier_keeps_the_displayed_rows_and_the_scroll_position() {
    let window = log_window();
    window.set_log_mode(1);
    let rows: Vec<main_ui::LogRow> = (0..20)
        .map(|index| main_ui::LogRow {
            id: format!("row-{index}").into(),
            at: "10:00:00".into(),
            level: "信息".into(),
            source: "chat2api".into(),
            text: format!("line-{index}").into(),
            detail: format!("line-{index}").into(),
        })
        .collect();
    window.set_logs(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(300));
    window.set_log_scroll_y(-138.0);
    window.set_older_logs(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(300));
    assert!(window.get_log_earlier_enabled());

    let earlier = Rc::new(Cell::new(0));
    window.on_load_earlier_logs({
        let earlier = Rc::clone(&earlier);
        move || earlier.set(earlier.get() + 1)
    });
    click_center(
        &window,
        window.get_log_earlier_x(),
        window.get_log_earlier_y(),
        window.get_log_earlier_width(),
        window.get_log_earlier_height(),
    );
    assert_eq!(earlier.get(), 1);
    assert_eq!(window.get_logs().row_count(), 20);
    assert!(
        (window.get_log_scroll_y() + 138.0).abs() < 1.0,
        "load earlier must keep the scroll position"
    );

    window.set_log_history_note("没有更早记录".into());
    window.set_older_logs(false);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_log_history_note_visible());
    assert_eq!(window.get_log_history_note_text().as_str(), "没有更早记录");
    assert!(!window.get_log_earlier_enabled());
}

fn at(text: &str) -> DateTime<Local> {
    DateTime::parse_from_rfc3339(text)
        .expect("rfc3339")
        .with_timezone(&Local)
}

#[test]
fn the_five_ranges_map_to_their_since_windows_and_all_has_no_since() {
    let now = at("2026-11-23T12:00:00+08:00");
    let midnight = at("2026-11-23T00:00:00+08:00");
    assert_eq!(
        HistoryRange::Today.since(now),
        Some(midnight.timestamp_millis())
    );
    assert_eq!(
        HistoryRange::Day.since(now),
        Some(now.timestamp_millis() - 24 * 60 * 60 * 1000)
    );
    assert_eq!(
        HistoryRange::Week.since(now),
        Some(now.timestamp_millis() - 7 * 24 * 60 * 60 * 1000)
    );
    assert_eq!(
        HistoryRange::Month.since(now),
        Some(now.timestamp_millis() - 30 * 24 * 60 * 60 * 1000)
    );
    assert_eq!(HistoryRange::All.since(now), None);

    assert_eq!(HistoryRange::Today.index(), 0);
    assert_eq!(HistoryRange::Day.index(), 1);
    assert_eq!(HistoryRange::Week.index(), 2);
    assert_eq!(HistoryRange::Month.index(), 3);
    assert_eq!(HistoryRange::All.index(), 4);
    assert_eq!(HistoryRange::from_index(4), HistoryRange::All);
    assert_eq!(HistoryRange::All.key(), "all");
}

#[test]
fn the_query_sends_repeated_levels_source_cursor_and_limit() {
    let now = at("2026-11-23T12:00:00+08:00");
    let query = HistoryQuery {
        range: HistoryRange::Day,
        levels: LevelSelection::from_flags(true, true, false, false),
        source: "chat2api".to_string(),
        keyword: "timeout".to_string(),
        cursor: Some("123".to_string()),
        limit: 100,
    };
    let text = query.query_string(now);
    assert!(
        text.contains(&format!("since={}", now.timestamp_millis() - 86_400_000)),
        "{text}"
    );
    assert!(text.contains("level=error"), "{text}");
    assert!(text.contains("level=warn"), "{text}");
    assert!(!text.contains("level=info"), "{text}");
    assert!(text.contains("source=chat2api"), "{text}");
    assert!(text.contains("cursor=123"), "{text}");
    assert!(text.contains("limit=100"), "{text}");
}

#[test]
fn level_selection_is_multi_select_and_unfiled_rows_show_when_empty() {
    let all = LevelSelection::default();
    assert!(all.matches("error"));
    assert!(all.matches("unknown"));
    assert!(all.matches("raw"));

    let errors = all.toggle("error");
    assert!(errors.matches("error"));
    assert!(!errors.matches("warn"));
    assert!(!errors.matches("unknown"), "未分级行只在没有级别筛选时显示");

    let errors_warn = errors.toggle("warn");
    assert!(errors_warn.matches("error"));
    assert!(errors_warn.matches("warn"));
    assert!(!errors_warn.matches("info"));
    assert_eq!(errors_warn.query_levels(), vec!["error", "warn"]);
}

#[test]
fn the_gateway_closed_sets_render_known_values_and_label_unknown_ones() {
    assert_eq!(level_label("error"), "错误");
    assert_eq!(level_label("warn"), "警告");
    assert_eq!(level_label("info"), "信息");
    assert_eq!(level_label("debug"), "调试");
    assert_eq!(level_label("unknown"), "未分级");
    assert_eq!(level_label("raw"), "原始");
    assert_eq!(level_label("trace"), "未知");

    let sources = vec!["chat2api".to_string()];
    assert_eq!(source_label("chat2api", &sources), "chat2api");
    assert_eq!(source_label("ghost", &sources), "未知");
    assert_eq!(source_label("", &sources), "未知");
}

#[test]
fn the_history_payload_parses_rows_sources_and_the_earlier_cursor() {
    let payload = json!({
        "status": "ok",
        "error": null,
        "lines": [
            {"key": "10", "at": 1000, "level": "error", "source": "chat2api", "text": "boom", "requestId": "req-1"},
            {"key": "9", "at": 2000, "level": "trace", "source": "ghost", "text": "mystery"},
            {"key": "8", "at": null, "level": "unknown", "source": "chat2api", "text": "raw"}
        ],
        "sources": ["chat2api"],
        "scope": "bounded",
        "earlier": "8"
    });
    let view = parse_history_payload(&payload).expect("history");
    assert_eq!(view.sources, vec!["chat2api"]);
    assert_eq!(view.rows.len(), 3);
    assert_eq!(view.rows[0].key, "10");
    assert_eq!(view.rows[0].at, Some(1000));
    assert_eq!(view.rows[0].level, "错误");
    assert_eq!(view.rows[0].source, "chat2api");
    assert!(view.rows[0].text.contains("boom"));
    assert_eq!(view.rows[1].level, "未知");
    assert_eq!(view.rows[1].source, "未知");
    assert_eq!(view.rows[2].level, "未分级");
    assert_eq!(view.earlier, Some("8".to_string()));
    assert_eq!(view.note, "");
    assert!(!view.empty);
}

#[test]
fn an_empty_history_page_is_a_legal_empty_state_not_no_more_history() {
    let payload = json!({
        "status": "ok",
        "error": null,
        "lines": [],
        "sources": ["chat2api"],
        "scope": "bounded",
        "earlier": null
    });
    let view = parse_history_payload(&payload).expect("history");
    assert!(view.empty);
    assert!(view.rows.is_empty());
    assert!(view.status.contains("没有符合"), "{}", view.status);
    assert_eq!(view.note, "没有更早记录");
    assert_eq!(view.tone, Tone::Neutral);
}

#[test]
fn a_last_page_says_there_are_no_earlier_records() {
    let payload = json!({
        "status": "ok",
        "error": null,
        "lines": [
            {"key": "1", "at": 1000, "level": "info", "source": "chat2api", "text": "only"}
        ],
        "sources": ["chat2api"],
        "earlier": null
    });
    let view = parse_history_payload(&payload).expect("history");
    assert_eq!(view.earlier, None);
    assert_eq!(view.note, "没有更早记录");
}

#[test]
fn a_missing_log_file_is_a_read_failure_with_its_reason() {
    let payload = json!({
        "status": "missing",
        "error": "file_missing",
        "lines": [],
        "sources": ["chat2api"],
        "earlier": null,
        "coverage": {"note": "日志文件缺失不是合法空列表"}
    });
    let view = parse_history_payload(&payload).expect("history");
    assert_eq!(view.tone, Tone::Bad);
    assert!(view.status.contains("日志文件缺失"), "{}", view.status);
    assert!(view.status.contains("file_missing"), "{}", view.status);
    assert!(!view.empty);
}

#[test]
fn a_malformed_payload_is_an_error_not_an_empty_history() {
    let error = parse_history_payload(&json!({"status": "ok", "sources": []}))
        .expect_err("missing lines must fail");
    assert!(error.contains("lines"), "{error}");
}

#[test]
fn the_keyword_filter_only_narrows_the_history_rows() {
    let mut timeout = row("2");
    timeout.text = "request timeout".to_string();
    timeout.detail = "request timeout".to_string();
    let rows = vec![row("1"), timeout];
    assert_eq!(filter_history_rows(&rows, "").len(), 2);
    let filtered = filter_history_rows(&rows, " TIMEOUT ");
    assert_eq!(filtered.len(), 1);
    assert_eq!(filtered[0].key, "2");
    assert!(filter_history_rows(&rows, "missing").is_empty());
}

#[test]
fn loading_earlier_appends_older_rows_without_repeating_keys() {
    let previous = vec![row("10"), row("9")];
    let page = view(vec![row("8"), row("7")], Some("7"));
    let rows = append_history_rows(&previous, &page);
    assert_eq!(
        rows.iter().map(|row| row.key.as_str()).collect::<Vec<_>>(),
        vec!["10", "9", "8", "7"]
    );
}

#[test]
fn a_failed_read_keeps_the_previous_rows_and_shows_the_reason() {
    let previous = view(vec![row("10")], Some("10"));
    let failure = history_failure("连接被拒绝".to_string(), Some(&previous));
    assert_eq!(failure.rows, previous.rows);
    assert_eq!(failure.sources, previous.sources);
    assert_eq!(failure.earlier, previous.earlier);
    assert!(failure.status.contains("连接被拒绝"), "{}", failure.status);
    assert_eq!(failure.tone, Tone::Bad);
}

fn row(key: &str) -> HistoryRow {
    HistoryRow {
        key: key.to_string(),
        at: Some(1000),
        level: "信息".to_string(),
        source: "chat2api".to_string(),
        text: format!("line-{key}"),
        detail: format!("line-{key}"),
    }
}

fn view(rows: Vec<HistoryRow>, earlier: Option<&str>) -> um_desktop::log_history::HistoryView {
    um_desktop::log_history::HistoryView {
        status: format!("历史 · 本页 {} 条", rows.len()),
        tone: Tone::Good,
        rows,
        sources: vec!["chat2api".to_string()],
        earlier: earlier.map(str::to_string),
        empty: false,
        note: if earlier.is_none() {
            "没有更早记录".to_string()
        } else {
            String::new()
        },
    }
}
