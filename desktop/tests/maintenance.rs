//! The overview maintenance menu (#161).
//!
//! The five actions target the app instance gateway, which is the UM service
//! itself. A closed action keeps its reason; the records area says 不适用 instead
//! of rendering an empty table.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use slint::platform::PointerEventButton;
use slint::{ComponentHandle, LogicalPosition, LogicalSize, Model};
use um_desktop::maintenance::{
    self, action_result, delegated_status, menu, Action, GatewayPhase, Menu, MenuRow, MenuState,
};
use um_desktop::overview::Tone;
use um_desktop::ui::*;

fn state(gateway: GatewayPhase, service_running: bool) -> MenuState {
    MenuState {
        gateway,
        service_running,
        manual_takeover: false,
        login_running: false,
    }
}

fn row(menu: &Menu, action: Action) -> &MenuRow {
    menu.items
        .iter()
        .find(|row| row.action == action)
        .expect("every action has a row")
}

#[test]
fn a_running_service_offers_four_actions_and_explains_the_closed_start() {
    let menu = menu(&state(GatewayPhase::Running, true));
    assert_eq!(menu.items.len(), 5);

    let start = row(&menu, Action::Start);
    assert!(!start.enabled);
    assert_eq!(start.reason, "本机网关运行中不需要启动");
    assert!(
        start.impact.contains("启动"),
        "the closed start still explains what it would do: {}",
        start.impact
    );

    for action in [Action::Verify, Action::Login, Action::Stop, Action::Restart] {
        let row = row(&menu, action);
        assert!(row.enabled, "{action:?}");
        assert!(row.reason.is_empty(), "{action:?}");
        assert!(!row.impact.is_empty(), "{action:?}");
    }

    assert_eq!(row(&menu, Action::Verify).label, "核验登录");
    assert_eq!(row(&menu, Action::Login).label, "检查并按需登录");
    assert_eq!(row(&menu, Action::Start).label, "启动");
    assert_eq!(row(&menu, Action::Stop).label, "停止");
    assert_eq!(row(&menu, Action::Restart).label, "重启");
}

#[test]
fn a_stopped_gateway_offers_start_and_explains_why_the_rest_are_closed() {
    let menu = menu(&state(GatewayPhase::Down, false));

    let start = row(&menu, Action::Start);
    assert!(start.enabled);
    assert!(start.reason.is_empty());

    for (action, reason) in [
        (Action::Verify, "网关未运行，无法核验登录"),
        (Action::Login, "网关未运行，无法按需登录"),
        (Action::Stop, "网关未运行，无需停止"),
        (Action::Restart, "网关未运行，无需重启"),
    ] {
        let row = row(&menu, action);
        assert!(!row.enabled, "{action:?}");
        assert_eq!(row.reason, reason, "{action:?}");
    }
}

#[test]
fn a_starting_gateway_closes_all_five_actions_until_it_is_ready() {
    let menu = menu(&state(GatewayPhase::Starting, false));
    for action in Action::ALL {
        let row = row(&menu, action);
        assert!(!row.enabled, "{action:?}");
        assert!(
            row.reason.contains("网关正在启动"),
            "{action:?}: {}",
            row.reason
        );
    }
}

#[test]
fn a_manual_takeover_closes_the_on_demand_login_with_the_automatic_submit_reason() {
    let mut current = state(GatewayPhase::Running, true);
    current.manual_takeover = true;
    let menu = menu(&current);

    let login = row(&menu, Action::Login);
    assert!(!login.enabled);
    assert_eq!(login.reason, "人工接管中禁止自动提交");
    assert!(
        row(&menu, Action::Verify).enabled,
        "verify is a read-only probe and stays available"
    );
}

#[test]
fn a_login_round_closes_the_on_demand_login_until_the_round_ends() {
    let mut current = state(GatewayPhase::Running, true);
    current.login_running = true;
    let menu = menu(&current);

    let login = row(&menu, Action::Login);
    assert!(!login.enabled);
    assert_eq!(login.reason, "登录进行中，等待本轮结束");
    assert!(row(&menu, Action::Verify).enabled);
}

#[test]
fn every_action_has_impact_copy_and_a_second_confirmation() {
    let menu = menu(&state(GatewayPhase::Running, true));
    for action in Action::ALL {
        let row = row(&menu, action);
        assert!(!row.impact.is_empty(), "{action:?}");
        assert!(!row.confirm.title.is_empty(), "{action:?}");
        assert!(!row.confirm.body.is_empty(), "{action:?}");
        assert!(!row.confirm.label.is_empty(), "{action:?}");
        assert_eq!(
            row.confirm.danger,
            matches!(action, Action::Stop | Action::Restart),
            "{action:?}"
        );
    }
    assert!(row(&menu, Action::Stop).confirm.body.contains("失败"));
    assert!(row(&menu, Action::Restart).confirm.body.contains("中断"));
    assert!(row(&menu, Action::Verify)
        .confirm
        .body
        .contains("不调用模型"));
}

#[test]
fn the_records_area_says_not_applicable_without_an_empty_table() {
    let menu = menu(&state(GatewayPhase::Running, true));
    assert_eq!(menu.records.status, "不适用（本机实例无维护记录来源）");
    assert!(menu.records.not_applicable);
    assert!(menu.records.rows.is_empty());
    assert_eq!(menu.records.tone, Tone::Neutral);
}

#[test]
fn a_failed_action_keeps_its_reason_and_is_never_folded_into_success() {
    for action in Action::ALL {
        let failed = action_result(action, Err("本机网关返回 HTTP 503".to_string()));
        assert!(
            failed.status.contains("失败"),
            "{action:?}: {}",
            failed.status
        );
        assert!(
            failed.status.contains("本机网关返回 HTTP 503"),
            "{action:?}: {}",
            failed.status
        );
        assert!(
            !failed.status.contains("已完成"),
            "{action:?}: {}",
            failed.status
        );
        assert_eq!(failed.tone, Tone::Bad, "{action:?}");

        let done = action_result(action, Ok("本机网关已停止".to_string()));
        assert!(
            done.status.contains("已完成"),
            "{action:?}: {}",
            done.status
        );
        assert!(done.status.contains("本机网关已停止"), "{action:?}");
        assert_eq!(done.tone, Tone::Good, "{action:?}");
    }
}

#[test]
fn a_delegated_action_reports_where_its_result_is_shown_instead_of_success() {
    for action in [Action::Verify, Action::Login, Action::Restart] {
        let (status, tone) = delegated_status(action);
        assert!(!status.contains("已完成"), "{action:?}: {status}");
        assert!(status.contains("结果见"), "{action:?}: {status}");
        assert_eq!(tone, Tone::Neutral, "{action:?}");
    }
}

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn window() -> MainWindow {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(0);
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    window
}

fn running_state() -> MenuState {
    state(GatewayPhase::Running, true)
}

fn stopped_state() -> MenuState {
    state(GatewayPhase::Down, false)
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

fn click_action(window: &MainWindow, action: Action) {
    let (x, y, width, height) = match action {
        Action::Verify => (
            window.get_maintenance_verify_x(),
            window.get_maintenance_verify_y(),
            window.get_maintenance_verify_width(),
            window.get_maintenance_verify_height(),
        ),
        Action::Login => (
            window.get_maintenance_login_x(),
            window.get_maintenance_login_y(),
            window.get_maintenance_login_width(),
            window.get_maintenance_login_height(),
        ),
        Action::Start => (
            window.get_maintenance_start_x(),
            window.get_maintenance_start_y(),
            window.get_maintenance_start_width(),
            window.get_maintenance_start_height(),
        ),
        Action::Stop => (
            window.get_maintenance_stop_x(),
            window.get_maintenance_stop_y(),
            window.get_maintenance_stop_width(),
            window.get_maintenance_stop_height(),
        ),
        Action::Restart => (
            window.get_maintenance_restart_x(),
            window.get_maintenance_restart_y(),
            window.get_maintenance_restart_width(),
            window.get_maintenance_restart_height(),
        ),
    };
    click(window, x + width / 2.0, y + height / 2.0);
}

fn click_confirm(window: &MainWindow) {
    click(
        window,
        window.get_maintenance_confirm_x() + window.get_maintenance_confirm_width() / 2.0,
        window.get_maintenance_confirm_y() + window.get_maintenance_confirm_height() / 2.0,
    );
}

fn click_cancel(window: &MainWindow) {
    click(
        window,
        window.get_maintenance_cancel_x() + window.get_maintenance_cancel_width() / 2.0,
        window.get_maintenance_cancel_y() + window.get_maintenance_cancel_height() / 2.0,
    );
}

fn open_maintenance_menu(window: &MainWindow) {
    click(
        window,
        window.get_maintenance_toggle_x() + window.get_maintenance_toggle_width() / 2.0,
        window.get_maintenance_toggle_y() + window.get_maintenance_toggle_height() / 2.0,
    );
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    assert!(
        window.get_maintenance_menu_visible(),
        "the maintenance button opens the menu"
    );
}

#[test]
fn the_overview_lists_the_five_actions_with_their_state_and_impact() {
    let window = window();
    let menu = maintenance::menu(&running_state());
    maintenance::apply_menu(&window, &menu);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    open_maintenance_menu(&window);

    let rows = window.get_maintenance_items();
    assert_eq!(rows.row_count(), 5);
    let labels: Vec<String> = (0..5)
        .map(|index| rows.row_data(index).expect("row").label.to_string())
        .collect();
    assert_eq!(
        labels,
        ["核验登录", "检查并按需登录", "启动", "停止", "重启"]
    );

    assert_eq!(
        window.get_maintenance_verify_note(),
        rows.row_data(0).expect("verify row").impact
    );
    assert!(window.get_maintenance_verify_enabled());
    assert!(!window.get_maintenance_start_enabled());
    assert!(
        window.get_maintenance_start_note().contains("不需要启动"),
        "{}",
        window.get_maintenance_start_note()
    );
    assert!(window.get_maintenance_records_not_applicable());
    assert_eq!(window.get_maintenance_record_count(), 0);
    assert!(!window.get_maintenance_table_visible());
    assert_eq!(
        window.get_maintenance_records_status(),
        "不适用（本机实例无维护记录来源）"
    );
}

#[test]
fn every_action_asks_once_and_the_confirm_runs_exactly_that_action() {
    for action in Action::ALL {
        let window = window();
        // Start is only open while the gateway is down; every other action is
        // open while the service runs.
        let menu = if action == Action::Start {
            maintenance::menu(&stopped_state())
        } else {
            maintenance::menu(&running_state())
        };
        maintenance::apply_menu(&window, &menu);
        i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
        open_maintenance_menu(&window);
        let runs = Rc::new(RefCell::new(Vec::new()));
        window.on_maintenance_act({
            let runs = Rc::clone(&runs);
            move |index| runs.borrow_mut().push(index)
        });

        click_action(&window, action);
        i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
        let expected = menu
            .items
            .iter()
            .find(|row| row.action == action)
            .expect("row");
        assert!(
            window.get_maintenance_confirm_visible(),
            "{action:?}: the first click only asks"
        );
        assert_eq!(
            window.get_maintenance_confirm_title(),
            expected.confirm.title,
            "{action:?}"
        );
        assert_eq!(
            window.get_maintenance_confirm_body(),
            expected.confirm.body,
            "{action:?}"
        );
        assert_eq!(
            window.get_maintenance_confirm_label(),
            expected.confirm.label,
            "{action:?}"
        );
        assert_eq!(
            window.get_maintenance_confirm_danger(),
            expected.confirm.danger,
            "{action:?}"
        );
        assert!(runs.borrow().is_empty(), "{action:?}: nothing ran yet");

        click_confirm(&window);
        i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
        assert_eq!(
            runs.borrow().as_slice(),
            [action.index()],
            "{action:?}: the confirm runs exactly that action"
        );
        assert!(!window.get_maintenance_confirm_visible(), "{action:?}");
    }
}

#[test]
fn a_closed_action_does_not_ask_or_run() {
    let window = window();
    let menu = maintenance::menu(&stopped_state());
    maintenance::apply_menu(&window, &menu);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    open_maintenance_menu(&window);
    let runs = Rc::new(RefCell::new(Vec::new()));
    window.on_maintenance_act({
        let runs = Rc::clone(&runs);
        move |index| runs.borrow_mut().push(index)
    });

    click_action(&window, Action::Verify);
    assert!(!window.get_maintenance_confirm_visible());
    assert!(runs.borrow().is_empty(), "a closed action never runs");
}

#[test]
fn cancelling_the_confirmation_does_not_run_the_action() {
    let window = window();
    let menu = maintenance::menu(&running_state());
    maintenance::apply_menu(&window, &menu);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    open_maintenance_menu(&window);
    let runs = Rc::new(RefCell::new(Vec::new()));
    window.on_maintenance_act({
        let runs = Rc::clone(&runs);
        move |index| runs.borrow_mut().push(index)
    });

    click_action(&window, Action::Verify);
    assert!(window.get_maintenance_confirm_visible());
    click_cancel(&window);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    assert!(!window.get_maintenance_confirm_visible());
    assert!(runs.borrow().is_empty(), "cancel runs nothing");
}

#[test]
fn a_failed_action_shows_its_reason_in_the_menu_status() {
    let window = window();
    open_maintenance_menu(&window);
    let result = action_result(Action::Stop, Err("本机网关返回 HTTP 503".to_string()));
    maintenance::apply_result(&window, &result);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert!(window.get_maintenance_status_visible());
    assert_eq!(window.get_maintenance_status(), result.status);
    assert!(
        window.get_maintenance_status().contains("停止失败"),
        "{}",
        window.get_maintenance_status()
    );
    assert!(
        window.get_maintenance_status().contains("HTTP 503"),
        "{}",
        window.get_maintenance_status()
    );
    assert_eq!(window.get_maintenance_status_tone(), 2);
    assert!(
        !window.get_maintenance_status().contains("已完成"),
        "a failure must not be folded into success: {}",
        window.get_maintenance_status()
    );
}

fn window_at(width: f32, height: f32) -> MainWindow {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(0);
    window.window().set_size(LogicalSize::new(width, height));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    window
}

fn press_escape(window: &MainWindow) {
    let text: slint::SharedString = slint::platform::Key::Escape.into();
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::KeyPressed { text: text.clone() });
    window
        .window()
        .dispatch_event(slint::platform::WindowEvent::KeyReleased { text });
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
}

#[test]
fn escape_and_an_outside_click_close_the_maintenance_menu() {
    let window = window();
    let menu = maintenance::menu(&running_state());
    maintenance::apply_menu(&window, &menu);
    open_maintenance_menu(&window);
    press_escape(&window);
    assert!(
        !window.get_maintenance_menu_visible(),
        "Escape closes the only way out of the menu"
    );

    open_maintenance_menu(&window);
    click(&window, 5.0, 400.0);
    assert!(
        !window.get_maintenance_menu_visible(),
        "a click outside the panel closes the menu"
    );
}

#[test]
fn the_small_maintenance_menu_scrolls_and_hides_the_records_area() {
    let window = window_at(540.0, 420.0);
    let menu = maintenance::menu(&running_state());
    maintenance::apply_menu(&window, &menu);
    open_maintenance_menu(&window);

    assert!(
        window.get_maintenance_menu_height() <= 420.0 - 56.0 + 1.0,
        "the menu never runs past the window bottom, actual {}",
        window.get_maintenance_menu_height()
    );
    assert!(
        !window.get_maintenance_menu_scrollable(),
        "the actions fit once the empty records area is hidden"
    );
    assert!(
        !window.get_maintenance_records_area_visible(),
        "a source with no records hides the records area"
    );
}
