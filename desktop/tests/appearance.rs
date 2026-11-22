//! Headless checks for the saved appearance settings (#148): theme, sidebar,
//! reduced motion and reduced transparency.

use std::cell::RefCell;
use std::rc::Rc;
use std::time::Duration;

use slint::platform::{PointerEventButton, WindowEvent};
use slint::ComponentHandle;
use um_desktop::config::{
    apply_motion, apply_reduce_transparency, apply_settings, apply_sidebar, load_from,
    parse_config_with_warnings, DesktopConfig, LoadOutcome, MotionPreference, Settings,
    SidebarMode,
};
use um_desktop::ui::*;

mod common;
use common::TempDir;

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
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    window
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
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
}

#[test]
fn appearance_defaults_are_expanded_system_motion_and_no_transparency() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));

    assert_eq!(window.get_sidebar_mode(), 0, "默认侧栏为展开");
    assert!(
        (window.get_rail_width() - 196.0).abs() < 0.5,
        "展开侧栏保持 196px，实际 {}",
        window.get_rail_width()
    );
    assert!(window.get_sidebar_labels_visible(), "展开侧栏显示文字");
    assert!(!window.get_sidebar_compact(), "宽窗口默认不是 compact");
    assert!(
        !window.get_sidebar_tooltips_visible(),
        "展开侧栏不需要悬停提示"
    );
    assert_eq!(window.get_motion_mode(), 0, "默认动效跟随系统");
    assert!(window.get_allow_motion(), "系统允许动效时默认开启动效");
    assert!(window.get_motion_enabled(), "Theme 动效开关默认开启");
    assert!(!window.get_reduce_transparency(), "默认不降低透明度");
    assert!(
        window.get_transparency_note_visible(),
        "降低透明度必须说明当前界面无透明度效果"
    );
    assert!(
        window
            .get_transparency_note_text()
            .contains("当前界面无透明度效果，开关已保存"),
        "透明度说明必须如实写明开关只保存、无视觉效果，实际 {}",
        window.get_transparency_note_text()
    );
}

#[test]
fn saved_appearance_is_applied_to_the_window() {
    let window = window_at(1060.0, 720.0);
    let config = DesktopConfig {
        settings: Settings {
            sidebar: SidebarMode::IconsOnly,
            reduced_motion: MotionPreference::Reduced,
            reduce_transparency: true,
            ..Settings::default()
        },
        ..DesktopConfig::default()
    };
    apply_settings(&window, &config, true);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));

    assert_eq!(window.get_sidebar_mode(), 1);
    assert!(
        (window.get_rail_width() - 84.0).abs() < 0.5,
        "仅图标侧栏宽 84px，实际 {}",
        window.get_rail_width()
    );
    assert!(!window.get_sidebar_labels_visible());
    assert!(window.get_sidebar_compact());
    assert!(window.get_sidebar_tooltips_visible());
    assert_eq!(window.get_motion_mode(), 1);
    assert!(!window.get_allow_motion(), "减少动效必须关闭界面动效");
    assert!(!window.get_motion_enabled());
    assert!(window.get_reduce_transparency());

    // 跟随系统动效时，系统关闭动效也必须关闭界面动效。
    apply_settings(&window, &DesktopConfig::default(), false);
    assert_eq!(window.get_motion_mode(), 0);
    assert!(!window.get_allow_motion());
    assert!(window.get_motion_enabled() == false);

    // 手动「减少动效」不受系统允许动效影响。
    apply_settings(&window, &config, true);
    assert!(!window.get_allow_motion());
}

#[test]
fn sidebar_segments_switch_between_expanded_and_icons_only() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));

    let selected = Rc::new(RefCell::new(Vec::new()));
    window.on_sidebar_mode_changed({
        let selected = Rc::clone(&selected);
        move |index| selected.borrow_mut().push(index)
    });

    // The second of two equal segments is 仅图标.
    let y = window.get_sidebar_mode_y() + window.get_sidebar_mode_height() / 2.0;
    let x = window.get_sidebar_mode_x() + window.get_sidebar_mode_width() * 3.0 / 4.0;
    click(&window, x, y);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    assert_eq!(*selected.borrow(), vec![1], "点击仅图标必须上报选择");
    assert_eq!(window.get_sidebar_mode(), 1);
    assert!(
        (window.get_rail_width() - 84.0).abs() < 0.5,
        "仅图标侧栏宽 84px，实际 {}",
        window.get_rail_width()
    );
    assert!(!window.get_sidebar_labels_visible(), "仅图标侧栏不显示文字");
    assert!(window.get_sidebar_compact());
    assert!(
        window.get_sidebar_tooltips_visible(),
        "仅图标侧栏必须保留悬停提示"
    );

    // The first segment returns to 展开 and the 196 px rail.
    let x = window.get_sidebar_mode_x() + window.get_sidebar_mode_width() / 4.0;
    click(&window, x, y);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    assert_eq!(*selected.borrow(), vec![1, 0]);
    assert_eq!(window.get_sidebar_mode(), 0);
    assert!(
        (window.get_rail_width() - 196.0).abs() < 0.5,
        "展开侧栏保持 196px，实际 {}",
        window.get_rail_width()
    );
    assert!(window.get_sidebar_labels_visible());
    assert!(!window.get_sidebar_tooltips_visible());
}

#[test]
fn narrow_window_uses_the_icon_rail_until_the_sidebar_choice_has_room() {
    let window = window_at(540.0, 420.0);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    assert!(
        window.get_sidebar_compact(),
        "窄窗口即使选择展开也必须使用仅图标 rail"
    );
    assert!((window.get_rail_width() - 84.0).abs() < 0.5);
    assert!(!window.get_sidebar_labels_visible());
    assert!(window.get_sidebar_tooltips_visible());
}

#[test]
fn motion_segments_disable_the_page_fade_and_follow_the_system() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    window.on_navigate({
        let window = window.as_weak();
        move |page| {
            if let Some(window) = window.upgrade() {
                window.set_page(page);
            }
        }
    });
    let selected = Rc::new(RefCell::new(Vec::new()));
    window.on_motion_mode_changed({
        let selected = Rc::clone(&selected);
        move |index| selected.borrow_mut().push(index)
    });

    // The second of two equal segments is 减少动效.
    let y = window.get_motion_mode_y() + window.get_motion_mode_height() / 2.0;
    let x = window.get_motion_mode_x() + window.get_motion_mode_width() * 3.0 / 4.0;
    click(&window, x, y);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    assert_eq!(*selected.borrow(), vec![1], "点击减少动效必须上报选择");
    assert_eq!(window.get_motion_mode(), 1);
    assert!(!window.get_allow_motion(), "减少动效必须关闭界面动效");
    assert!(!window.get_motion_enabled(), "Theme 动效开关必须关闭");

    // Reduced motion swaps pages immediately instead of fading them out.
    window.invoke_navigate_to(1);
    assert_eq!(window.get_page(), 1, "减少动效时页面切换必须立即完成");
    assert!(
        (window.get_page_title_opacity() - 1.0).abs() < 0.01,
        "减少动效时不能淡出页面，实际 {}",
        window.get_page_title_opacity()
    );

    // 跟随系统 returns to the system setting and restores the fade.
    window.set_motion_mode(0);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));
    assert_eq!(window.get_motion_mode(), 0);
    assert!(window.get_allow_motion());
    assert!(window.get_motion_enabled());

    window.invoke_navigate_to(2);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(80));
    assert_eq!(window.get_page(), 1, "允许动效时页面在淡出期间不能先切换");
    assert!(
        window.get_page_title_opacity() < 0.9,
        "允许动效时页面必须淡出，实际 {}",
        window.get_page_title_opacity()
    );
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(160));
    assert_eq!(window.get_page(), 2);
}

#[test]
fn transparency_toggle_reports_and_keeps_the_no_effect_note() {
    let window = window_at(1060.0, 720.0);
    window.set_page(2);
    i_slint_backend_testing::mock_elapsed_time(Duration::from_millis(250));

    let toggled = Rc::new(RefCell::new(Vec::new()));
    window.on_transparency_changed({
        let toggled = Rc::clone(&toggled);
        move |checked| toggled.borrow_mut().push(checked)
    });

    let x = window.get_transparency_toggle_x() + window.get_transparency_toggle_width() / 2.0;
    let y = window.get_transparency_toggle_y() + window.get_transparency_toggle_height() / 2.0;
    click(&window, x, y);
    assert_eq!(*toggled.borrow(), vec![true], "点击开关必须上报降低透明度");
    assert!(window.get_reduce_transparency());
    assert!(
        window.get_transparency_note_visible(),
        "降低透明度必须保留说明"
    );
    assert!(
        window
            .get_transparency_note_text()
            .contains("当前界面无透明度效果，开关已保存"),
        "开关不能伪造不存在的透明度效果，实际 {}",
        window.get_transparency_note_text()
    );

    click(&window, x, y);
    assert_eq!(*toggled.borrow(), vec![true, false]);
    assert!(!window.get_reduce_transparency());
}

#[test]
fn appearance_choices_persist_to_desktop_json() {
    let dir = TempDir::new("appearance-settings");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let mut config = DesktopConfig::default();

    apply_sidebar(Some(&path), &mut config, SidebarMode::IconsOnly).expect("save sidebar");
    apply_motion(Some(&path), &mut config, MotionPreference::Reduced).expect("save motion");
    apply_reduce_transparency(Some(&path), &mut config, true).expect("save transparency");
    assert_eq!(config.settings.sidebar, SidebarMode::IconsOnly);
    assert_eq!(config.settings.reduced_motion, MotionPreference::Reduced);
    assert!(config.settings.reduce_transparency);

    match load_from(&path) {
        LoadOutcome::Loaded(loaded) => {
            assert_eq!(loaded.settings.sidebar, SidebarMode::IconsOnly);
            assert_eq!(loaded.settings.reduced_motion, MotionPreference::Reduced);
            assert!(loaded.settings.reduce_transparency);
        }
        other => panic!("保存后的外观设置必须能重新加载，实际 {other:?}"),
    }
}

#[test]
fn failed_appearance_save_keeps_the_choice_in_memory_and_reports_reason() {
    let dir = TempDir::new("appearance-settings-fail");
    let blocker = dir.path().join("blocked");
    std::fs::write(&blocker, "not a directory").expect("write blocker");
    let path = blocker.join("desktop.json");
    let mut config = DesktopConfig::default();

    let error = apply_sidebar(Some(&path), &mut config, SidebarMode::IconsOnly)
        .expect_err("save must fail");
    assert!(error.contains("保存失败"), "{error}");
    assert_eq!(
        config.settings.sidebar,
        SidebarMode::IconsOnly,
        "保存失败时内存中的选择必须保留"
    );
}

#[test]
fn older_config_keeps_default_appearance_and_invalid_values_fall_back() {
    let (older, warnings) = parse_config_with_warnings(r#"{"version":1,"model_api_port":7999}"#)
        .expect("older config loads");
    assert!(warnings.is_empty(), "{warnings:?}");
    let window = window_at(1060.0, 720.0);
    apply_settings(&window, &older, true);
    assert_eq!(window.get_sidebar_mode(), 0);
    assert!((window.get_rail_width() - 196.0).abs() < 0.5);
    assert_eq!(window.get_motion_mode(), 0);
    assert!(window.get_allow_motion());
    assert!(!window.get_reduce_transparency());

    let (invalid, warnings) = parse_config_with_warnings(
        r#"{
            "version":2,
            "model_api_port":7999,
            "sidebar":"hidden",
            "reduced_motion":"always",
            "reduce_transparency":"yes"
        }"#,
    )
    .expect("invalid appearance values are warnings");
    for field in ["sidebar", "reduced_motion", "reduce_transparency"] {
        assert!(
            warnings.iter().any(|warning| warning.contains(field)),
            "{field} 非法时必须说明原因：{warnings:?}"
        );
    }
    apply_settings(&window, &invalid, true);
    assert_eq!(window.get_sidebar_mode(), 0);
    assert_eq!(window.get_motion_mode(), 0);
    assert!(window.get_allow_motion());
    assert!(!window.get_reduce_transparency());
}
