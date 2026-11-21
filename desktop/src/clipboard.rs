//! Clipboard text writes for the desktop shell.
//!
//! The shell owns the clipboard write; Slint only sends the URL it wants copied
//! and renders the result the shell reports back.

/// UTF-16 with the trailing NUL that `CF_UNICODETEXT` requires.
pub fn utf16_with_nul(text: &str) -> Vec<u16> {
    let mut wide: Vec<u16> = text.encode_utf16().collect();
    wide.push(0);
    wide
}

#[cfg(windows)]
pub fn write_text(text: &str) -> Result<(), String> {
    use windows::Win32::System::DataExchange::CloseClipboard;

    let wide = utf16_with_nul(text);
    let bytes = wide.len() * std::mem::size_of::<u16>();
    unsafe {
        open_clipboard()?;
        let result = write_open_clipboard(&wide, bytes);
        // CloseClipboard only releases our ownership; SetClipboardData already
        // committed the text, so a close failure does not undo this copy.
        let _ = CloseClipboard();
        result
    }
}

#[cfg(windows)]
fn open_clipboard() -> Result<(), String> {
    use windows::Win32::System::DataExchange::OpenClipboard;

    // Another process can own the clipboard for a few milliseconds. Retry a
    // bounded number of times so a normal copy does not report "busy" on
    // transient contention; after that the clipboard really is occupied.
    for attempt in 0..5 {
        if unsafe { OpenClipboard(None) }.is_ok() {
            return Ok(());
        }
        if attempt < 4 {
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }
    Err("剪贴板被占用".to_string())
}

#[cfg(windows)]
unsafe fn write_open_clipboard(wide: &[u16], bytes: usize) -> Result<(), String> {
    use windows::Win32::Foundation::{GlobalFree, HANDLE};
    use windows::Win32::System::DataExchange::{EmptyClipboard, SetClipboardData};
    use windows::Win32::System::Memory::{GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE};
    use windows::Win32::System::Ole::CF_UNICODETEXT;

    unsafe {
        EmptyClipboard().map_err(|error| format!("清空剪贴板失败：{error}"))?;
        let handle = GlobalAlloc(GMEM_MOVEABLE, bytes)
            .map_err(|error| format!("分配剪贴板内存失败：{error}"))?;
        let pointer = GlobalLock(handle);
        if pointer.is_null() {
            let _ = GlobalFree(Some(handle));
            return Err("锁定剪贴板内存失败".to_string());
        }
        std::ptr::copy_nonoverlapping(wide.as_ptr(), pointer.cast::<u16>(), wide.len());
        // GlobalUnlock returns FALSE once the lock count reaches zero; the
        // memory stays valid, so only the allocation result matters here.
        let _ = GlobalUnlock(handle);
        if let Err(error) = SetClipboardData(CF_UNICODETEXT.0 as u32, Some(HANDLE(handle.0))) {
            let _ = GlobalFree(Some(handle));
            return Err(format!("写入剪贴板失败：{error}"));
        }
        Ok(())
    }
}

#[cfg(not(windows))]
pub fn write_text(_text: &str) -> Result<(), String> {
    Err("剪贴板只在 Windows 上可用".to_string())
}

#[cfg(test)]
mod tests {
    use super::utf16_with_nul;

    #[test]
    fn clipboard_text_is_utf16_with_a_terminator() {
        assert_eq!(utf16_with_nul("a/中"), vec![0x61, 0x2f, 0x4e2d, 0]);
    }
}
