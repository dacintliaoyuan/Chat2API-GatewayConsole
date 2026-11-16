//! Login profile ownership and the on-demand manual display.
//!
//! One profile directory may be driven by at most one login browser. The kernel
//! lock on `<profile>/login-browser.lock` is authoritative across processes;
//! the lock file also carries pid and process start time from byte 1 for
//! diagnostics. A stale Chromium-family process on the SAME profile is stopped
//! before a new browser opens, and no other browser or profile is touched.

use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

pub const PROFILE_LOGIN_LOCK_NAME: &str = "login-browser.lock";
pub const PROFILE_BROWSER_TERM_WAIT: Duration = Duration::from_secs(5);
pub const PROFILE_BROWSER_KILL_WAIT: Duration = Duration::from_secs(2);
pub const PROFILE_BROWSER_POLL: Duration = Duration::from_millis(100);

const PROFILE_BROWSER_NAMES: [&str; 17] = [
    "chrome",
    "chrome.exe",
    "chromium",
    "chromium.exe",
    "chromium-browser",
    "chromium-browser.exe",
    "google-chrome",
    "google-chrome.exe",
    "google-chrome-stable",
    "google-chrome-stable.exe",
    "microsoft-edge",
    "microsoft-edge.exe",
    "microsoft-edge-stable",
    "microsoft-edge-stable.exe",
    "msedge",
    "msedge.exe",
    "headless_shell",
];

/// The diagnostic owner recorded in the profile lock file.
#[derive(Debug, Clone, PartialEq)]
pub struct ProfileLockOwner {
    pub pid: u32,
    pub process_started_epoch: Option<f64>,
    pub acquired_epoch: f64,
    pub profile_dir: String,
}

/// The profile is already held by another login session.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProfileBusy {
    pub detail: String,
    pub owner: Option<Value>,
}

/// One held profile lock. Dropping it releases the OS lock and the
/// in-process guard, exactly like Python's `_release_profile_login_lock`.
#[derive(Debug)]
pub struct ProfileLock {
    file: File,
    path: PathBuf,
    key: String,
}

impl ProfileLock {
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for ProfileLock {
    fn drop(&mut self) {
        platform_unlock(&self.file);
        if let Some(locks) = profile_locks().lock().ok().as_mut() {
            locks.remove(&self.key);
        }
    }
}

/// The lock file path for one profile directory.
pub fn profile_lock_path(profile_dir: &Path) -> PathBuf {
    profile_dir.join(PROFILE_LOGIN_LOCK_NAME)
}

/// Take the ONE login-browser slot for this profile, or report the owner.
pub fn acquire_profile_lock(profile_dir: &Path) -> Result<ProfileLock, ProfileBusy> {
    fs::create_dir_all(profile_dir).map_err(|error| ProfileBusy {
        detail: format!("cannot create profile {}: {error}", profile_dir.display()),
        owner: None,
    })?;
    let key = normalized_profile_key(profile_dir);
    {
        let locks = profile_locks().lock().expect("profile locks poisoned");
        if locks.contains(&key) {
            return Err(profile_busy(profile_dir));
        }
    }
    let path = profile_lock_path(profile_dir);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(&path)
        .map_err(|error| ProfileBusy {
            detail: format!("cannot open profile login lock {}: {error}", path.display()),
            owner: read_profile_lock_owner(profile_dir),
        })?;
    if !platform_try_lock(&file) {
        return Err(profile_busy(profile_dir));
    }
    profile_locks()
        .lock()
        .expect("profile locks poisoned")
        .insert(key.clone());
    let lock = ProfileLock { file, path, key };
    write_profile_lock_owner(&lock);
    Ok(lock)
}

/// Best-effort diagnostic owner of the profile lock.
pub fn read_profile_lock_owner(profile_dir: &Path) -> Option<Value> {
    let path = profile_lock_path(profile_dir);
    let mut file = File::open(path).ok()?;
    file.seek(SeekFrom::Start(1)).ok()?;
    let mut raw = Vec::new();
    file.read_to_end(&mut raw).ok()?;
    let value: Value = serde_json::from_slice(&raw).ok()?;
    value.is_object().then_some(value)
}

/// The readable detail and owner carried by a busy profile.
pub fn profile_busy(profile_dir: &Path) -> ProfileBusy {
    let owner = read_profile_lock_owner(profile_dir);
    let mut detail = format!("profile {}", profile_dir.display());
    if let Some(pid) = owner.as_ref().and_then(|owner| owner.get("pid")) {
        if let Some(pid) = pid.as_u64() {
            detail.push_str(&format!("; owner pid={pid}"));
            if let Some(started) = owner
                .as_ref()
                .and_then(|owner| owner.get("process_started_epoch"))
                .and_then(Value::as_f64)
            {
                detail.push_str(&format!(" started={started:.3}"));
            }
        }
    }
    ProfileBusy { detail, owner }
}

fn profile_locks() -> &'static Mutex<HashSet<String>> {
    static LOCKS: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    LOCKS.get_or_init(|| Mutex::new(HashSet::new()))
}

fn normalized_profile_key(profile_dir: &Path) -> String {
    let resolved = fs::canonicalize(profile_dir).unwrap_or_else(|_| {
        if profile_dir.is_absolute() {
            profile_dir.to_path_buf()
        } else {
            std::env::current_dir()
                .unwrap_or_else(|_| PathBuf::from("."))
                .join(profile_dir)
        }
    });
    let text = resolved.to_string_lossy().to_string();
    if cfg!(windows) {
        text.to_ascii_lowercase()
    } else {
        text
    }
}

fn write_profile_lock_owner(lock: &ProfileLock) {
    let payload = json!({
        "pid": std::process::id(),
        "process_started_epoch": process_start_epoch(std::process::id()),
        "profile_dir": lock.path.parent().map(|path| path.display().to_string()),
        "acquired_epoch": epoch_now(),
    });
    let raw = serde_json::to_vec(&payload).unwrap_or_default();
    let Ok(mut file) = File::options().write(true).open(&lock.path) else {
        return;
    };
    if file.seek(SeekFrom::Start(1)).is_err() {
        return;
    }
    let _ = file.write_all(&raw);
    let _ = file.set_len(1 + raw.len() as u64);
}

#[cfg(unix)]
fn platform_try_lock(file: &File) -> bool {
    use std::os::unix::io::AsRawFd;
    unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) == 0 }
}

#[cfg(unix)]
fn platform_unlock(file: &File) {
    use std::os::unix::io::AsRawFd;
    unsafe {
        libc::flock(file.as_raw_fd(), libc::LOCK_UN);
    }
}

#[cfg(windows)]
mod windows_lock {
    use std::ffi::c_void;
    use std::fs::File;
    use std::os::windows::io::AsRawHandle;

    #[repr(C)]
    struct Overlapped {
        internal: usize,
        internal_high: usize,
        offset: u32,
        offset_high: u32,
        h_event: *mut c_void,
    }

    impl Default for Overlapped {
        fn default() -> Self {
            Self {
                internal: 0,
                internal_high: 0,
                offset: 0,
                offset_high: 0,
                h_event: std::ptr::null_mut(),
            }
        }
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn LockFileEx(
            file: *mut c_void,
            flags: u32,
            reserved: u32,
            bytes_low: u32,
            bytes_high: u32,
            overlapped: *mut Overlapped,
        ) -> i32;
        fn UnlockFileEx(
            file: *mut c_void,
            reserved: u32,
            bytes_low: u32,
            bytes_high: u32,
            overlapped: *mut Overlapped,
        ) -> i32;
    }

    const LOCKFILE_FAIL_IMMEDIATELY: u32 = 0x1;
    const LOCKFILE_EXCLUSIVE_LOCK: u32 = 0x2;

    pub fn try_lock(file: &File) -> bool {
        let mut overlapped = Overlapped::default();
        unsafe {
            LockFileEx(
                file.as_raw_handle().cast(),
                LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                0,
                1,
                0,
                &mut overlapped,
            ) != 0
        }
    }

    pub fn unlock(file: &File) {
        let mut overlapped = Overlapped::default();
        unsafe {
            UnlockFileEx(file.as_raw_handle().cast(), 0, 1, 0, &mut overlapped);
        }
    }
}

#[cfg(windows)]
fn platform_try_lock(file: &File) -> bool {
    windows_lock::try_lock(file)
}

#[cfg(windows)]
fn platform_unlock(file: &File) {
    windows_lock::unlock(file)
}

#[cfg(not(any(unix, windows)))]
fn platform_try_lock(_file: &File) -> bool {
    true
}

#[cfg(not(any(unix, windows)))]
fn platform_unlock(_file: &File) {}

/// Whether a PID still exists; `EPERM` still means the process exists.
pub fn process_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    platform_process_alive(pid)
}

#[cfg(unix)]
fn platform_process_alive(pid: u32) -> bool {
    let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

#[cfg(windows)]
fn platform_process_alive(pid: u32) -> bool {
    windows_process::alive(pid)
}

#[cfg(not(any(unix, windows)))]
fn platform_process_alive(_pid: u32) -> bool {
    true
}

#[cfg(unix)]
fn process_start_epoch(pid: u32) -> Option<f64> {
    let stat = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let fields: Vec<&str> = stat.rsplit(')').next()?.split_whitespace().collect();
    let start_ticks: f64 = fields.get(19)?.parse().ok()?;
    let boot: f64 = fs::read_to_string("/proc/stat")
        .ok()?
        .lines()
        .find_map(|line| line.strip_prefix("btime "))
        .and_then(|value| value.trim().parse().ok())?;
    let hertz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    let hertz = if hertz > 0 { hertz as f64 } else { 100.0 };
    Some(boot + start_ticks / hertz)
}

#[cfg(windows)]
fn process_start_epoch(pid: u32) -> Option<f64> {
    windows_process::start_epoch(pid)
}

#[cfg(not(any(unix, windows)))]
fn process_start_epoch(_pid: u32) -> Option<f64> {
    None
}

#[cfg(windows)]
mod windows_process {
    use std::ffi::c_void;

    #[repr(C)]
    struct FileTime {
        low: u32,
        high: u32,
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn GetProcessTimes(
            process: *mut c_void,
            creation: *mut FileTime,
            exit: *mut FileTime,
            kernel: *mut FileTime,
            user: *mut FileTime,
        ) -> i32;
        fn GetExitCodeProcess(process: *mut c_void, code: *mut u32) -> i32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }

    const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
    const STILL_ACTIVE: u32 = 259;

    pub fn alive(pid: u32) -> bool {
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return false;
            }
            let mut code = 0;
            let ok = GetExitCodeProcess(handle, &mut code) != 0;
            CloseHandle(handle);
            ok && code == STILL_ACTIVE
        }
    }

    pub fn start_epoch(pid: u32) -> Option<f64> {
        unsafe {
            let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if handle.is_null() {
                return None;
            }
            let mut creation = FileTime { low: 0, high: 0 };
            let mut exit = FileTime { low: 0, high: 0 };
            let mut kernel = FileTime { low: 0, high: 0 };
            let mut user = FileTime { low: 0, high: 0 };
            let ok = GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) != 0;
            CloseHandle(handle);
            if !ok {
                return None;
            }
            let ticks = ((creation.high as u64) << 32) | creation.low as u64;
            Some(ticks as f64 / 10_000_000.0 - 11_644_473_600.0)
        }
    }
}

/// One Chromium-family process that keeps a login profile open.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProfileBrowserProcess {
    pub pid: u32,
    pub name: String,
    pub argv: Vec<String>,
}

/// Whether this process is a Chromium-family browser on THIS exact profile.
pub fn profile_browser_process_matches(profile_dir: &Path, name: &str, argv: &[String]) -> bool {
    if !PROFILE_BROWSER_NAMES.contains(&browser_process_name(name).as_str()) {
        return false;
    }
    let Some(used_profile) = profile_argument_from_argv(argv) else {
        return false;
    };
    normalized_profile_path(Path::new(used_profile)) == normalized_profile_path(profile_dir)
}

fn browser_process_name(name: &str) -> String {
    Path::new(name.trim())
        .file_name()
        .map(|name| name.to_string_lossy().to_ascii_lowercase())
        .unwrap_or_default()
}

fn profile_argument_from_argv(argv: &[String]) -> Option<&str> {
    let mut index = 0;
    while index < argv.len() {
        let argument = strip_optional_quotes(&argv[index]);
        if argument == "--user-data-dir" {
            return argv
                .get(index + 1)
                .map(|value| strip_optional_quotes(value));
        }
        if let Some(value) = argument.strip_prefix("--user-data-dir=") {
            return Some(strip_optional_quotes(value));
        }
        index += 1;
    }
    None
}

fn strip_optional_quotes(value: &str) -> &str {
    let value = value.trim();
    if value.len() >= 2
        && (value.starts_with('"') && value.ends_with('"')
            || value.starts_with('\'') && value.ends_with('\''))
    {
        &value[1..value.len() - 1]
    } else {
        value
    }
}

fn normalized_profile_path(path: &Path) -> String {
    let resolved = fs::canonicalize(path).unwrap_or_else(|_| {
        if path.is_absolute() {
            path.to_path_buf()
        } else {
            std::env::current_dir()
                .unwrap_or_else(|_| PathBuf::from("."))
                .join(path)
        }
    });
    let text = resolved.to_string_lossy().to_string();
    if cfg!(windows) {
        text.to_ascii_lowercase()
    } else {
        text
    }
}

/// Stop stale Chromium-family processes on THIS exact profile.
///
/// The caller already holds the profile lock, so no new login browser can
/// appear; the listing and signalling are best-effort like Python's cleanup.
pub fn cleanup_profile_browsers(profile_dir: &Path) {
    let Some(processes) = list_profile_browsers(profile_dir) else {
        return;
    };
    for process in &processes {
        signal_profile_browser(process.pid, false);
    }
    wait_for_exit(&processes, PROFILE_BROWSER_TERM_WAIT);
    let remaining = list_profile_browsers(profile_dir).unwrap_or_default();
    for process in &remaining {
        signal_profile_browser(process.pid, true);
    }
    wait_for_exit(&remaining, PROFILE_BROWSER_KILL_WAIT);
}

fn wait_for_exit(processes: &[ProfileBrowserProcess], budget: Duration) {
    let deadline = Instant::now() + budget;
    while Instant::now() < deadline {
        if !processes.iter().any(|process| process_alive(process.pid)) {
            return;
        }
        std::thread::sleep(PROFILE_BROWSER_POLL);
    }
}

#[cfg(unix)]
fn list_profile_browsers(profile_dir: &Path) -> Option<Vec<ProfileBrowserProcess>> {
    let entries = fs::read_dir("/proc").ok()?;
    let mut found = Vec::new();
    for entry in entries.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse().ok())
        else {
            continue;
        };
        let Ok(raw) = fs::read(format!("/proc/{pid}/cmdline")) else {
            continue;
        };
        if raw.is_empty() {
            continue;
        }
        let argv: Vec<String> = raw
            .split(|byte| *byte == 0)
            .filter(|part| !part.is_empty())
            .map(|part| String::from_utf8_lossy(part).to_string())
            .collect();
        let Some(name) = argv.first() else {
            continue;
        };
        let name = Path::new(name)
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_default();
        if profile_browser_process_matches(profile_dir, &name, &argv) {
            found.push(ProfileBrowserProcess { pid, name, argv });
        }
    }
    Some(found)
}

#[cfg(windows)]
fn list_profile_browsers(profile_dir: &Path) -> Option<Vec<ProfileBrowserProcess>> {
    let output = std::process::Command::new("powershell.exe")
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let value: Value = serde_json::from_slice(&output.stdout).ok()?;
    let items: Vec<&Value> = match &value {
        Value::Array(items) => items.iter().collect(),
        Value::Object(_) => vec![&value],
        _ => return None,
    };
    let mut found = Vec::new();
    for item in items {
        let Some(pid) = item.get("ProcessId").and_then(Value::as_u64) else {
            continue;
        };
        let name = item
            .get("Name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let command_line = item
            .get("CommandLine")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let argv = split_windows_command_line(command_line);
        if profile_browser_process_matches(profile_dir, &name, &argv) {
            found.push(ProfileBrowserProcess {
                pid: pid as u32,
                name,
                argv,
            });
        }
    }
    Some(found)
}

#[cfg(not(any(unix, windows)))]
fn list_profile_browsers(_profile_dir: &Path) -> Option<Vec<ProfileBrowserProcess>> {
    None
}

#[cfg(windows)]
fn split_windows_command_line(command_line: &str) -> Vec<String> {
    // The cleanup only needs the `--user-data-dir` operand; a small
    // quote-aware splitter keeps a quoted profile path intact.
    let mut arguments = Vec::new();
    let mut current = String::new();
    let mut quoted = false;
    let mut characters = command_line.chars().peekable();
    while let Some(character) = characters.next() {
        match character {
            '"' => quoted = !quoted,
            '\\' if quoted && characters.peek() == Some(&'"') => {
                current.push('"');
                characters.next();
            }
            ' ' | '\t' if !quoted => {
                if !current.is_empty() {
                    arguments.push(std::mem::take(&mut current));
                }
            }
            _ => current.push(character),
        }
    }
    if !current.is_empty() {
        arguments.push(current);
    }
    arguments
}

#[cfg(unix)]
fn signal_profile_browser(pid: u32, kill: bool) {
    let signal = if kill { libc::SIGKILL } else { libc::SIGTERM };
    unsafe {
        libc::kill(pid as libc::pid_t, signal);
    }
}

#[cfg(windows)]
fn signal_profile_browser(pid: u32, _kill: bool) {
    windows_terminate::terminate(pid);
}

#[cfg(not(any(unix, windows)))]
fn signal_profile_browser(_pid: u32, _kill: bool) {}

#[cfg(windows)]
mod windows_terminate {
    use std::ffi::c_void;

    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn TerminateProcess(process: *mut c_void, code: u32) -> i32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }

    const PROCESS_TERMINATE: u32 = 0x1;

    pub fn terminate(pid: u32) {
        unsafe {
            let handle = OpenProcess(PROCESS_TERMINATE, 0, pid);
            if handle.is_null() {
                return;
            }
            TerminateProcess(handle, 15);
            CloseHandle(handle);
        }
    }
}

/// The active manual display's DISPLAY/XAUTHORITY, when every part exists.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DisplayEnvironment {
    pub display: String,
    pub xauthority: String,
}

/// Read the on-demand manual display marker; absence is the headless default.
pub fn manual_display(path: &Path) -> Option<DisplayEnvironment> {
    let raw = fs::read_to_string(path).ok()?;
    let mut values = std::collections::HashMap::new();
    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        values.insert(key.trim().to_string(), value.trim().to_string());
    }
    let display = values.get("UM_LOGIN_DISPLAY")?.clone();
    let xauthority = values.get("UM_LOGIN_XAUTHORITY")?.clone();
    let socket_path = values.get("UM_LOGIN_DISPLAY_SOCKET")?.clone();
    if display.is_empty() || xauthority.is_empty() || socket_path.is_empty() {
        return None;
    }
    let number = display.strip_prefix(':')?.split('.').next()?;
    if number.is_empty() || !number.chars().all(|character| character.is_ascii_digit()) {
        return None;
    }
    if File::open(&xauthority).is_err() {
        return None;
    }
    if !Path::new(&socket_path).exists() {
        return None;
    }
    if let Some(pid) = values.get("UM_LOGIN_DISPLAY_PID") {
        let pid: u32 = pid.parse().ok()?;
        if pid == 0 || !process_alive(pid) {
            return None;
        }
    }
    Some(DisplayEnvironment {
        display,
        xauthority,
    })
}

/// Whether an explicit visible window is possible on this host.
pub fn visible_login_supported() -> bool {
    if cfg!(windows) {
        return true;
    }
    std::env::var("DISPLAY")
        .ok()
        .is_some_and(|value| !value.trim().is_empty())
}

pub fn epoch_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs_f64())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir() -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_nanos())
            .unwrap_or_default();
        let path =
            std::env::temp_dir().join(format!("um-login-profile-{}-{stamp}", std::process::id()));
        fs::create_dir_all(&path).expect("temp dir");
        path
    }

    #[test]
    fn a_second_profile_lock_is_busy_until_the_first_is_dropped() {
        let dir = temp_dir();
        let first = acquire_profile_lock(&dir).expect("first lock");
        let busy = acquire_profile_lock(&dir).expect_err("second lock");
        assert!(busy.detail.contains("profile"));
        let owner = busy.owner.expect("owner");
        assert_eq!(owner["pid"], std::process::id());
        drop(first);
        let second = acquire_profile_lock(&dir).expect("lock after release");
        drop(second);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_lock_file_records_pid_and_start_time_from_byte_one() {
        let dir = temp_dir();
        let lock = acquire_profile_lock(&dir).expect("lock");
        let owner = read_profile_lock_owner(&dir).expect("owner");
        assert_eq!(owner["pid"], std::process::id());
        assert_eq!(owner["profile_dir"], dir.display().to_string());
        assert!(owner["acquired_epoch"]
            .as_f64()
            .is_some_and(|value| value > 0.0));
        // The recorded start time is a 100ns-tick FILETIME projection; serde_json's
        // default float parser may be off by 1 ULP, so compare at microsecond
        // precision instead of bit equality.
        let recorded = owner["process_started_epoch"]
            .as_f64()
            .expect("recorded start time");
        let fresh = process_start_epoch(std::process::id()).expect("fresh start time");
        assert!(
            (recorded - fresh).abs() < 1e-6,
            "recorded {recorded} != fresh {fresh}"
        );
        drop(lock);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_profile_browser_matches_only_the_exact_profile() {
        let profile = Path::new("/tmp/um-profile");
        assert!(profile_browser_process_matches(
            profile,
            "chrome.exe",
            &[
                "chrome.exe".to_string(),
                "--user-data-dir=/tmp/um-profile".to_string(),
            ],
        ));
        assert!(profile_browser_process_matches(
            profile,
            "chromium",
            &[
                "chromium".to_string(),
                "--user-data-dir".to_string(),
                "/tmp/um-profile".to_string(),
            ],
        ));
        assert!(!profile_browser_process_matches(
            profile,
            "chrome.exe",
            &[
                "chrome.exe".to_string(),
                "--user-data-dir=/tmp/um-profile-other".to_string(),
            ],
        ));
        assert!(!profile_browser_process_matches(
            profile,
            "firefox.exe",
            &[
                "firefox.exe".to_string(),
                "--user-data-dir=/tmp/um-profile".to_string(),
            ],
        ));
        assert!(!profile_browser_process_matches(
            profile,
            "chrome.exe",
            &["chrome.exe".to_string()],
        ));
    }

    #[test]
    fn an_absent_display_marker_is_the_headless_default() {
        let dir = temp_dir();
        assert!(manual_display(&dir.join("missing.env")).is_none());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn a_complete_display_marker_is_accepted() {
        let dir = temp_dir();
        let auth = dir.join("Xauthority");
        fs::write(&auth, "cookie").expect("auth");
        let socket = dir.join("X11-unix-X99");
        fs::write(&socket, "").expect("socket");
        let marker = dir.join("login-display.env");
        fs::write(
            &marker,
            format!(
                "UM_LOGIN_DISPLAY=:99\nUM_LOGIN_XAUTHORITY={}\n\
                 UM_LOGIN_DISPLAY_SOCKET={}\nUM_LOGIN_DISPLAY_PID={}\n",
                auth.display(),
                socket.display(),
                std::process::id()
            ),
        )
        .expect("marker");
        let display = manual_display(&marker).expect("display");
        assert_eq!(display.display, ":99");
        assert_eq!(display.xauthority, auth.display().to_string());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn an_incomplete_display_marker_falls_back_to_headless() {
        let dir = temp_dir();
        let auth = dir.join("Xauthority");
        fs::write(&auth, "cookie").expect("auth");
        let marker = dir.join("login-display.env");
        fs::write(
            &marker,
            format!(
                "UM_LOGIN_DISPLAY=:99\nUM_LOGIN_XAUTHORITY={}\n",
                auth.display()
            ),
        )
        .expect("marker");
        assert!(manual_display(&marker).is_none());
        let _ = fs::remove_dir_all(dir);
    }
}
