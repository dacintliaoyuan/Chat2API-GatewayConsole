use std::path::Path;
use std::sync::Once;

use slint::ComponentHandle;
use um_desktop::config::{
    apply_settings, apply_theme, config_path, load_from, load_from_with_warnings, model_api_bind,
    parse_config, parse_config_with_warnings, save_to, save_window_settings, settings_from_window,
    validate_port, AutoRefresh, DesktopConfig, LoadOutcome, LogLevels, LogRange, MotionPreference,
    RequestResult, Settings, SidebarMode, ThemeMode, TimeRange, DEFAULT_MODEL_API_PORT,
    MODEL_API_HOST,
};
use um_desktop::ui::*;

mod common;
use common::TempDir;

thread_local! {
    static INIT: Once = Once::new();
}

fn window() -> MainWindow {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.show().expect("show MainWindow");
    window
}

fn assert_default_settings(settings: &Settings) {
    assert_eq!(settings.auto_refresh, AutoRefresh::Five);
    assert_eq!(settings.auto_refresh.seconds(), 5);
    assert_eq!(settings.usage_range, TimeRange::Today);
    assert_eq!(settings.request_range, TimeRange::Today);
    assert_eq!(settings.request_result, RequestResult::All);
    assert_eq!(settings.request_search, "");
    assert_eq!(settings.log_range, LogRange::Live);
    assert_eq!(settings.log_level, LogLevels::default());
    assert!(settings.log_level.flags() == (false, false, false, false));
    assert_eq!(settings.log_keyword, "");
    assert_eq!(settings.sidebar, SidebarMode::Expanded);
    assert_eq!(settings.reduced_motion, MotionPreference::System);
    assert!(!settings.reduce_transparency);
    assert_eq!(settings.request_chart_collapsed, None);
}

fn non_default_settings() -> Settings {
    Settings {
        auto_refresh: AutoRefresh::Fifteen,
        usage_range: TimeRange::Month,
        request_range: TimeRange::Week,
        request_result: RequestResult::Failed,
        request_search: "fixture".to_string(),
        log_range: LogRange::Month,
        log_level: LogLevels::from_flags(false, true, false, false),
        log_keyword: "timeout".to_string(),
        sidebar: SidebarMode::IconsOnly,
        reduced_motion: MotionPreference::Reduced,
        reduce_transparency: true,
        request_chart_collapsed: Some(true),
    }
}

#[test]
fn default_model_api_port_is_7999_on_loopback() {
    let config = DesktopConfig::default();
    assert_eq!(config.model_api_port, DEFAULT_MODEL_API_PORT);
    assert_eq!(config.model_api_port, 7999);
    assert_eq!(MODEL_API_HOST, "127.0.0.1");
    assert_eq!(model_api_bind(&config), "127.0.0.1:7999");
}

#[test]
fn config_path_lives_under_local_app_data() {
    let root = Path::new(r"C:\Users\someone\AppData\Local");
    assert_eq!(
        config_path(root),
        Path::new(r"C:\Users\someone\AppData\Local\Chat2API\desktop.json")
    );
}

#[test]
fn missing_file_is_a_legal_first_open_default() {
    let dir = TempDir::new("config-missing");
    let outcome = load_from(&dir.path().join("Chat2API").join("desktop.json"));
    assert_eq!(outcome, LoadOutcome::Missing);
}

#[test]
fn save_then_load_round_trips_the_port() {
    let dir = TempDir::new("config-roundtrip");
    let path = dir.path().join("Chat2API").join("desktop.json");
    save_to(&path, &DesktopConfig { model_api_port: 7998, ..DesktopConfig::default() }).expect("save config");
    assert_eq!(
        load_from(&path),
        LoadOutcome::Loaded(DesktopConfig {
            model_api_port: 7998, ..DesktopConfig::default()
        })
    );
}

#[test]
fn saving_again_replaces_the_previous_port() {
    let dir = TempDir::new("config-overwrite");
    let path = dir.path().join("Chat2API").join("desktop.json");
    save_to(&path, &DesktopConfig { model_api_port: 7999, ..DesktopConfig::default() }).expect("first save");
    save_to(&path, &DesktopConfig { model_api_port: 7998, ..DesktopConfig::default() }).expect("second save");
    assert_eq!(
        load_from(&path),
        LoadOutcome::Loaded(DesktopConfig {
            model_api_port: 7998, ..DesktopConfig::default()
        })
    );
}

#[test]
fn missing_theme_is_a_system_theme_from_older_configs() {
    let config = parse_config(r#"{"version":1,"model_api_port":7999}"#).expect("old config");
    assert_eq!(config.theme, ThemeMode::System);
}

#[test]
fn invalid_theme_values_fall_back_to_system() {
    for text in [
        r#"{"version":1,"model_api_port":7999,"theme":"sepia"}"#,
        r#"{"version":1,"model_api_port":7999,"theme":3}"#,
        r#"{"version":1,"model_api_port":7999,"theme":null}"#,
    ] {
        let config = parse_config(text).expect("theme is optional");
        assert_eq!(config.theme, ThemeMode::System, "{text}");
    }
}

#[test]
fn save_then_load_round_trips_the_theme() {
    let dir = TempDir::new("config-theme-roundtrip");
    let path = dir.path().join("Chat2API").join("desktop.json");
    save_to(&path, &DesktopConfig { theme: ThemeMode::Dark, ..DesktopConfig::default() })
        .expect("save theme");
    let text = std::fs::read_to_string(&path).expect("read config");
    assert!(text.contains(r#""theme": "dark""#), "{text}");
    assert_eq!(
        load_from(&path),
        LoadOutcome::Loaded(DesktopConfig {
            theme: ThemeMode::Dark, ..DesktopConfig::default()
        })
    );
}

#[test]
fn applying_a_theme_persists_it() {
    let dir = TempDir::new("config-theme-apply");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let mut config = DesktopConfig::default();
    apply_theme(Some(&path), &mut config, ThemeMode::Light).expect("save theme");
    assert_eq!(config.theme, ThemeMode::Light);
    assert_eq!(
        load_from(&path),
        LoadOutcome::Loaded(DesktopConfig {
            theme: ThemeMode::Light, ..DesktopConfig::default()
        })
    );
}

#[test]
fn failed_theme_save_keeps_the_new_mode_in_memory() {
    let dir = TempDir::new("config-theme-save-fail");
    let blocker = dir.path().join("blocked");
    std::fs::write(&blocker, "not a directory").expect("write blocker");
    let path = blocker.join("desktop.json");
    let mut config = DesktopConfig::default();
    let error = apply_theme(Some(&path), &mut config, ThemeMode::Dark).expect_err("save must fail");
    assert!(error.contains("主题已切换，但保存失败"), "{error}");
    assert_eq!(config.theme, ThemeMode::Dark);
    assert!(!path.exists());
}

#[test]
fn corrupt_json_is_reported_not_treated_as_default() {
    let dir = TempDir::new("config-corrupt");
    let path = dir.path().join("Chat2API").join("desktop.json");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(&path, "{not json").unwrap();
    match load_from(&path) {
        LoadOutcome::Invalid(reason) => assert!(reason.contains("JSON"), "{reason}"),
        other => panic!("corrupt config must be invalid, got {other:?}"),
    }
}

#[test]
fn missing_port_is_reported() {
    let error = parse_config(r#"{"version":1}"#).expect_err("missing port");
    assert!(error.contains("model_api_port"), "{error}");
}

#[test]
fn port_zero_and_out_of_range_are_rejected() {
    assert!(validate_port("0").is_err());
    assert!(validate_port("65536").is_err());
    assert!(validate_port("not-a-port").is_err());
    assert_eq!(validate_port(" 7999 ").unwrap(), 7999);
    assert_eq!(validate_port("7998").unwrap(), 7998);
    assert!(parse_config(r#"{"version":1,"model_api_port":0}"#).is_err());
    assert!(parse_config(r#"{"version":1,"model_api_port":70000}"#).is_err());
}

#[test]
fn save_rejects_port_zero_without_writing() {
    let dir = TempDir::new("config-zero");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let error = save_to(&path, &DesktopConfig { model_api_port: 0, ..DesktopConfig::default() }).expect_err("port zero");
    assert!(error.contains("1-65535"), "{error}");
    assert!(!path.exists());
}

#[test]
fn defaults_cover_every_persisted_setting() {
    let config = DesktopConfig::default();
    assert_eq!(config.model_api_port, DEFAULT_MODEL_API_PORT);
    assert_eq!(config.theme, ThemeMode::System);
    assert!(!config.intro_dismissed);
    assert_default_settings(&config.settings);
}

#[test]
fn saving_writes_every_setting_to_desktop_json() {
    let dir = TempDir::new("config-settings-save");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let config = DesktopConfig {
        settings: non_default_settings(),
        ..DesktopConfig::default()
    };
    save_to(&path, &config).expect("save settings");
    let text = std::fs::read_to_string(&path).expect("read config");
    let body: serde_json::Value = serde_json::from_str(&text).expect("valid json");
    assert_eq!(body["auto_refresh"], 15, "{text}");
    assert_eq!(body["usage_range"], "30d", "{text}");
    assert_eq!(body["request_range"], "7d", "{text}");
    assert_eq!(body["request_result"], "failed", "{text}");
    assert_eq!(body["request_search"], "fixture", "{text}");
    assert_eq!(body["log_range"], "30d", "{text}");
    assert_eq!(body["log_level"], serde_json::json!(["warn"]), "{text}");
    assert_eq!(body["log_keyword"], "timeout", "{text}");
    assert_eq!(body["sidebar"], "icons", "{text}");
    assert_eq!(body["reduced_motion"], "reduced", "{text}");
    assert_eq!(body["reduce_transparency"], true, "{text}");
    assert_eq!(body["request_chart_collapsed"], true, "{text}");
}

#[test]
fn older_config_without_settings_uses_defaults_without_warnings() {
    let (config, warnings) = parse_config_with_warnings(r#"{"version":1,"model_api_port":7999}"#)
        .expect("old config");
    assert!(warnings.is_empty(), "{warnings:?}");
    assert_default_settings(&config.settings);
}

#[test]
fn log_level_accepts_the_legacy_single_value_and_the_multi_select_array() {
    let legacy = parse_config(r#"{"version":2,"model_api_port":7999,"log_level":"warn"}"#)
        .expect("legacy single level loads");
    assert_eq!(
        legacy.settings.log_level,
        LogLevels::from_flags(false, true, false, false)
    );

    let multi =
        parse_config(r#"{"version":2,"model_api_port":7999,"log_level":["error","info"]}"#)
            .expect("level array loads");
    assert_eq!(
        multi.settings.log_level,
        LogLevels::from_flags(true, false, true, false)
    );

    let (invalid, warnings) =
        parse_config_with_warnings(r#"{"version":2,"model_api_port":7999,"log_level":["error","trace"]}"#)
            .expect("unknown level names are warnings");
    assert_eq!(invalid.settings.log_level, LogLevels::default());
    assert!(
        warnings.iter().any(|warning| warning.contains("log_level")),
        "{warnings:?}"
    );
}

#[test]
fn an_invalid_log_range_lists_every_accepted_value() {
    let (_, warnings) = parse_config_with_warnings(
        r#"{"version":2,"model_api_port":7999,"log_range":"yesterday"}"#,
    )
    .expect("invalid log range is a warning");
    let warning = warnings
        .iter()
        .find(|warning| warning.contains("log_range"))
        .expect("log_range warning");
    assert!(warning.contains("24h"), "{warning}");
    assert!(warning.contains("all"), "{warning}");
}

#[test]
fn invalid_setting_values_fall_back_with_reasons() {
    let (config, warnings) = parse_config_with_warnings(
        r#"{
            "version":1,
            "model_api_port":7999,
            "auto_refresh":7,
            "usage_range":"year",
            "request_range":"tomorrow",
            "request_result":"maybe",
            "request_search":42,
            "log_range":"yesterday",
            "log_level":"trace",
            "log_keyword":false,
            "sidebar":"hidden",
            "reduced_motion":"always",
            "reduce_transparency":"yes",
            "request_chart_collapsed":"auto"
        }"#,
    )
    .expect("invalid settings are warnings, not load failures");
    assert_default_settings(&config.settings);
    for field in [
        "auto_refresh",
        "usage_range",
        "request_range",
        "request_result",
        "request_search",
        "log_range",
        "log_level",
        "log_keyword",
        "sidebar",
        "reduced_motion",
        "reduce_transparency",
        "request_chart_collapsed",
    ] {
        assert!(
            warnings.iter().any(|warning| warning.contains(field)),
            "{field} 非法时必须说明原因：{warnings:?}"
        );
    }
}

#[test]
fn save_then_load_keeps_every_setting() {
    let dir = TempDir::new("config-settings-roundtrip");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let settings = non_default_settings();
    save_to(
        &path,
        &DesktopConfig {
            settings: settings.clone(),
            ..DesktopConfig::default()
        },
    )
    .expect("save settings");
    let (outcome, warnings) = load_from_with_warnings(&path);
    assert!(warnings.is_empty(), "{warnings:?}");
    match outcome {
        LoadOutcome::Loaded(config) => assert_eq!(config.settings, settings),
        other => panic!("saved settings must load, got {other:?}"),
    }
}

#[test]
fn a_manual_request_chart_choice_round_trips_through_desktop_json() {
    let dir = TempDir::new("config-request-chart");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let first = window();
    first.set_request_chart_collapsed_explicit(true);
    first.set_request_chart_collapsed(false);
    let captured = settings_from_window(&first);
    assert_eq!(captured.request_chart_collapsed, Some(false));

    let mut config = DesktopConfig {
        settings: captured,
        ..DesktopConfig::default()
    };
    save_window_settings(Some(&path), &mut config, &first).expect("save chart choice");
    let (outcome, warnings) = load_from_with_warnings(&path);
    assert!(warnings.is_empty(), "{warnings:?}");
    let saved = match outcome {
        LoadOutcome::Loaded(config) => config,
        other => panic!("saved settings must load, got {other:?}"),
    };
    assert_eq!(saved.settings.request_chart_collapsed, Some(false));

    let restored = window();
    apply_settings(&restored, &saved, true);
    assert!(restored.get_request_chart_collapsed_explicit());
    assert!(
        !restored.get_request_chart_collapsed(),
        "the saved manual expand must beat the short-viewport default"
    );
}

#[test]
fn failed_setting_save_keeps_the_new_setting_in_memory_and_reports_reason() {
    let dir = TempDir::new("config-settings-save-fail");
    let blocker = dir.path().join("blocked");
    std::fs::write(&blocker, "not a directory").expect("write blocker");
    let path = blocker.join("desktop.json");
    let mut config = DesktopConfig::default();
    let window = window();
    window.set_auto_refresh(0);
    window.set_usage_period(4);
    let error = save_window_settings(Some(&path), &mut config, &window).expect_err("save must fail");
    assert!(error.contains("保存失败"), "{error}");
    assert_eq!(config.settings.auto_refresh, AutoRefresh::Off);
    assert_eq!(config.settings.usage_range, TimeRange::All);
    assert!(!path.exists());
}

#[test]
fn saving_window_settings_keeps_appearance_choices_without_window_controls() {
    let dir = TempDir::new("config-settings-appearance");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let mut config = DesktopConfig {
        settings: non_default_settings(),
        ..DesktopConfig::default()
    };
    let window = window();
    window.set_auto_refresh(60);
    save_window_settings(Some(&path), &mut config, &window).expect("save window settings");
    assert_eq!(config.settings.auto_refresh, AutoRefresh::Sixty);
    assert_eq!(config.settings.sidebar, SidebarMode::IconsOnly);
    assert_eq!(config.settings.reduced_motion, MotionPreference::Reduced);
    assert!(config.settings.reduce_transparency);
    match load_from(&path) {
        LoadOutcome::Loaded(loaded) => {
            assert_eq!(loaded.settings.sidebar, SidebarMode::IconsOnly);
            assert_eq!(loaded.settings.reduced_motion, MotionPreference::Reduced);
            assert!(loaded.settings.reduce_transparency);
        }
        other => panic!("saved settings must load, got {other:?}"),
    }
}

#[test]
fn default_motion_preference_follows_the_system_setting() {
    let window = window();
    let config = DesktopConfig::default();
    apply_settings(&window, &config, false);
    assert!(!window.get_allow_motion());
    apply_settings(&window, &config, true);
    assert!(window.get_allow_motion());
}

#[test]
fn applying_a_saved_config_sets_the_window_properties() {
    let window = window();
    let config = DesktopConfig {
        settings: non_default_settings(),
        ..DesktopConfig::default()
    };
    apply_settings(&window, &config, true);
    assert_eq!(window.get_auto_refresh(), 15);
    assert_eq!(window.get_auto_refresh_index(), 1);
    assert_eq!(window.get_usage_period(), 3);
    assert_eq!(window.get_request_period(), 2);
    assert_eq!(window.get_request_result(), 2);
    assert_eq!(window.get_request_keyword().as_str(), "fixture");
    assert_eq!(window.get_log_period(), 3);
    assert!(!window.get_log_level_error());
    assert!(window.get_log_level_warn());
    assert!(!window.get_log_level_info());
    assert!(!window.get_log_level_debug());
    assert_eq!(window.get_log_filter_keyword().as_str(), "timeout");
    assert!(!window.get_allow_motion(), "减少动效必须关闭界面动效");
}

#[test]
fn window_settings_survive_a_restart() {
    let dir = TempDir::new("config-settings-restart");
    let path = dir.path().join("Chat2API").join("desktop.json");
    let first = window();
    first.set_auto_refresh(30);
    first.set_usage_period(4);
    first.set_request_period(2);
    first.set_request_result(1);
    first.set_request_keyword("fixture".into());
    first.set_log_period(2);
    first.set_log_level_error(true);
    first.set_log_level_info(true);
    first.set_log_filter_keyword("timeout".into());
    let mut config = DesktopConfig::default();
    save_window_settings(Some(&path), &mut config, &first).expect("save window settings");

    // A restart builds a fresh window from the same desktop.json.
    let second = window();
    let (outcome, warnings) = load_from_with_warnings(&path);
    assert!(warnings.is_empty(), "{warnings:?}");
    match outcome {
        LoadOutcome::Loaded(loaded) => apply_settings(&second, &loaded, true),
        other => panic!("saved settings must load, got {other:?}"),
    }
    assert_eq!(second.get_auto_refresh(), 30);
    assert_eq!(second.get_auto_refresh_index(), 2);
    assert_eq!(second.get_usage_period(), 4);
    assert_eq!(second.get_request_period(), 2);
    assert_eq!(second.get_request_result(), 1);
    assert_eq!(second.get_request_keyword().as_str(), "fixture");
    assert_eq!(second.get_log_period(), 2);
    assert!(second.get_log_level_error());
    assert!(!second.get_log_level_warn());
    assert!(second.get_log_level_info());
    assert!(!second.get_log_level_debug());
    assert_eq!(second.get_log_filter_keyword().as_str(), "timeout");
}

#[cfg(windows)]
mod windows_acl {
    use std::ffi::c_void;
    use std::mem::{offset_of, size_of};
    use std::os::windows::ffi::OsStrExt;

    use um_desktop::config::{save_to, DesktopConfig};
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{LocalFree, ERROR_SUCCESS, HLOCAL};
    use windows::Win32::Security::Authorization::{GetNamedSecurityInfoW, SE_FILE_OBJECT};
    use windows::Win32::Security::{
        EqualSid, GetAclInformation, GetAce, GetSecurityDescriptorDacl, GetTokenInformation,
        AclSizeInformation, ACL_SIZE_INFORMATION, DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR,
        PSID, TokenUser, TOKEN_QUERY, TOKEN_USER,
    };
    use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    use super::TempDir;

    fn current_user_sid() -> PSID {
        unsafe {
            let mut token = windows::Win32::Foundation::HANDLE::default();
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token).expect("token");
            let mut size = 0u32;
            let _ = GetTokenInformation(token, TokenUser, None, 0, &mut size);
            let mut buffer = vec![0u8; size as usize];
            GetTokenInformation(
                token,
                TokenUser,
                Some(buffer.as_mut_ptr().cast::<c_void>()),
                size,
                &mut size,
            )
            .expect("token user");
            let _ = windows::Win32::Foundation::CloseHandle(token);
            (*buffer.as_ptr().cast::<TOKEN_USER>()).User.Sid
        }
    }

    fn read_dacl(path: &std::path::Path) -> (u32, bool) {
        unsafe {
            let wide: Vec<u16> = path
                .as_os_str()
                .encode_wide()
                .chain(std::iter::once(0))
                .collect();
            let mut dacl = std::ptr::null_mut();
            let mut descriptor = PSECURITY_DESCRIPTOR::default();
            let result = GetNamedSecurityInfoW(
                PCWSTR(wide.as_ptr()),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(&mut dacl),
                None,
                &mut descriptor,
            );
            assert_eq!(result, ERROR_SUCCESS);
            let mut present = windows::core::BOOL::default();
            let mut dacl_from_sd = std::ptr::null_mut();
            let mut defaulted = windows::core::BOOL::default();
            GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl_from_sd, &mut defaulted)
                .expect("descriptor dacl");
            assert!(present.as_bool());
            let mut info = ACL_SIZE_INFORMATION::default();
            GetAclInformation(
                dacl_from_sd,
                (&mut info as *mut ACL_SIZE_INFORMATION).cast::<c_void>(),
                size_of::<ACL_SIZE_INFORMATION>() as u32,
                AclSizeInformation,
            )
            .expect("acl info");
            let current = current_user_sid();
            let mut only_current_user = true;
            for index in 0..info.AceCount {
                let mut ace = std::ptr::null_mut();
                GetAce(dacl_from_sd, index, &mut ace).expect("ace");
                let sid = PSID(
                    (ace.cast::<u8>()).add(offset_of!(
                        windows::Win32::Security::ACCESS_ALLOWED_ACE,
                        SidStart
                    )) as *mut c_void,
                );
                only_current_user &= EqualSid(sid, current).is_ok();
            }
            let _ = LocalFree(Some(HLOCAL(descriptor.0)));
            (info.AceCount, only_current_user)
        }
    }

    #[test]
    fn saved_config_acl_is_restricted_to_current_user() {
        let dir = TempDir::new("config-acl");
        let path = dir.path().join("Chat2API").join("desktop.json");
        save_to(&path, &DesktopConfig { model_api_port: 7999, ..DesktopConfig::default() }).expect("save config");
        let (ace_count, only_current_user) = read_dacl(&path);
        assert_eq!(ace_count, 1, "saved config must carry one ACE");
        assert!(only_current_user, "the only ACE must be the current user");
    }
}
