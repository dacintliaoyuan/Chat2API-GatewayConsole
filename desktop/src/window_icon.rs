//! Applies the executable's embedded icon to the Slint window and tray.

#[cfg(windows)]
pub const APP_ICON: windows::core::PCWSTR = windows::core::PCWSTR(101usize as *const u16);

#[cfg(windows)]
pub fn apply() {
    use windows::core::w;
    use windows::Win32::Foundation::{LPARAM, WPARAM};
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::UI::WindowsAndMessaging::{
        FindWindowW, GetWindowThreadProcessId, LoadImageW, SendMessageW, ICON_BIG, ICON_SMALL,
        IMAGE_ICON, LR_SHARED, WM_SETICON,
    };

    unsafe {
        let Ok(hwnd) = FindWindowW(None, w!("本机控制台")) else {
            return;
        };
        let mut pid = 0;
        GetWindowThreadProcessId(hwnd, Some(&mut pid));
        if pid != std::process::id() {
            return;
        }
        let Ok(module) = GetModuleHandleW(None) else {
            return;
        };
        for (kind, size) in [(ICON_SMALL, 16), (ICON_BIG, 32)] {
            if let Ok(icon) =
                LoadImageW(Some(module.into()), APP_ICON, IMAGE_ICON, size, size, LR_SHARED)
            {
                SendMessageW(
                    hwnd,
                    WM_SETICON,
                    Some(WPARAM(kind as usize)),
                    Some(LPARAM(icon.0 as isize)),
                );
            }
        }
    }
}

#[cfg(not(windows))]
pub fn apply() {}
