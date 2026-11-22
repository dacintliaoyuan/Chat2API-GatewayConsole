//! Windows keeps the window class icon unless the window sets one explicitly;
//! embedding the icon in the executable only covers Explorer and shortcuts.

/// Applies the local console cover to the window itself.
#[cfg(windows)]
pub fn apply() {
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{LPARAM, WPARAM};
    use windows::Win32::UI::WindowsAndMessaging::{
        FindWindowW, LoadImageW, SendMessageW, ICON_BIG, ICON_SMALL, IMAGE_ICON, LR_LOADFROMFILE,
        WM_SETICON,
    };

    const ICON: &[u8] = include_bytes!("../../assets/AI网关控制台.ico");
    const TITLE: &str = "本机控制台";

    let Some(dir) = std::env::var_os("LOCALAPPDATA").map(std::path::PathBuf::from) else {
        return;
    };
    let path = dir.join("Chat2API").join("window-icon.ico");
    if let Some(parent) = path.parent() {
        if std::fs::create_dir_all(parent).is_err() {
            return;
        }
    }
    if std::fs::write(&path, ICON).is_err() {
        return;
    }

    let title: Vec<u16> = TITLE.encode_utf16().chain(std::iter::once(0)).collect();
    let icon_path: Vec<u16> = path
        .to_string_lossy()
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    // The window is created by `show()`, but Windows may publish the handle a
    // moment later; a few bounded retries keep startup deterministic.
    let mut hwnd = None;
    for _ in 0..40 {
        match unsafe { FindWindowW(PCWSTR::null(), PCWSTR::from_raw(title.as_ptr())) } {
            Ok(found) if !found.0.is_null() => {
                hwnd = Some(found);
                break;
            }
            _ => std::thread::sleep(std::time::Duration::from_millis(50)),
        }
    }
    let Some(hwnd) = hwnd else { return };
    for (kind, size) in [(ICON_SMALL, 16), (ICON_BIG, 32)] {
        let Ok(icon) = (unsafe {
            LoadImageW(
                None,
                PCWSTR::from_raw(icon_path.as_ptr()),
                IMAGE_ICON,
                size,
                size,
                LR_LOADFROMFILE,
            )
        }) else {
            continue;
        };
        unsafe {
            SendMessageW(hwnd, WM_SETICON, Some(WPARAM(kind as usize)), Some(LPARAM(icon.0 as isize)));
        }
    }
}

#[cfg(not(windows))]
pub fn apply() {}
