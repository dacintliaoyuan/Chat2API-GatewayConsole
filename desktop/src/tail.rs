//! Bounded tail reads for the app's own log files.

use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

/// Reads at most `max_bytes` from the end of `path`.
///
/// A partial first line is dropped when the read started in the middle of the
/// file, so callers never receive half a JSONL record. A missing file is an
/// error; an empty file returns an empty string.
pub fn read_tail(path: &Path, max_bytes: u64) -> Result<String, String> {
    let mut file = std::fs::File::open(path)
        .map_err(|error| format!("打开日志失败（{}）：{error}", path.display()))?;
    let length = file
        .metadata()
        .map_err(|error| format!("读取日志大小失败（{}）：{error}", path.display()))?
        .len();
    let start = length.saturating_sub(max_bytes);
    file.seek(SeekFrom::Start(start))
        .map_err(|error| format!("定位日志失败（{}）：{error}", path.display()))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)
        .map_err(|error| format!("读取日志失败（{}）：{error}", path.display()))?;
    if start > 0 {
        if let Some(index) = bytes.iter().position(|byte| *byte == b'\n') {
            bytes.drain(..=index);
        }
    }
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}
