//! #87: the local 8001 action face runs the retained Python handlers.
//!
//! The local role lists the same twelve actions as Python's
//! `console.adapters.build_registry()` and executes each one in a one-shot Python
//! sidecar. These tests use a fake `console` package under a temp repo root, so no
//! test touches the real UM runtime, the model-sync state or the network.

mod support;

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::config::Role;
use um_console::ConsoleServer;
use um_test_harness::TempDataDir;

use support::*;

const ACTIONS: [&str; 12] = [
    "service.start",
    "service.stop",
    "service.restart",
    "account.um_status",
    "account.um_verify",
    "account.login",
    "model.context_probe_preview",
    "model.context_probe_run",
    "model.context_overrides",
    "model.context_override_set",
    "model.context_override_clear",
    "model.catalog",
];

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
    panic!("python interpreter is required for the local action sidecar tests");
}

const ADAPTERS_MODULE: &str = r#"
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _record(name, payload):
    with open(os.path.join(ROOT, name), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


class RuntimeServiceAdapter:
    def service_start(self, target, params, ctx=None):
        _record("service_calls.txt", {"action": "service.start",
                                     "target": target, "params": params})
        return {"key": target, "status": "Started", "action": "start"}

    def service_stop(self, target, params, ctx=None):
        _record("service_calls.txt", {"action": "service.stop",
                                     "target": target, "params": params})
        return {"key": target, "status": "Stopped", "action": "stop"}

    def service_restart(self, target, params, ctx=None):
        _record("service_calls.txt", {"action": "service.restart",
                                     "target": target, "params": params})
        return {"key": target, "status": "Restarted", "action": "restart"}
"#;

const MODEL_ACTIONS_MODULE: &str = r#"
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _record(action, target, params):
    with open(os.path.join(ROOT, "model_calls.txt"), "a",
              encoding="utf-8") as handle:
        handle.write(json.dumps({"action": action, "target": target,
                                 "params": params}, sort_keys=True) + "\n")


def model_catalog(target, params, ctx=None):
    _record("model.catalog", target, params)
    return {"catalogs": {"um": [{"id": "fake-model"}]},
            "warnings": [], "sources": ["um"], "checked_epoch": 1}


def model_context_probe_preview(target, params, ctx=None):
    _record("model.context_probe_preview", target, params)
    return {"queue": [{"source": "um", "model_id": "fake-model"}],
            "totals": {"records": 1},
            "label": "上下文探测预览（只读）",
            "note": "队列+计划+估算，零请求零写入"}


def model_context_probe_run(target, params, ctx=None):
    _record("model.context_probe_run", target, params)
    return {"requests": 0, "results": []}


def model_context_overrides(target, params, ctx=None):
    _record("model.context_overrides", target, params)
    return {"overrides": {}, "models": {}}


class _Error(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def model_context_override_set(target, params, ctx=None):
    _record("model.context_override_set", target, params)
    if params.get("model_id") == "fail":
        raise _Error("invalid_params", "模型标识无效；请重新选择后再保存")
    return {"ok": True, "source": params.get("source"),
            "model_id": params.get("model_id")}


def model_context_override_clear(target, params, ctx=None):
    _record("model.context_override_clear", target, params)
    return {"ok": True, "existed": False}
"#;

fn fake_repo(temp: &TempDataDir) -> PathBuf {
    let root = temp.path().join("repo");
    let console = root.join("console");
    std::fs::create_dir_all(&console).expect("fake console package");
    std::fs::write(console.join("__init__.py"), "").expect("package marker");
    std::fs::write(console.join("adapters.py"), ADAPTERS_MODULE).expect("fake adapters");
    std::fs::write(console.join("model_actions.py"), MODEL_ACTIONS_MODULE)
        .expect("fake model actions");
    root
}

fn start_local_console(temp: &TempDataDir, repo: &Path) -> Console {
    let mut config = base_config(temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = 8000;
    config.python_exe = python();
    config.repo_root = repo.to_path_buf();
    config.console_lock_dir = Some(temp.path().join("console-locks"));
    let server =
        ConsoleServer::start_with_runtime(config, stopped_runtime()).expect("start local console");
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
        assert!(
            Instant::now() < deadline,
            "operation never finished: {operation}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn submit(console: &Console, action: &str, target: &str, params: Value) -> ClientResponse {
    request(
        console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": action, "target": target, "params": params})),
    )
}

#[test]
fn local_action_catalogue_lists_all_twelve_enabled_actions() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let response = request(&console, "GET", "/api/console/actions", None);
    assert_eq!(response.status, 200);
    let actions = response.json()["actions"]
        .as_array()
        .expect("actions")
        .clone();
    let names: Vec<&str> = actions
        .iter()
        .filter_map(|row| row["action"].as_str())
        .collect();
    assert_eq!(names, ACTIONS);
    for row in &actions {
        assert_eq!(row["enabled"], json!(true), "{row}");
        assert_eq!(row["disabled_reason"], json!(null), "{row}");
        assert_eq!(row["cancelable"], json!(true), "{row}");
    }
    let login = actions
        .iter()
        .find(|row| row["action"] == json!("account.login"))
        .expect("account.login listed");
    assert_eq!(
        login["param_schema"],
        json!({
            "start_wait_s": {"type": "int", "min": 1, "max": 120},
            "login_budget_s": {"type": "int", "min": 1, "max": 3600},
            "poll_interval_s": {"type": "int", "min": 1, "max": 10},
            "force": {"type": "bool"},
            "visible": {"type": "bool"},
        })
    );
    let probe_run = actions
        .iter()
        .find(|row| row["action"] == json!("model.context_probe_run"))
        .expect("model.context_probe_run listed");
    assert_eq!(
        probe_run["param_schema"],
        json!({
            "max_requests": {"type": "int", "min": 1, "max": 20},
            "max_tokens": {"type": "int", "min": 1, "max": 64},
            "sources": {"type": "str", "max_len": 512},
            "models": {"type": "object"},
            "retest": {"type": "bool"},
        })
    );
    let override_set = actions
        .iter()
        .find(|row| row["action"] == json!("model.context_override_set"))
        .expect("model.context_override_set listed");
    assert_eq!(
        override_set["param_schema"],
        json!({
            "source": {"type": "str", "max_len": 128},
            "model_id": {"type": "str", "max_len": 128},
            "context": {"type": "int", "min": 1, "max": 10_000_000},
            "expected_set_at": {"type": "str", "max_len": 64},
        })
    );
}

#[test]
fn service_start_runs_the_retained_adapter_and_refuses_other_targets() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let started = submit(&console, "service.start", "chat2api", json!({}));
    assert_eq!(started.status, 201);
    let operation = wait_terminal(&console, &started.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"));
    assert_eq!(operation["result"]["status"], json!("Started"));
    assert_eq!(operation["result"]["key"], json!("chat2api"));
    let calls =
        std::fs::read_to_string(repo.join("service_calls.txt")).expect("service call record");
    let call: Value = serde_json::from_str(calls.trim()).expect("call json");
    assert_eq!(call["action"], json!("service.start"));
    assert_eq!(call["target"], json!("chat2api"));

    let before = request(&console, "GET", "/api/console/operations?limit=50", None);
    assert_eq!(before.status, 200);
    let before_count = before.json()["operations"]
        .as_array()
        .map(Vec::len)
        .unwrap_or_default();

    let refused = submit(&console, "service.start", "other", json!({}));
    assert_eq!(refused.status, 403);
    let payload = refused.json();
    assert_eq!(payload["error"], json!("action_disabled"));
    assert!(payload["reason"]
        .as_str()
        .unwrap_or_default()
        .contains("目标不在部署授权包批准的授权集合内"));

    let after = request(&console, "GET", "/api/console/operations?limit=50", None);
    assert_eq!(after.status, 200);
    let after_count = after.json()["operations"]
        .as_array()
        .map(Vec::len)
        .unwrap_or_default();
    assert_eq!(
        before_count, after_count,
        "refused target must not queue a row"
    );
}

#[test]
fn model_context_probe_preview_returns_the_retained_queue() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let created = submit(&console, "model.context_probe_preview", "model", json!({}));
    assert_eq!(created.status, 201);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"));
    assert_eq!(operation["result"]["queue"][0]["source"], json!("um"));
    assert_eq!(
        operation["result"]["queue"][0]["model_id"],
        json!("fake-model")
    );
}

#[test]
fn every_retained_local_action_executes_through_the_sidecar() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let cases: [(&str, &str, Value); 9] = [
        ("service.start", "chat2api", json!({})),
        ("service.stop", "chat2api", json!({})),
        ("service.restart", "chat2api", json!({})),
        ("model.context_probe_preview", "model", json!({})),
        ("model.context_probe_run", "model", json!({})),
        ("model.context_overrides", "model", json!({})),
        (
            "model.context_override_set",
            "model",
            json!({"source": "um", "model_id": "fake-model", "context": 32000}),
        ),
        (
            "model.context_override_clear",
            "model",
            json!({"source": "um", "model_id": "fake-model"}),
        ),
        ("model.catalog", "model", json!({})),
    ];
    for (action, target, params) in cases {
        let created = submit(&console, action, target, params);
        assert_eq!(created.status, 201, "{action}");
        let operation = wait_terminal(&console, &created.json()["operation"]);
        assert_eq!(
            operation["state"],
            json!("succeeded"),
            "{action}: {operation}"
        );
    }
}

/// #95: Windows Python defaults stdio to the ANSI code page (cp936 on a
/// Chinese host), but the sidecar envelope is UTF-8 JSON and the Rust parent
/// always decodes it as UTF-8. Re-exec this test with `PYTHONIOENCODING=cp936`
/// so the sidecar child would inherit a non-UTF-8 stdout on any host; the
/// production sidecar must pin UTF-8 stdio and keep the Chinese `label`, `note`
/// and error text intact instead of U+FFFD.
#[test]
fn sidecar_keeps_chinese_text_intact_under_a_cp936_default_stdout() {
    const CHILD: &str = "UM_SIDECAR_CP936_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let output = Command::new(std::env::current_exe().expect("test executable"))
            .args([
                "--exact",
                "sidecar_keeps_chinese_text_intact_under_a_cp936_default_stdout",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .env("PYTHONIOENCODING", "cp936")
            .env_remove("PYTHONUTF8")
            .output()
            .expect("run the nested sidecar encoding test");
        assert!(
            output.status.success(),
            "nested sidecar encoding test failed\nstdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }

    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let created = submit(&console, "model.context_probe_preview", "model", json!({}));
    assert_eq!(created.status, 201);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        operation["result"]["label"],
        json!("上下文探测预览（只读）"),
        "{operation}"
    );
    assert_eq!(
        operation["result"]["note"],
        json!("队列+计划+估算，零请求零写入"),
        "{operation}"
    );
    for field in ["label", "note"] {
        let text = operation["result"][field].as_str().unwrap_or_default();
        assert!(!text.contains('\u{FFFD}'), "{field}: {operation}");
    }

    let invalid = submit(
        &console,
        "model.context_override_set",
        "model",
        json!({"source": "um", "model_id": "fail", "context": 32000}),
    );
    assert_eq!(invalid.status, 201);
    let operation = wait_terminal(&console, &invalid.json()["operation"]);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(
        operation["error"]["code"],
        json!("invalid_params"),
        "{operation}"
    );
    assert_eq!(
        operation["error"]["message"],
        json!("模型标识无效；请重新选择后再保存"),
        "{operation}"
    );
    assert!(
        !operation["error"]["message"]
            .as_str()
            .unwrap_or_default()
            .contains('\u{FFFD}'),
        "{operation}"
    );
}
