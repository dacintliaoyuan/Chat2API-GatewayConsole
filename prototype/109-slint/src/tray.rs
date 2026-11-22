//! Minimal Win32 system-tray icon for the Slint prototype.
//!
//! Slint owns the winit event loop, so the tray uses a message-only window and
//! a `Shell_NotifyIconW` icon whose callback message is pumped from a Slint
//! timer on the UI thread. This keeps the prototype free of a second event loop
//! and of the tray-icon/tao dependency stack.

use std::sync::atomic::{AtomicI64, AtomicU8, Ordering};

use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, POINT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Shell::{
    Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NOTIFYICONDATAW,
};
use windows::Win32::UI::WindowsAndMessaging::{
    AppendMenuW, CreatePopupMenu, CreateWindowExW, DefWindowProcW, DestroyMenu, DestroyWindow,
    DispatchMessageW, GetCursorPos, LoadIconW, PeekMessageW, PostMessageW, RegisterClassW,
    SetForegroundWindow, TrackPopupMenu, TranslateMessage, UnregisterClassW, IDI_APPLICATION,
    MF_STRING, MSG, PM_REMOVE, TPM_LEFTALIGN, TPM_NONOTIFY, TPM_RETURNCMD, WM_APP,
    WM_LBUTTONUP, WM_NULL, WM_RBUTTONUP, WNDCLASSW, WINDOW_EX_STYLE, WINDOW_STYLE,
};

pub const SHOW: u8 = 1;
pub const QUIT: u8 = 2;
pub const MENU: u8 = 3;

pub static FLAG: AtomicU8 = AtomicU8::new(0);
pub static CLICK_MS: AtomicI64 = AtomicI64::new(0);

const TRAY_ID: u32 = 1;
const TRAY_CALLBACK: u32 = WM_APP + 1;
const ID_SHOW: usize = 1001;
const ID_QUIT: usize = 1002;

unsafe extern "system" fn wnd_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if message == TRAY_CALLBACK {
        match lparam.0 as u32 {
            WM_LBUTTONUP => {
                CLICK_MS.store(crate::metrics::now_ms(), Ordering::SeqCst);
                FLAG.store(SHOW, Ordering::SeqCst);
            }
            WM_RBUTTONUP => FLAG.store(MENU, Ordering::SeqCst),
            _ => {}
        }
        return LRESULT(0);
    }
    DefWindowProcW(hwnd, message, wparam, lparam)
}

pub struct Tray {
    hwnd: HWND,
    menu: windows::Win32::UI::WindowsAndMessaging::HMENU,
    data: NOTIFYICONDATAW,
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

pub fn install() -> Result<Tray, String> {
    unsafe {
        let instance: HINSTANCE = GetModuleHandleW(None)
            .map_err(|error| error.to_string())?
            .into();
        let class = w!("Proto109TrayWnd");
        let wnd_class = WNDCLASSW {
            lpfnWndProc: Some(wnd_proc),
            hInstance: instance,
            lpszClassName: class,
            ..Default::default()
        };
        if RegisterClassW(&wnd_class) == 0 {
            return Err("RegisterClassW failed".to_string());
        }
        let hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            class,
            w!("proto109-tray"),
            WINDOW_STYLE(0),
            0,
            0,
            0,
            0,
            Some(windows::Win32::UI::WindowsAndMessaging::HWND_MESSAGE),
            None,
            Some(instance),
            None,
        )
        .map_err(|error| error.to_string())?;
        let menu = CreatePopupMenu().map_err(|error| error.to_string())?;
        AppendMenuW(menu, MF_STRING, ID_SHOW, w!("显示主窗口"))
            .map_err(|error| error.to_string())?;
        AppendMenuW(menu, MF_STRING, ID_QUIT, w!("退出"))
            .map_err(|error| error.to_string())?;
        let icon = LoadIconW(None, IDI_APPLICATION).map_err(|error| error.to_string())?;
        let mut data = NOTIFYICONDATAW {
            cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: hwnd,
            uID: TRAY_ID,
            uFlags: NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage: TRAY_CALLBACK,
            hIcon: icon,
            ..Default::default()
        };
        let tip = wide("本机控制台 109 原型");
        let length = tip.len().min(data.szTip.len());
        data.szTip[..length].copy_from_slice(&tip[..length]);
        if !Shell_NotifyIconW(NIM_ADD, &data).as_bool() {
            return Err("Shell_NotifyIconW(NIM_ADD) failed".to_string());
        }
        Ok(Tray { hwnd, menu, data })
    }
}

impl Tray {
    /// Pumps the tray window's messages and returns the pending menu command.
    pub fn pump(&self) -> Option<u8> {
        unsafe {
            let mut message = MSG::default();
            while PeekMessageW(&mut message, Some(self.hwnd), 0, 0, PM_REMOVE).as_bool() {
                let _ = TranslateMessage(&message);
                DispatchMessageW(&message);
            }
            let flag = FLAG.swap(0, Ordering::SeqCst);
            match flag {
                SHOW | QUIT => Some(flag),
                MENU => {
                    let mut point = POINT::default();
                    let _ = GetCursorPos(&mut point);
                    let _ = SetForegroundWindow(self.hwnd);
                    let command = TrackPopupMenu(
                        self.menu,
                        TPM_LEFTALIGN | TPM_RETURNCMD | TPM_NONOTIFY,
                        point.x,
                        point.y,
                        None,
                        self.hwnd,
                        None,
                    )
                    .0 as usize;
                    let _ = PostMessageW(Some(self.hwnd), WM_NULL, WPARAM(0), LPARAM(0));
                    match command {
                        ID_SHOW => Some(SHOW),
                        ID_QUIT => Some(QUIT),
                        _ => None,
                    }
                }
                _ => None,
            }
        }
    }

    pub fn remove(&self) {
        unsafe {
            let _ = Shell_NotifyIconW(NIM_DELETE, &self.data);
            let _ = DestroyMenu(self.menu);
            let _ = DestroyWindow(self.hwnd);
            if let Ok(instance) = GetModuleHandleW(None) {
                let _ = UnregisterClassW(PCWSTR(w!("Proto109TrayWnd").as_ptr()), Some(instance.into()));
            }
        }
    }
}
