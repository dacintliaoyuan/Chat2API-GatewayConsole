//! Linux pid-file runtime facts: state mapping and read-only identity checks.
//!
//! These tests use a fake `/proc` tree and an injected liveness probe; they
//! never read the real `/proc`, never signal a process and never write the
//! pid file, state dir or projection.

mod support;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use um_console::config::EntryKind;
use um_console::runtime::{LinuxPidFileFacts, ProcessProbe};

use support::*;

struct FakeProbe {
    alive: Option<bool>,
    calls: AtomicUsize,
}

impl ProcessProbe for FakeProbe {
    fn is_alive(&self, _pid: u32) -> Option<bool> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        self.alive
    }
}

fn facts(
    temp: &um_test_harness::TempDataDir,
    alive: Option<bool>,
) -> (LinuxPidFileFacts, Arc<FakeProbe>) {
    let pid_file = temp.path().join("share-gateway.pid");
    std::fs::write(&pid_file, "4242").expect("write pid file");
    let proc_root = temp.path().join("proc");
    write_proc_cmdline(
        &proc_root,
        4242,
        &[
            "/opt/um-share/venv/bin/python",
            "/opt/um-share/chat2api.py",
            "--port",
            "8003",
        ],
    );
    let probe = Arc::new(FakeProbe {
        alive,
        calls: AtomicUsize::new(0),
    });
    let facts = LinuxPidFileFacts::new(
        pid_file,
        temp.path().join("run"),
        proc_root,
        "chat2api.py",
        EntryKind::Python,
        8003,
        None,
        probe.clone(),
    );
    (facts, probe)
}

#[test]
fn matching_identity_is_running() {
    let temp = temp();
    let (facts, probe) = facts(&temp, Some(true));
    let snapshot = facts.status();
    assert_eq!(snapshot.state, "running");
    assert_eq!(snapshot.display_state(), "Running");
    assert!(snapshot.healthy);
    assert_eq!(snapshot.detail, "ok");
    assert_eq!(snapshot.process_ids, vec![4242]);
    assert_eq!(probe.calls.load(Ordering::SeqCst), 1);
}

#[test]
fn absent_pid_file_is_stopped_without_a_liveness_probe() {
    let temp = temp();
    let probe = Arc::new(FakeProbe {
        alive: Some(true),
        calls: AtomicUsize::new(0),
    });
    let facts = LinuxPidFileFacts::new(
        temp.path().join("missing.pid"),
        temp.path().join("run"),
        temp.path().join("proc"),
        "chat2api.py",
        EntryKind::Python,
        8003,
        None,
        probe.clone(),
    );
    let snapshot = facts.status();
    assert_eq!(snapshot.state, "stopped");
    assert_eq!(snapshot.detail, "pid file absent");
    assert!(snapshot.process_ids.is_empty());
    assert_eq!(probe.calls.load(Ordering::SeqCst), 0);
}

#[test]
fn gone_process_is_stopped() {
    let temp = temp();
    let (facts, _) = facts(&temp, Some(false));
    let snapshot = facts.status();
    assert_eq!(snapshot.state, "stopped");
    assert_eq!(snapshot.detail, "process gone");
    assert!(snapshot.process_ids.is_empty());
}

#[test]
fn undecidable_liveness_is_unknown_not_stopped() {
    let temp = temp();
    let (facts, _) = facts(&temp, None);
    let snapshot = facts.status();
    assert_eq!(snapshot.state, "unknown");
    assert_eq!(snapshot.detail, "process liveness undecidable");
    assert_eq!(snapshot.process_ids, vec![4242]);
    assert!(!snapshot.healthy);
}

#[test]
fn wrong_port_is_conflict() {
    let temp = temp();
    let pid_file = temp.path().join("share-gateway.pid");
    std::fs::write(&pid_file, "4242").expect("write pid file");
    let proc_root = temp.path().join("proc");
    write_proc_cmdline(
        &proc_root,
        4242,
        &[
            "/opt/um-share/venv/bin/python",
            "/opt/um-share/chat2api.py",
            "--port",
            "8100",
        ],
    );
    let facts = LinuxPidFileFacts::new(
        pid_file,
        temp.path().join("run"),
        proc_root,
        "chat2api.py",
        EntryKind::Python,
        8003,
        None,
        Arc::new(FakeProbe {
            alive: Some(true),
            calls: AtomicUsize::new(0),
        }),
    );
    let snapshot = facts.status();
    assert_eq!(snapshot.state, "conflict");
    assert_eq!(snapshot.display_state(), "Conflict");
    assert_eq!(snapshot.detail, "process command line lacks the share port");
    assert_eq!(snapshot.process_ids, vec![4242]);
}

#[test]
fn wrong_entry_is_conflict() {
    let temp = temp();
    let pid_file = temp.path().join("share-gateway.pid");
    std::fs::write(&pid_file, "4242").expect("write pid file");
    let proc_root = temp.path().join("proc");
    write_proc_cmdline(
        &proc_root,
        4242,
        &[
            "/opt/um-share/venv/bin/python",
            "/opt/um-share/other.py",
            "--port",
            "8003",
        ],
    );
    let facts = LinuxPidFileFacts::new(
        pid_file,
        temp.path().join("run"),
        proc_root,
        "chat2api.py",
        EntryKind::Python,
        8003,
        None,
        Arc::new(FakeProbe {
            alive: Some(true),
            calls: AtomicUsize::new(0),
        }),
    );
    assert_eq!(facts.status().state, "conflict");
    assert_eq!(
        facts.status().detail,
        "process command line lacks the share entry"
    );
}

#[test]
fn native_entry_requires_the_binary_not_an_interpreter() {
    let temp = temp();
    let pid_file = temp.path().join("share-gateway.pid");
    std::fs::write(&pid_file, "4242").expect("write pid file");
    let proc_root = temp.path().join("proc");
    write_proc_cmdline(
        &proc_root,
        4242,
        &["/opt/um-share/um-gateway", "--port", "8003"],
    );
    let native = LinuxPidFileFacts::new(
        pid_file.clone(),
        temp.path().join("run"),
        proc_root.clone(),
        "um-gateway",
        EntryKind::Native,
        8003,
        None,
        Arc::new(FakeProbe {
            alive: Some(true),
            calls: AtomicUsize::new(0),
        }),
    );
    assert_eq!(native.status().state, "running");

    write_proc_cmdline(
        &proc_root,
        4242,
        &[
            "/opt/um-share/venv/bin/python",
            "/opt/um-share/chat2api.py",
            "--port",
            "8003",
        ],
    );
    let interpreter = LinuxPidFileFacts::new(
        pid_file,
        temp.path().join("run"),
        proc_root,
        "um-gateway",
        EntryKind::Native,
        8003,
        None,
        Arc::new(FakeProbe {
            alive: Some(true),
            calls: AtomicUsize::new(0),
        }),
    );
    assert_eq!(interpreter.status().state, "conflict");
    assert_eq!(
        interpreter.status().detail,
        "configured command is an interpreter, not a native entry"
    );
}

#[test]
fn intent_is_read_only_and_absent_is_unknown() {
    let temp = temp();
    let (facts, _) = facts(&temp, Some(true));
    assert_eq!(facts.read_intent(), None);
    std::fs::create_dir_all(temp.path().join("run")).expect("state dir");
    std::fs::write(
        temp.path().join("run").join("share-host-desired.json"),
        r#"{"schema_version":1,"state":"stopped","reason":"manual","at":1}"#,
    )
    .expect("write intent");
    assert_eq!(facts.read_intent(), Some("stopped"));
}
