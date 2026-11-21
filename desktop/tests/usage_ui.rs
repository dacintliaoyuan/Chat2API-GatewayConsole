//! Slint-level behaviour for the usage chart and the request detail panel.
//!
//! The chart tests drive the same callbacks and keyboard handlers the window
//! uses; the detail tests assert that closing the panel restores the list width
//! without touching the active filters, page or scroll position.

use std::time::Duration;

use slint::platform::{Key, PointerEventButton, WindowEvent};
use slint::{ComponentHandle, LogicalPosition, LogicalSize, Model, ModelRc, SharedString};

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

fn chart_points() -> Vec<main_ui::UsagePoint> {
    vec![
        main_ui::UsagePoint {
            label: "00:00".into(),
            detail_label: "北京时间 00:00".into(),
            token_value: "40".into(),
            request_value: "1".into(),
            token_count: 40.0,
            request_count: 1.0,
        },
        main_ui::UsagePoint {
            label: "01:00".into(),
            detail_label: "北京时间 01:00".into(),
            token_value: "120".into(),
            request_value: "2".into(),
            token_count: 120.0,
            request_count: 2.0,
        },
        main_ui::UsagePoint {
            label: "02:00".into(),
            detail_label: "北京时间 02:00".into(),
            token_value: "80".into(),
            request_value: "1".into(),
            token_count: 80.0,
            request_count: 1.0,
        },
    ]
}

fn chart() -> main_ui::MainWindow {
    init_backend();
    let window = main_ui::MainWindow::new().expect("create MainWindow");
    main_ui::register_chart(&window);
    window.set_page(0);
    let points = chart_points();
    window.set_usage_points(ModelRc::from(points.as_slice()));
    window.set_usage_labels(ModelRc::from([
        SharedString::from("00:00"),
        "01:00".into(),
        "02:00".into(),
    ]));
    window.set_usage_token_counts(ModelRc::from([40.0, 120.0, 80.0]));
    window.set_usage_request_counts(ModelRc::from([1.0, 2.0, 1.0]));
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
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
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
}

fn click_bucket(chart: &main_ui::MainWindow, index: usize) {
    let count = chart.get_usage_points().row_count() as f32;
    let x = chart.get_usage_plot_x() + chart.get_usage_plot_width() / count * (index as f32 + 0.5);
    let y = chart.get_usage_plot_y() + chart.get_usage_plot_height() / 2.0;
    click(chart, x, y);
}

fn click_unpin(chart: &main_ui::MainWindow) {
    let x = chart.get_usage_unpin_x() + chart.get_usage_unpin_width() / 2.0;
    let y = chart.get_usage_unpin_y() + chart.get_usage_unpin_height() / 2.0;
    click(chart, x, y);
}

fn press<T: ComponentHandle>(component: &T, key: Key) {
    let text: SharedString = key.into();
    let window = component.window();
    window.dispatch_event(WindowEvent::KeyPressed { text: text.clone() });
    window.dispatch_event(WindowEvent::KeyReleased { text });
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
}

fn click_request_row(window: &main_ui::MainWindow, row: usize) {
    let x = window.get_request_list_x() + 20.0;
    let y = window.get_request_list_y() + 25.0 + row as f32 * 50.0;
    click(window, x, y);
}

#[test]
fn switching_bar_line_and_token_request_keeps_the_pinned_value() {
    let chart = chart();
    assert!(chart.get_usage_plot_width() > 100.0);
    assert!(chart.get_usage_plot_height() > 50.0);
    click_bucket(&chart, 1);
    assert_eq!(chart.get_usage_pinned_index(), 1);
    assert_eq!(chart.get_usage_selection_label(), "北京时间 01:00");
    assert_eq!(chart.get_usage_selection_value(), "120");
    assert!(chart.get_usage_can_unpin());

    chart.set_usage_show_line(true);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    assert_eq!(chart.get_usage_pinned_index(), 1);
    assert_eq!(chart.get_usage_selection_value(), "120");

    chart.set_usage_show_requests(true);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    assert_eq!(chart.get_usage_pinned_index(), 1);
    assert_eq!(chart.get_usage_selection_value(), "2");

    chart.set_usage_show_line(false);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    assert_eq!(chart.get_usage_pinned_index(), 1);
    assert_eq!(chart.get_usage_selection_value(), "2");
}

#[test]
fn clicking_the_same_bucket_again_or_the_unpin_option_clears_the_pin() {
    let chart = chart();
    click_bucket(&chart, 1);
    assert_eq!(chart.get_usage_pinned_index(), 1);
    click_bucket(&chart, 1);
    assert_eq!(chart.get_usage_pinned_index(), -1);
    assert!(!chart.get_usage_can_unpin());

    click_bucket(&chart, 2);
    assert_eq!(chart.get_usage_pinned_index(), 2);
    assert!(chart.get_usage_can_unpin());
    click_unpin(&chart);
    assert_eq!(chart.get_usage_pinned_index(), -1);
    assert!(!chart.get_usage_can_unpin());
}

#[test]
fn keyboard_browses_and_pins_chart_data() {
    let chart = chart();
    click_bucket(&chart, 0);
    assert_eq!(chart.get_usage_pinned_index(), 0);

    chart.window().dispatch_event(WindowEvent::PointerMoved {
        position: LogicalPosition::new(2.0, 2.0),
    });
    press(&chart, Key::Escape);
    assert!(!chart.get_usage_can_unpin());

    press(&chart, Key::Home);
    press(&chart, Key::RightArrow);
    assert_eq!(chart.get_usage_active_index(), 1);
    assert_eq!(chart.get_usage_selection_value(), "120");
    press(&chart, Key::Return);
    assert_eq!(chart.get_usage_pinned_index(), 1);

    press(&chart, Key::Escape);
    press(&chart, Key::End);
    assert_eq!(chart.get_usage_active_index(), 2);
    press(&chart, Key::Return);
    assert_eq!(chart.get_usage_pinned_index(), 2);
}

fn click_segment<T: ComponentHandle>(
    component: &T,
    x: f32,
    y: f32,
    width: f32,
    index: usize,
    count: usize,
) {
    let item = (width - 4.0) / count as f32;
    click(component, x + 2.0 + item * (index as f32 + 0.5), y + 20.0);
}

#[test]
fn changing_range_through_the_segmented_control_reloads_the_chart() {
    let chart = chart();
    let selected = std::rc::Rc::new(std::cell::Cell::new(None));
    chart.on_usage_range_changed({
        let selected = selected.clone();
        move |index| selected.set(Some(index))
    });

    click_bucket(&chart, 1);
    let x = chart.get_usage_period_x();
    let y = chart.get_usage_period_y();
    let width = chart.get_usage_period_width();
    assert!(width >= 300.0, "five range options need room for 24 小时");

    // The selection block animates for 160 ms; the chart data reload starts
    // only after that, so the block and the plot never disagree.
    click_segment(&chart, x, y, width, 1, 5);
    assert_eq!(chart.get_usage_period(), 1);
    assert_eq!(selected.get(), None);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(160));

    assert_eq!(chart.get_usage_period(), 1);
    assert_eq!(selected.get(), Some(1));
    assert!(!chart.get_usage_can_unpin());

    for index in [2usize, 3, 4, 0] {
        click_segment(&chart, x, y, width, index, 5);
        i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(200));
        assert_eq!(chart.get_usage_period(), index as i32);
        assert_eq!(selected.get(), Some(index as i32));
        assert!(!chart.get_usage_can_unpin());
    }
}

#[test]
fn the_metric_switch_sits_in_the_chart_card_and_switches_the_plot() {
    let chart = chart();
    let chart_x = chart.get_overview_chart_x();
    let chart_y = chart.get_overview_chart_y();
    let chart_width = chart.get_overview_chart_width();
    let chart_height = chart.get_overview_chart_height();
    let metric_x = chart.get_usage_metric_x();
    let metric_y = chart.get_usage_metric_y();
    let metric_width = chart.get_usage_metric_width();
    let metric_height = chart.get_usage_metric_height();
    println!(
        "chart x={chart_x} y={chart_y} w={chart_width} h={chart_height} metric x={metric_x} y={metric_y} w={metric_width} h={metric_height}"
    );
    assert!(
        metric_width > 0.0 && metric_height > 0.0,
        "the metric switch must be visible"
    );
    assert!(
        metric_x >= chart_x
            && metric_y >= chart_y
            && metric_x + metric_width <= chart_x + chart_width + 1.0
            && metric_y + metric_height <= chart_y + chart_height + 1.0,
        "the metric switch must stay inside the usage card"
    );

    click_segment(&chart, metric_x, metric_y, metric_width, 1, 2);
    assert!(chart.get_usage_show_requests());
    click_segment(&chart, metric_x, metric_y, metric_width, 0, 2);
    assert!(!chart.get_usage_show_requests());
}

#[test]
fn the_plot_fills_the_chart_card_without_a_blank_band() {
    let chart = chart();
    let chart_y = chart.get_overview_chart_absolute_y();
    let chart_height = chart.get_overview_chart_height();
    let plot_y = chart.get_usage_plot_y();
    let plot_height = chart.get_usage_plot_height();
    let selection_y = chart.get_usage_selection_y();
    let selection_height = chart.get_usage_selection_height();
    println!(
        "chart y={chart_y} h={chart_height} bottom={} content={} controls y={} h={} plot y={plot_y} h={plot_height} bottom={} selection y={selection_y} h={selection_height} bottom={}",
        chart_y + chart_height,
        chart.get_overview_chart_content_height(),
        chart.get_usage_controls_y(),
        chart.get_usage_controls_height(),
        plot_y + plot_height,
        selection_y + selection_height,
    );
    assert!(
        plot_height > chart_height * 0.45,
        "the plot must take the chart card's remaining height"
    );
    assert!(
        plot_y + plot_height <= selection_y + 1.0,
        "the plot must reach the selection row without a blank band"
    );
    assert!(
        (selection_y + selection_height + 16.0 - (chart_y + chart_height)).abs() < 2.0,
        "the selection row and the card padding must close the card"
    );
}

#[test]
fn changing_request_range_through_the_segmented_control_queries_the_new_range() {
    init_backend();
    let window = main_ui::MainWindow::new().expect("create MainWindow");
    main_ui::register_chart(&window);
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.set_page(1);
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    let queries = std::rc::Rc::new(std::cell::Cell::new(0));
    window.on_query_requests({
        let queries = queries.clone();
        move |_| queries.set(queries.get() + 1)
    });

    let x = window.get_request_period_x();
    let y = window.get_request_period_y();
    let width = window.get_request_period_width();
    assert!(width >= 280.0, "five range options need room for 24 小时");
    for index in 0..5 {
        click_segment(&window, x, y, width, index, 5);
        assert_eq!(window.get_request_period(), index as i32);
        assert_eq!(queries.get(), index + 1);
    }
}

fn request_row(index: usize) -> main_ui::RequestRow {
    main_ui::RequestRow {
        id: format!("key-{index}").into(),
        at: format!("11-20 00:{index:02}:00").into(),
        model: "fixture-model".into(),
        outcome: "成功".into(),
        tone: 0,
        summary: "1200 ms  f314e7d8c7526367baec95ac155b6406".into(),
        detail: "".into(),
    }
}

#[test]
fn closing_request_detail_keeps_filters_page_and_scroll_position() {
    init_backend();
    let window = main_ui::MainWindow::new().expect("create MainWindow");
    main_ui::register_chart(&window);
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.set_page(1);
    let rows = (0..20).map(request_row).collect::<Vec<_>>();
    window.set_requests(ModelRc::from(rows.as_slice()));
    window.set_request_keyword("fixture".into());
    window.set_request_period(1);
    window.set_request_result(2);
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    window.set_request_scroll_y(-138.0);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    let closed_width = window.get_request_list_width();
    window.set_selected_request("key-3".into());
    window.set_request_detail("请求详情".into());
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    click_request_row(&window, 0);
    let open_width = window.get_request_list_width();
    assert!(closed_width > 100.0);
    assert!(
        open_width < closed_width,
        "the detail panel must take width from the list"
    );

    press(&window, Key::Escape);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    assert_eq!(window.get_selected_request().as_str(), "");
    assert_eq!(window.get_request_keyword().as_str(), "fixture");
    assert_eq!(window.get_request_period(), 1);
    assert_eq!(window.get_request_result(), 2);
    assert_eq!(window.get_requests().row_count(), 20);
    assert!(
        (window.get_request_scroll_y() + 138.0).abs() < 1.0,
        "closing the detail must keep the scroll position"
    );
    assert!(
        (window.get_request_list_width() - closed_width).abs() < 1.0,
        "closing the detail must return the full list width"
    );

    let refreshed = std::rc::Rc::new(std::cell::Cell::new(0));
    window.on_refresh_requests({
        let refreshed = refreshed.clone();
        move || refreshed.set(refreshed.get() + 1)
    });
    let x = window.get_request_refresh_x() + window.get_request_refresh_width() / 2.0;
    let y = window.get_request_refresh_y() + window.get_request_refresh_height() / 2.0;
    click(&window, x, y);
    assert_eq!(refreshed.get(), 1);
    assert_eq!(window.get_request_keyword().as_str(), "fixture");
    assert_eq!(window.get_request_period(), 1);
    assert_eq!(window.get_request_result(), 2);
    assert_eq!(window.get_requests().row_count(), 20);
    assert!(
        (window.get_request_scroll_y() + 138.0).abs() < 1.0,
        "refreshing the request page must keep the scroll position"
    );
    assert!(window.get_refreshing());
    for _ in 0..4 {
        i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(150));
    }
    assert!(!window.get_refreshing());
}
