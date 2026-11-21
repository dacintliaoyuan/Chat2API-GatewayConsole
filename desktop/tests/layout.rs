use std::cell::RefCell;
use std::rc::Rc;

use slint::platform::{Key, PointerEventButton, WindowEvent};
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
    slintplot::register!(window);
    window
        .window()
        .set_size(slint::LogicalSize::new(width, height));
    window.show().expect("show MainWindow");
    // Let the page and chart layout animations settle before reading seams.
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
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

fn click(window: &MainWindow, x: f32, y: f32) {
    let position = slint::LogicalPosition::new(x, y);
    window
        .window()
        .dispatch_event(WindowEvent::PointerMoved { position });
    window.window().dispatch_event(WindowEvent::PointerPressed {
        position,
        button: PointerEventButton::Left,
    });
    window.window().dispatch_event(WindowEvent::PointerReleased {
        position,
        button: PointerEventButton::Left,
    });
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
fn first_open_offers_skip_and_keeps_the_configure_entry_after_skipping() {
    let window = window_at(1060.0, 720.0);
    window.set_show_intro(true);
    window.set_account_label("尚未登录".into());
    window.set_account_detail("".into());
    window.set_account_saved("未配置账号".into());
    window.set_account_saved_detail("".into());
    window.set_account_can_clear(false);
    window.set_account_can_continue(false);
    window.set_login_status("".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(window.get_login_card_visible());
    assert!(window.get_skip_login_visible());
    assert!(window.get_configure_account_visible());

    window.on_skip_login({
        let window = window.as_weak();
        move || {
            if let Some(window) = window.upgrade() {
                window.set_show_intro(false);
            }
        }
    });
    window.invoke_skip_login();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(window.get_login_card_visible());
    assert!(!window.get_skip_login_visible());
    assert!(
        window.get_configure_account_visible(),
        "跳过后总览仍保留「配置账号」入口"
    );

    // Skipping with saved credentials must keep the card too: the explanation
    // and the way back to the account form cannot vanish with the intro flag.
    window.set_account_can_clear(true);
    window.set_show_intro(true);
    window.set_account_label("尚未登录".into());
    window.set_account_saved_detail("".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_login_card_visible());
    window.invoke_skip_login();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(
        window.get_login_card_visible(),
        "已有凭据时跳过登录也不能整卡隐藏"
    );
    assert!(
        window.get_configure_account_visible(),
        "已有凭据时跳过后仍保留「配置账号」入口"
    );
}

#[test]
fn the_um_account_card_shows_the_saved_detail_and_clear_action() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_account_can_clear(true);
    window.set_account_saved_detail("凭据缺少密码".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_account_saved_detail_visible());
    assert!(window.get_account_clear_visible());
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
fn narrow_log_filters_keep_the_level_and_leave_room_for_four_complete_rows() {
    let window = window_at(540.0, 420.0);
    window.set_page(3);
    let rows: Vec<LogRow> = (0..20).map(log_row).collect();
    window.set_logs(ModelRc::from(Rc::new(VecModel::from(rows))));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let height = window.get_log_list_height();
    assert!(
        height >= 4.0 * 36.0,
        "log list height at minimum size: {height}"
    );

    window.set_log_filter_keyword("timeout".into());
    let applied = Rc::new(RefCell::new(None));
    window.on_apply_log_filter({
        let applied = Rc::clone(&applied);
        move |level, keyword| *applied.borrow_mut() = Some((level, keyword))
    });

    // At the minimum width the level filter stays a 5-item segmented control;
    // click the second item ("信息").
    let x = window.get_log_level_x() + window.get_log_level_width() * 0.3;
    let y = window.get_log_level_y() + window.get_log_level_height() / 2.0;
    let position = slint::LogicalPosition::new(x, y);
    window.window().dispatch_event(WindowEvent::PointerPressed {
        position,
        button: PointerEventButton::Left,
    });
    window.window().dispatch_event(WindowEvent::PointerReleased {
        position,
        button: PointerEventButton::Left,
    });
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(*applied.borrow(), Some((1, "timeout".into())));

    window.window().set_size(slint::LogicalSize::new(1060.0, 720.0));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window.window().set_size(slint::LogicalSize::new(540.0, 420.0));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_log_filter_level(), 1);
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
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
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
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
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

#[test]
fn clearing_credentials_requires_a_second_click() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_account_can_clear(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_account_clear_visible());
    assert!(!window.get_clear_confirm_visible());

    let cleared = Rc::new(std::cell::Cell::new(false));
    window.on_clear_credentials({
        let cleared = Rc::clone(&cleared);
        move || cleared.set(true)
    });
    click(
        &window,
        window.get_clear_button_x() + window.get_clear_button_width() / 2.0,
        window.get_clear_button_y() + window.get_clear_button_height() / 2.0,
    );
    assert!(!cleared.get(), "the first click only arms the confirmation");
    assert!(window.get_clear_confirm_visible());
    assert!(!window.get_account_clear_visible());

    click(
        &window,
        window.get_clear_confirm_x() + window.get_clear_confirm_width() / 2.0,
        window.get_clear_confirm_y() + window.get_clear_confirm_height() / 2.0,
    );
    assert!(cleared.get(), "the second click clears the credentials");
}

#[test]
fn the_status_row_tone_follows_the_shell() {
    let window = window_at(1060.0, 720.0);
    assert_eq!(window.get_status_dot_tone(), 0);
    window.set_status_tone(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_status_dot_tone(), 2);
}

#[test]
fn the_page_title_fades_with_the_page_host() {
    let window = window_at(1060.0, 720.0);
    window.on_navigate({
        let window = window.as_weak();
        move |page| {
            if let Some(window) = window.upgrade() {
                window.set_page(page);
            }
        }
    });
    assert_eq!(window.get_page(), 0);
    window.invoke_navigate_to(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(80));
    assert!(
        (window.get_page_title_opacity() - window.get_page_host_opacity()).abs() < 0.01,
        "the title and the page share one opacity"
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(500));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_eq!(window.get_page(), 1);
    assert!((window.get_page_title_opacity() - 1.0).abs() < 0.01);
}

#[test]
fn the_overview_address_chip_copies_the_base_url() {
    let window = window_at(1060.0, 720.0);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let copied = Rc::new(RefCell::new(None::<String>));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| *copied.borrow_mut() = Some(text.to_string())
    });

    let x = window.get_overview_address_x() + window.get_overview_address_width() / 2.0;
    let y = window.get_overview_address_y() + window.get_overview_address_height() / 2.0;
    click(&window, x, y);

    assert_eq!(
        copied.borrow().clone(),
        Some("http://127.0.0.1:7999".to_string()),
        "the overview chip must copy the base URL"
    );
}

#[test]
fn the_overview_v1_chip_copies_the_v1_url() {
    let window = window_at(1060.0, 720.0);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let copied = Rc::new(RefCell::new(None::<String>));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| *copied.borrow_mut() = Some(text.to_string())
    });

    let x = window.get_overview_v1_x() + window.get_overview_v1_width() / 2.0;
    let y = window.get_overview_v1_y() + window.get_overview_v1_height() / 2.0;
    click(&window, x, y);

    assert_eq!(
        copied.borrow().clone(),
        Some("http://127.0.0.1:7999/v1".to_string()),
        "the /v1 chip must copy the base URL with the /v1 suffix"
    );
}

#[test]
fn the_settings_address_row_copies_the_base_and_v1_urls() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_active_address("127.0.0.1:8001".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let copied = Rc::new(RefCell::new(Vec::<String>::new()));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| copied.borrow_mut().push(text.to_string())
    });

    click(
        &window,
        window.get_settings_base_copy_x() + window.get_settings_base_copy_width() / 2.0,
        window.get_settings_base_copy_y() + window.get_settings_base_copy_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().last().map(String::as_str),
        Some("http://127.0.0.1:8001"),
        "the settings base copy entry must copy the base URL"
    );

    click(
        &window,
        window.get_settings_v1_copy_x() + window.get_settings_v1_copy_width() / 2.0,
        window.get_settings_v1_copy_y() + window.get_settings_v1_copy_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().last().map(String::as_str),
        Some("http://127.0.0.1:8001/v1"),
        "the settings /v1 copy entry must copy the base URL with the /v1 suffix"
    );
}

#[test]
fn a_successful_copy_checks_the_clicked_icon_and_clears_after_two_seconds() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window.on_copy_text(|_| {});

    click(
        &window,
        window.get_settings_base_copy_x() + window.get_settings_base_copy_width() / 2.0,
        window.get_settings_base_copy_y() + window.get_settings_base_copy_height() / 2.0,
    );
    window.set_copy_ok(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_settings_base_icon(), "check");
    assert_eq!(window.get_settings_v1_icon(), "copy");
    assert_eq!(window.get_copy_status(), "已复制到剪贴板");
    assert_eq!(window.get_copy_tone(), 0);
    assert!(window.get_copy_status_visible());

    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(1900));
    assert!(window.get_copy_status_visible());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(200));
    assert!(!window.get_copy_status_visible(), "the copy status clears after two seconds");
    assert_eq!(window.get_settings_base_icon(), "copy");

    click(
        &window,
        window.get_settings_v1_copy_x() + window.get_settings_v1_copy_width() / 2.0,
        window.get_settings_v1_copy_y() + window.get_settings_v1_copy_height() / 2.0,
    );
    window.set_copy_ok(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_settings_base_icon(), "copy");
    assert_eq!(window.get_settings_v1_icon(), "check");
    assert_eq!(window.get_copy_status(), "已复制到剪贴板");
}

#[test]
fn a_failed_copy_reports_the_busy_clipboard() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window.on_copy_text(|_| {});

    click(
        &window,
        window.get_settings_base_copy_x() + window.get_settings_base_copy_width() / 2.0,
        window.get_settings_base_copy_y() + window.get_settings_base_copy_height() / 2.0,
    );
    window.set_copy_error("剪贴板被占用".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert_eq!(window.get_copy_status(), "复制失败：剪贴板被占用");
    assert_eq!(window.get_copy_tone(), 2);
    assert!(window.get_copy_status_visible());
    assert_eq!(window.get_settings_base_icon(), "copy");
    assert_eq!(window.get_settings_v1_icon(), "copy");

    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(2100));
    assert!(!window.get_copy_status_visible());
}

#[test]
fn copy_urls_follow_the_active_port() {
    let window = window_at(1060.0, 720.0);
    let copied = Rc::new(RefCell::new(None::<String>));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| *copied.borrow_mut() = Some(text.to_string())
    });

    window.set_active_address("127.0.0.1:8000".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(
        &window,
        window.get_overview_address_x() + window.get_overview_address_width() / 2.0,
        window.get_overview_address_y() + window.get_overview_address_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().clone(),
        Some("http://127.0.0.1:8000".to_string()),
        "the base copy entry follows the port before a switch"
    );

    window.set_active_address("127.0.0.1:8100".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(
        &window,
        window.get_overview_v1_x() + window.get_overview_v1_width() / 2.0,
        window.get_overview_v1_y() + window.get_overview_v1_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().clone(),
        Some("http://127.0.0.1:8100/v1".to_string()),
        "the /v1 copy entry follows the port after a switch"
    );
}

#[test]
fn addresses_stay_copyable_when_the_gateway_is_not_running() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_gateway_value("未运行".into());
    window.set_gateway_tone(2);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let copied = Rc::new(RefCell::new(None::<String>));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| *copied.borrow_mut() = Some(text.to_string())
    });

    click(
        &window,
        window.get_settings_base_copy_x() + window.get_settings_base_copy_width() / 2.0,
        window.get_settings_base_copy_y() + window.get_settings_base_copy_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().clone(),
        Some("http://127.0.0.1:7999".to_string()),
        "copying a saved address must not depend on the gateway state"
    );
}

#[test]
fn the_page_swap_never_shows_a_blank_frame() {
    let window = window_at(1060.0, 720.0);
    window.on_navigate({
        let window = window.as_weak();
        move |page| {
            if let Some(window) = window.upgrade() {
                window.set_page(page);
            }
        }
    });
    window.invoke_navigate_to(2);
    // Let the change handlers register the page-out timer, then advance past it.
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(160));
    assert_eq!(window.get_page(), 2, "the swap must have happened");
    assert!(
        window.get_page_title_opacity() > 0.05,
        "the new page must already be fading in at 160 ms, not blank"
    );
    assert!(
        (window.get_page_title_opacity() - window.get_page_host_opacity()).abs() < 0.01,
        "the title and the page share one opacity"
    );
}
