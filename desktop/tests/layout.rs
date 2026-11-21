use std::cell::RefCell;
use std::rc::Rc;

use slint::platform::{Key, WindowEvent};
use slint::{ComponentHandle, ModelRc, VecModel};

slint::include_modules!();

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn init_backend() {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
}

fn window_at(width: f32, height: f32) -> MainWindow {
    init_backend();
    let window = MainWindow::new().expect("create MainWindow");
    window
        .window()
        .set_size(slint::LogicalSize::new(width, height));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
}

fn press(window: &MainWindow, key: Key) {
    let text: slint::SharedString = key.into();
    window
        .window()
        .dispatch_event(WindowEvent::KeyPressed { text: text.clone() });
    window
        .window()
        .dispatch_event(WindowEvent::KeyReleased { text });
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
}

fn type_text(window: &MainWindow, text: &str) {
    for ch in text.chars() {
        let text: slint::SharedString = ch.to_string().into();
        window
            .window()
            .dispatch_event(WindowEvent::KeyPressed { text: text.clone() });
        window
            .window()
            .dispatch_event(WindowEvent::KeyReleased { text });
    }
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
}

fn open_account_form(window: &MainWindow) {
    // The production shell owns the account form state; this hook only opens the
    // same form so the keyboard path can be exercised without the gateway.
    window.on_configure_account({
        let window = window.as_weak();
        move || {
            if let Some(window) = window.upgrade() {
                window.set_page(2);
                window.set_account_form_visible(true);
            }
        }
    });
    window.on_toggle_password_visible({
        let window = window.as_weak();
        move || {
            if let Some(window) = window.upgrade() {
                window.set_password_visible(!window.get_password_visible());
            }
        }
    });
    window.on_close_account_form({
        let window = window.as_weak();
        move || {
            if let Some(window) = window.upgrade() {
                window.set_account_form_visible(false);
            }
        }
    });
    window.invoke_configure_account();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
}

#[test]
fn overview_content_is_visible_and_scrolls_at_minimum_size() {
    let window = window_at(1060.0, 720.0);
    println!(
        "window={}x{} content={} viewport={} scroll_h={} state={} state_content={}x{} login={} login_content={} chart_y={} chart_h={} status_y={} status_h={}",
        window.get_window_width(),
        window.get_window_height(),
        window.get_overview_content_height(),
        window.get_overview_viewport_height(),
        window.get_overview_scroll_content_height(),
        window.get_overview_state_card_height(),
        window.get_overview_state_content_width(),
        window.get_overview_state_content_height(),
        window.get_overview_login_card_height(),
        window.get_overview_login_content_height(),
        window.get_overview_chart_y(),
        window.get_overview_chart_height(),
        window.get_status_row_y(),
        window.get_status_row_height(),
    );
    assert!(window.get_overview_content_height() > 0.0);
    assert!(window.get_overview_state_card_height() > 0.0);
    assert!(window.get_overview_login_card_height() > 0.0);
    assert!(
        window.get_overview_state_card_height() < window.get_overview_viewport_height() / 2.0,
        "the state card must be content-sized, not stretched to the viewport"
    );
    assert!(
        window.get_overview_login_card_height() < window.get_overview_viewport_height() / 2.0,
        "the login card must be content-sized, not stretched to the viewport"
    );
    assert!(
        window.get_overview_chart_y() + window.get_overview_chart_height()
            <= window.get_overview_viewport_height() + 1.0,
        "the usage chart must be visible in the default window"
    );
    assert!(
        window.get_status_row_y() + window.get_status_row_height() <= 720.0 + 1.0,
        "the status row must not be clipped in the default window"
    );

    let window = window_at(540.0, 420.0);
    println!(
        "minimum: window={}x{} content={} viewport={} scroll_content_h={} status_y={} status_h={}",
        window.get_window_width(),
        window.get_window_height(),
        window.get_overview_content_height(),
        window.get_overview_viewport_height(),
        window.get_overview_scroll_content_height(),
        window.get_status_row_y(),
        window.get_status_row_height(),
    );
    assert!(
        window.get_overview_content_height() > window.get_overview_viewport_height(),
        "the overview must scroll at the minimum window size"
    );
    assert!(
        window.get_overview_scroll_content_height() >= window.get_overview_content_height(),
        "the scroll extent must cover the overview content"
    );
    assert!(
        window.get_status_row_y() + window.get_status_row_height() <= 420.0 + 1.0,
        "the status row must not be clipped at the minimum window size"
    );
}

#[test]
fn settings_scrolls_to_the_account_form_at_minimum_size() {
    let window = window_at(540.0, 420.0);
    window.set_page(2);
    open_account_form(&window);
    println!(
        "settings: content={} viewport={}",
        window.get_settings_content_height(),
        window.get_settings_viewport_height(),
    );
    assert!(window.get_account_form_visible());
    assert!(
        window.get_settings_content_height() > window.get_settings_viewport_height(),
        "the settings page must scroll at the minimum window size"
    );
}

#[test]
fn account_form_can_be_completed_with_the_keyboard() {
    let window = window_at(1060.0, 720.0);
    let saved = Rc::new(RefCell::new(None::<(String, String)>));
    let captured = Rc::clone(&saved);
    window.on_save_credentials(move |account, password| {
        *captured.borrow_mut() = Some((account.to_string(), password.to_string()));
    });

    open_account_form(&window);
    assert!(window.get_account_form_visible());
    assert!(
        window.get_account_input_focused(),
        "opening the form focuses the account field"
    );

    type_text(&window, "someone@um.edu.mo");
    press(&window, Key::Return);
    assert!(
        window.get_password_input_focused(),
        "Enter in the account field moves to the password field"
    );

    type_text(&window, "secret");
    press(&window, Key::Return);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        saved.borrow().clone(),
        Some(("someone@um.edu.mo".to_string(), "secret".to_string())),
        "Enter in the password field saves the credentials"
    );
}

fn log_row(index: usize) -> LogRow {
    LogRow {
        id: format!("row-{index}").into(),
        at: "10:00:00".into(),
        level: "信息".into(),
        text: format!("line-{index}").into(),
        detail: format!("2026-11-21 10:00:00 +08:00  信息\n\nline-{index}").into(),
    }
}

#[test]
fn closing_the_log_detail_gives_its_height_back_to_the_list() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    let rows: Vec<LogRow> = (0..20).map(log_row).collect();
    window.set_logs(ModelRc::from(Rc::new(VecModel::from(rows))));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let closed_list = window.get_log_list_height();
    assert!(closed_list > 0.0, "the log list must fill the page");
    assert_eq!(window.get_log_details_height(), 0.0);

    window.set_open_log_id("row-1".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    let open_list = window.get_log_list_height();
    assert!(
        window.get_log_details_height() > 0.0,
        "opening a log row shows the detail panel"
    );
    assert!(
        open_list < closed_list,
        "the open detail panel must shorten the list ({open_list} vs {closed_list})"
    );

    window.set_open_log_id("".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_log_details_height(), 0.0);
    assert!(
        (window.get_log_list_height() - closed_list).abs() < 1.0,
        "closing the detail must restore the list height ({} vs {closed_list})",
        window.get_log_list_height()
    );
}

#[test]
fn account_form_tab_order_reaches_toggle_and_buttons() {
    let window = window_at(1060.0, 720.0);
    open_account_form(&window);
    type_text(&window, "someone@um.edu.mo");
    press(&window, Key::Tab);
    assert!(window.get_password_input_focused());
    type_text(&window, "secret");
    press(&window, Key::Tab);
    assert!(
        window.get_password_toggle_focused(),
        "Tab reaches the show-password checkbox"
    );
    press(&window, Key::Space);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_password_visible(), "Space toggles show-password");
    press(&window, Key::Tab);
    assert!(window.get_cancel_button_focused(), "Tab reaches cancel");
    press(&window, Key::Tab);
    assert!(window.get_save_button_focused(), "Tab reaches save");
    press(&window, Key::Escape);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(!window.get_account_form_visible(), "Escape cancels the form");
}
