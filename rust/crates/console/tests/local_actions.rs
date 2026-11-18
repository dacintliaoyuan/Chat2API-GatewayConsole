//! #87: the local 8001 service/account actions run the retained Python
//! handlers; #94 model-sync actions run in Rust. These tests use a fake
//! `console` package under a temp repo root, so no test touches the real UM
//! runtime, the model-sync state or the network.

mod support;

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::config::Role;
use um_console::ConsoleServer;
use um_test_harness::{HttpRequest, HttpResponse, TempDataDir, TestServer};

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

const UM_ADAPTER_MODULE: &str = r#"
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _record(payload):
    with open(os.path.join(ROOT, "account_calls.txt"), "a",
              encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def um_auth_status(target, params, ctx=None):
    _record({"action": "account.um_status", "target": target,
             "params": params})
    return {"state": "valid", "source": "memory-state",
            "verified": False, "state_known": True}


def um_verify_account(target, params, ctx=None):
    _record({"action": "account.um_verify", "target": target,
             "params": params})
    return {"verify": "valid", "source": "memory-state"}
"#;

const UM_LOGIN_MODULE: &str = r#"
import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Error(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _record(payload):
    with open(os.path.join(ROOT, "login_calls.txt"), "a",
              encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def relogin_um(target, params, ctx=None):
    _record({"action": "account.login", "target": target,
             "params": params})
    if params.get("visible") and not params.get("force"):
        raise _Error(
            "invalid_params",
            "visible=true 只能与 force=true 一起使用；可见窗口只用于显式强制重新登录")
    if params.get("start_wait_s") == 120:
        with open(os.path.join(ROOT, "login_started.txt"), "a",
                  encoding="utf-8") as handle:
            handle.write("started\n")
        time.sleep(30)
    return {"status": "valid", "force": bool(params.get("force")),
            "visible": bool(params.get("visible"))}
"#;

fn fake_repo(temp: &TempDataDir) -> PathBuf {
    let root = temp.path().join("repo");
    let console = root.join("console");
    std::fs::create_dir_all(&console).expect("fake console package");
    std::fs::write(console.join("__init__.py"), "").expect("package marker");
    std::fs::write(console.join("adapters.py"), ADAPTERS_MODULE).expect("fake adapters");
    std::fs::write(console.join("um_adapter.py"), UM_ADAPTER_MODULE).expect("fake um adapter");
    std::fs::write(console.join("um_login.py"), UM_LOGIN_MODULE).expect("fake um login");
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
fn submitting_account_um_status_runs_the_retained_handler() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let created = submit(&console, "account.um_status", "chat2api", json!({}));
    assert_eq!(created.status, 201);
    let operation = wait_terminal(&console, &created.json()["operation"]);

    assert_eq!(operation["state"], json!("succeeded"));
    assert_eq!(operation["result"]["state"], json!("valid"));
    assert_eq!(operation["result"]["source"], json!("memory-state"));
    let calls =
        std::fs::read_to_string(repo.join("account_calls.txt")).expect("account call record");
    let call: Value = serde_json::from_str(calls.trim()).expect("call json");
    assert_eq!(call["action"], json!("account.um_status"));
    assert_eq!(call["target"], json!("chat2api"));
    assert_eq!(call["params"], json!({}));
}

#[test]
fn account_login_rejects_visible_without_force_and_runs_forced_visible_login() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let invalid = submit(
        &console,
        "account.login",
        "chat2api",
        json!({"visible": true, "force": false}),
    );
    assert_eq!(invalid.status, 201);
    let operation = wait_terminal(&console, &invalid.json()["operation"]);
    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["result"], Value::Null);
    assert_eq!(operation["error"]["code"], json!("invalid_params"));
    assert!(operation["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("visible=true"));

    let forced = submit(
        &console,
        "account.login",
        "chat2api",
        json!({"visible": true, "force": true}),
    );
    assert_eq!(forced.status, 201);
    let operation = wait_terminal(&console, &forced.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"));
    assert_eq!(operation["result"]["status"], json!("valid"));
    assert_eq!(operation["result"]["force"], json!(true));
    assert_eq!(operation["result"]["visible"], json!(true));

    let calls = std::fs::read_to_string(repo.join("login_calls.txt")).expect("login call record");
    let calls: Vec<Value> = calls
        .lines()
        .map(|line| serde_json::from_str(line).expect("call json"))
        .collect();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[1]["params"], json!({"visible": true, "force": true}));
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

fn write_model_state(repo: &Path) {
    let path = repo.join("data").join("model-sync-state.json");
    std::fs::create_dir_all(path.parent().expect("data dir")).expect("data dir");
    std::fs::write(
        &path,
        serde_json::to_string_pretty(&json!({
            "version": 4,
            "catalogCounts": {},
            "contextProbes": {
                "um|fake-model": {
                    "source": "um", "model_id": "fake-model",
                    "declared_context": 128000, "status": "pending",
                    "observed_floor": null, "attempts": 0,
                    "history": []
                }
            },
            "contextOverrides": {}
        }))
        .expect("state json")
            + "
",
    )
    .expect("write state");
}

fn fake_gateway() -> TestServer {
    TestServer::start(|request: HttpRequest| {
        if request.method != "POST" {
            return HttpResponse::json(200, &json!({"data": [{"id": "fake-model"}]}).to_string());
        }
        let body: Value = serde_json::from_slice(&request.body).expect("request body");
        let content = body["messages"][0]["content"].as_str().unwrap_or_default();
        let marker = content
            .split("[ctxprobe ")
            .nth(1)
            .and_then(|rest| rest.split(']').next())
            .unwrap_or("run001");
        HttpResponse::json(
            200,
            &json!({"choices": [{"message": {
                "content": format!("[ctxprobe {marker}] FINAL anchor — quote this line verbatim")}}]})
            .to_string(),
        )
    })
    .expect("fake gateway")
}

#[test]
fn model_actions_run_without_a_python_interpreter() {
    let temp = temp();
    let repo = fake_repo(&temp);
    write_model_state(&repo);
    let gateway = fake_gateway();
    let mut config = base_config(&temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = gateway.port();
    config.python_exe = PathBuf::from("definitely-not-python-94");
    config.repo_root = repo.clone();
    let server =
        ConsoleServer::start_with_runtime(config, stopped_runtime()).expect("start local console");
    let console = Console::from_server(server);

    let cases: [(&str, Value); 4] = [
        ("model.catalog", json!({})),
        ("model.context_probe_preview", json!({})),
        (
            "model.context_probe_run",
            json!({"models": {"um": ["fake-model"]}, "max_requests": 1}),
        ),
        ("model.context_overrides", json!({})),
    ];
    for (action, params) in cases {
        let created = submit(&console, action, "model", params);
        assert_eq!(created.status, 201, "{action}");
        let operation = wait_terminal(&console, &created.json()["operation"]);
        assert_eq!(operation["state"], json!("succeeded"), "{action}: {operation}");
    }
    let set = submit(
        &console,
        "model.context_override_set",
        "model",
        json!({"source": "um", "model_id": "fake-model", "context": 200000}),
    );
    assert_eq!(set.status, 201);
    let set = wait_terminal(&console, &set.json()["operation"]);
    assert_eq!(set["state"], json!("succeeded"), "{set}");
    let set_at = set["result"]["set_at"].as_str().expect("set_at").to_string();
    let clear = submit(
        &console,
        "model.context_override_clear",
        "model",
        json!({"source": "um", "model_id": "fake-model", "expected_set_at": set_at}),
    );
    assert_eq!(clear.status, 201);
    let clear = wait_terminal(&console, &clear.json()["operation"]);
    assert_eq!(clear["state"], json!("succeeded"), "{clear}");
    assert!(!repo.join("model_calls.txt").exists());
}

#[test]
fn cancelling_a_running_login_kills_the_sidecar_and_records_cancelled() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let created = submit(
        &console,
        "account.login",
        "chat2api",
        json!({"force": true, "start_wait_s": 120}),
    );
    assert_eq!(created.status, 201);
    let op_id = created.json()["operation"]["id"]
        .as_str()
        .expect("operation id")
        .to_string();
    wait_for_path(&repo.join("login_started.txt"));

    let cancelled = request(
        &console,
        "POST",
        &format!("/api/console/operations/{op_id}/cancel"),
        None,
    );
    assert_eq!(cancelled.status, 200);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("cancelled"));
    assert_eq!(operation["result"], Value::Null);
    assert_eq!(operation["error"]["code"], json!("cancelled"));

    let list = request(&console, "GET", "/api/console/operations?limit=50", None);
    assert_eq!(list.status, 200);
    let operations = list.json()["operations"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    assert!(operations
        .iter()
        .filter(|row| row["action"] == json!("account.login"))
        .all(|row| row["state"] != json!("succeeded")));
}

#[test]
fn service_and_account_actions_still_execute_through_the_sidecar() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let cases: [(&str, &str, Value); 6] = [
        ("service.start", "chat2api", json!({})),
        ("service.stop", "chat2api", json!({})),
        ("service.restart", "chat2api", json!({})),
        ("account.um_status", "chat2api", json!({})),
        ("account.um_verify", "chat2api", json!({})),
        ("account.login", "chat2api", json!({"force": true})),
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

fn wait_for_path(path: &Path) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !path.exists() {
        assert!(
            Instant::now() < deadline,
            "sidecar never started: {}",
            path.display()
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}
