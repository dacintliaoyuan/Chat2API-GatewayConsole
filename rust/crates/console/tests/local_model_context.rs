//! #94: the five `model.context_*` actions run in Rust against the shared
//! `model-sync-state.json`. These tests seed the state file directly and use a
//! fake gateway for catalogue reads; no Python process is started.

mod support;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::config::Role;
use um_console::ConsoleServer;
use um_test_harness::{HttpRequest, HttpResponse, TempDataDir, TestServer};

use support::*;

fn start_local_console(temp: &TempDataDir, gateway_port: u16) -> Console {
    let mut config = base_config(temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = gateway_port;
    config.repo_root = temp.path().to_path_buf();
    config.python_exe = PathBuf::from("definitely-not-python-94");
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

fn submit(console: &Console, action: &str, params: Value) -> Value {
    let created = request(
        console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": action, "target": "model", "params": params})),
    );
    assert_eq!(created.status, 201);
    wait_terminal(console, &created.json()["operation"])
}

fn gateway(models: Value) -> TestServer {
    TestServer::start(move |_request: HttpRequest| {
        HttpResponse::json(200, &json!({"data": models}).to_string())
    })
    .expect("fake gateway")
}

fn state_path(temp: &TempDataDir) -> PathBuf {
    temp.path().join("data").join("model-sync-state.json")
}

fn write_state(temp: &TempDataDir, state: &Value) {
    let path = state_path(temp);
    std::fs::create_dir_all(path.parent().expect("data dir")).expect("create data dir");
    std::fs::write(
        &path,
        serde_json::to_string_pretty(state).expect("state json") + "\n",
    )
    .expect("write state");
}

fn read_state(temp: &TempDataDir) -> Value {
    serde_json::from_str(&std::fs::read_to_string(state_path(temp)).expect("read state"))
        .expect("state json")
}

fn base_state() -> Value {
    json!({
        "version": 4,
        "catalogCounts": {},
        "contextProbes": {
            "um|model-a": {"declared_at": "2026-10-01T00:00:00+00:00"},
            "um|model-b": {"declared_at": "2026-10-02T00:00:00+00:00"},
        },
        "contextOverrides": {},
    })
}

#[test]
fn context_overrides_reports_declared_override_inert_and_malformed_rows() {
    let temp = temp();
    let mut state = base_state();
    state["contextOverrides"] = json!({
        "um|model-a": {"context": 200000, "set_at": "2026-09-30T08:00:00+00:00", "source": "manual"},
        "um|model-b": {"context": 200000, "set_at": "2026-09-30T09:00:00+00:00", "source": "manual"},
        "um|equal": {"context": 128000, "set_at": "2026-09-30T10:00:00+00:00", "source": "manual"},
        "um|ghost": {"context": 64000, "set_at": "2026-09-30T11:00:00+00:00", "source": "manual"},
        "um|bad": "junk"
    });
    write_state(&temp, &state);
    let before = std::fs::read(state_path(&temp)).expect("before bytes");
    let gateway = gateway(json!([
        {"id": "model-a", "max_model_len": 128000},
        {"id": "model-b"},
        {"id": "equal", "max_model_len": 128000},
        {"id": "bad", "max_model_len": 1000}
    ]));
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(&console, "model.context_overrides", json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(std::fs::read(state_path(&temp)).expect("after bytes"), before);
    let rows: std::collections::BTreeMap<String, Value> = operation["result"]["overrides"]
        .as_array()
        .expect("overrides")
        .iter()
        .map(|row| {
            (
                format!(
                    "{}|{}",
                    row["source"].as_str().unwrap_or_default(),
                    row["model_id"].as_str().unwrap_or_default()
                ),
                row.clone(),
            )
        })
        .collect();
    let applied = &rows["um|model-a"];
    assert_eq!(applied["declared_context"], json!(128000));
    assert_eq!(applied["override_context"], json!(200000));
    assert_eq!(applied["effective_context"], json!(200000));
    assert_eq!(applied["effective_source"], json!("override"));
    assert_eq!(applied["override_at"], json!("2026-09-30T08:00:00+00:00"));
    assert_eq!(applied["override_applied"], json!(true));
    assert_eq!(applied["override_state"], json!("applied"));
    assert_eq!(applied["declared_at"], json!("2026-10-01T00:00:00+00:00"));

    let none_declaration = &rows["um|model-b"];
    assert_eq!(none_declaration["declared_context"], Value::Null);
    assert_eq!(none_declaration["effective_source"], json!("override"));
    assert_eq!(none_declaration["effective_context"], json!(200000));
    assert_eq!(none_declaration["override_state"], json!("applied"));

    let equal = &rows["um|equal"];
    assert_eq!(equal["effective_source"], json!("declared"));
    assert_eq!(equal["effective_context"], json!(128000));
    assert_eq!(equal["override_applied"], json!(false));
    assert_eq!(equal["override_state"], json!("equal"));

    let inert = &rows["um|ghost"];
    assert_eq!(inert["override_context"], json!(64000));
    assert_eq!(inert["effective_context"], Value::Null);
    assert_eq!(inert["effective_source"], json!("unknown"));
    assert_eq!(inert["override_applied"], json!(false));
    assert_eq!(inert["override_state"], json!("model_missing"));

    let malformed = &rows["um|bad"];
    assert_eq!(malformed["override_state"], json!("malformed"));
    assert_eq!(malformed["effective_source"], json!("declared"));
    assert_eq!(malformed["effective_context"], json!(1000));
    assert_eq!(
        operation["result"]["note"],
        json!("只读读面：declared=目录声明、override=已保存覆盖、effective=有效值；覆盖保存≠客户端已应用")
    );
}

#[test]
fn context_overrides_local_only_never_reads_the_gateway() {
    let temp = temp();
    let state = base_state();
    write_state(&temp, &state);
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counter = Arc::clone(&calls);
    let gateway = TestServer::start(move |_request: HttpRequest| {
        counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        HttpResponse::json(200, &json!({"data": [{"id": "gateway-model"}]}).to_string())
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(
        &console,
        "model.context_overrides",
        json!({"local_only": true, "models": {"um": ["local-model"]}}),
    );

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(
        calls.load(std::sync::atomic::Ordering::SeqCst),
        0,
        "local_only must not read the gateway"
    );
    let rows = operation["result"]["overrides"]
        .as_array()
        .expect("overrides");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["model_id"], json!("local-model"));
    assert_eq!(rows[0]["declared_context"], Value::Null);
    assert_eq!(rows[0]["override_state"], json!("none"));
}

#[test]
fn context_overrides_rejects_unknown_params_and_models_without_local_only() {
    let temp = temp();
    write_state(&temp, &base_state());
    let gateway = gateway(json!([{"id": "model-a"}]));
    let console = start_local_console(&temp, gateway.port());

    let unknown = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({
            "action": "model.context_overrides",
            "target": "model",
            "params": {"nope": true},
        })),
    );
    assert_eq!(unknown.status, 400);
    assert_eq!(unknown.json()["error"], json!("invalid_params"));

    let models = submit(
        &console,
        "model.context_overrides",
        json!({"models": {"um": ["model-a"]}}),
    );
    assert_eq!(models["state"], json!("failed"), "{models}");
    assert_eq!(models["error"]["code"], json!("invalid_params"));
    assert!(models["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("models 只用于本地设置读取"));
}

#[test]
fn context_override_set_persists_the_override_and_reports_the_new_version() {
    let temp = temp();
    write_state(&temp, &base_state());
    let gateway = gateway(json!([{"id": "model-a", "max_model_len": 128000}]));
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(
        &console,
        "model.context_override_set",
        json!({"source": "um", "model_id": "model-a", "context": 200000}),
    );

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["source"], json!("um"));
    assert_eq!(result["model_id"], json!("model-a"));
    assert_eq!(result["context"], json!(200000));
    let set_at = result["set_at"].as_str().expect("set_at").to_string();
    assert!(result["note"]
        .as_str()
        .unwrap_or_default()
        .contains("覆盖保存≠客户端已应用"));

    let state = read_state(&temp);
    assert_eq!(state["contextOverrides"]["um|model-a"]["context"], json!(200000));
    assert_eq!(state["contextOverrides"]["um|model-a"]["source"], json!("manual"));
    assert_eq!(state["contextOverrides"]["um|model-a"]["set_at"], json!(set_at));

    let backup_root = temp.path().join("data").join("model-sync-backups");
    let backups: Vec<_> = std::fs::read_dir(&backup_root)
        .expect("backup dir")
        .filter_map(Result::ok)
        .collect();
    assert_eq!(backups.len(), 1, "{backups:?}");
    assert!(backups[0]
        .path()
        .join("model-sync-state.json.bak")
        .exists());
}

#[test]
fn context_override_set_never_clobbers_a_concurrent_version() {
    let temp = temp();
    let mut state = base_state();
    state["contextOverrides"] = json!({
        "um|model-a": {"context": 200000, "set_at": "2026-09-30T08:00:00+00:00", "source": "manual"}
    });
    write_state(&temp, &state);
    let before = std::fs::read(state_path(&temp)).expect("before bytes");
    let gateway = gateway(json!([{"id": "model-a", "max_model_len": 128000}]));
    let console = start_local_console(&temp, gateway.port());

    let bare = submit(
        &console,
        "model.context_override_set",
        json!({"source": "um", "model_id": "model-a", "context": 64000}),
    );
    assert_eq!(bare["state"], json!("failed"), "{bare}");
    assert_eq!(bare["error"]["code"], json!("version_conflict"));
    assert_eq!(std::fs::read(state_path(&temp)).expect("bytes"), before);

    let stale = submit(
        &console,
        "model.context_override_set",
        json!({
            "source": "um", "model_id": "model-a", "context": 64000,
            "expected_set_at": "2020-01-01T00:00:00+00:00"
        }),
    );
    assert_eq!(stale["state"], json!("failed"), "{stale}");
    assert_eq!(stale["error"]["code"], json!("version_conflict"));
    assert_eq!(std::fs::read(state_path(&temp)).expect("bytes"), before);

    let updated = submit(
        &console,
        "model.context_override_set",
        json!({
            "source": "um", "model_id": "model-a", "context": 64000,
            "expected_set_at": "2026-09-30T08:00:00+00:00"
        }),
    );
    assert_eq!(updated["state"], json!("succeeded"), "{updated}");
    assert_eq!(updated["result"]["context"], json!(64000));
    assert_ne!(
        updated["result"]["set_at"],
        json!("2026-09-30T08:00:00+00:00")
    );
}

#[test]
fn context_override_set_distinguishes_catalog_unavailable_from_model_absent() {
    let temp = temp();
    write_state(&temp, &base_state());
    let unavailable = TestServer::start(|_request: HttpRequest| {
        HttpResponse::json(500, &json!({"error": "boom"}).to_string())
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, unavailable.port());

    let failed = submit(
        &console,
        "model.context_override_set",
        json!({"source": "um", "model_id": "model-a", "context": 200000}),
    );
    assert_eq!(failed["state"], json!("failed"), "{failed}");
    assert_eq!(failed["error"]["code"], json!("invalid_params"));
    assert!(failed["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("当前目录不可用"));

    drop(console);
    let absent = gateway(json!([{"id": "model-b", "max_model_len": 128000}]));
    let console = start_local_console(&temp, absent.port());
    let failed = submit(
        &console,
        "model.context_override_set",
        json!({"source": "um", "model_id": "model-a", "context": 200000}),
    );
    assert_eq!(failed["state"], json!("failed"), "{failed}");
    assert_eq!(failed["error"]["code"], json!("invalid_params"));
    assert!(failed["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("模型不在当前目录"));
}

#[test]
fn context_override_clear_is_idempotent_and_keeps_probe_history() {
    let temp = temp();
    let mut state = base_state();
    state["contextOverrides"] = json!({
        "um|model-a": {"context": 200000, "set_at": "2026-09-30T08:00:00+00:00", "source": "manual"}
    });
    state["contextProbes"]["um|model-a"] = json!({
        "source": "um", "model_id": "model-a", "declared_context": 128000,
        "declared_at": "t", "status": "verified", "observed_floor": 256000,
        "attempts": 4, "last_run": "t", "last_error": null,
        "history": [{"observed_floor": 64000, "reason": "retest"}]
    });
    write_state(&temp, &state);
    let gateway = gateway(json!([{"id": "model-a", "max_model_len": 128000}]));
    let console = start_local_console(&temp, gateway.port());

    let conflict = submit(
        &console,
        "model.context_override_clear",
        json!({"source": "um", "model_id": "model-a"}),
    );
    assert_eq!(conflict["state"], json!("failed"), "{conflict}");
    assert_eq!(conflict["error"]["code"], json!("version_conflict"));

    let cleared = submit(
        &console,
        "model.context_override_clear",
        json!({
            "source": "um", "model_id": "model-a",
            "expected_set_at": "2026-09-30T08:00:00+00:00"
        }),
    );
    assert_eq!(cleared["state"], json!("succeeded"), "{cleared}");
    assert_eq!(cleared["result"]["existed"], json!(true));
    assert_eq!(cleared["result"]["changed"], json!(true));
    assert!(cleared["result"]["note"]
        .as_str()
        .unwrap_or_default()
        .contains("已清除"));
    let after = read_state(&temp);
    assert!(after["contextOverrides"].get("um|model-a").is_none());
    assert_eq!(
        after["contextProbes"]["um|model-a"]["history"],
        json!([{"observed_floor": 64000, "reason": "retest"}])
    );
    assert_eq!(
        after["contextProbes"]["um|model-a"]["observed_floor"],
        json!(256000)
    );

    let before_again = std::fs::read(state_path(&temp)).expect("before bytes");
    let again = submit(
        &console,
        "model.context_override_clear",
        json!({"source": "um", "model_id": "model-a"}),
    );
    assert_eq!(again["state"], json!("succeeded"), "{again}");
    assert_eq!(again["result"]["cleared"], json!(true));
    assert_eq!(again["result"]["existed"], json!(false));
    assert!(!again["result"]["note"]
        .as_str()
        .unwrap_or_default()
        .contains("已恢复默认"));
    assert!(again["result"]["note"]
        .as_str()
        .unwrap_or_default()
        .contains("不存在"));
    assert_eq!(std::fs::read(state_path(&temp)).expect("bytes"), before_again);
}

fn preview_state() -> Value {
    json!({
        "version": 4,
        "catalogCounts": {},
        "contextProbes": {
            "um|model-a": {
                "source": "um", "model_id": "model-a",
                "declared_context": 128000, "declared_context_known": true,
                "declared_note": null,
                "declared_at": "2026-10-01T00:00:00+00:00",
                "status": "pending", "observed_floor": null,
                "observed_ceiling": null, "attempts": 0,
                "last_run": null, "last_success": null, "last_error": null,
                "evidence_revision": 1, "stale_reason": null,
                "token_evidence": null, "scheme_revision": 1, "history": []
            },
            "um|model-b": {
                "source": "um", "model_id": "model-b",
                "declared_context": null, "status": "verified",
                "observed_floor": 256000, "observed_ceiling": null,
                "attempts": 4, "last_run": "2026-10-02T00:00:00+00:00",
                "last_success": "2026-10-02T00:00:00+00:00",
                "last_error": null, "evidence_revision": 2,
                "stale_reason": null, "token_evidence": 1234,
                "scheme_revision": 1,
                "history": [{
                    "status": "verified", "observed_floor": 64000,
                    "reason": "retest", "archived_at": "2026-10-01T00:00:00+00:00",
                    "junk": "ignored"
                }]
            },
            "um|model-c": {
                "source": "um", "model_id": "model-c",
                "declared_context": 64000, "status": "stale",
                "observed_floor": 64000, "attempts": 2,
                "evidence_revision": 2, "scheme_revision": 1,
                "history": []
            }
        },
        "contextOverrides": {}
    })
}

#[test]
fn context_probe_preview_reports_the_queue_and_totals_without_writing() {
    let temp = temp();
    write_state(&temp, &preview_state());
    let before = std::fs::read(state_path(&temp)).expect("before bytes");
    // Preview is a pure read: no gateway request is needed.
    let console = start_local_console(&temp, 1);

    let operation = submit(&console, "model.context_probe_preview", json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(std::fs::read(state_path(&temp)).expect("bytes"), before);
    let result = &operation["result"];
    assert_eq!(result["steps"], json!([4000, 16000, 64000, 256000]));
    let records: std::collections::BTreeMap<String, Value> = result["records"]
        .as_array()
        .expect("records")
        .iter()
        .map(|row| {
            (
                format!(
                    "{}|{}",
                    row["source"].as_str().unwrap_or_default(),
                    row["model_id"].as_str().unwrap_or_default()
                ),
                row.clone(),
            )
        })
        .collect();
    let pending = &records["um|model-a"];
    assert_eq!(pending["declared_context"], json!(128000));
    assert_eq!(pending["declared_context_known"], json!(true));
    assert_eq!(pending["probe_endpoint"], json!(true));
    assert_eq!(pending["paused"], json!(false));
    assert_eq!(pending["executable"], json!(true));
    assert_eq!(pending["estimated_requests"], json!(4));
    assert_eq!(pending["input_chars_estimate"], json!(340000));
    assert_eq!(pending["input_unit"], json!("chars"));
    assert_eq!(pending["output_unit"], json!("tokens"));
    assert_eq!(pending["output_token_cap"], json!(64));
    assert_eq!(pending["estimate_note"], Value::Null);
    assert_eq!(pending["history_count"], json!(0));

    let verified = &records["um|model-b"];
    assert_eq!(verified["estimated_requests"], json!(0));
    assert_eq!(verified["estimate_note"], Value::Null);
    assert_eq!(verified["history_count"], json!(1));
    assert_eq!(verified["history"][0]["observed_floor"], json!(64000));
    assert_eq!(verified["history"][0]["reason"], json!("retest"));
    assert_eq!(verified["history"][0]["character_unit"], json!("chars"));
    assert_eq!(verified["history"][0]["token_unit"], json!("tokens"));
    assert!(verified["history"][0].get("junk").is_none());

    let stale = &records["um|model-c"];
    assert_eq!(stale["estimated_requests"], json!(4));
    assert_eq!(stale["input_chars_estimate"], json!(340000));
    assert_eq!(stale["estimate_note"], Value::Null);

    assert_eq!(
        result["totals"],
        json!({
            "estimated_requests": 8,
            "estimated_input_chars": 680000,
            "runnable_records": 2,
            "blocked_records": 0,
            "idle_records": 1,
        })
    );
    assert_eq!(result["recovery"], Value::Null);
    assert!(result["budget_note"]
        .as_str()
        .unwrap_or_default()
        .contains("(4000, 16000, 64000, 256000)"));
    assert!(result["budget_note"]
        .as_str()
        .unwrap_or_default()
        .contains("max_tokens ≤ 64"));
    assert!(result["note"]
        .as_str()
        .unwrap_or_default()
        .contains("本预览不发请求"));
}

#[test]
fn context_probe_preview_honours_selection_and_retest() {
    let temp = temp();
    write_state(&temp, &preview_state());
    let console = start_local_console(&temp, 1);

    let selected = submit(
        &console,
        "model.context_probe_preview",
        json!({"models": {"um": ["model-a"]}}),
    );
    assert_eq!(selected["state"], json!("succeeded"), "{selected}");
    let result = &selected["result"];
    let records: std::collections::BTreeMap<String, Value> = result["records"]
        .as_array()
        .expect("records")
        .iter()
        .map(|row| {
            (
                format!(
                    "{}|{}",
                    row["source"].as_str().unwrap_or_default(),
                    row["model_id"].as_str().unwrap_or_default()
                ),
                row.clone(),
            )
        })
        .collect();
    assert_eq!(records["um|model-a"]["estimated_requests"], json!(4));
    assert_eq!(records["um|model-b"]["estimated_requests"], json!(0));
    assert_eq!(
        records["um|model-b"]["estimate_note"],
        json!("本轮未选中（不在请求集合），实际零请求")
    );
    assert_eq!(result["totals"]["estimated_requests"], json!(4));
    assert_eq!(result["totals"]["runnable_records"], json!(1));
    assert_eq!(result["totals"]["idle_records"], json!(2));

    let retested = submit(
        &console,
        "model.context_probe_preview",
        json!({"models": {"um": ["model-b"]}, "retest": true}),
    );
    assert_eq!(retested["state"], json!("succeeded"), "{retested}");
    let records: std::collections::BTreeMap<String, Value> = retested["result"]["records"]
        .as_array()
        .expect("records")
        .iter()
        .map(|row| {
            (
                format!(
                    "{}|{}",
                    row["source"].as_str().unwrap_or_default(),
                    row["model_id"].as_str().unwrap_or_default()
                ),
                row.clone(),
            )
        })
        .collect();
    assert_eq!(records["um|model-b"]["estimated_requests"], json!(4));
    assert_eq!(
        records["um|model-b"]["estimate_note"],
        json!("retest：先归档旧代次再按全梯重测")
    );

    let unknown = submit(
        &console,
        "model.context_probe_preview",
        json!({"models": {"um": ["ghost"]}}),
    );
    assert_eq!(unknown["state"], json!("failed"), "{unknown}");
    assert_eq!(unknown["error"]["code"], json!("invalid_params"));
    assert!(unknown["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("以下模型不在探测队列"));
}

#[test]
fn a_legacy_python_state_is_migrated_and_still_writable() {
    let temp = temp();
    let state = json!({
        "version": 2,
        "pinned": ["um|model-a"],
        "catalogCounts": {},
        "contextProbes": {
            "um|model-a": {
                "source": "um", "model_id": "model-a",
                "declared_context": 128000, "status": "pending",
                "observed_floor": null, "attempts": 0,
                "evidence_revision": 1, "scheme_revision": 1,
                "history": []
            }
        },
        "contextOverrides": {}
    });
    write_state(&temp, &state);
    let gateway = gateway(json!([{"id": "model-a", "max_model_len": 128000}]));
    let console = start_local_console(&temp, gateway.port());

    let preview = submit(&console, "model.context_probe_preview", json!({}));
    assert_eq!(preview["state"], json!("succeeded"), "{preview}");
    assert_eq!(
        preview["result"]["records"][0]["declared_context"],
        json!(128000)
    );

    let set = submit(
        &console,
        "model.context_override_set",
        json!({"source": "um", "model_id": "model-a", "context": 200000}),
    );
    assert_eq!(set["state"], json!("succeeded"), "{set}");
    let after = read_state(&temp);
    assert_eq!(after["version"], json!(4));
    assert!(after.get("pinned").is_none());
    assert_eq!(after["contextOverrides"]["um|model-a"]["context"], json!(200000));
}
