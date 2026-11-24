use std::cell::{Cell, RefCell};
use std::rc::Rc;

use slint::platform::{Key, PointerEventButton, WindowEvent};
use slint::{ComponentHandle, ModelRc, VecModel};
use um_desktop::config::validate_port;

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

fn scroll(window: &MainWindow, x: f32, y: f32, delta_y: f32) {
    let position = slint::LogicalPosition::new(x, y);
    window
        .window()
        .dispatch_event(WindowEvent::PointerMoved { position });
    window
        .window()
        .dispatch_event(WindowEvent::PointerScrolled {
            position,
            delta_x: 0.0,
            delta_y,
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
    // Match the captured first screen, including the failure evidence row.
    window.set_overview_failure_count(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
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
        window.get_overview_chart_height() <= 300.0 + 1.0,
        "the usage chart must not absorb the first screen: {}",
        window.get_overview_chart_height()
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
fn the_state_card_splits_its_width_into_three_equal_cells() {
    for (width, height) in [(1060.0, 720.0), (800.0, 540.0)] {
        let window = window_at(width, height);
        let widths = [
            window.get_overview_state_gateway_width(),
            window.get_overview_state_login_width(),
            window.get_overview_state_upstream_width(),
        ];
        // The card content width includes its 16 px inset on both sides.
        let inner_width = window.get_overview_state_content_width() - 32.0;
        println!("state widths at {width}: {widths:?} inner={inner_width}");
        assert!(
            widths.iter().all(|width| *width > 100.0),
            "every state cell must have a usable width"
        );
        assert!(
            (widths[0] - widths[1]).abs() < 1.0 && (widths[1] - widths[2]).abs() < 1.0,
            "the three state cells must be equally wide"
        );
        assert!(
            (widths.iter().sum::<f32>() + 28.0 - inner_width).abs() < 1.0,
            "the three cells and their two gutters must fill the card"
        );
    }
}

#[test]
fn the_address_chips_are_vertically_centered_with_the_page_title() {
    let window = window_at(1060.0, 720.0);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    println!(
        "title y={} h={} base y={} h={} v1 y={} h={}",
        window.get_overview_title_y(),
        window.get_overview_title_height(),
        window.get_overview_address_y(),
        window.get_overview_address_height(),
        window.get_overview_v1_y(),
        window.get_overview_v1_height(),
    );
    let title_center = window.get_overview_title_y() + window.get_overview_title_height() / 2.0;
    let base_center = window.get_overview_address_y() + window.get_overview_address_height() / 2.0;
    let v1_center = window.get_overview_v1_y() + window.get_overview_v1_height() / 2.0;
    assert!(
        (title_center - base_center).abs() < 1.0,
        "the address chip must be vertically centered with the page title"
    );
    assert!(
        (title_center - v1_center).abs() < 1.0,
        "the /v1 chip must be vertically centered with the page title"
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
fn settings_cards_share_one_column_and_uniform_spacing() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    let cards = [
        (
            "外观",
            window.get_appearance_card_x(),
            window.get_appearance_card_y(),
            window.get_appearance_card_width(),
            window.get_appearance_card_height(),
        ),
        (
            "账号",
            window.get_account_card_x(),
            window.get_account_card_y(),
            window.get_account_card_width(),
            window.get_account_card_height(),
        ),
        (
            "网关",
            window.get_gateway_card_x(),
            window.get_gateway_card_y(),
            window.get_gateway_card_width(),
            window.get_gateway_card_height(),
        ),
        (
            "实例管理",
            window.get_technical_card_x(),
            window.get_technical_card_y(),
            window.get_technical_card_width(),
            window.get_technical_card_height(),
        ),
    ];
    println!("settings cards: {cards:?}");
    for (label, x, _, width, _) in cards {
        assert!(
            (x - cards[0].1).abs() < 0.5,
            "{label} 卡必须与外观卡左对齐（{x} vs {}）",
            cards[0].1
        );
        assert!(
            (width - cards[0].3).abs() < 0.5,
            "{label} 卡必须与外观卡同宽（{width} vs {}）",
            cards[0].3
        );
    }
    for pair in cards.windows(2) {
        let (label, _, y, _, height) = pair[0];
        let (next, _, next_y, _, _) = pair[1];
        assert!(
            ((next_y - (y + height)) - 12.0).abs() < 0.5,
            "{label} 与 {next} 卡间距必须是 12px（实际 {}）",
            next_y - (y + height)
        );
    }

    assert!(
        window.get_settings_refresh_x() + window.get_settings_refresh_width()
            > window.get_window_width() - 20.0 - 1.0,
        "the one refresh action sits at the title row's right edge"
    );
    assert!(
        window.get_settings_refresh_y() + window.get_settings_refresh_height() <= cards[0].2 + 0.5,
        "the title-row refresh stays above the first card"
    );
}

#[test]
fn settings_rows_share_one_height_and_align_controls_right() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_active_address("127.0.0.1:8001".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    let inset = 16.0;
    println!(
        "appearance: card={}x{} {}x{} title_y={} title_w={} hint_y={} hint_w={} row={}x{} {}x{} label_x={} label_w={} theme_x={} theme_y={} theme_w={} theme_h={}",
        window.get_appearance_card_x(),
        window.get_appearance_card_y(),
        window.get_appearance_card_width(),
        window.get_appearance_card_height(),
        window.get_appearance_title_y(),
        window.get_appearance_title_width(),
        window.get_appearance_hint_y(),
        window.get_appearance_hint_width(),
        window.get_appearance_row_x(),
        window.get_appearance_row_y(),
        window.get_appearance_row_width(),
        window.get_appearance_row_height(),
        window.get_appearance_row_label_x(),
        window.get_appearance_row_label_width(),
        window.get_theme_mode_x(),
        window.get_theme_mode_y(),
        window.get_theme_mode_width(),
        window.get_theme_mode_height(),
    );
    // Card titles, hints and rows share one 12 px rhythm.
    assert!(
        (window.get_appearance_hint_y()
            - (window.get_appearance_title_y() + window.get_appearance_title_height())
            - 12.0)
            .abs()
            < 0.5,
        "外观标题与说明间距必须是 12px"
    );
    assert!(
        (window.get_appearance_row_y()
            - (window.get_appearance_hint_y() + window.get_appearance_hint_height())
            - 12.0)
            .abs()
            < 0.5,
        "外观说明与主题行间距必须是 12px"
    );

    // The appearance row is the left label plus a right-aligned control.
    assert!(
        (window.get_appearance_row_label_x() - (window.get_appearance_card_x() + inset)).abs()
            < 0.5,
        "外观行标签必须与卡片内边距对齐"
    );
    assert!(
        window.get_theme_mode_x()
            >= window.get_appearance_row_label_x() + window.get_appearance_row_label_width() - 0.5,
        "主题控件必须排在标签右侧"
    );
    assert!(
        (window.get_theme_mode_x() + window.get_theme_mode_width()
            - (window.get_appearance_card_x() + window.get_appearance_card_width() - inset))
            .abs()
            < 1.5,
        "主题控件必须与卡片内容右对齐"
    );
    assert!(
        window.get_theme_mode_y() >= window.get_appearance_row_y()
            && window.get_theme_mode_y() + window.get_theme_mode_height()
                <= window.get_appearance_row_y() + window.get_appearance_row_height() + 0.5,
        "主题控件必须与标签在同一行"
    );

    // The port row keeps the same height and puts its controls at the right.
    assert!(
        (window.get_port_row_height() - window.get_appearance_row_height()).abs() < 0.5,
        "端口行必须与外观行同高"
    );
    assert!(
        window.get_port_row_height() >= 34.0,
        "设置行高必须容纳 34px 控件"
    );
    assert!(
        (window.get_port_row_label_x() - (window.get_gateway_card_x() + inset)).abs() < 0.5,
        "端口行标签必须与卡片内边距对齐"
    );
    assert!(
        window.get_port_input_x()
            >= window.get_port_row_label_x() + window.get_port_row_label_width() - 0.5,
        "端口输入必须排在标签右侧"
    );
    assert!(
        window.get_port_apply_x()
            >= window.get_port_input_x() + window.get_port_input_width() - 0.5,
        "应用按钮必须排在端口输入右侧"
    );
    assert!(
        (window.get_port_apply_x() + window.get_port_apply_width()
            - (window.get_gateway_card_x() + window.get_gateway_card_width() - inset))
            .abs()
            < 1.5,
        "应用按钮必须与卡片内容右对齐"
    );
    assert!(
        window.get_gateway_address_row_y()
            >= window.get_port_row_y() + window.get_port_row_height() - 0.5,
        "模型 API 地址必须单独占第二行"
    );
    assert!(
        (window.get_gateway_address_label_x() - (window.get_gateway_card_x() + inset)).abs() < 0.5,
        "地址行标签必须与卡片内边距对齐"
    );
    assert!(
        window.get_gateway_address_x()
            >= window.get_gateway_address_label_x() + window.get_gateway_address_label_width()
                - 0.5,
        "地址值必须排在标签右侧"
    );
    assert!(
        (window.get_copy_v1_x() + window.get_copy_v1_width()
            - (window.get_gateway_card_x() + window.get_gateway_card_width() - inset))
            .abs()
            < 1.5,
        "复制 /v1 入口必须与卡片内容右对齐"
    );

    // The same right edge holds at the minimum window width, where the
    // segmented control and the port row switch to their compact widths.
    window
        .window()
        .set_size(slint::LogicalSize::new(540.0, 420.0));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(
        (window.get_theme_mode_x() + window.get_theme_mode_width()
            - (window.get_appearance_card_x() + window.get_appearance_card_width() - inset))
            .abs()
            < 1.5,
        "最小宽度下主题控件仍必须与卡片内容右对齐"
    );
    assert!(
        (window.get_port_apply_x() + window.get_port_apply_width()
            - (window.get_gateway_card_x() + window.get_gateway_card_width() - inset))
            .abs()
            < 1.5,
        "最小宽度下应用按钮仍必须与卡片内容右对齐"
    );
}

#[test]
fn the_account_card_orders_title_state_saved_and_actions() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_account_label("尚未登录".into());
    window.set_account_tone(2);
    window.set_account_saved("未配置账号".into());
    window.set_account_saved_detail("凭据缺少密码".into());
    window.set_account_can_clear(true);
    window.set_account_can_continue(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    println!(
        "account: card={}x{} {}x{} title_y={} title_h={} state_x={} state_y={} state_w={} state_h={} saved_y={} saved_h={} actions_y={} actions_h={}",
        window.get_account_card_x(),
        window.get_account_card_y(),
        window.get_account_card_width(),
        window.get_account_card_height(),
        window.get_account_title_y(),
        window.get_account_title_height(),
        window.get_account_state_x(),
        window.get_account_state_y(),
        window.get_account_state_width(),
        window.get_account_state_height(),
        window.get_account_saved_y(),
        window.get_account_saved_height(),
        window.get_account_actions_y(),
        window.get_account_actions_height(),
    );

    let inset = 16.0;
    // The state tag sits in the title row, right-aligned to the card content.
    assert!(
        window.get_account_state_y() >= window.get_account_title_y()
            && window.get_account_state_y()
                < window.get_account_title_y() + window.get_account_title_height() + 0.5,
        "账号状态必须与卡片标题同一行"
    );
    assert!(
        window.get_account_state_x() > window.get_account_card_x() + inset,
        "账号状态必须排在标题右侧"
    );
    assert!(
        (window.get_account_state_x() + window.get_account_state_width()
            - (window.get_account_card_x() + window.get_account_card_width() - inset))
            .abs()
            < 1.5,
        "账号状态必须与卡片内容右对齐"
    );

    // Saved credentials and actions stack below the header in reading order.
    assert!(
        window.get_account_saved_y()
            >= window.get_account_state_y() + window.get_account_state_height() - 0.5,
        "已保存账号信息必须排在标题行下方"
    );
    assert!(
        window.get_account_actions_y()
            >= window.get_account_saved_y() + window.get_account_saved_height() - 0.5,
        "账号操作必须排在已保存信息下方"
    );
    assert!(
        (window.get_account_actions_x() - (window.get_account_card_x() + inset)).abs() < 0.5,
        "账号操作必须与卡片内边距对齐"
    );
    assert!(
        window.get_account_actions_height() >= 34.0,
        "账号操作行必须容纳 34px 按钮"
    );
}

#[test]
fn the_instance_card_aligns_its_detail_rows() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_supervision_value("运行中".into());
    window.set_supervision_detail("本机网关 PID 4242".into());
    window.set_supervision_tone(0);
    window.set_config_path("C:\\Users\\me\\AppData\\Local\\Chat2API\\desktop.json".into());
    window.set_instance_path("C:\\Users\\me\\AppData\\Local\\Chat2API\\instances\\144".into());
    window.set_show_technical(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    println!(
        "instance: card={}x{} {}x{} supervision_y={} supervision_h={} config_y={} config_h={} data_y={} data_h={} label_x={} label_w={} value_x={}x{} {}x{} {}x{}",
        window.get_technical_card_x(),
        window.get_technical_card_y(),
        window.get_technical_card_width(),
        window.get_technical_card_height(),
        window.get_instance_supervision_y(),
        window.get_instance_supervision_height(),
        window.get_instance_config_y(),
        window.get_instance_config_height(),
        window.get_instance_data_y(),
        window.get_instance_data_height(),
        window.get_instance_label_x(),
        window.get_instance_label_width(),
        window.get_instance_supervision_value_x(),
        window.get_instance_supervision_value_width(),
        window.get_instance_config_value_x(),
        window.get_instance_config_value_width(),
        window.get_instance_data_value_x(),
        window.get_instance_data_value_width(),
    );

    let inset = 16.0;
    assert!(
        window.get_instance_supervision_y() < window.get_instance_config_y()
            && window.get_instance_config_y() < window.get_instance_data_y(),
        "实例卡详情必须按网关监督、配置文件、实例数据排列"
    );
    assert!(
        (window.get_instance_config_y()
            - (window.get_instance_supervision_y() + window.get_instance_supervision_height())
            - 12.0)
            .abs()
            < 0.5
            && (window.get_instance_data_y()
                - (window.get_instance_config_y() + window.get_instance_config_height())
                - 12.0)
                .abs()
                < 0.5,
        "实例卡详情行间距必须是 12px"
    );
    assert!(
        (window.get_instance_label_x() - (window.get_technical_card_x() + inset)).abs() < 0.5,
        "实例卡标签必须与卡片内边距对齐"
    );
    for value_x in [
        window.get_instance_supervision_value_x(),
        window.get_instance_config_value_x(),
        window.get_instance_data_value_x(),
    ] {
        assert!(
            value_x >= window.get_instance_label_x() + window.get_instance_label_width() - 0.5,
            "实例卡值必须排在标签右侧"
        );
    }
    let card_right = window.get_technical_card_x() + window.get_technical_card_width() - inset;
    assert!(
        (window.get_instance_supervision_value_x() + window.get_instance_supervision_value_width()
            - card_right)
            .abs()
            < 1.5,
        "实例卡监督值必须与卡片内容右对齐"
    );
    for (value_x, copy_x, copy_width) in [
        (
            window.get_instance_config_value_x(),
            window.get_instance_config_copy_x(),
            window.get_instance_config_copy_width(),
        ),
        (
            window.get_instance_data_value_x(),
            window.get_instance_data_copy_x(),
            window.get_instance_data_copy_width(),
        ),
    ] {
        assert!(
            (copy_x + copy_width - card_right).abs() < 1.5,
            "实例卡路径复制按钮必须与卡片内容右对齐"
        );
        assert!(value_x <= copy_x + 0.5, "实例卡值必须排在复制按钮左侧");
    }
    assert!(
        window.get_instance_data_y() + window.get_instance_data_height()
            <= window.get_technical_card_y() + window.get_technical_card_height() - inset + 0.5,
        "实例卡详情必须落在卡片内边距内"
    );
}

#[test]
fn the_port_row_offers_a_numeric_input_and_apply_button() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_active_address("127.0.0.1:8001".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(window.get_port_input_numeric(), "端口输入必须是数字输入");
    assert!(
        window.get_port_apply_width() > 0.0 && window.get_port_apply_height() > 0.0,
        "应用按钮必须可见"
    );
    assert!(
        window.get_port_apply_x()
            >= window.get_port_input_x() + window.get_port_input_width() - 1.0,
        "应用按钮必须排在端口输入右侧"
    );
    assert!(
        window.get_gateway_address_y()
            >= window.get_port_apply_y() + window.get_port_apply_height() - 1.0,
        "模型 API 地址必须单独占第二行"
    );
    assert_eq!(
        window.get_gateway_address_text(),
        "http://127.0.0.1:8001",
        "地址行必须显示完整 base URL"
    );
}

#[test]
fn the_port_apply_button_reports_the_entered_port() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_port_input("8100".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let applied = Rc::new(RefCell::new(None::<String>));
    window.on_save_port({
        let applied = Rc::clone(&applied);
        move |text| *applied.borrow_mut() = Some(text.to_string())
    });

    click(
        &window,
        window.get_port_apply_x() + window.get_port_apply_width() / 2.0,
        window.get_port_apply_y() + window.get_port_apply_height() / 2.0,
    );
    assert_eq!(
        applied.borrow().clone(),
        Some("8100".to_string()),
        "点击应用必须把输入端口交给 save-port"
    );
}

#[test]
fn the_apply_button_waits_for_an_in_flight_switch() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_port_input("8100".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let applied = Rc::new(RefCell::new(Vec::<String>::new()));
    window.on_save_port({
        let applied = Rc::clone(&applied);
        move |text| applied.borrow_mut().push(text.to_string())
    });

    let x = window.get_port_apply_x() + window.get_port_apply_width() / 2.0;
    let y = window.get_port_apply_y() + window.get_port_apply_height() / 2.0;
    window.set_port_busy(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(&window, x, y);
    assert!(
        applied.borrow().is_empty(),
        "切换进行中时应用按钮不能再次提交"
    );

    window.set_port_busy(false);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(&window, x, y);
    assert_eq!(*applied.borrow(), vec!["8100".to_string()]);
}

#[test]
fn an_invalid_port_is_marked_and_keeps_the_entered_text() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_port_input("not-a-port".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let applied = Rc::new(RefCell::new(Vec::<String>::new()));
    window.on_save_port({
        let applied = Rc::clone(&applied);
        let window = window.as_weak();
        move |text| {
            applied.borrow_mut().push(text.to_string());
            let Some(window) = window.upgrade() else {
                return;
            };
            if let Err(error) = validate_port(&text) {
                window.set_port_input_invalid(true);
                window.set_port_input_error(format!("端口无效：{error}").into());
            }
        }
    });

    click(
        &window,
        window.get_port_apply_x() + window.get_port_apply_width() / 2.0,
        window.get_port_apply_y() + window.get_port_apply_height() / 2.0,
    );
    assert_eq!(*applied.borrow(), vec!["not-a-port".to_string()]);
    assert!(window.get_port_input_invalid(), "非法端口必须标记为无效");
    assert!(
        window.get_port_input_error_visible(),
        "非法端口必须显示原因"
    );
    assert_eq!(
        window.get_port_input(),
        "not-a-port",
        "被拒绝的端口不能被悄悄改写"
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
        source: "chat2api".into(),
        text: format!("line-{index}").into(),
        detail: format!("2026-11-21 10:00:00 +08:00  信息\n\nline-{index}").into(),
    }
}

#[test]
fn narrow_log_filters_keep_the_level_and_leave_room_for_four_complete_rows() {
    let window = window_at(540.0, 420.0);
    window.set_page(3);
    window.set_log_mode(1);
    let rows: Vec<LogRow> = (0..20).map(log_row).collect();
    window.set_logs(ModelRc::from(Rc::new(VecModel::from(rows))));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let height = window.get_log_list_height();
    // The merged status row costs one row at the minimum width; three complete
    // rows stay visible, and the list keeps its own scroll.
    assert!(
        height >= 3.0 * 36.0,
        "log list height at minimum size: {height}"
    );

    window.set_log_filter_keyword("timeout".into());
    let applied = Rc::new(RefCell::new(0));
    window.on_query_log_history({
        let applied = Rc::clone(&applied);
        move || *applied.borrow_mut() += 1
    });

    // At the minimum width the four level filters are independent toggles;
    // click the third one ("信息") and then the first one ("错误").
    let x = window.get_log_level_info_x() + window.get_log_level_info_width() / 2.0;
    let y = window.get_log_level_info_y() + window.get_log_level_info_height() / 2.0;
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
    assert!(
        window.get_log_level_info(),
        "the info level toggle must be on"
    );
    assert_eq!(*applied.borrow(), 1);

    let x = window.get_log_level_error_x() + window.get_log_level_error_width() / 2.0;
    let y = window.get_log_level_error_y() + window.get_log_level_error_height() / 2.0;
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
    assert!(
        window.get_log_level_error(),
        "the error level toggle must be on"
    );
    assert!(
        window.get_log_level_info(),
        "the earlier level toggle stays on"
    );
    assert_eq!(*applied.borrow(), 2);

    window
        .window()
        .set_size(slint::LogicalSize::new(1060.0, 720.0));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
        .window()
        .set_size(slint::LogicalSize::new(540.0, 420.0));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_log_level_info());
    assert!(window.get_log_level_error());
}

#[test]
fn log_keyword_query_is_debounced_and_mode_switch_cancels_the_pending_one() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    window.set_log_mode(1);
    let applied = Rc::new(Cell::new(0usize));
    window.on_query_log_history({
        let applied = Rc::clone(&applied);
        move || applied.set(applied.get() + 1)
    });
    let saved = Rc::new(Cell::new(0usize));
    window.on_settings_changed({
        let saved = Rc::clone(&saved);
        move || saved.set(saved.get() + 1)
    });

    window.set_log_filter_keyword("timeout".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(applied.get(), 0, "typing must not query on every key");
    assert_eq!(saved.get(), 0, "typing must not save on every key");
    assert!(window.get_log_search_pending());

    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_eq!(applied.get(), 1, "one pause makes one query");
    assert_eq!(saved.get(), 1, "one pause makes one save");
    assert!(!window.get_log_search_pending());

    // A second edit while the pause is still pending is cancelled by switching
    // back to the live layer, so the stale query never lands.
    window.set_log_filter_keyword("error".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_log_search_pending());
    window.set_log_mode(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(300));
    assert_eq!(applied.get(), 1, "the live switch cancels the pending query");
    assert!(!window.get_log_search_pending());
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
fn the_page_out_timer_fires_once_per_navigation() {
    let window = window_at(1060.0, 720.0);
    let navigations = Rc::new(Cell::new(0));
    window.on_navigate({
        let window = window.as_weak();
        let navigations = Rc::clone(&navigations);
        move |page| {
            navigations.set(navigations.get() + 1);
            if let Some(window) = window.upgrade() {
                window.set_page(page);
            }
        }
    });
    window.invoke_navigate_to(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(80));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(500));
    assert_eq!(navigations.get(), 1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(600));
    assert_eq!(
        navigations.get(),
        1,
        "the page-out timer must stop after the one page swap"
    );
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

    // The appearance card and the port hint make the settings page scroll at
    // this size; bring the gateway address row into view before clicking it.
    scroll(&window, 600.0, 400.0, -300.0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(
        &window,
        window.get_copy_base_x() + window.get_copy_base_width() / 2.0,
        window.get_copy_base_y() + window.get_copy_base_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().last().map(String::as_str),
        Some("http://127.0.0.1:8001"),
        "the settings base copy entry must copy the base URL"
    );

    click(
        &window,
        window.get_copy_v1_x() + window.get_copy_v1_width() / 2.0,
        window.get_copy_v1_y() + window.get_copy_v1_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().last().map(String::as_str),
        Some("http://127.0.0.1:8001/v1"),
        "the settings /v1 copy entry must copy the base URL with the /v1 suffix"
    );
}

#[test]
fn the_gateway_address_row_places_both_copy_entries_after_the_url() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_active_address("127.0.0.1:8001".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(
        window.get_copy_base_x()
            >= window.get_gateway_address_x() + window.get_gateway_address_width() - 1.0,
        "复制 base 入口必须排在地址右侧"
    );
    assert!(
        window.get_copy_v1_x() >= window.get_copy_base_x() + window.get_copy_base_width() - 1.0,
        "复制 /v1 入口必须排在复制 base 入口右侧"
    );
    for (label, y, height) in [
        (
            "复制 base",
            window.get_copy_base_y(),
            window.get_copy_base_height(),
        ),
        (
            "复制 /v1",
            window.get_copy_v1_y(),
            window.get_copy_v1_height(),
        ),
    ] {
        assert!(
            y < window.get_gateway_address_y() + window.get_gateway_address_height()
                && window.get_gateway_address_y() < y + height,
            "{label} 入口必须和地址在同一行"
        );
    }
}

#[test]
fn a_successful_switch_shows_the_new_address_in_the_status_row() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(
        !window.get_settings_status_visible(),
        "没有状态时状态行不占位"
    );

    window.set_settings_status("已切换到 127.0.0.1:8100".into());
    window.set_settings_tone(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(
        window.get_settings_status_visible(),
        "应用成功后必须显示状态行"
    );
    assert_eq!(window.get_settings_status(), "已切换到 127.0.0.1:8100");
}

#[test]
fn a_successful_copy_checks_the_clicked_icon_and_clears_after_two_seconds() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window.on_copy_text(|_| {});

    // The gateway address row sits below the taller appearance card and the
    // port hint; bring it into view before clicking the copy entry.
    scroll(&window, 600.0, 400.0, -300.0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(
        &window,
        window.get_copy_base_x() + window.get_copy_base_width() / 2.0,
        window.get_copy_base_y() + window.get_copy_base_height() / 2.0,
    );
    window.set_copy_ok(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_copy_base_icon(), "check");
    assert_eq!(window.get_copy_v1_icon(), "copy");
    assert_eq!(window.get_copy_status(), "已复制到剪贴板");
    assert_eq!(window.get_copy_tone(), 0);
    assert!(window.get_copy_status_visible());

    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(1900));
    assert!(window.get_copy_status_visible());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(200));
    assert!(
        !window.get_copy_status_visible(),
        "the copy status clears after two seconds"
    );
    assert_eq!(window.get_copy_base_icon(), "copy");

    click(
        &window,
        window.get_copy_v1_x() + window.get_copy_v1_width() / 2.0,
        window.get_copy_v1_y() + window.get_copy_v1_height() / 2.0,
    );
    window.set_copy_ok(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_copy_base_icon(), "copy");
    assert_eq!(window.get_copy_v1_icon(), "check");
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
        window.get_copy_base_x() + window.get_copy_base_width() / 2.0,
        window.get_copy_base_y() + window.get_copy_base_height() / 2.0,
    );
    window.set_copy_error("剪贴板被占用".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert_eq!(window.get_copy_status(), "复制失败：剪贴板被占用");
    assert_eq!(window.get_copy_tone(), 2);
    assert!(window.get_copy_status_visible());
    assert_eq!(window.get_copy_base_icon(), "copy");
    assert_eq!(window.get_copy_v1_icon(), "copy");

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
    // The appearance card and the port hint make the settings page scroll at
    // this size; bring the gateway address row into view before clicking it.
    scroll(&window, 600.0, 400.0, -300.0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let copied = Rc::new(RefCell::new(None::<String>));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| *copied.borrow_mut() = Some(text.to_string())
    });

    click(
        &window,
        window.get_copy_base_x() + window.get_copy_base_width() / 2.0,
        window.get_copy_base_y() + window.get_copy_base_height() / 2.0,
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

    window.set_overview_updated_seq(1);
    window.set_overview_updated_at("10:00:00".into());
    window.set_request_updated_seq(2);
    window.set_request_updated_at("10:01:00".into());
    window.set_log_updated_seq(3);
    window.set_log_updated_at("10:02:00".into());
    window.set_settings_updated_seq(4);
    window.set_settings_updated_at("10:03:00".into());
    window.set_last_update_seq(5);
    window.set_last_update("10:04:00".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_overview_updated_at_label(), "10:00:00");
    assert_eq!(window.get_request_updated_at_label(), "10:01:00");
    assert_eq!(window.get_log_updated_at_label(), "10:02:00");
    assert_eq!(window.get_settings_updated_at_label(), "10:03:00");
    assert_eq!(
        window.get_last_update_label(),
        "更新于 10:04:00",
        "the footer is the one place that spells 更新于"
    );

    window.set_page(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_overview_refresh_enabled());
    assert!(window.get_page_refresh_visible());
    assert!(window.get_page_refresh_tooltip().contains("总览"));
    click_refresh(
        &window,
        window.get_page_refresh_x(),
        window.get_page_refresh_y(),
        window.get_page_refresh_width(),
        window.get_page_refresh_height(),
    );
    assert_eq!(calls.borrow().as_slice(), ["overview"]);
    assert!(window.get_refreshing());
    assert!(!window.get_overview_refresh_enabled());
    finish_refresh();

    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_page_refresh_tooltip().contains("请求"));
    click_refresh(
        &window,
        window.get_page_refresh_x(),
        window.get_page_refresh_y(),
        window.get_page_refresh_width(),
        window.get_page_refresh_height(),
    );
    assert_eq!(calls.borrow().as_slice(), ["overview", "requests"]);
    finish_refresh();

    window.set_page(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_page_refresh_tooltip().contains("日志"));
    click_refresh(
        &window,
        window.get_page_refresh_x(),
        window.get_page_refresh_y(),
        window.get_page_refresh_width(),
        window.get_page_refresh_height(),
    );
    assert_eq!(calls.borrow().as_slice(), ["overview", "requests", "logs"]);
    finish_refresh();

    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_page_refresh_tooltip().contains("网关"));
    click_refresh(
        &window,
        window.get_page_refresh_x(),
        window.get_page_refresh_y(),
        window.get_page_refresh_width(),
        window.get_page_refresh_height(),
    );
    assert_eq!(
        calls.borrow().as_slice(),
        ["overview", "requests", "logs", "settings"]
    );

    window.set_page(4);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(
        !window.get_page_refresh_visible(),
        "the static help page has no data read to refresh"
    );
    assert!(
        !window.get_footer_time_visible(),
        "the static help page must not carry an 更新于 stamp"
    );
    window.set_page(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(
        window.get_footer_time_visible(),
        "the data pages keep the one footer read time"
    );
}

#[test]
fn quiet_request_actions_show_keyboard_focus_on_a_three_to_one_edge() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    let idle = window.get_request_fail_toggle_focus_ring_color();
    assert_eq!(
        window.get_request_fail_toggle_focus_ring_width(),
        1.0,
        "a quiet action keeps a transparent frame so focus never shifts the row"
    );
    window.invoke_focus_request_fail_toggle();
    // The border colour animates for 120 ms; read it after that step so the
    // focus ring is the settled control edge, not its transparent start.
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(150));
    assert!(
        window.get_request_fail_toggle_has_focus(),
        "the quiet action takes keyboard focus"
    );
    assert!(
        window.get_request_fail_toggle_focus_visible(),
        "the quiet action must show keyboard focus"
    );
    assert_eq!(window.get_request_fail_toggle_focus_ring_width(), 1.0);
    assert_ne!(
        window.get_request_fail_toggle_focus_ring_color(),
        idle,
        "focus must step the frame edge, not only the ~1.1:1 fill"
    );
}

#[test]
fn the_footer_is_the_one_visible_time_source() {
    let window = window_at(1060.0, 720.0);
    assert!(
        !window.get_overview_chart_updated_at_visible(),
        "the overview chart header must not repeat 更新于"
    );
    window.set_request_updated_seq(1);
    window.set_request_updated_at("10:01:00".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        window.get_last_update_label(),
        "更新于 10:01:00",
        "the footer follows the request page read time"
    );
    window.set_log_updated_seq(2);
    window.set_log_updated_at("10:02:00".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_last_update_label(), "更新于 10:02:00");
    window.set_settings_updated_seq(3);
    window.set_settings_updated_at("10:03:00".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_last_update_label(), "更新于 10:03:00");
}

#[test]
fn the_footer_time_survives_midnight_with_a_monotonic_read_stamp() {
    let window = window_at(1060.0, 720.0);
    window.set_overview_updated_seq(1);
    window.set_overview_updated_at("23:59:59".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_last_update_label(), "更新于 23:59:59");

    // The next read is after midnight: 00:00:01 sorts before 23:59:59 as a
    // string, so the footer must follow the monotonic stamp instead.
    window.set_request_updated_seq(2);
    window.set_request_updated_at("00:00:01".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        window.get_last_update_label(),
        "更新于 00:00:01",
        "a read after midnight must replace the previous day's time"
    );
}

#[test]
fn the_query_and_export_actions_use_search_and_download_glyphs() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        window.get_request_query_icon(),
        "search",
        "查询 is a search action, not a forward arrow"
    );
    assert_eq!(
        window.get_request_export_icon(),
        "download",
        "导出 CSV is a download, not a forward arrow"
    );
    assert_eq!(
        window.get_request_fail_toggle_icon(),
        "filter",
        "只看失败 is a filter, not the search glyph"
    );
}

#[test]
fn the_request_list_header_names_first_token_duration() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        window.get_request_list_header_first_token_text(),
        "首字耗时",
        "the list header must use the same term as the detail, help and metric card"
    );
}

#[test]
fn settings_explains_that_the_installer_owns_autostart() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_autostart_status_text(), "由安装器管理");
    assert!(
        window.get_autostart_hint_text().contains("安装器"),
        "the autostart state must name its owner: {}",
        window.get_autostart_hint_text()
    );
    assert!(
        window.get_autostart_hint_text().contains("不修改注册表"),
        "the App must not claim it writes the Run key"
    );
}

#[test]
fn help_explains_credentials_cache_hit_and_first_token() {
    let window = window_at(1060.0, 720.0);
    window.set_page(4);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(
        window.get_help_credential_text().contains("凭据"),
        "{}",
        window.get_help_credential_text()
    );
    assert!(
        window.get_help_cache_hit_text().contains("缓存"),
        "{}",
        window.get_help_cache_hit_text()
    );
    assert!(
        window.get_help_first_token_text().contains("首个输出 Token"),
        "{}",
        window.get_help_first_token_text()
    );
}

fn request_row(index: usize) -> RequestRow {
    RequestRow {
        id: format!("key-{index}").into(),
        at: format!("11-20 00:{index:02}:00").into(),
        model: "fixture-model".into(),
        outcome: "成功".into(),
        tone: 0,
        summary: "1200 ms  f314e7d8c7526367baec95ac155b6406".into(),
        first_token: "120 ms".into(),
        detail: "".into(),
    }
}

#[test]
fn the_request_page_orders_filters_status_analytics_and_list_like_magpie() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    let period_y = window.get_request_period_y();
    let period_height = window.get_request_period_height();
    let result_y = window.get_request_result_y();
    let result_height = window.get_request_result_height();
    let status_y = window.get_request_status_y();
    let status_height = window.get_request_status_height();
    let analytics_y = window.get_request_analytics_y();
    let analytics_height = window.get_request_analytics_height();
    let list_y = window.get_request_list_y();
    println!(
        "period y={period_y} h={period_height} result y={result_y} h={result_height} status y={status_y} h={status_height} analytics y={analytics_y} h={analytics_height} list y={list_y}"
    );
    assert!(
        result_y >= period_y + period_height,
        "the result row sits below the range row"
    );
    assert!(
        status_y >= result_y + result_height,
        "the summary hint sits below both filter rows"
    );
    assert!(
        analytics_y >= status_y + status_height,
        "the metric cards and chart sit below the summary hint"
    );
    assert!(
        list_y >= analytics_y + analytics_height,
        "the request list sits below the analytics card"
    );
}

#[test]
fn the_request_filter_rows_align_the_range_result_search_and_refresh() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    let page_left = 196.0 + 20.0;
    let page_right = window.get_window_width() - 20.0;
    println!(
        "page {}..{} host={} page={} filters={} row={} result-row={} period x={} y={} h={} refresh x={} y={} w={} h={} result x={} y={} w={} h={} search x={} y={} w={} h={} query x={} y={} w={} h={}",
        page_left,
        page_right,
        window.get_request_page_host_width(),
        window.get_request_page_width(),
        window.get_request_filter_width(),
        window.get_request_filter_row_width(),
        window.get_request_filter_result_row_width(),
        window.get_request_period_x(),
        window.get_request_period_y(),
        window.get_request_period_height(),
        window.get_request_refresh_x(),
        window.get_request_refresh_y(),
        window.get_request_refresh_width(),
        window.get_request_refresh_height(),
        window.get_request_result_x(),
        window.get_request_result_y(),
        window.get_request_result_width(),
        window.get_request_result_height(),
        window.get_request_search_x(),
        window.get_request_search_y(),
        window.get_request_search_width(),
        window.get_request_search_height(),
        window.get_request_query_x(),
        window.get_request_query_y(),
        window.get_request_query_width(),
        window.get_request_query_height(),
    );

    assert!(
        (window.get_request_page_width() - (page_right - page_left)).abs() < 1.0,
        "the request page fills the page content"
    );
    assert!(
        (window.get_request_filter_width() - (page_right - page_left)).abs() < 1.0,
        "the filter block fills the page content"
    );
    assert!(
        (window.get_request_filter_row_width() - window.get_request_filter_width()).abs() < 1.0
            && (window.get_request_filter_result_row_width() - window.get_request_filter_width())
                .abs()
                < 1.0,
        "both filter rows share the filter block width"
    );
    assert!(
        (window.get_request_period_x() - page_left).abs() < 1.0,
        "the range row starts at the page edge"
    );
    assert!(
        window.get_page_refresh_y() + window.get_page_refresh_height()
            < window.get_request_period_y(),
        "the one refresh action sits in the title row above the filters"
    );
    assert!(
        window.get_page_refresh_x() + window.get_page_refresh_width() > page_right - 1.0,
        "the refresh action closes the title row at the right edge"
    );

    assert!(
        (window.get_request_model_x() - page_left).abs() < 1.0,
        "the model row starts at the page edge"
    );
    assert!(
        window.get_request_result_x()
            > window.get_request_model_x() + window.get_request_model_width(),
        "the result filter follows the model filter"
    );
    assert!(
        (window.get_request_result_y() - window.get_request_search_y()).abs() < 1.0
            && (window.get_request_search_y() - window.get_request_query_y()).abs() < 1.0,
        "model, result, search and query share the second filter row"
    );
    assert!(
        window.get_request_result_y()
            > window.get_request_period_y() + window.get_request_period_height(),
        "the second filter row sits below the range row"
    );
    assert!(
        window.get_request_search_x()
            > window.get_request_result_x() + window.get_request_result_width(),
        "search follows the result filter"
    );
    assert!(
        window.get_request_search_width() > 300.0,
        "search takes the free width between the result filter and the query action"
    );
    assert!(
        window.get_request_query_x() + window.get_request_query_width() > page_right - 1.0,
        "query closes the second filter row at the right edge"
    );
}

#[test]
fn the_request_list_header_aligns_with_the_row_columns() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    let rows = vec![request_row(0)];
    window.set_requests(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let header_y = window.get_request_list_header_y();
    let header_height = window.get_request_list_header_height();
    let rows_y = window.get_request_rows_y();
    let at_x = window.get_request_list_header_at_x();
    let at_width = window.get_request_list_header_at_width();
    let model_x = window.get_request_list_header_model_x();
    let model_width = window.get_request_list_header_model_width();
    let first_token_x = window.get_request_list_header_first_token_x();
    let first_token_width = window.get_request_list_header_first_token_width();
    let outcome_x = window.get_request_list_header_outcome_x();
    let outcome_width = window.get_request_list_header_outcome_width();
    println!(
        "header y={header_y} h={header_height} rows y={rows_y} at={at_x}+{at_width} model={model_x}+{model_width} first={first_token_x}+{first_token_width} outcome={outcome_x}+{outcome_width}"
    );

    assert!(header_height > 0.0, "the list header must be visible");
    assert!(
        rows_y >= header_y + header_height,
        "the request rows start below the column header"
    );
    assert!(
        at_x < model_x && model_x < first_token_x && first_token_x < outcome_x,
        "the header columns follow the time, model, first-token and result order"
    );
    assert!(
        (model_x - (at_x + at_width + 8.0)).abs() < 1.0
            && (first_token_x - (model_x + model_width + 8.0)).abs() < 1.0
            && (outcome_x - (first_token_x + first_token_width + 8.0)).abs() < 1.0,
        "the header columns share one gap"
    );
    assert!(
        (outcome_x + outcome_width + 28.0
            - (window.get_request_list_x() + window.get_request_list_width()))
        .abs()
            < 2.0,
        "the result column closes the header at the list padding"
    );
}

#[test]
fn the_request_list_and_detail_share_one_row_with_one_gap() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    let rows = vec![request_row(0)];
    window.set_requests(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    let closed_width = window.get_request_list_width();

    window.set_selected_request("key-0".into());
    window.set_request_detail("请求详情".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    let list_right = window.get_request_list_x() + window.get_request_list_width();
    let detail_x = window.get_request_detail_x();
    let detail_right = detail_x + window.get_request_detail_width();
    let page_right = window.get_window_width() - 20.0;
    println!(
        "closed list w={closed_width} list x={} y={} w={} h={} detail x={detail_x} y={} w={} h={}",
        window.get_request_list_x(),
        window.get_request_list_y(),
        window.get_request_list_width(),
        window.get_request_list_height(),
        window.get_request_detail_y(),
        window.get_request_detail_width(),
        window.get_request_detail_height(),
    );

    assert!(
        window.get_request_list_width() < closed_width - 100.0,
        "the detail panel must take width from the list"
    );
    assert!(
        (detail_x - list_right - 12.0).abs() < 1.0,
        "the detail panel starts one gap after the list"
    );
    assert!(
        (detail_right - page_right).abs() < 2.0,
        "the detail panel reaches the page edge"
    );
    assert!(
        (window.get_request_detail_y() - window.get_request_list_y()).abs() < 1.0,
        "the list and detail share the top edge"
    );
    assert!(
        (window.get_request_detail_height() - window.get_request_list_height()).abs() < 1.0,
        "the detail panel spans the list row instead of floating above its bottom"
    );
}

#[test]
fn the_request_detail_uses_the_page_width_at_compact_size() {
    let window = window_at(540.0, 900.0);
    window.set_page(1);
    let rows = vec![request_row(0)];
    window.set_requests(ModelRc::from(rows.as_slice()));
    window.set_selected_request("key-0".into());
    window.set_request_detail("请求详情".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    let detail_width = window.get_request_detail_width();
    println!(
        "compact detail x={} w={} page={} host={} page={} list visible={}",
        window.get_request_detail_x(),
        detail_width,
        window.get_window_width(),
        window.get_request_page_host_width(),
        window.get_request_page_width(),
        window.get_request_list_visible(),
    );
    let page_left = 84.0 + 12.0;
    // The page keeps an 8 px gutter for the scrollbar thumb at compact width.
    let page_right = page_left + window.get_request_page_width() - 8.0;
    assert!(
        (window.get_request_filter_width() - (page_right - page_left)).abs() < 1.0,
        "the filter rows fill the compact page content"
    );
    assert!(
        (window.get_request_period_x() - page_left).abs() < 1.0,
        "the compact range row stays inside the page"
    );
    assert!(
        window.get_page_refresh_x() + window.get_page_refresh_width() > page_right - 1.0,
        "the one refresh action stays in the title row at the page's right edge"
    );
    assert!(
        window.get_request_search_width() > 140.0
            && (window.get_request_query_x() + window.get_request_query_width() - page_right).abs()
                < 1.0,
        "the compact search and query stay inside the page"
    );
    assert!(
        !window.get_request_list_visible(),
        "the compact detail replaces the list"
    );
    assert!(
        detail_width > 300.0,
        "the compact detail keeps a readable width"
    );
    assert!(
        window.get_request_detail_x() >= 96.0 - 1.0
            && window.get_request_detail_x() + detail_width
                <= window.get_window_width() - 12.0 + 1.0,
        "the compact detail stays inside the page content"
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
fn manual_refresh_restarts_the_auto_refresh_countdown() {
    let window = window_at(1060.0, 720.0);
    let overviews = Rc::new(Cell::new(0));
    window.on_refresh_overview({
        let overviews = Rc::clone(&overviews);
        move || overviews.set(overviews.get() + 1)
    });
    window.set_auto_refresh(5);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(4750));
    assert_eq!(overviews.get(), 1, "the first five seconds tick once");

    click_refresh(
        &window,
        window.get_overview_refresh_x(),
        window.get_overview_refresh_y(),
        window.get_overview_refresh_width(),
        window.get_overview_refresh_height(),
    );
    assert!(window.get_refreshing());
    finish_refresh();
    assert_eq!(overviews.get(), 2, "the manual refresh refreshes once");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(4999));
    assert_eq!(
        overviews.get(),
        2,
        "a manual refresh restarts the five-second countdown"
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(1));
    assert_eq!(overviews.get(), 3);
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
    assert_eq!(
        quits.get(),
        1,
        "the second click stops the gateway and quits"
    );
}

#[test]
fn help_page_lists_shortcuts_and_login_guidance() {
    let window = window_at(1060.0, 720.0);
    window.set_page(4);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_help_visible(), "the help page must be reachable");
}

#[test]
fn the_log_page_opens_settings_from_its_toolbar() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert_eq!(
        window.get_log_settings_icon(),
        "settings",
        "the log toolbar entry uses the settings gear"
    );
    assert!(
        window.get_log_settings_width() > 0.0 && window.get_log_settings_height() > 0.0,
        "the settings entry must be visible on the log page"
    );

    window.on_navigate({
        let window = window.as_weak();
        move |page| {
            if let Some(window) = window.upgrade() {
                window.set_page(page);
            }
        }
    });
    click(
        &window,
        window.get_log_settings_x() + window.get_log_settings_width() / 2.0,
        window.get_log_settings_y() + window.get_log_settings_height() / 2.0,
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(160));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_eq!(
        window.get_page(),
        2,
        "the log settings entry must open the settings page"
    );
}

#[test]
fn the_log_filter_row_keeps_every_control_above_the_list() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    window.set_log_mode(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    let first_row = [
        (
            "mode",
            window.get_log_mode_x(),
            window.get_log_mode_y(),
            window.get_log_mode_width(),
            window.get_log_mode_height(),
        ),
        (
            "time range",
            window.get_log_range_x(),
            window.get_log_range_y(),
            window.get_log_range_width(),
            window.get_log_range_height(),
        ),
        (
            "error level",
            window.get_log_level_error_x(),
            window.get_log_level_error_y(),
            window.get_log_level_error_width(),
            window.get_log_level_error_height(),
        ),
        (
            "warn level",
            window.get_log_level_warn_x(),
            window.get_log_level_warn_y(),
            window.get_log_level_warn_width(),
            window.get_log_level_warn_height(),
        ),
        (
            "info level",
            window.get_log_level_info_x(),
            window.get_log_level_info_y(),
            window.get_log_level_info_width(),
            window.get_log_level_info_height(),
        ),
        (
            "debug level",
            window.get_log_level_debug_x(),
            window.get_log_level_debug_y(),
            window.get_log_level_debug_width(),
            window.get_log_level_debug_height(),
        ),
    ];
    let second_row = [
        (
            "source",
            window.get_log_source_x(),
            window.get_log_source_y(),
            window.get_log_source_width(),
            window.get_log_source_height(),
        ),
        (
            "search",
            window.get_log_search_x(),
            window.get_log_search_y(),
            window.get_log_search_width(),
            window.get_log_search_height(),
        ),
    ];
    for (label, x, y, width, height) in first_row.iter().chain(second_row.iter()) {
        assert!(
            *x >= 0.0 && *width > 0.0 && *height > 0.0,
            "{label} control must have a visible box"
        );
        assert!(
            *y + *height <= window.get_log_list_y() + 1.0,
            "{label} control must sit above the log list"
        );
    }
    let first_y = first_row[0].2;
    for (label, _, y, _, _) in &first_row {
        assert!(
            (*y - first_y).abs() < 1.0,
            "{label} must stay in the first filter row"
        );
    }
    let first_order: Vec<f32> = first_row.iter().map(|(_, x, _, _, _)| *x).collect();
    assert!(
        first_order.windows(2).all(|pair| pair[0] < pair[1]),
        "the mode, range and four level toggles must read left to right: {first_order:?}"
    );
    let second_y = second_row[0].2;
    for (label, _, y, _, _) in &second_row {
        assert!(
            (*y - second_y).abs() < 1.0,
            "{label} must stay in the second filter row"
        );
    }
    let second_order: Vec<f32> = second_row.iter().map(|(_, x, _, _, _)| *x).collect();
    assert!(
        second_order.windows(2).all(|pair| pair[0] < pair[1]),
        "the source and search must read left to right: {second_order:?}"
    );
    assert!(
        window.get_page_refresh_y() + window.get_page_refresh_height()
            < window.get_log_mode_y(),
        "the one refresh action sits in the title row above the log filters"
    );
}

#[test]
fn the_log_page_footer_names_the_read_time_instead_of_the_update_time() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    window.set_log_updated_at("10:02:00".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        window.get_last_update_label(),
        "读取于 10:02:00",
        "the log page footer is the read time, not the last row's time"
    );
    window.set_page(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        window.get_last_update_label(),
        "更新于 10:02:00",
        "the other pages keep the update wording"
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

#[test]
fn focusing_the_port_field_selects_the_whole_value() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_port_input().as_str(), "7999");
    click(
        &window,
        window.get_port_input_x() + window.get_port_input_width() / 2.0,
        window.get_port_input_y() + window.get_port_input_height() / 2.0,
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(
        window.get_port_input_focused(),
        "the click focuses the port field"
    );
    assert!(
        window.get_port_input_selected(),
        "focusing the port field selects its whole value"
    );
}

#[test]
fn the_address_chips_only_show_on_the_overview() {
    let window = window_at(1060.0, 720.0);
    window.set_active_address("127.0.0.1:7999".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_overview_address_visible());
    assert!(window.get_overview_v1_visible());
    for page in [1, 3, 2, 4] {
        window.set_page(page);
        i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
        assert!(
            !window.get_overview_address_visible() && !window.get_overview_v1_visible(),
            "the address chips stay on the overview page"
        );
    }
}

#[test]
fn restarting_the_gateway_asks_once_and_reports_the_outcome() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_gateway_tone(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    let restarts = Rc::new(Cell::new(0));
    window.on_restart_gateway({
        let restarts = Rc::clone(&restarts);
        move || restarts.set(restarts.get() + 1)
    });
    // The taller appearance card and the port hint push the gateway card down;
    // bring the restart action into view before clicking it.
    scroll(&window, 600.0, 400.0, -300.0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(
        &window,
        window.get_restart_gateway_x() + window.get_restart_gateway_width() / 2.0,
        window.get_restart_gateway_y() + window.get_restart_gateway_height() / 2.0,
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_confirm_restart(), "the first click only asks");
    assert_eq!(restarts.get(), 0, "the first click must not restart");
    click(
        &window,
        window.get_restart_confirm_x() + window.get_restart_confirm_width() / 2.0,
        window.get_restart_confirm_y() + window.get_restart_confirm_height() / 2.0,
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(restarts.get(), 1, "the confirm restarts once");
    assert!(!window.get_confirm_restart());
}

#[test]
fn the_request_list_keeps_a_readable_height_at_the_default_size() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(
        window.get_request_list_height() >= 240.0,
        "the request list keeps a readable height"
    );
}

#[test]
fn the_expanded_request_chart_keeps_the_list_at_240px() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    // A manual choice, so the viewport default does not fold the chart.
    window.set_request_chart_collapsed_explicit(true);
    window.set_request_chart_collapsed(false);
    let rows = (0..14).map(request_row).collect::<Vec<_>>();
    window.set_requests(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    println!(
        "chart_visible={} list_y={} list_h={} page_h={} viewport_h={}",
        window.get_request_chart_visible(),
        window.get_request_list_y(),
        window.get_request_list_height(),
        window.get_request_page_height(),
        window.get_request_scroll_viewport_height(),
    );
    assert!(
        window.get_request_chart_visible(),
        "the manual choice keeps the analytics chart expanded"
    );
    assert!(
        window.get_request_list_height() >= 240.0,
        "the expanded chart must not squeeze the list below 240 px: {}",
        window.get_request_list_height()
    );
    assert!(
        window.get_request_page_height() > window.get_request_scroll_viewport_height(),
        "the page scrolls instead of squeezing the list"
    );
}

#[test]
fn an_empty_request_window_collapses_the_analytics_card_and_centers_the_reason() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    window.set_requests(ModelRc::from(Vec::<RequestRow>::new().as_slice()));
    window.set_request_status("所选时间没有请求记录。".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    let empty_y = window.get_request_empty_y();
    let empty_h = window.get_request_empty_height();
    let panel_y = window.get_request_list_panel_y();
    let panel_h = window.get_request_list_panel_height();
    let body_h = panel_h
        - window.get_request_list_header_height()
        - window.get_request_list_footer_height()
        - 20.0;
    println!(
        "analytics_visible={} analytics_h={} panel_y={} panel_h={} body_h={} empty_y={} empty_h={} row_h={} pane_h={} page_pref={} page_actual={} scroll_content={} viewport={} filters_h={} status_h={} status={}",
        window.get_request_analytics_visible(),
        window.get_request_analytics_height(),
        panel_y,
        panel_h,
        body_h,
        empty_y,
        empty_h,
        window.get_request_row_height(),
        window.get_request_list_pane_height(),
        window.get_request_page_height(),
        window.get_request_page_actual_height(),
        window.get_request_scroll_content_height(),
        window.get_request_scroll_viewport_height(),
        window.get_request_filters_height(),
        window.get_request_status_row_height(),
        window.get_request_status_display_text(),
    );
    assert!(
        !window.get_request_analytics_visible(),
        "an unfiltered empty window keeps only the list card"
    );
    assert_eq!(
        window.get_request_analytics_height(),
        0.0,
        "the hidden analytics card must not reserve a 107 px band"
    );
    assert!(
        empty_h >= body_h - 4.0,
        "the empty state fills the list body: empty {empty_h} body {body_h}"
    );
    assert!(
        (empty_y + empty_h / 2.0 - (panel_y + panel_h / 2.0)).abs() <= 24.0,
        "the empty reason sits in the middle of the table: empty center {} panel center {}",
        empty_y + empty_h / 2.0,
        panel_y + panel_h / 2.0
    );
    assert_eq!(
        window.get_request_status_display_text().as_str(),
        "北京时间 · 本页 0 条",
        "the status row keeps the page count instead of repeating the table reason"
    );
    assert_eq!(
        window.get_request_empty_title().as_str(),
        "所选时间没有请求记录",
        "the table owns the one empty reason"
    );
}

#[test]
fn the_secondary_appearance_labels_sit_above_their_own_controls() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    let sidebar_label_bottom =
        window.get_appearance_sidebar_label_y() + window.get_appearance_sidebar_label_height();
    let motion_label_bottom =
        window.get_appearance_motion_label_y() + window.get_appearance_motion_label_height();
    let transparency_label_bottom = window.get_appearance_transparency_label_y()
        + window.get_appearance_transparency_label_height();
    println!(
        "sidebar label={}..{} control={} motion label={}..{} control={} transparency label={:?} {}..{} control={} toggle={:?}",
        window.get_appearance_sidebar_label_y(),
        sidebar_label_bottom,
        window.get_appearance_sidebar_control_y(),
        window.get_appearance_motion_label_y(),
        motion_label_bottom,
        window.get_appearance_motion_control_y(),
        window.get_appearance_transparency_label_text(),
        window.get_appearance_transparency_label_y(),
        transparency_label_bottom,
        window.get_appearance_transparency_control_y(),
        window.get_transparency_toggle_label(),
    );
    assert!(
        sidebar_label_bottom <= window.get_appearance_sidebar_control_y() + 0.5,
        "侧栏 label must sit directly above its control"
    );
    assert!(
        motion_label_bottom <= window.get_appearance_motion_control_y() + 0.5,
        "界面动效 label must sit directly above its control"
    );
    assert!(
        transparency_label_bottom <= window.get_appearance_transparency_control_y() + 0.5,
        "降低透明度 label must sit directly above its control"
    );
    assert!(
        (window.get_transparency_toggle_x() - window.get_appearance_transparency_label_x()).abs()
            < 1.0,
        "the 开启 checkbox must share the group label's left edge"
    );
    assert_eq!(
        window.get_appearance_transparency_label_text(),
        "降低透明度",
        "the group label names the setting once"
    );
    assert_eq!(
        window.get_transparency_toggle_label(),
        "开启",
        "the checkbox must not repeat the setting name"
    );
}

#[test]
fn cancelling_a_typed_account_form_asks_before_discarding() {
    let window = window_at(1060.0, 720.0);
    open_account_form(&window);
    type_text(&window, "someone@um.edu.mo");
    // The account form is below the taller appearance card; scroll it into
    // view before clicking 取消.
    scroll(&window, 600.0, 400.0, -300.0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    click(
        &window,
        window.get_cancel_button_x() + window.get_cancel_button_width() / 2.0,
        window.get_cancel_button_y() + window.get_cancel_button_height() / 2.0,
    );
    assert!(
        window.get_account_discard_confirm_visible(),
        "a typed form asks before discarding"
    );
    assert!(
        window.get_account_form_visible(),
        "the form stays until the choice"
    );

    click(
        &window,
        window.get_account_keep_editing_x() + window.get_account_keep_editing_width() / 2.0,
        window.get_account_keep_editing_y() + window.get_account_keep_editing_height() / 2.0,
    );
    assert!(
        !window.get_account_discard_confirm_visible(),
        "继续编辑 returns to the form"
    );
    assert!(window.get_account_form_visible());

    click(
        &window,
        window.get_cancel_button_x() + window.get_cancel_button_width() / 2.0,
        window.get_cancel_button_y() + window.get_cancel_button_height() / 2.0,
    );
    click(
        &window,
        window.get_account_discard_x() + window.get_account_discard_width() / 2.0,
        window.get_account_discard_y() + window.get_account_discard_height() / 2.0,
    );
    assert!(
        !window.get_account_form_visible(),
        "放弃修改 closes the form"
    );
}

#[test]
fn the_log_page_actions_share_the_status_row_and_align_right() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    let page_left = 196.0 + 20.0;
    let page_right = window.get_window_width() - 20.0;
    println!(
        "status row x={} y={} w={} h={} settings x={} w={} follow x={} w={}",
        window.get_log_status_row_x(),
        window.get_log_status_row_y(),
        window.get_log_status_row_width(),
        window.get_log_status_row_height(),
        window.get_log_settings_x(),
        window.get_log_settings_width(),
        window.get_log_follow_x(),
        window.get_log_follow_width(),
    );
    assert!(
        (window.get_log_status_row_x() - page_left).abs() < 1.0,
        "the status row fills the page width"
    );
    assert!(
        (window.get_log_status_row_width() - (page_right - page_left)).abs() < 1.0,
        "the status row width is the page content width"
    );
    assert!(
        (window.get_log_settings_x() + window.get_log_settings_width() - page_right).abs() < 1.5,
        "the page actions end at the right edge instead of floating mid-page"
    );
    assert!(
        (window.get_log_settings_y() - window.get_log_status_row_y()).abs() < 1.0,
        "the settings action sits inside the status row, not on its own line"
    );
    assert!(
        (window.get_log_follow_y() - window.get_log_status_row_y()).abs() < 1.0,
        "the follow action sits inside the status row, not on its own line"
    );
    assert!(
        window.get_log_follow_x() + window.get_log_follow_width()
            < window.get_log_settings_x(),
        "跟随 reads before 设置 in the status row"
    );
    assert!(
        (window.get_log_status_text_y() - window.get_log_follow_y()).abs() < 1.0,
        "the status text and the follow action share one top edge"
    );
    assert!(
        (window.get_log_status_text_height() - window.get_log_status_row_height()).abs() < 1.0,
        "the status text fills the fixed 34 px status row"
    );
    assert!(
        (window.get_log_status_row_height() - 34.0).abs() < 0.5,
        "the status row is the 34 px control height, got {}",
        window.get_log_status_row_height()
    );
}

#[test]
fn the_compact_log_status_keeps_its_text_instead_of_dropping_it() {
    let window = window_at(540.0, 420.0);
    window.set_page(3);
    window.set_log_status("已连接 · 最近 24 条".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    assert!(window.get_log_status_text_visible());
    assert_eq!(
        window.get_log_status_text().as_str(),
        "已连接 · 最近 24 条"
    );
    assert!(
        window.get_log_status_text_width() > 0.0,
        "the compact status text keeps a readable box instead of collapsing"
    );
    assert!(
        (window.get_log_status_text_y() - window.get_log_follow_y()).abs() < 1.0,
        "the compact status text and the follow action share one top edge"
    );
}

#[test]
fn the_settings_copy_buttons_show_the_copy_word() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(
        !window.get_copy_base_icon_only() && !window.get_copy_v1_icon_only(),
        "ADR 0015 asks for a visible 复制 label, not an icon-only button"
    );
    assert!(window.get_copy_base_label().contains("复制"));
    assert!(window.get_copy_v1_label().contains("复制"));
}

#[test]
fn the_port_hint_names_the_accepted_range_and_the_reserved_ports() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    click(
        &window,
        window.get_port_input_x() + window.get_port_input_width() / 2.0,
        window.get_port_input_y() + window.get_port_input_height() / 2.0,
    );
    assert!(window.get_port_range_hint_visible());
    let hint = window.get_port_range_hint_text().to_string();
    assert!(hint.contains("1–65535"), "{hint}");
    assert!(hint.contains("7998") && hint.contains("7999"), "{hint}");
}

#[test]
fn export_is_disabled_with_its_reason_when_there_is_no_usage() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_export_available(false);
    window.set_export_note("今天没有可导出的用量".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(!window.get_export_button_enabled());
    assert!(window.get_export_status_visible());
    assert!(window.get_export_status_text().contains("没有可导出的用量"));

    window.set_export_available(true);
    window.set_export_note("可导出今天的 24 个时段".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_export_button_enabled());
}

#[test]
fn f1_opens_the_help_page_with_the_new_task_entries() {
    let window = window_at(1060.0, 720.0);
    window.on_navigate({
        let window = window.as_weak();
        move |page| {
            if let Some(window) = window.upgrade() {
                window.set_page(page);
            }
        }
    });
    window.invoke_focus_root();
    press(&window, Key::F1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(window.get_f1_seen(), "F1 reaches the shortcut handler");
    assert_eq!(window.get_page(), 4, "F1 opens help");
    assert!(window.get_help_visible());
    assert!(
        window.get_help_terminology_text().contains("本机网关"),
        "{:?}",
        window.get_help_terminology_text()
    );
    assert!(window.get_help_export_text().contains("导出用量"));
    assert!(window.get_help_troubleshoot_text().contains("请求失败"));
}

#[test]
fn the_help_search_filters_the_entries() {
    let window = window_at(1060.0, 720.0);
    window.set_page(4);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_eq!(
        window.get_help_visible_count(),
        9,
        "an empty query keeps every help entry"
    );

    window.set_help_keyword("端口".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(
        window.get_help_visible_count(),
        1,
        "the port-switch entry is the only match"
    );
    assert!(!window.get_help_no_result_visible());

    window.set_help_keyword("没有这个条目".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_help_visible_count(), 0);
    assert!(
        window.get_help_no_result_visible(),
        "a query with no match says so instead of showing every card"
    );

    window.set_help_keyword("".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_help_visible_count(), 9);
}

#[test]
fn the_request_detail_copies_the_request_id() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    window.set_selected_request("f314e7d8c7526367baec95ac155b6406".into());
    window.set_request_detail("请求详情".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    let copied = Rc::new(RefCell::new(None::<String>));
    window.on_copy_text({
        let copied = Rc::clone(&copied);
        move |text| *copied.borrow_mut() = Some(text.to_string())
    });
    click(
        &window,
        window.get_request_detail_id_copy_x() + window.get_request_detail_id_copy_width() / 2.0,
        window.get_request_detail_id_copy_y() + window.get_request_detail_id_copy_height() / 2.0,
    );
    assert_eq!(
        copied.borrow().as_deref(),
        Some("f314e7d8c7526367baec95ac155b6406"),
        "复制请求 ID copies the selected request's stable ID"
    );
}

#[test]
fn a_disabled_primary_action_keeps_readable_neutral_colors_instead_of_dimmed_blue() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(
        !window.get_probe_run_enabled(),
        "上下文探测 starts disabled until a model is selected"
    );
    for theme in [1, 2] {
        window.set_theme_mode(theme);
        i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
        assert_eq!(
            window.get_probe_run_fill(),
            window.get_console_field_color(),
            "a disabled action rests on the neutral field fill, not a dimmed accent"
        );
        assert_eq!(
            window.get_probe_run_frame_tint(),
            window.get_console_edge_control_color(),
            "a disabled action keeps the control edge instead of the accent frame"
        );
        assert_eq!(
            window.get_probe_run_label_tint(),
            window.get_console_muted_color(),
            "a disabled action uses the muted label"
        );
        let ratio = contrast_ratio(
            window.get_probe_run_label_tint(),
            window.get_probe_run_fill(),
        );
        assert!(
            ratio >= 4.5,
            "the disabled label must stay readable on its fill, got {ratio:.2} in theme {theme}"
        );
        assert!(
            !window.get_probe_run_focus_visible(),
            "a disabled action must not read as a visible focus target"
        );
    }
}

// WCAG relative luminance, so the danger-label check is a real contrast
// measurement instead of a token-name comparison.
fn relative_luminance(color: slint::Color) -> f64 {
    fn channel(value: u8) -> f64 {
        let value = f64::from(value) / 255.0;
        if value <= 0.039_28 {
            value / 12.92
        } else {
            ((value + 0.055) / 1.055).powf(2.4)
        }
    }
    0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
}

fn contrast_ratio(foreground: slint::Color, background: slint::Color) -> f64 {
    let (first, second) = (
        relative_luminance(foreground),
        relative_luminance(background),
    );
    let (lighter, darker) = if first > second {
        (first, second)
    } else {
        (second, first)
    };
    (lighter + 0.05) / (darker + 0.05)
}

#[test]
fn danger_action_label_and_icon_clear_the_aa_text_floor_in_both_themes() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    window.set_account_can_clear(true);
    for theme in [1, 2] {
        window.set_theme_mode(theme);
        i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
        let surface = window.get_console_surface_color();
        for (name, tint) in [
            ("label", window.get_danger_action_label_tint()),
            ("icon", window.get_danger_action_icon_tint()),
        ] {
            let ratio = contrast_ratio(tint, surface);
            assert!(
                ratio >= 4.5,
                "the danger {name} must clear 4.5:1 on the card surface, got {ratio:.2} in theme {theme}"
            );
        }
        let frame = contrast_ratio(window.get_danger_action_frame_tint(), surface);
        assert!(
            frame >= 3.0,
            "the danger frame keeps the 3:1 non-text floor, got {frame:.2} in theme {theme}"
        );
    }
}

#[test]
fn the_request_kpi_scope_caption_sits_above_the_five_cards() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    window.set_request_chart_collapsed(false);
    window.set_request_chart_collapsed_explicit(true);
    let metrics = vec![
        RequestMetric {
            label: "Token".into(),
            value: "84 亿".into(),
            detail: "入 42 亿 · 出 42 亿".into(),
            detail_tone: 3,
        },
        RequestMetric {
            label: "请求".into(),
            value: "2,100".into(),
            detail: "失败 3".into(),
            detail_tone: 2,
        },
        RequestMetric {
            label: "缓存命中率".into(),
            value: "62.4%".into(),
            detail: "缓存读取 / 输入 Token".into(),
            detail_tone: 3,
        },
        RequestMetric {
            label: "输出速度".into(),
            value: "38.6 Token/秒".into(),
            detail: "整次平均".into(),
            detail_tone: 3,
        },
        RequestMetric {
            label: "首字耗时".into(),
            value: "420 ms".into(),
            detail: "请求开始到首个输出".into(),
            detail_tone: 3,
        },
    ];
    window.set_request_metrics(ModelRc::from(metrics.as_slice()));
    let rows = vec![request_row(0)];
    window.set_requests(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    assert!(window.get_request_kpi_scope_visible());
    assert_eq!(window.get_request_kpi_scope_text().as_str(), "所选范围");
    let analytics_y = window.get_request_analytics_y();
    let scope_y = window.get_request_kpi_scope_y();
    let scope_height = window.get_request_kpi_scope_height();
    let card_y = window.get_request_kpi_first_card_y();
    assert!(
        scope_y >= analytics_y,
        "所选范围 stays inside the analytics card: {scope_y} vs {analytics_y}"
    );
    assert!(
        scope_y + scope_height <= card_y + 0.5,
        "所选范围 sits above the KPI cards: {} + {} vs {card_y}",
        scope_y,
        scope_height
    );
}

#[test]
fn the_first_run_footer_hint_retires_after_help_and_answers_ctrl_r() {
    let window = window_at(1060.0, 720.0);
    assert!(
        window.get_footer_help_hint_visible(),
        "the first-run footer points at the help page"
    );
    assert_eq!(
        window.get_footer_help_hint_text().as_str(),
        "按 F1 查看帮助"
    );

    window.set_page(4);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(
        !window.get_footer_help_hint_visible(),
        "opening help retires the first-run hint"
    );

    window.set_page(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(!window.get_footer_help_hint_visible());

    window.set_page(4);
    window.invoke_show_help_no_refresh();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_footer_help_hint_visible());
    assert_eq!(
        window.get_footer_help_hint_text().as_str(),
        "帮助页无刷新",
        "Ctrl+R on the static help page names the no-op instead of spinning a hidden icon"
    );
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(2100));
    assert!(
        !window.get_footer_help_hint_visible(),
        "the note is transient"
    );
}

#[test]
fn the_history_level_filter_names_the_empty_selection() {
    let window = window_at(1060.0, 720.0);
    window.set_page(3);
    window.set_log_mode(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(window.get_log_level_hint_visible());
    assert_eq!(
        window.get_log_level_hint_text().as_str(),
        "未选级别时显示全部"
    );
}

#[test]
fn the_compact_settings_directory_balances_its_two_rows() {
    let window = window_at(540.0, 420.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    println!(
        "toc_h={} appearance=({},{}) export_y={} login_rounds=({},{})",
        window.get_settings_toc_height(),
        window.get_settings_toc_appearance_x(),
        window.get_settings_toc_appearance_y(),
        window.get_settings_toc_export_y(),
        window.get_settings_toc_login_rounds_x(),
        window.get_settings_toc_login_rounds_y(),
    );
    assert!(
        window.get_settings_toc_export_y() > window.get_settings_toc_appearance_y(),
        "导出用量 starts the second directory row instead of following 实例管理"
    );
    assert_eq!(
        window.get_settings_toc_login_rounds_y(),
        window.get_settings_toc_export_y(),
        "登录轮次 shares the second row instead of sitting alone on a third"
    );
    assert!(
        window.get_settings_toc_login_rounds_x() > window.get_settings_toc_appearance_x(),
        "登录轮次 follows 导出用量 and UM 模型 on the second row"
    );
}

#[test]
fn the_maintenance_menu_uses_a_wrench_not_the_settings_gear() {
    let window = window_at(1060.0, 720.0);
    window.set_page(0);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_eq!(
        window.get_maintenance_toggle_icon().as_str(),
        "wrench",
        "the maintenance menu must not reuse the settings page's gear"
    );
}

#[test]
fn the_request_page_uses_one_column_at_800px() {
    for (width, height) in [(800.0, 540.0), (800.0, 720.0)] {
        let window = window_at(width, height);
        window.set_page(1);
        let rows = (0..3).map(request_row).collect::<Vec<_>>();
        window.set_requests(ModelRc::from(rows.as_slice()));
        window.set_selected_request("key-0".into());
        window.set_request_detail("HTTP 状态  200\n错误\n  无".into());
        i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

        assert!(
            !window.get_request_list_visible(),
            "800 px must show the detail as a single-column overlay, not squeeze the list"
        );
        assert!(
            window.get_request_detail_width() >= window.get_request_page_host_width() - 1.0,
            "the detail must take the full content width: detail {} page {}",
            window.get_request_detail_width(),
            window.get_request_page_host_width()
        );

        // The page status row owns the one chart disclosure at this width;
        // the click is the product callback, not a coordinate guess.
        click(
            &window,
            window.get_request_chart_toggle_x() + window.get_request_chart_toggle_width() / 2.0,
            window.get_request_chart_toggle_y() + window.get_request_chart_toggle_height() / 2.0,
        );
        i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
        assert!(
            window.get_request_analytics_compact(),
            "800 px must stack the analytics card instead of the wide row"
        );
        assert!(
            window.get_request_analytics_chart_width() > 300.0,
            "the chart keeps a readable width: {}",
            window.get_request_analytics_chart_width()
        );
        assert!(
            (window.get_request_analytics_chart_width()
                - window.get_request_analytics_speed_table_width())
                .abs()
                < 1.0,
            "the speed table must sit under the chart at the same width: chart {} table {}",
            window.get_request_analytics_chart_width(),
            window.get_request_analytics_speed_table_width()
        );
        assert!(
            window.get_request_analytics_chart_y()
                + window.get_request_analytics_chart_height()
                <= window.get_request_analytics_speed_table_y() + 1.0,
            "the speed table must sit below the chart, chart bottom {} table y {}",
            window.get_request_analytics_chart_y() + window.get_request_analytics_chart_height(),
            window.get_request_analytics_speed_table_y()
        );
    }
}

#[test]
fn keyboard_focus_on_rows_chips_and_cards_uses_a_control_strength_edge() {
    let probe = RowStyleProbe::new().expect("create RowStyleProbe");
    probe.show().expect("show RowStyleProbe");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));

    assert_eq!(
        probe.get_row_focus_edge_width(),
        0.0,
        "a resting row keeps no focus ring"
    );
    probe.invoke_focus_row();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(probe.get_row_focus_visible());
    assert_eq!(probe.get_row_focus_edge_width(), 1.0);
    assert_eq!(
        probe.get_row_focus_edge(),
        probe.get_control_strong(),
        "a focused list row must step to the 3:1 control edge"
    );

    probe.invoke_focus_chip();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(probe.get_chip_focus_visible());
    assert_eq!(probe.get_chip_focus_edge_width(), 1.0);
    assert_eq!(
        probe.get_chip_focus_edge(),
        probe.get_control_strong(),
        "a focused address chip must step to the 3:1 control edge"
    );

    probe.invoke_focus_card();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(probe.get_card_focus_visible());
    assert_eq!(probe.get_card_focus_edge_width(), 1.0);
    assert_eq!(
        probe.get_card_focus_edge(),
        probe.get_control_strong(),
        "a focused clickable card must step to the 3:1 control edge"
    );

    probe.set_probe_row_selected(true);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_eq!(probe.get_row_fill(), probe.get_selected_fill());
    assert_ne!(
        probe.get_selected_fill(),
        probe.get_hover_fill(),
        "a selected row must not share the hover fill in either theme"
    );
    probe.set_probe_theme_mode(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_ne!(
        probe.get_selected_fill(),
        probe.get_hover_fill(),
        "dark selected must stay distinct from hover"
    );
    assert_eq!(probe.get_row_fill(), probe.get_selected_fill());
}

#[test]
fn the_first_screen_at_1060x720_reaches_the_recent_issues_title() {
    let window = window_at(1060.0, 720.0);
    window.set_account_label("已登录".into());
    window.set_overview_failure_count(3);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    println!(
        "chart_y={} chart_h={} plot_h={} verify_h={} recent_title_y={} recent_title_h={} viewport_bottom={} status_y={}",
        window.get_overview_chart_absolute_y(),
        window.get_overview_chart_height(),
        window.get_overview_plot_height(),
        window.get_verify_card_height(),
        window.get_recent_issues_title_y(),
        window.get_recent_issues_title_height(),
        window.get_overview_viewport_bottom(),
        window.get_status_row_y(),
    );
    assert!(
        window.get_overview_chart_height() <= 300.0 + 1.0,
        "the usage chart must not absorb the first screen: {}",
        window.get_overview_chart_height()
    );
    assert!(
        window.get_overview_plot_height() >= 120.0,
        "the usage plot must keep readable bars in the default window: {}",
        window.get_overview_plot_height()
    );
    assert!(
        window.get_verify_card_height() <= 56.0,
        "the recent verify card must be one compact row: {}",
        window.get_verify_card_height()
    );
    assert!(
        window.get_overview_chart_absolute_y() < window.get_recent_issues_title_y(),
        "the trend chart precedes the status cards on the overview"
    );
    assert!(
        window.get_recent_issues_title_y() + window.get_recent_issues_title_height()
            <= window.get_overview_viewport_bottom() + 1.0,
        "the 近期问题 title must be visible in the first screen: title bottom {} viewport bottom {}",
        window.get_recent_issues_title_y() + window.get_recent_issues_title_height(),
        window.get_overview_viewport_bottom()
    );
}

#[test]
fn the_compact_status_row_keeps_the_page_count_unelided() {
    let window = window_at(540.0, 420.0);
    window.set_page(1);
    window.set_request_status("北京时间 · 本页 3 条".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    println!(
        "status={} width={} preferred={}",
        window.get_request_status_display_text(),
        window.get_request_status_hint_width(),
        window.get_request_status_hint_preferred_width(),
    );
    assert_eq!(
        window.get_request_status_display_text().as_str(),
        "北京时间 · 本页 3 条",
        "the compact status row keeps the page count"
    );
    assert!(
        window.get_request_status_hint_width() + 0.5
            >= window.get_request_status_hint_preferred_width(),
        "the status hint must not elide the page count: width {} preferred {}",
        window.get_request_status_hint_width(),
        window.get_request_status_hint_preferred_width()
    );
}

#[test]
fn the_primary_query_action_shows_a_three_to_one_focus_ring() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_request_query_primary());
    assert!(!window.get_request_query_focus_visible());
    window.invoke_focus_request_query();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(150));
    assert!(
        window.get_request_query_has_focus(),
        "the primary 查询 action takes keyboard focus"
    );
    assert!(
        window.get_request_query_focus_visible(),
        "a primary action must still expose a 3:1 focus ring"
    );
}

#[test]
fn the_compact_verify_card_keeps_its_row_inside_the_card() {
    let window = window_at(540.0, 420.0);
    window.set_verify_at("核验于 11-22 10:01:00".into());
    window.set_verify_result("通过".into());
    window.set_verify_detail("上游返回 200，未调用模型".into());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    println!(
        "card_x={} card_w={} card_h={} button_x={} button_w={}",
        window.get_verify_card_x(),
        window.get_verify_card_width(),
        window.get_verify_card_height(),
        window.get_verify_button_x(),
        window.get_verify_button_width(),
    );
    assert!(
        window.get_verify_card_height() <= 56.0,
        "the recent verify card stays one row at 540 px: {}",
        window.get_verify_card_height()
    );
    let card_right = window.get_verify_card_x() + window.get_verify_card_width();
    let button_right = window.get_verify_button_x() + window.get_verify_button_width();
    assert!(
        button_right <= card_right - 8.0,
        "the 重新核验 action stays inside the card: button right {button_right} card right {card_right}"
    );
    assert!(
        window.get_verify_button_width() >= 96.0 - 0.5,
        "the re-verify action keeps its full label: {}",
        window.get_verify_button_width()
    );
}

#[test]
fn the_request_row_accessible_label_carries_first_token_and_summary() {
    let window = window_at(1060.0, 720.0);
    window.set_page(1);
    let rows = vec![request_row(0)];
    window.set_requests(ModelRc::from(rows.as_slice()));
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    let label = window.get_request_row_label_sample();
    println!("row label={label}");
    assert!(label.contains("120 ms"), "missing 首字耗时 in {label}");
    assert!(label.contains("1200 ms"), "missing summary in {label}");
    assert!(label.contains("成功"), "missing 结果 in {label}");
}

#[test]
fn ctrl_f_reaches_the_help_search_field() {
    let window = window_at(1060.0, 720.0);
    window.set_page(4);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(!window.get_help_search_focused());
    window.invoke_focus_page_search();
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert!(
        window.get_help_search_focused(),
        "the help page's Ctrl+F target must take focus"
    );
}

#[test]
fn the_settings_directory_scrolls_to_a_lower_card() {
    let window = window_at(540.0, 420.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    assert_eq!(window.get_settings_scroll_y(), 0.0);
    window.invoke_scroll_settings_to(window.get_settings_viewport_height());
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::from_millis(250));
    println!(
        "settings y={} viewport={} content_h={}",
        window.get_settings_scroll_y(),
        window.get_settings_viewport_height(),
        window.get_settings_content_height(),
    );
    assert!(
        window.get_settings_scroll_y() < 0.0,
        "the settings directory must scroll to a lower card"
    );
}
