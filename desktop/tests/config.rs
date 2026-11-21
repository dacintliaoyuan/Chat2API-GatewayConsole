use std::path::Path;

use um_desktop::config::{
    config_path, load_from, model_api_bind, parse_config, save_to, validate_port, DesktopConfig,
    LoadOutcome, DEFAULT_MODEL_API_PORT, MODEL_API_HOST,
};

mod common;
use common::TempDir;

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
