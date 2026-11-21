#[cfg(windows)]
#[test]
fn a_missing_window_is_reported_not_ignored() {
    let title = format!("um-desktop-dwm-missing-{}", std::process::id());
    let error = um_desktop::dwm::apply_dark_title_bar(&title, true).expect_err("no such window");
    assert!(error.contains("未找到"), "{error}");
}
