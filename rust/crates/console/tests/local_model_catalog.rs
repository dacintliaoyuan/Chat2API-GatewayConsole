//! #84: the local `model.catalog` action runs the Python `model_sync` sidecar.
//!
//! `model_sync` is not migrated yet. The local role submits `model.catalog` and
//! the Rust console executes it by running `console.model_actions.model_catalog`
//! in a one-shot child process. These tests use a fake `console.model_actions`
//! module under a temp repo root, so no test touches the real model sync state,
//! the UM runtime or the network.

mod support;

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::config::{ConsoleConfig, Role};
use um_console::ConsoleServer;
use um_test_harness::TempDataDir;

use support::*;

const SUCCESS_MODULE: &str = r#"
import atexit
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _record(name):
    with open(os.path.join(ROOT, name), "a", encoding="utf-8") as handle:
        handle.write(str(os.getpid()) + "\n")


_record("runs.txt")
atexit.register(_record, "exits.txt")


def model_catalog(target, params, ctx=None):
    with open(os.path.join(ROOT, "params.txt"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"target": target, "params": params},
                                sort_keys=True) + "\n")
    return {
        "catalogs": {"um": [{"id": "fake-model", "display_name": "Fake"}]},
        "warnings": [],
        "sources": ["um"],
        "checked_epoch": 1700000000,
    }
"#;

const FAILING_MODULE: &str = r#"
def model_catalog(target, params, ctx=None):
    raise RuntimeError("upstream unavailable")
"#;

const INVALID_JSON_MODULE: &str = r#"
def model_catalog(target, params, ctx=None):
    print("not-json")
    return {"catalogs": {}, "warnings": [], "sources": [], "checked_epoch": 1}
"#;

const NONZERO_SUCCESS_MODULE: &str = r#"
import json
import os
import sys


def model_catalog(target, params, ctx=None):
    sys.stdout.write(json.dumps({"ok": True, "result": {"catalogs": {}}}))
    sys.stdout.flush()
    os._exit(7)
"#;

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
    panic!("python interpreter is required for the model.catalog sidecar tests");
}

fn fake_repo(temp: &TempDataDir, module: &str) -> PathBuf {
    let root = temp.path().join("repo");
    let console = root.join("console");
    std::fs::create_dir_all(&console).expect("fake console package");
    std::fs::write(console.join("__init__.py"), "").expect("package marker");
    std::fs::write(console.join("model_actions.py"), module).expect("fake model actions");
    root
}

fn start_local_console(temp: &TempDataDir, repo: &Path) -> Console {
    let mut config = base_config(temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = 8000;
    config.python_exe = python();
    config.repo_root = repo.to_path_buf();
    let server = ConsoleServer::start_with_runtime(config, stopped_runtime())
        .expect("start local console");
    Console::from_server(server)
}

fn request(console: &Console, method: &str, target: &str, body: Option<Value>) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    if let Some(body) = body {
        request = request.json_body(body.to_string());
    }
    client_request(console.addr, &request).expect("request")
}

fn wait_terminal(console: &Console, operation: &Value) -> Value {
    let op_id = operation["id"].as_str().expect("operation id").to_string();
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let fetched = request(
            console,
            "GET",
            &format!("/api/console/operations/{op_id}"),
            None,
        );
        assert_eq!(fetched.status, 200);
        let operation = fetched.json()["operation"].clone();
        if matches!(
            operation["state"].as_str(),
            Some("succeeded" | "failed" | "cancelled" | "interrupted")
        ) {
            return operation;
        }
        assert!(Instant::now() < deadline, "operation never finished: {operation}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn submit_catalog(console: &Console) -> Value {
    let created = request(
        console,
        "POST",
        "/api/console/operations",
        Some(json!({
            "action": "model.catalog",
            "target": "model",
            "params": {},
        })),
    );
    assert_eq!(created.status, 201);
    wait_terminal(console, &created.json()["operation"])
}

#[test]
fn submitting_model_catalog_runs_the_python_sidecar_and_returns_the_catalog() {
    let temp = temp();
    let repo = fake_repo(&temp, SUCCESS_MODULE);
    let console = start_local_console(&temp, &repo);

    let operation = submit_catalog(&console);

    assert_eq!(operation["state"], json!("succeeded"));
    assert_eq!(operation["result"]["sources"], json!(["um"]));
    assert_eq!(operation["result"]["warnings"], json!([]));
    assert_eq!(
        operation["result"]["catalogs"]["um"][0]["id"],
        json!("fake-model")
    );
    let calls = std::fs::read_to_string(repo.join("params.txt")).expect("sidecar call record");
    let call: Value = serde_json::from_str(calls.trim()).expect("call json");
    assert_eq!(call["target"], json!("model"));
    assert_eq!(call["params"], json!({}));
}

#[test]
fn a_failing_sidecar_is_an_explicit_operation_error() {
    let temp = temp();
    let repo = fake_repo(&temp, FAILING_MODULE);
    let console = start_local_console(&temp, &repo);

    let operation = submit_catalog(&console);

    assert_eq!(operation["state"], json!("failed"));
    assert_eq!(operation["result"], Value::Null);
    assert_eq!(operation["error"]["code"], json!("catalog_failed"));
    assert_eq!(
        operation["error"]["message"],
        json!("模型目录查询失败：RuntimeError: upstream unavailable")
    );
}

#[test]
fn invalid_sidecar_json_is_an_explicit_operation_error() {
    let temp = temp();
    let repo = fake_repo(&temp, INVALID_JSON_MODULE);
    let console = start_local_console(&temp, &repo);

    let operation = submit_catalog(&console);

    assert_eq!(operation["state"], json!("failed"));
    assert_eq!(operation["result"], Value::Null);
    assert_eq!(operation["error"]["code"], json!("catalog_failed"));
    let message = operation["error"]["message"]
        .as_str()
        .expect("error message");
    assert!(message.contains("未返回合法 JSON"), "{message}");
}

#[test]
fn a_nonzero_sidecar_exit_never_counts_as_success() {
    let temp = temp();
    let repo = fake_repo(&temp, NONZERO_SUCCESS_MODULE);
    let console = start_local_console(&temp, &repo);

    let operation = submit_catalog(&console);

    assert_eq!(operation["state"], json!("failed"));
    assert_eq!(operation["result"], Value::Null);
    assert_eq!(operation["error"]["code"], json!("catalog_failed"));
    let message = operation["error"]["message"]
        .as_str()
        .expect("error message");
    assert!(message.contains("退出码非零"), "{message}");
}

#[test]
fn the_local_console_config_defaults_to_the_python_on_path() {
    let config = ConsoleConfig::default();
    assert_eq!(config.python_exe, PathBuf::from("python"));
    assert_eq!(config.repo_root, PathBuf::from("."));
}
