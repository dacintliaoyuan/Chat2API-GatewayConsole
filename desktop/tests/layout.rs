use std::cell::{Cell, RefCell};
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
    window
        .window()
        .dispatch_event(WindowEvent::PointerReleased {
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
fn theme_mode_switches_dark_and_follows_the_system_theme() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);

    window.set_theme_mode(2);
    assert!(window.get_theme_dark(), "深色档必须启用深色配色");

    window.set_theme_mode(1);
    assert!(!window.get_theme_dark(), "浅色档必须使用浅色配色");

    window.set_system_dark(true);
    window.set_theme_mode(0);
    assert!(window.get_theme_dark(), "跟随系统时使用系统深色");

    window.set_system_dark(false);
    assert!(!window.get_theme_dark(), "跟随系统时使用系统浅色");

    window.set_theme_mode(2);
    assert!(window.get_theme_dark(), "手动深色不受系统浅色影响");
}

#[test]
fn system_theme_changes_notify_the_shell() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    let seen = Rc::new(RefCell::new(Vec::new()));
    window.on_theme_dark_changed({
        let seen = Rc::clone(&seen);
        move |dark| seen.borrow_mut().push(dark)
    });

    window.set_theme_mode(0);
    // The shell reads the derived value once after show; that read is what keeps
    // the binding live for later system changes.
    assert!(!window.get_theme_dark());
    window.set_system_dark(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window.set_system_dark(false);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        *seen.borrow(),
        vec![true, false],
        "跟随系统时，系统主题变化必须通知外壳（DWM 标题栏依赖该回调）"
    );
}

#[test]
fn appearance_segments_report_the_selected_theme_mode() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    let selected = Rc::new(RefCell::new(Vec::new()));
    window.on_theme_mode_changed({
        let selected = Rc::clone(&selected);
        move |index| selected.borrow_mut().push(index)
    });

    // Three equal segments: the third one is 深色.
    let y = window.get_theme_mode_y() + window.get_theme_mode_height() / 2.0;
    let x = window.get_theme_mode_x() + window.get_theme_mode_width() * 5.0 / 6.0;
    click(&window, x, y);
    assert_eq!(*selected.borrow(), vec![2]);
    assert_eq!(window.get_theme_mode(), 2);
    assert!(window.get_theme_dark());

    // The first segment returns to 跟随系统.
    let x = window.get_theme_mode_x() + window.get_theme_mode_width() / 6.0;
    click(&window, x, y);
    assert_eq!(*selected.borrow(), vec![2, 0]);
    assert_eq!(window.get_theme_mode(), 0);
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
    window
        .window()
        .dispatch_event(WindowEvent::PointerReleased {
        position,
        button: PointerEventButton::Left,
    });
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(*applied.borrow(), Some((1, "timeout".into())));

    window
        .window()
        .set_size(slint::LogicalSize::new(1060.0, 720.0));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
        .window()
        .set_size(slint::LogicalSize::new(540.0, 420.0));
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
    assert!(
        !window.get_account_form_visible(),
        "Escape cancels the form"
    );
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

fn click_refresh(window: &MainWindow, x: f32, y: f32, width: f32, height: f32) {
    click(window, x + width / 2.0, y + height / 2.0);
}

fn finish_refresh() {
    for _ in 0..4 {
        i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(150));
    }
}

#[test]
fn every_page_has_a_manual_refresh_button_and_an_updated_at_label() {
    let window = window_at(1060.0, 720.0);
    let calls = Rc::new(RefCell::new(Vec::<&'static str>::new()));
    let overview = Rc::clone(&calls);
    window.on_refresh_overview(move || overview.borrow_mut().push("overview"));
    let requests = Rc::clone(&calls);
    window.on_refresh_requests(move || requests.borrow_mut().push("requests"));
    let logs = Rc::clone(&calls);
    window.on_refresh_logs(move || logs.borrow_mut().push("logs"));
    let settings = Rc::clone(&calls);
    window.on_refresh_settings(move || settings.borrow_mut().push("settings"));

    window.set_overview_updated_at("10:00:00".into());
    window.set_request_updated_at("10:01:00".into());
    window.set_log_updated_at("10:02:00".into());
    window.set_settings_updated_at("10:03:00".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_overview_updated_at_label(), "更新于 10:00:00");
    assert_eq!(window.get_request_updated_at_label(), "更新于 10:01:00");
    assert_eq!(window.get_log_updated_at_label(), "更新于 10:02:00");
    assert_eq!(window.get_settings_updated_at_label(), "更新于 10:03:00");

    window.set_page(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_overview_refresh_enabled());
    click_refresh(
        &window,
        window.get_overview_refresh_x(),
        window.get_overview_refresh_y(),
        window.get_overview_refresh_width(),
        window.get_overview_refresh_height(),
    );
    assert_eq!(calls.borrow().as_slice(), ["overview"]);
    assert!(window.get_refreshing());
    assert!(!window.get_overview_refresh_enabled());
    finish_refresh();

    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click_refresh(
        &window,
        window.get_request_refresh_x(),
        window.get_request_refresh_y(),
        window.get_request_refresh_width(),
        window.get_request_refresh_height(),
    );
    assert_eq!(calls.borrow().as_slice(), ["overview", "requests"]);
    finish_refresh();

    window.set_page(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click_refresh(
        &window,
        window.get_log_refresh_x(),
        window.get_log_refresh_y(),
        window.get_log_refresh_width(),
        window.get_log_refresh_height(),
    );
    assert_eq!(calls.borrow().as_slice(), ["overview", "requests", "logs"]);
    finish_refresh();

    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click_refresh(
        &window,
        window.get_settings_refresh_x(),
        window.get_settings_refresh_y(),
        window.get_settings_refresh_width(),
        window.get_settings_refresh_height(),
    );
    assert_eq!(
        calls.borrow().as_slice(),
        ["overview", "requests", "logs", "settings"]
    );
}

#[test]
fn escape_clears_only_the_current_pages_detail() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    window.set_selected_request("key-1".into());
    window.set_open_log_id("row-1".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    press(&window, Key::Escape);
    assert_eq!(window.get_selected_request().as_str(), "");
    assert_eq!(
        window.get_open_log_id().as_str(),
        "row-1",
        "Escape on the request page must not clear a hidden log detail"
    );

    window.set_page(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    press(&window, Key::Escape);
    assert_eq!(window.get_open_log_id().as_str(), "");
}

#[test]
fn copy_detail_acknowledges_for_1_2_seconds() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    window.set_selected_request("key-1".into());
    window.set_request_detail("request body".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(!window.get_request_detail_copied());

    click(
        &window,
        window.get_request_detail_copy_x() + window.get_request_detail_copy_width() / 2.0,
        window.get_request_detail_copy_y() + window.get_request_detail_copy_height() / 2.0,
    );
    assert!(
        window.get_request_detail_copied(),
        "the copy action must acknowledge itself"
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(1300));
    assert!(
        !window.get_request_detail_copied(),
        "the copy acknowledgement must return to its normal state"
    );
}

#[test]
fn auto_refresh_refreshes_only_the_overview_on_the_selected_interval() {
    let window = window_at(1060.0, 720.0);
    assert_eq!(window.get_auto_refresh(), 5);
    let overviews = Rc::new(Cell::new(0));
    window.on_refresh_overview({
        let overviews = Rc::clone(&overviews);
        move || overviews.set(overviews.get() + 1)
    });
    let logs = Rc::new(Cell::new(0));
    window.on_refresh_logs({
        let logs = Rc::clone(&logs);
        move || logs.set(logs.get() + 1)
    });

    window.set_page(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(5000));
    assert_eq!(
        overviews.get(),
        1,
        "the first five seconds refresh the overview"
    );
    assert_eq!(logs.get(), 0, "the log page never joins auto refresh");
    assert!(window.get_log_follow(), "auto refresh keeps live follow");

    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(5000));
    assert_eq!(overviews.get(), 2);

    window.set_auto_refresh(15);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(5000));
    assert_eq!(overviews.get(), 2, "15 seconds is not five");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(10000));
    assert_eq!(overviews.get(), 3);
    assert_eq!(window.get_auto_refresh_index(), 1);

    window.set_auto_refresh(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(60000));
    assert_eq!(overviews.get(), 3, "off stops the timer");
    assert_eq!(window.get_auto_refresh_index(), 4);
}

#[test]
fn hiding_the_window_pauses_auto_refresh_and_showing_refreshes_immediately() {
    let window = window_at(1060.0, 720.0);
    let overviews = Rc::new(Cell::new(0));
    window.on_refresh_overview({
        let overviews = Rc::clone(&overviews);
        move || overviews.set(overviews.get() + 1)
    });

    window.set_refresh_paused(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(15000));
    assert_eq!(overviews.get(), 0, "a hidden window pauses auto refresh");

    window.set_refresh_paused(false);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        overviews.get(),
        1,
        "showing the window refreshes immediately"
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(5000));
    assert_eq!(
        overviews.get(),
        2,
        "the auto timer restarts after the resume refresh"
    );
}

#[test]
fn clearing_the_search_empties_it_and_reruns_the_query() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    window.set_request_keyword("fixture".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_request_search_clear_visible());

    let queries = Rc::new(std::cell::Cell::new(0));
    window.on_query_requests({
        let queries = Rc::clone(&queries);
        move |_| queries.set(queries.get() + 1)
    });
    click(
        &window,
        window.get_request_search_clear_x() + window.get_request_search_clear_width() / 2.0,
        window.get_request_search_clear_y() + window.get_request_search_clear_height() / 2.0,
    );
    assert_eq!(window.get_request_keyword().as_str(), "");
    assert_eq!(queries.get(), 1, "clearing the search must rerun the query");
}

#[test]
fn tray_less_quit_requires_a_second_click() {
    let window = window_at(1060.0, 720.0);
    window.set_tray_available(false);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let quits = Rc::new(std::cell::Cell::new(0));
    window.on_quit_requested({
        let quits = Rc::clone(&quits);
        move || quits.set(quits.get() + 1)
    });
    assert!(!window.get_quit_confirm_visible());

    click(
        &window,
        window.get_quit_button_x() + window.get_quit_button_width() / 2.0,
        window.get_quit_button_y() + window.get_quit_button_height() / 2.0,
    );
    assert!(window.get_quit_confirm_visible());
    assert_eq!(quits.get(), 0, "the first click only arms the confirmation");

    click(
        &window,
        window.get_quit_confirm_x() + window.get_quit_confirm_width() / 2.0,
        window.get_quit_confirm_y() + window.get_quit_confirm_height() / 2.0,
    );
    assert_eq!(quits.get(), 1, "the second click stops the gateway and quits");
}

#[test]
fn help_page_lists_shortcuts_and_login_guidance() {
    let window = window_at(1060.0, 720.0);
    window.set_page(4);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_help_visible(), "the help page must be reachable");
}

#[test]
fn log_refresh_acknowledgement_expires() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    window.set_log_refreshed(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_log_refreshed());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(1300));
    assert!(
        !window.get_log_refreshed(),
        "the refresh acknowledgement must return to its normal state"
    );
}

#[test]
fn manual_refresh_spins_for_six_hundred_ms_and_disables_the_button() {
    let window = window_at(1060.0, 720.0);
    let overviews = Rc::new(Cell::new(0));
    window.on_refresh_overview({
        let overviews = Rc::clone(&overviews);
        move || overviews.set(overviews.get() + 1)
    });
    assert!(!window.get_refreshing());
    assert_eq!(window.get_refresh_rotation(), 0.0);
    assert!(window.get_overview_refresh_enabled());

    click_refresh(
        &window,
        window.get_overview_refresh_x(),
        window.get_overview_refresh_y(),
        window.get_overview_refresh_width(),
        window.get_overview_refresh_height(),
    );
    assert_eq!(overviews.get(), 1);
    assert!(window.get_refreshing());
    assert!(!window.get_overview_refresh_enabled());

    click_refresh(
        &window,
        window.get_overview_refresh_x(),
        window.get_overview_refresh_y(),
        window.get_overview_refresh_width(),
        window.get_overview_refresh_height(),
    );
    assert_eq!(
        overviews.get(),
        1,
        "the disabled button ignores a second click"
    );

    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(150));
    assert!(
        (window.get_refresh_rotation() - 90.0).abs() < 0.1,
        "the first 150 ms turns the icon 90 degrees"
    );
    assert!(window.get_refreshing());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(150));
    assert!((window.get_refresh_rotation() - 180.0).abs() < 0.1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(150));
    assert!((window.get_refresh_rotation() - 270.0).abs() < 0.1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(150));
    assert!(!window.get_refreshing());
    assert!(window.get_overview_refresh_enabled());
    assert_eq!(window.get_refresh_rotation(), 0.0);

    click_refresh(
        &window,
        window.get_overview_refresh_x(),
        window.get_overview_refresh_y(),
        window.get_overview_refresh_width(),
        window.get_overview_refresh_height(),
    );
    assert_eq!(overviews.get(), 2);
}

#[test]
fn the_um_cell_always_has_a_detail_line() {
    let window = window_at(1060.0, 720.0);
    window.set_account_label("尚未登录".into());
    window.set_account_detail("".into());
    window.set_account_saved("未配置账号".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_account_state_detail_text(), "未配置账号");

    window.set_account_saved("someone@um.edu.mo".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_account_state_detail_text(), "登录后可用");
}
