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

use crate::logs::{parse_log_line, LogEntry};

/// The number of newest lines the log page keeps.
pub const DEFAULT_MAX_LINES: usize = 400;
/// The largest tail read on open or after a reset.
pub const DEFAULT_TAIL_BYTES: u64 = 256 * 1024;
/// The prefix compared to detect a replaced/rotated file.
const HEAD_BYTES: u64 = 256;

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

/// A bounded, incremental reader over one log file.
#[derive(Debug)]
pub struct LogStream {
    path: PathBuf,
    offset: u64,
    head: Vec<u8>,
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
        let (mut file, length) = open_len(&path)?;
        let tail = read_tail(&mut file, length, tail_bytes, &path)?;
        let mut stream = Self {
            path,
            offset: tail.offset,
            head: tail.head,
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
        let (mut file, length) = open_len(&self.path)?;
        let head = read_head(&mut file, length, &self.path)?;

        if self.rotated(length, &head) {
            let tail = read_tail(&mut file, length, self.tail_bytes, &self.path)?;
            let old = self.lines.len();
            self.offset = tail.offset;
            self.head = tail.head;
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
        let id = LineId {
            offset,
            hash: hash_line(&text),
        };
        if !self.ids.insert(id) {
            return None;
        }
        let line = StreamLine {
            id,
            entry: parse_log_line(&text),
        };
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

    fn rotated(&self, length: u64, head: &[u8]) -> bool {
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
}

fn open_len(path: &Path) -> Result<(File, u64), String> {
    let file =
        File::open(path).map_err(|error| format!("读取日志失败（{}）：{error}", path.display()))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("读取日志大小失败（{}）：{error}", path.display()))?;
    if !metadata.is_file() {
        return Err(format!("日志路径不是文件（{}）", path.display()));
    }
    Ok((file, metadata.len()))
}

fn read_tail(
    file: &mut File,
    length: u64,
    tail_bytes: u64,
    path: &Path,
) -> Result<TailRead, String> {
    let start = length.saturating_sub(tail_bytes);
    file.seek(SeekFrom::Start(start))
        .map_err(|error| format!("定位日志失败（{}）：{error}", path.display()))?;
    let mut bytes = Vec::new();
    file.by_ref()
        .take(length - start)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("读取日志失败（{}）：{error}", path.display()))?;

    let mut cursor = 0usize;
    if start > 0 {
        match bytes.iter().position(|byte| *byte == b'\n') {
            Some(index) => cursor = index + 1,
            None => cursor = bytes.len(),
        }
    }
    let mut lines = Vec::new();
    while let Some(index) = bytes[cursor..].iter().position(|byte| *byte == b'\n') {
        let line_end = cursor + index;
        let text = String::from_utf8_lossy(&bytes[cursor..line_end])
            .trim_end_matches('\r')
            .to_string();
        if !text.trim().is_empty() {
            lines.push((start + cursor as u64, text));
        }
        cursor = line_end + 1;
    }
    let head = read_head(file, length, path)?;
    Ok(TailRead {
        lines,
        offset: start + cursor as u64,
        head,
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
