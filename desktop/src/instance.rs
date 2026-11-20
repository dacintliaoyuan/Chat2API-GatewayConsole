//! Single-instance claim and duplicate-open activation.
//!
//! The first process owns a per-session named mutex; a duplicate open claims the
//! same name, finds it busy, asks the existing window to show itself and exits.
//! `ExistingWindow` is the injectable seam: production uses `FindWindowW`, tests
//! use a fake so no real window is needed.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InstanceError {
    AlreadyRunning { activated: bool },
    Unsupported(String),
}

pub trait ExistingWindow {
    fn activate(&self) -> bool;
}

pub const APP_MUTEX: &str = "Local\\Chat2API-UmDesktop";
pub const WINDOW_TITLE: &str = "本机控制台";

pub fn activate_existing(window: &dyn ExistingWindow) -> bool {
    window.activate()
}

#[cfg(windows)]
pub fn open_or_activate(
    name: &str,
    window: &dyn ExistingWindow,
) -> Result<InstanceGuard, InstanceError> {
    match claim(name) {
        Ok(guard) => Ok(guard),
        Err(InstanceError::AlreadyRunning { .. }) => {
            let activated = activate_existing(window);
            Err(InstanceError::AlreadyRunning { activated })
        }
        Err(error) => Err(error),
    }
}

#[cfg(not(windows))]
pub fn open_or_activate(
    _name: &str,
    _window: &dyn ExistingWindow,
) -> Result<InstanceGuard, InstanceError> {
    Err(InstanceError::Unsupported(
        "单实例只在 Windows 上提供".to_string(),
    ))
}

#[cfg(windows)]
fn claim(name: &str) -> Result<InstanceGuard, InstanceError> {
    use std::os::windows::ffi::OsStrExt;

    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS};
    use windows::Win32::System::Threading::CreateMutexW;

    let wide: Vec<u16> = std::ffi::OsStr::new(name)
        .encode_wide()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        let handle = CreateMutexW(None, true, PCWSTR(wide.as_ptr()))
            .map_err(|error| InstanceError::Unsupported(format!("创建单实例互斥量失败：{error}")))?;
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = CloseHandle(handle);
            return Err(InstanceError::AlreadyRunning { activated: false });
        }
        Ok(InstanceGuard { handle })
    }
}

#[cfg(windows)]
#[derive(Debug)]
pub struct InstanceGuard {
    handle: windows::Win32::Foundation::HANDLE,
}

#[cfg(windows)]
impl Drop for InstanceGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = windows::Win32::Foundation::CloseHandle(self.handle);
        }
    }
}

#[cfg(windows)]
pub struct Win32Window {
    title: String,
}

#[cfg(windows)]
impl Win32Window {
    pub fn new(title: &str) -> Self {
        Self {
            title: title.to_string(),
        }
    }
}

#[cfg(windows)]
impl ExistingWindow for Win32Window {
    fn activate(&self) -> bool {
        use windows::core::PCWSTR;
        use windows::Win32::UI::WindowsAndMessaging::{
            FindWindowW, SetForegroundWindow, ShowWindow, SW_RESTORE,
        };

        let wide: Vec<u16> = self
            .title
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();
        unsafe {
            let Ok(hwnd) = FindWindowW(None, PCWSTR(wide.as_ptr())) else {
                return false;
            };
            if hwnd.0.is_null() {
                return false;
            }
            let _ = ShowWindow(hwnd, SW_RESTORE);
            let _ = SetForegroundWindow(hwnd);
            true
        }
    }
}

#[cfg(not(windows))]
#[derive(Debug)]
pub struct InstanceGuard;
