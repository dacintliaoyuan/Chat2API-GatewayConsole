//! #96: the console resource lock contract under contention.
//!
//! The lock file is an exclusive handle (`share_mode(0)` on Windows,
//! `LOCK_EX|LOCK_NB` on POSIX). A brief sharing conflict must be waited out
//! within the acquire budget; a contention timeout, a cancel while waiting and a
//! genuine open failure must stay three distinguishable outcomes. These tests
//! drive the action manager, not `try_open_exclusive` alone.

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::lock::{lock_path, ResourceLock};
use um_console::operation_manager::{
    local_registry, ActionExecutor, ActionFailure, OperationContext, OperationManager,
};
use um_test_harness::TempDataDir;

fn manager_with_timeout(
    temp: &TempDataDir,
    executor: Arc<dyn ActionExecutor>,
    acquire_timeout: Duration,
) -> Arc<OperationManager> {
    Arc::new(
        OperationManager::with_timeout(
            temp.path().join("console.sqlite3"),
            temp.path().join("locks"),
            local_registry(),
            executor,
            acquire_timeout,
        )
        .expect("operation manager"),
    )
}

fn wait_state(manager: &OperationManager, op_id: &str, states: &[&str]) -> Value {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let operation = manager
            .get(op_id)
            .expect("read operation")
            .expect("operation exists");
        if states.contains(&operation["state"].as_str().unwrap_or_default()) {
            return operation;
        }
        assert!(
            Instant::now() < deadline,
            "operation {op_id} never reached {states:?}: {operation}"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

struct RecordingExecutor {
    calls: Mutex<Vec<String>>,
}

impl RecordingExecutor {
    fn new() -> Self {
        Self {
            calls: Mutex::new(Vec::new()),
        }
    }
}

impl ActionExecutor for RecordingExecutor {
    fn execute(
        &self,
        action: &str,
        _target: &str,
        _params: &serde_json::Map<String, Value>,
        _ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        self.calls
            .lock()
            .expect("calls lock")
            .push(action.to_string());
        Ok(json!({"action": action}))
    }
}

struct Gate {
    open: Mutex<bool>,
    ready: Condvar,
}

impl Gate {
    fn new() -> Self {
        Self {
            open: Mutex::new(false),
            ready: Condvar::new(),
        }
    }

    fn wait(&self) {
        let mut open = self.open.lock().expect("gate lock");
        while !*open {
            open = self.ready.wait(open).expect("gate wait");
        }
    }

    fn release(&self) {
        *self.open.lock().expect("gate lock") = true;
        self.ready.notify_all();
    }
}

struct GatedExecutor {
    gate: Arc<Gate>,
    active: AtomicUsize,
    max_active: AtomicUsize,
    calls: Mutex<Vec<String>>,
}

impl ActionExecutor for GatedExecutor {
    fn execute(
        &self,
        action: &str,
        _target: &str,
        _params: &serde_json::Map<String, Value>,
        _ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        let active = self.active.fetch_add(1, Ordering::SeqCst) + 1;
        self.max_active.fetch_max(active, Ordering::SeqCst);
        self.calls
            .lock()
            .expect("calls lock")
            .push(action.to_string());
        self.gate.wait();
        self.active.fetch_sub(1, Ordering::SeqCst);
        Ok(json!({"action": action}))
    }
}

fn hold_account_lock(temp: &TempDataDir) -> ResourceLock {
    ResourceLock::acquire(
        &temp.path().join("locks"),
        "account:um",
        "test-holder",
        Duration::from_secs(30),
        &AtomicBool::new(false),
    )
    .expect("hold the account resource lock")
}

#[test]
fn concurrent_actions_on_the_same_resource_stay_mutually_exclusive() {
    let temp = TempDataDir::new("um-console-resource-lock-").expect("temp dir");
    let gate = Arc::new(Gate::new());
    let executor = Arc::new(GatedExecutor {
        gate: Arc::clone(&gate),
        active: AtomicUsize::new(0),
        max_active: AtomicUsize::new(0),
        calls: Mutex::new(Vec::new()),
    });
    let manager = manager_with_timeout(&temp, executor.clone(), Duration::from_secs(10));

    let (_, first) = manager
        .submit("account.um_status", "chat2api", None, None)
        .expect("first submit");
    let first_id = first["id"].as_str().unwrap().to_string();
    let deadline = Instant::now() + Duration::from_secs(10);
    while executor.calls.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline, "first action never started");
        std::thread::sleep(Duration::from_millis(5));
    }

    let (_, second) = manager
        .submit("account.um_verify", "chat2api", None, None)
        .expect("second submit");
    let second_id = second["id"].as_str().unwrap().to_string();

    // The second action shares `account:um`, so it must wait for the lock
    // instead of entering the executor beside the first one.
    std::thread::sleep(Duration::from_millis(250));
    let waiting = manager.get(&second_id).unwrap().unwrap();
    assert_eq!(waiting["state"], "queued", "{waiting}");
    assert_eq!(executor.calls.lock().unwrap().len(), 1);

    gate.release();
    let first = wait_state(&manager, &first_id, &["succeeded"]);
    let second = wait_state(&manager, &second_id, &["succeeded"]);
    assert_eq!(first["state"], "succeeded");
    assert_eq!(second["state"], "succeeded");
    assert_eq!(executor.max_active.load(Ordering::SeqCst), 1);
    assert_eq!(executor.calls.lock().unwrap().len(), 2);
}

#[test]
fn a_held_lock_times_out_with_resource_busy_timeout() {
    let temp = TempDataDir::new("um-console-resource-lock-").expect("temp dir");
    let holder = hold_account_lock(&temp);
    let executor = Arc::new(RecordingExecutor::new());
    let manager = manager_with_timeout(&temp, executor.clone(), Duration::from_millis(300));

    let (_, operation) = manager
        .submit("account.um_verify", "chat2api", None, None)
        .expect("submit");
    let op_id = operation["id"].as_str().unwrap().to_string();
    let failed = wait_state(&manager, &op_id, &["failed"]);
    assert_eq!(failed["error"]["code"], json!("resource_busy_timeout"));
    assert!(
        failed["error"]["message"]
            .as_str()
            .unwrap_or_default()
            .contains("0.3s"),
        "{failed}"
    );
    assert!(
        executor.calls.lock().unwrap().is_empty(),
        "a timed-out lock wait must never execute the action"
    );

    drop(holder);
    std::thread::sleep(Duration::from_millis(100));
    assert!(executor.calls.lock().unwrap().is_empty());
}

#[test]
fn cancelling_a_waiting_action_is_distinct_from_a_timeout() {
    let temp = TempDataDir::new("um-console-resource-lock-").expect("temp dir");
    let holder = hold_account_lock(&temp);
    let executor = Arc::new(RecordingExecutor::new());
    let manager = manager_with_timeout(&temp, executor.clone(), Duration::from_secs(10));

    let (_, operation) = manager
        .submit("account.um_verify", "chat2api", None, None)
        .expect("submit");
    let op_id = operation["id"].as_str().unwrap().to_string();
    std::thread::sleep(Duration::from_millis(250));
    let waiting = manager.get(&op_id).unwrap().unwrap();
    assert_eq!(waiting["state"], "queued", "{waiting}");

    let cancelled = manager.cancel(&op_id).expect("cancel while waiting");
    assert_eq!(cancelled["state"], "cancelled");
    let cancelled = wait_state(&manager, &op_id, &["cancelled"]);
    assert_eq!(cancelled["error"]["code"], json!("cancelled"));
    assert!(executor.calls.lock().unwrap().is_empty());

    drop(holder);
}

#[test]
fn a_genuine_open_failure_fails_fast_and_is_not_contention() {
    let temp = TempDataDir::new("um-console-resource-lock-").expect("temp dir");
    let lock_dir = temp.path().join("locks");
    std::fs::create_dir_all(&lock_dir).expect("lock dir");
    // A directory at the lock file path makes the exclusive open fail for a
    // real path/permission reason, never with a sharing violation.
    std::fs::create_dir_all(lock_path(&lock_dir, "account:um")).expect("blocking directory");

    let executor = Arc::new(RecordingExecutor::new());
    let manager = manager_with_timeout(&temp, executor.clone(), Duration::from_secs(10));
    let started = Instant::now();
    let (_, operation) = manager
        .submit("account.um_verify", "chat2api", None, None)
        .expect("submit");
    let op_id = operation["id"].as_str().unwrap().to_string();
    let failed = wait_state(&manager, &op_id, &["failed"]);
    assert_eq!(
        failed["error"]["code"],
        json!("lock_open_failed"),
        "{failed}"
    );
    assert!(
        started.elapsed() < Duration::from_secs(2),
        "a genuine open failure must return promptly, took {:?}",
        started.elapsed()
    );
    assert!(executor.calls.lock().unwrap().is_empty());
}

fn python() -> PathBuf {
    let candidates: &[&str] = if cfg!(windows) {
        &["python", "python3"]
    } else {
        &["python3", "python"]
    };
    for candidate in candidates {
        let works = Command::new(candidate)
            .arg("--version")
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false);
        if works {
            return PathBuf::from(candidate);
        }
    }
    panic!("python interpreter is required for the cross-process lock test");
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

const PYTHON_LOCK_HOLDER: &str = r#"
import sys

from console.operations import ResourceFileLock

lock = ResourceFileLock(sys.argv[1], sys.argv[2], "python-test-holder",
                        timeout_s=10)
lock.acquire()
sys.stdout.write("locked\n")
sys.stdout.flush()
sys.stdin.readline()
lock.__exit__(None, None, None)
"#;

fn spawn_python_lock_holder(lock_dir: &Path) -> Child {
    let mut child = Command::new(python())
        .arg("-c")
        .arg(PYTHON_LOCK_HOLDER)
        .arg(lock_dir)
        .arg("account:um")
        .current_dir(repo_root())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("spawn python lock holder");
    let mut stdout = BufReader::new(child.stdout.take().expect("child stdout"));
    let mut handshake = String::new();
    stdout
        .read_line(&mut handshake)
        .expect("read holder handshake");
    assert_eq!(
        handshake.trim(),
        "locked",
        "python ResourceFileLock did not acquire"
    );
    child
}

fn wait_child(child: &mut Child, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait().expect("try wait") {
            assert!(status.success(), "python lock holder exited with {status}");
            return;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            panic!("python lock holder did not exit");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

#[test]
fn a_python_resource_file_lock_holder_blocks_and_releases_the_action() {
    let temp = TempDataDir::new("um-console-resource-lock-").expect("temp dir");
    let lock_dir = temp.path().join("locks");
    std::fs::create_dir_all(&lock_dir).expect("lock dir");
    let mut holder = spawn_python_lock_holder(&lock_dir);

    let executor = Arc::new(RecordingExecutor::new());
    let manager = manager_with_timeout(&temp, executor.clone(), Duration::from_secs(10));
    let (_, operation) = manager
        .submit("account.um_verify", "chat2api", None, None)
        .expect("submit");
    let op_id = operation["id"].as_str().unwrap().to_string();

    std::thread::sleep(Duration::from_millis(400));
    let waiting = manager.get(&op_id).unwrap().unwrap();
    assert_eq!(
        waiting["state"], "queued",
        "cross-process holder must keep the action waiting: {waiting}"
    );
    assert!(executor.calls.lock().unwrap().is_empty());

    drop(holder.stdin.take());
    wait_child(&mut holder, Duration::from_secs(10));
    let succeeded = wait_state(&manager, &op_id, &["succeeded"]);
    assert_eq!(succeeded["state"], "succeeded");
    assert_eq!(executor.calls.lock().unwrap().len(), 1);
}
