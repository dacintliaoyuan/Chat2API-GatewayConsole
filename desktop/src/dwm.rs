//! Windows immersive dark mode for the native title bar.
//!
//! Slint paints the client area; the caption stays with DWM. Setting
//! `DWMWA_USE_IMMERSIVE_DARK_MODE` makes the caption follow the same
//! light/dark choice as the palette.

#[cfg(windows)]
pub fn apply_dark_title_bar(title: &str, dark: bool) -> Result<(), String> {
    use std::ffi::c_void;
    use std::mem::size_of;

    use windows::core::PCWSTR;
    use windows::Win32::Graphics::Dwm::{DwmSetWindowAttribute, DWMWA_USE_IMMERSIVE_DARK_MODE};
    use windows::Win32::UI::WindowsAndMessaging::FindWindowW;

    let wide: Vec<u16> = title.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {
        let hwnd = FindWindowW(None, PCWSTR(wide.as_ptr()))
            .map_err(|error| format!("未找到控制台窗口：{error}"))?;
        let value: i32 = i32::from(dark);
        DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            (&value as *const i32).cast::<c_void>(),
            size_of::<i32>() as u32,
        )
        .map_err(|error| format!("设置标题栏深色模式失败：{error}"))
    }
}

#[cfg(not(windows))]
pub fn apply_dark_title_bar(_title: &str, _dark: bool) -> Result<(), String> {
    Err("深色标题栏只在 Windows 上提供".to_string())
}
