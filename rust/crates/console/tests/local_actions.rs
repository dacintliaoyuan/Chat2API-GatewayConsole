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
            "totals": {"records": 1}}


def model_context_probe_run(target, params, ctx=None):
    _record("model.context_probe_run", target, params)
    return {"requests": 0, "results": []}


def model_context_overrides(target, params, ctx=None):
    _record("model.context_overrides", target, params)
    return {"overrides": {}, "models": {}}


def model_context_override_set(target, params, ctx=None):
    _record("model.context_override_set", target, params)
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
