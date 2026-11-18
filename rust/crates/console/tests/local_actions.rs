//! #87: the local 8001 model-sync actions run in Rust.
//!
//! The local role lists the same twelve actions as Python's
//! `console.adapters.build_registry()`; the service actions (#92) execute through
//! the managed PowerShell runtime, the UM auth actions (#93) run in Rust, and the
//! model-sync actions (#94) run in-process. These tests use a fake `console`
//! package under a temp repo root, so no test touches the real UM runtime, the
//! model-sync state or the network.

mod support;

use std::path::{Path, PathBuf};
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

fn fake_repo(temp: &TempDataDir) -> PathBuf {
    let root = temp.path().join("repo");
    std::fs::create_dir_all(&root).expect("fake repo root");
    root
}

fn start_local_console(temp: &TempDataDir, repo: &Path) -> Console {
    let mut config = base_config(temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = 8000;
    config.python_exe = PathBuf::from("definitely-not-python-94");
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
fn an_unauthorized_service_target_is_refused_before_any_operation_row() {
    let temp = temp();
    let repo = fake_repo(&temp);
    let console = start_local_console(&temp, &repo);

    let refused = submit(&console, "service.start", "other", json!({}));
    assert_eq!(refused.status, 403);
    let payload = refused.json();
    assert_eq!(payload["error"], json!("action_disabled"));
    assert!(payload["reason"]
        .as_str()
        .unwrap_or_default()
        .contains("目标不在部署授权包批准的授权集合内"));

    let list = request(&console, "GET", "/api/console/operations?limit=50", None);
    assert_eq!(list.status, 200);
    assert_eq!(list.json()["operations"], json!([]));
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
            + "\n",
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
