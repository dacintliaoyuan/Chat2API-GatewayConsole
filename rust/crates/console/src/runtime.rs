//! Read-only cloud runtime facts for the 8002 console.
//!
//! `console/adapters.py::cloud_service_status` reads the UM share entry from
//! the Linux pid file and the real `/proc` process identity. This module ports
//! the read-only part only: no pid file, state file or projection is written,
//! no process is signalled, and no PowerShell/tasklist/netstat is invoked.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::config::{ConsoleConfig, EntryKind};

pub const HOST_INTENT_FILE: &str = "share-host-desired.json";
pub const HOST_INTENT_SCHEMA: i64 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeSnapshot {
    pub state: String,
    pub healthy: bool,
    pub detail: String,
    pub process_ids: Vec<u32>,
}

impl RuntimeSnapshot {
    pub fn stopped(detail: impl Into<String>) -> Self {
        Self {
            state: "stopped".to_string(),
            healthy: false,
            detail: detail.into(),
            process_ids: Vec::new(),
        }
    }

    pub fn unknown(detail: impl Into<String>, process_ids: Vec<u32>) -> Self {
        Self {
            state: "unknown".to_string(),
            healthy: false,
            detail: detail.into(),
            process_ids,
        }
    }

    pub fn conflict(detail: impl Into<String>, process_ids: Vec<u32>) -> Self {
        Self {
            state: "conflict".to_string(),
            healthy: false,
            detail: detail.into(),
            process_ids,
        }
    }

    pub fn running(process_ids: Vec<u32>) -> Self {
        Self {
            state: "running".to_string(),
            healthy: true,
            detail: "ok".to_string(),
            process_ids,
        }
    }

    /// `cloud_service_status` maps the host state to the console's title case.
    pub fn display_state(&self) -> &'static str {
        match self.state.as_str() {
            "running" => "Running",
            "stopped" => "Stopped",
            "conflict" => "Conflict",
            _ => "Unknown",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RuntimeError;

pub trait RuntimeFacts: Send + Sync {
    fn snapshot(&self) -> Result<RuntimeSnapshot, RuntimeError>;
}

/// The kernel-facing liveness probe. `None` means undecidable, never a
/// fabricated stopped/running verdict.
pub trait ProcessProbe: Send + Sync {
    fn is_alive(&self, pid: u32) -> Option<bool>;
}

pub struct SystemProcessProbe;

impl ProcessProbe for SystemProcessProbe {
    #[cfg(unix)]
    fn is_alive(&self, pid: u32) -> Option<bool> {
        let result = unsafe { libc::kill(pid as libc::pid_t, 0) };
        if result == 0 {
            return Some(true);
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(code) if code == libc::ESRCH => Some(false),
            Some(code) if code == libc::EPERM => Some(true),
            _ => None,
        }
    }

    #[cfg(not(unix))]
    fn is_alive(&self, _pid: u32) -> Option<bool> {
        // There is no /proc or os.kill(pid, 0) on this host: the honest
        // answer is unknown, never a fabricated stopped verdict.
        None
    }
}

/// Paths and identity facts for the read-only Linux pid-file runtime.
pub struct LinuxPidFileConfig {
    pub pid_file: PathBuf,
    pub state_dir: PathBuf,
    pub proc_root: PathBuf,
    pub expected_entry: String,
    pub entry_kind: EntryKind,
    pub expected_port: u16,
    pub expected_uid: Option<u32>,
}

/// Read-only Linux pid-file facts for the UM share entry.
pub struct LinuxPidFileFacts {
    pid_file: PathBuf,
    state_dir: PathBuf,
    proc_root: PathBuf,
    expected_entry: String,
    entry_kind: EntryKind,
    expected_port: u16,
    expected_uid: Option<u32>,
    probe: Arc<dyn ProcessProbe>,
}

impl LinuxPidFileFacts {
    pub fn from_config(config: &ConsoleConfig) -> Self {
        Self::new(
            LinuxPidFileConfig {
                pid_file: config.gateway_pid_file.clone(),
                state_dir: config.gateway_state_dir.clone(),
                proc_root: config.proc_root.clone(),
                expected_entry: config.gateway_entry.clone(),
                entry_kind: config.gateway_entry_kind,
                expected_port: config.gateway_port,
                expected_uid: config.expected_uid,
            },
            Arc::new(SystemProcessProbe),
        )
    }

    pub fn new(config: LinuxPidFileConfig, probe: Arc<dyn ProcessProbe>) -> Self {
        Self {
            pid_file: config.pid_file,
            state_dir: config.state_dir,
            proc_root: config.proc_root,
            expected_entry: config.expected_entry,
            entry_kind: config.entry_kind,
            expected_port: config.expected_port,
            expected_uid: config.expected_uid,
            probe,
        }
    }

    fn read_pid(&self) -> Option<u32> {
        let raw = fs::read_to_string(&self.pid_file).ok()?;
        let raw = raw.trim();
        if raw.is_empty() || !raw.bytes().all(|byte| byte.is_ascii_digit()) {
            return None;
        }
        let pid = raw.parse::<u32>().ok()?;
        (pid > 0).then_some(pid)
    }

    fn cmdline(&self, pid: u32) -> Option<Vec<String>> {
        let raw = fs::read(self.proc_root.join(pid.to_string()).join("cmdline")).ok()?;
        Some(
            String::from_utf8_lossy(&raw)
                .split('\0')
                .filter(|part| !part.is_empty())
                .map(str::to_string)
                .collect(),
        )
    }

    fn uid(&self, pid: u32) -> Option<u32> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            return fs::metadata(self.proc_root.join(pid.to_string()))
                .ok()
                .map(|metadata| metadata.uid());
        }
        #[cfg(not(unix))]
        {
            let _ = pid;
            None
        }
    }

    fn identity_error(&self, pid: u32) -> Result<(), &'static str> {
        let Some(cmdline) = self.cmdline(pid) else {
            return Err("process command line lacks the configured entry");
        };
        if cmdline.is_empty() {
            return Err("process command line lacks the configured entry");
        }
        let interpreter = base_name(&cmdline[0]).starts_with("python");
        if self.entry_kind == EntryKind::Python && !interpreter {
            return Err("configured command is not a python entry");
        }
        if self.entry_kind == EntryKind::Native && interpreter {
            return Err("configured command is an interpreter, not a native entry");
        }
        let expected = base_name(&self.expected_entry);
        let actual = match self.entry_kind {
            EntryKind::Native => cmdline.first().map(|entry| base_name(entry)),
            EntryKind::Python => entry_script(&cmdline).map(|entry| base_name(&entry)),
        };
        if actual.as_deref() != Some(expected.as_str()) {
            return Err("process command line lacks the share entry");
        }
        let port = self.expected_port.to_string();
        let paired = cmdline
            .windows(2)
            .any(|pair| pair[0] == "--port" && pair[1] == port);
        if !paired {
            return Err("process command line lacks the share port");
        }
        if let Some(expected_uid) = self.expected_uid {
            if self.uid(pid) != Some(expected_uid) {
                return Err("process uid unknown or different (never signalled)");
            }
        }
        Ok(())
    }

    pub fn status(&self) -> RuntimeSnapshot {
        let Some(pid) = self.read_pid() else {
            return RuntimeSnapshot::stopped("pid file absent");
        };
        match self.probe.is_alive(pid) {
            None => RuntimeSnapshot::unknown("process liveness undecidable", vec![pid]),
            Some(false) => RuntimeSnapshot::stopped("process gone"),
            Some(true) => match self.identity_error(pid) {
                Ok(()) => RuntimeSnapshot::running(vec![pid]),
                Err(detail) => RuntimeSnapshot::conflict(detail, vec![pid]),
            },
        }
    }

    /// Read-only intent fact: `running`/`stopped`, or `None` when absent,
    /// unreadable or malformed. The cloud services read face does not expose it.
    pub fn read_intent(&self) -> Option<&'static str> {
        let path = self.state_dir.join(HOST_INTENT_FILE);
        let record: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(path).ok()?).ok()?;
        let object = record.as_object()?;
        if object
            .get("schema_version")
            .and_then(serde_json::Value::as_i64)
            != Some(HOST_INTENT_SCHEMA)
        {
            return None;
        }
        match object.get("state").and_then(serde_json::Value::as_str) {
            Some("running") => Some("running"),
            Some("stopped") => Some("stopped"),
            _ => None,
        }
    }
}

impl RuntimeFacts for LinuxPidFileFacts {
    fn snapshot(&self) -> Result<RuntimeSnapshot, RuntimeError> {
        Ok(self.status())
    }
}

fn base_name(value: &str) -> String {
    Path::new(value)
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| value.to_string())
}

/// `share/broker_ops.py::linux_entry_script`.
fn entry_script(argv: &[String]) -> Option<String> {
    let mut index = 1;
    while index < argv.len() {
        let token = &argv[index];
        if token == "--" {
            index += 1;
            break;
        }
        if token == "-" {
            return None;
        }
        if !token.starts_with('-') {
            return Some(token.clone());
        }
        if !token.starts_with("--") {
            let flags = &token[1..];
            if flags.contains('c') || flags.contains('m') {
                return None;
            }
            if flags
                .chars()
                .last()
                .is_some_and(|last| matches!(last, 'W' | 'X' | 'Q'))
            {
                index += 1;
            }
        }
        index += 1;
    }
    argv.get(index).cloned()
}

/// Local (Windows) runtime facts: the ONE service the local role owns is the
/// chat2api UM runtime on its loopback port. The read is a bounded TCP
/// connect; a refused connection is `stopped`, a timeout/unexpected error is
/// `unknown` (never folded into a fabricated stopped verdict). No process is
/// started, signalled or inspected.
pub struct LocalTcpFacts {
    port: u16,
    timeout: std::time::Duration,
}

impl LocalTcpFacts {
    pub fn new(port: u16) -> Self {
        Self {
            port,
            timeout: std::time::Duration::from_millis(250),
        }
    }
}

impl RuntimeFacts for LocalTcpFacts {
    fn snapshot(&self) -> Result<RuntimeSnapshot, RuntimeError> {
        let address = std::net::SocketAddr::from(([127, 0, 0, 1], self.port));
        match std::net::TcpStream::connect_timeout(&address, self.timeout) {
            Ok(_) => Ok(RuntimeSnapshot::running(Vec::new())),
            Err(error) if error.kind() == std::io::ErrorKind::ConnectionRefused => Ok(
                RuntimeSnapshot::stopped("loopback 端口未监听（实例可能未就绪）"),
            ),
            Err(error) if error.kind() == std::io::ErrorKind::TimedOut => Ok(
                RuntimeSnapshot::unknown("TCP 连接超时（探测未定，非未监听）", Vec::new()),
            ),
            Err(_) => Ok(RuntimeSnapshot::stopped(
                "loopback 端口未监听（实例可能未就绪）",
            )),
        }
    }
}

/// One test-injected runtime fact source.
pub struct StaticRuntimeFacts {
    snapshot: RuntimeSnapshot,
}

impl StaticRuntimeFacts {
    pub fn new(snapshot: RuntimeSnapshot) -> Self {
        Self { snapshot }
    }
}

impl RuntimeFacts for StaticRuntimeFacts {
    fn snapshot(&self) -> Result<RuntimeSnapshot, RuntimeError> {
        Ok(self.snapshot.clone())
    }
}

/// A runtime fact source that fails every read (the Python 502 path).
pub struct FailingRuntimeFacts;

impl RuntimeFacts for FailingRuntimeFacts {
    fn snapshot(&self) -> Result<RuntimeSnapshot, RuntimeError> {
        Err(RuntimeError)
    }
}
