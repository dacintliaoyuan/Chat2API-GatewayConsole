//! Incremental tail of the app instance's service log (issue #126).
//!
//! The gateway service log is a text/JSONL file with no event id. The stable line
//! identity is the absolute byte offset of the line plus a hash of its content; the
//! page never displays a synthetic event id. Reads advance a file offset so a poll
//! only returns lines that were not shown before. Truncation, rotation and
//! replacement reset the stream and re-read the new tail.

use std::collections::{HashSet, VecDeque};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

use chrono::{DateTime, Local, TimeZone};

use crate::logs::{parse_log_line, sanitize_text, LogEntry};

/// The height of one log row in the Slint list.
pub const LOG_ROW_HEIGHT: f32 = 36.0;
/// The number of newest lines the log page keeps.
pub const DEFAULT_MAX_LINES: usize = 400;
/// The largest tail read on open or after a reset.
pub const DEFAULT_TAIL_BYTES: u64 = 256 * 1024;
/// The prefix compared to detect a replaced/rotated file.
const HEAD_BYTES: u64 = 256;

#[cfg(windows)]
type FileId = (u32, u64);
#[cfg(not(windows))]
type FileId = ();

/// The short and precise display text for one log timestamp.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LogTime {
    /// `%H:%M:%S` for today, `%m-%d %H:%M:%S` for an earlier day.
    pub label: String,
    /// Full local date, time and offset for the expanded detail.
    pub precise: String,
}

/// Formats a log timestamp against the current local day. A record from an earlier
/// day keeps its month and day in the list; today only needs the time.
pub fn format_log_time(at: Option<i64>, now: DateTime<Local>) -> LogTime {
    match at.and_then(|at| Local.timestamp_millis_opt(at).single()) {
        Some(time) => {
            let label = if time.date_naive() == now.date_naive() {
                time.format("%H:%M:%S").to_string()
            } else {
                time.format("%m-%d %H:%M:%S").to_string()
            };
            LogTime {
                label,
                precise: time.format("%Y-%m-%d %H:%M:%S %:z").to_string(),
            }
        }
        None => LogTime {
            label: "时间未知".to_string(),
            precise: "时间未知".to_string(),
        },
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct LineId {
    /// Absolute byte offset of the line in the log file.
    pub offset: u64,
    /// FNV-1a hash of the line content (without the trailing newline).
    pub hash: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamLine {
    pub id: LineId,
    pub entry: LogEntry,
    pub detail: String,
}

impl StreamLine {
    fn from_text(offset: u64, text: &str) -> Self {
        let entry = parse_log_line(text);
        let detail = if text == entry.text {
            entry.text.clone()
        } else {
            sanitize_text(text)
        };
        Self {
            id: LineId { offset, hash: hash_line(text) },
            entry,
            detail,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PollOutcome {
    /// Lines appended by this poll (empty when nothing new was complete).
    pub appended: Vec<StreamLine>,
    /// True when the file was truncated, rotated or replaced and the window was rebuilt.
    pub reset: bool,
    /// Lines dropped from the front because the bounded window was exceeded.
    pub trimmed: usize,
    /// Lines currently retained after the poll.
    pub total: usize,
}

/// The visible rows and scroll/count changes for one log-page update.
#[derive(Debug, Clone, PartialEq)]
pub struct LogPagePlan {
    /// Every row the page should show after the update.
    pub rows: Vec<StreamLine>,
    /// A new scroll position when the update rebuilt the list; `None` keeps it.
    pub scroll_y: Option<f32>,
    /// The new "有新日志" count.
    pub new_count: i32,
    /// The number of matching appended lines in this update.
    pub added: usize,
    /// True when the page must replace the whole row list instead of appending.
    pub rebuild: bool,
}

/// Plans one live log-page update from a poll outcome. While the user has paused
/// following, appended rows extend the list without moving the scroll position, and
/// the new-line count grows; resuming follow clears the count and lets the view jump
/// to the newest row. When the bounded window trims the front, the row at the top of
/// the viewport is looked up again so a paused reading does not jump.
#[allow(clippy::too_many_arguments)]
pub fn plan_log_page(
    previous: &[StreamLine],
    entries: &[StreamLine],
    appended: &[StreamLine],
    reset: bool,
    trimmed: usize,
    filter_changed: bool,
    follow: bool,
    scroll_y: f32,
    new_count: i32,
    level: LevelFilter,
    keyword: &str,
) -> LogPagePlan {
    let keyword = keyword.trim().to_lowercase();
    let matches = |line: &&StreamLine| matches_line(line, level, &keyword);
    let added = appended.iter().filter(matches).count();
    let current_rows: Vec<_> = previous.iter().filter(matches).cloned().collect();
    let rebuild = reset || trimmed > 0 || filter_changed;
    let rows = if rebuild {
        entries.iter().filter(matches).cloned().collect()
    } else if added > 0 {
        let mut rows = current_rows.clone();
        rows.extend(appended.iter().filter(matches).cloned());
        rows
    } else {
        current_rows.clone()
    };
    let scroll_y = if rebuild && !follow {
        let scroll = scroll_y.min(0.0);
        let first = (-scroll / LOG_ROW_HEIGHT).floor() as usize;
        let remainder = -scroll - first as f32 * LOG_ROW_HEIGHT;
        let anchor = (!filter_changed && !reset)
            .then(|| current_rows.get(first))
            .flatten();
        let new_first = anchor.and_then(|anchor| rows.iter().position(|row| row.id == anchor.id));
        Some(new_first.map_or(0.0, |new_first| {
            -(new_first as f32 * LOG_ROW_HEIGHT + remainder)
        }))
    } else {
        None
    };
    let new_count = if follow || filter_changed {
        0
    } else {
        new_count.saturating_add(added as i32)
    };
    LogPagePlan {
        rows,
        scroll_y,
        new_count,
        added,
        rebuild,
    }
}

/// A bounded, incremental reader over one log file.
#[derive(Debug)]
pub struct LogStream {
    path: PathBuf,
    offset: u64,
    head: Vec<u8>,
    file_id: Option<FileId>,
    lines: VecDeque<StreamLine>,
    ids: HashSet<LineId>,
    max_lines: usize,
    tail_bytes: u64,
}

impl LogStream {
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, String> {
        Self::open_with_limits(path, DEFAULT_MAX_LINES, DEFAULT_TAIL_BYTES)
    }

    pub fn open_with_limits(
        path: impl Into<PathBuf>,
        max_lines: usize,
        tail_bytes: u64,
    ) -> Result<Self, String> {
        let path = path.into();
        let (mut file, length, file_id) = open_len(&path)?;
        let tail = read_tail(&mut file, length, tail_bytes, &path)?;
        let mut stream = Self {
            path,
            offset: tail.offset,
            head: tail.head,
            file_id,
            lines: VecDeque::new(),
            ids: HashSet::new(),
            max_lines: max_lines.max(1),
            tail_bytes: tail_bytes.max(1),
        };
        stream.install(tail.lines);
        Ok(stream)
    }

    /// Reads everything appended since the last poll.
    ///
    /// A partial trailing line is left unread until its newline arrives. On an
    /// unreadable file the previous window is kept and the error carries the path
    /// and the operating-system reason.
    pub fn poll(&mut self) -> Result<PollOutcome, String> {
        let (mut file, length, file_id) = open_len(&self.path)?;
        let head = read_head(&mut file, length, &self.path)?;

        if self.rotated(length, &head, file_id) {
            let tail = read_tail(&mut file, length, self.tail_bytes, &self.path)?;
            let old = self.lines.len();
            self.offset = tail.offset;
            self.head = tail.head;
            self.file_id = file_id;
            self.install(tail.lines);
            return Ok(PollOutcome {
                appended: self.lines.iter().cloned().collect(),
                reset: true,
                trimmed: old,
                total: self.lines.len(),
            });
        }

        if length == self.offset {
            self.refresh_head(head);
            return Ok(PollOutcome {
                appended: Vec::new(),
                reset: false,
                trimmed: 0,
                total: self.lines.len(),
            });
        }

        file.seek(SeekFrom::Start(self.offset))
            .map_err(|error| format!("定位日志失败（{}）：{error}", self.path.display()))?;
        let mut bytes = Vec::new();
        file.take(length - self.offset)
            .read_to_end(&mut bytes)
            .map_err(|error| format!("读取日志失败（{}）：{error}", self.path.display()))?;

        let mut cursor = 0usize;
        let mut appended = Vec::new();
        while let Some(index) = bytes[cursor..].iter().position(|byte| *byte == b'\n') {
            let line_end = cursor + index;
            let text = String::from_utf8_lossy(&bytes[cursor..line_end])
                .trim_end_matches('\r')
                .to_string();
            let line_offset = self.offset + cursor as u64;
            if !text.trim().is_empty() {
                if let Some(line) = self.push(line_offset, text) {
                    appended.push(line);
                }
            }
            cursor = line_end + 1;
        }
        self.offset += cursor as u64;
        self.refresh_head(head);
        let trimmed = self.trim_front();
        Ok(PollOutcome {
            appended,
            reset: false,
            trimmed,
            total: self.lines.len(),
        })
    }

    pub fn entries(&self) -> Vec<LogEntry> {
        self.lines.iter().map(|line| line.entry.clone()).collect()
    }

    pub fn lines(&self) -> &VecDeque<StreamLine> {
        &self.lines
    }

    pub fn offset(&self) -> u64 {
        self.offset
    }

    fn install(&mut self, lines: Vec<(u64, String)>) {
        self.lines.clear();
        self.ids.clear();
        for (offset, text) in lines {
            self.push(offset, text);
        }
        self.trim_front();
    }

    fn push(&mut self, offset: u64, text: String) -> Option<StreamLine> {
        let line = StreamLine::from_text(offset, &text);
        if !self.ids.insert(line.id) {
            return None;
        }
        self.lines.push_back(line.clone());
        Some(line)
    }

    fn trim_front(&mut self) -> usize {
        let mut trimmed = 0;
        while self.lines.len() > self.max_lines {
            if let Some(line) = self.lines.pop_front() {
                self.ids.remove(&line.id);
                trimmed += 1;
            }
        }
        trimmed
    }

    fn rotated(&self, length: u64, head: &[u8], file_id: Option<FileId>) -> bool {
        if self.file_id.is_some() && file_id.is_some() && self.file_id != file_id {
            return true;
        }
        if length < self.offset {
            return true;
        }
        if self.head.is_empty() {
            return false;
        }
        let common = self.head.len().min(head.len());
        common > 0 && self.head[..common] != head[..common]
    }

    fn refresh_head(&mut self, head: Vec<u8>) {
        if head.len() >= self.head.len() {
            self.head = head;
        }
    }
}

struct TailRead {
    lines: Vec<(u64, String)>,
    offset: u64,
    head: Vec<u8>,
    start: u64,
}

fn open_len(path: &Path) -> Result<(File, u64, Option<FileId>), String> {
    let file =
        File::open(path).map_err(|error| format!("读取日志失败（{}）：{error}", path.display()))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("读取日志大小失败（{}）：{error}", path.display()))?;
    if !metadata.is_file() {
        return Err(format!("日志路径不是文件（{}）", path.display()));
    }
    let file_id = file_id(&file);
    Ok((file, metadata.len(), file_id))
}

#[cfg(windows)]
fn file_id(file: &File) -> Option<FileId> {
    use std::os::windows::io::AsRawHandle;

    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe {
        GetFileInformationByHandle(HANDLE(file.as_raw_handle() as _), &mut info).ok()?;
    }
    Some((
        info.dwVolumeSerialNumber,
        ((info.nFileIndexHigh as u64) << 32) | info.nFileIndexLow as u64,
    ))
}

#[cfg(not(windows))]
fn file_id(_file: &File) -> Option<FileId> {
    None
}

fn read_tail(
    file: &mut File,
    length: u64,
    tail_bytes: u64,
    path: &Path,
) -> Result<TailRead, String> {
    let start = length.saturating_sub(tail_bytes);
    let read_from = start.saturating_sub(1);
    file.seek(SeekFrom::Start(read_from))
        .map_err(|error| format!("定位日志失败（{}）：{error}", path.display()))?;
    let mut bytes = Vec::new();
    file.by_ref()
        .take(length - read_from)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("读取日志失败（{}）：{error}", path.display()))?;

    let mut cursor = 0usize;
    if read_from < start {
        match bytes.iter().position(|byte| *byte == b'\n') {
            Some(index) => cursor = index + 1,
            None => cursor = bytes.len(),
        }
    }
    let first_complete = read_from + cursor as u64;
    let mut lines = Vec::new();
    while let Some(index) = bytes[cursor..].iter().position(|byte| *byte == b'\n') {
        let line_end = cursor + index;
        let text = String::from_utf8_lossy(&bytes[cursor..line_end])
            .trim_end_matches('\r')
            .to_string();
        if !text.trim().is_empty() {
            lines.push((read_from + cursor as u64, text));
        }
        cursor = line_end + 1;
    }
    let head = read_head(file, length, path)?;
    Ok(TailRead {
        lines,
        offset: read_from + cursor as u64,
        head,
        start: first_complete.min(start.saturating_add(tail_bytes)),
    })
}

fn read_head(file: &mut File, length: u64, path: &Path) -> Result<Vec<u8>, String> {
    if length == 0 {
        return Ok(Vec::new());
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|error| format!("定位日志失败（{}）：{error}", path.display()))?;
    let mut head = vec![0u8; length.min(HEAD_BYTES) as usize];
    file.read_exact(&mut head)
        .map_err(|error| format!("读取日志失败（{}）：{error}", path.display()))?;
    Ok(head)
}

fn hash_line(text: &str) -> u64 {
    let mut hash = 0xcbf2_9ce4_8422_2325u64;
    for byte in text.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LevelFilter {
    All,
    Info,
    Warn,
    Error,
    Raw,
}

impl LevelFilter {
    pub fn from_index(index: i32) -> Self {
        match index {
            1 => Self::Info,
            2 => Self::Warn,
            3 => Self::Error,
            4 => Self::Raw,
            _ => Self::All,
        }
    }

    pub fn index(self) -> i32 {
        match self {
            Self::All => 0,
            Self::Info => 1,
            Self::Warn => 2,
            Self::Error => 3,
            Self::Raw => 4,
        }
    }

    pub fn matches(self, level: &str) -> bool {
        match self {
            Self::All => true,
            Self::Info => level.eq_ignore_ascii_case("info"),
            Self::Warn => {
                level.eq_ignore_ascii_case("warn") || level.eq_ignore_ascii_case("warning")
            }
            Self::Error => level.eq_ignore_ascii_case("error"),
            Self::Raw => level.eq_ignore_ascii_case("raw"),
        }
    }
}

/// A bounded page of earlier complete lines in the current service log. The
/// cursor is a byte boundary, so appending lines does not move a historical page.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HistoryPage {
    pub lines: Vec<StreamLine>,
    pub older: Option<u64>,
}

pub fn read_history(
    path: &Path,
    before: Option<u64>,
    since: i64,
    level: LevelFilter,
    keyword: &str,
) -> Result<HistoryPage, String> {
    let (mut file, length, _) = open_len(path)?;
    if before.is_some_and(|offset| offset > length) {
        return Err("日志已轮换，请从最新一页重新读取".to_string());
    }
    let end = before.unwrap_or(length);
    let tail = read_tail(&mut file, end, DEFAULT_TAIL_BYTES, path)?;
    let keyword = keyword.trim().to_lowercase();
    let mut lines = Vec::new();
    let mut older = if end > DEFAULT_TAIL_BYTES {
        let boundary = tail.start;
        Some(if boundary < end { boundary } else { end - DEFAULT_TAIL_BYTES })
    } else {
        None
    };
    for (offset, text) in tail.lines.iter().rev() {
        let line = StreamLine::from_text(*offset, text);
        if line.entry.at.is_some_and(|time| time < since)
            || line.entry.at.is_none()
            || !matches_line(&line, level, &keyword)
        {
            continue;
        }
        if lines.len() == 120 {
            older = lines.last().map(|line: &StreamLine| line.id.offset);
            break;
        }
        lines.push(line);
    }
    Ok(HistoryPage { lines, older })
}

pub fn matches_line(line: &StreamLine, level: LevelFilter, keyword_lowercase: &str) -> bool {
    level.matches(&line.entry.level)
        && (keyword_lowercase.is_empty() || line.detail.to_lowercase().contains(keyword_lowercase))
}

/// Filters entries by level and a case-insensitive keyword in the display text.
pub fn filter_entries(entries: &[LogEntry], level: LevelFilter, keyword: &str) -> Vec<LogEntry> {
    let needle = keyword.trim().to_lowercase();
    entries
        .iter()
        .filter(|entry| {
            level.matches(&entry.level)
                && (needle.is_empty() || entry.text.to_lowercase().contains(&needle))
        })
        .cloned()
        .collect()
}
