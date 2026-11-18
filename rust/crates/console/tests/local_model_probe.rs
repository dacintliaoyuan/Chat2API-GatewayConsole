//! #94: `model.context_probe_preview` and `model.context_probe_run` execute in
//! Rust against the shared `model-sync-state.json`. These tests seed state and a
//! fake local gateway; no Python process is started.

mod support;

use std::collections::BTreeMap;
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
    let deadline = Instant::now() + Duration::from_secs(30);
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

fn records_by_key(result: &Value) -> BTreeMap<String, Value> {
    result["records"]
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
        .collect()
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
    let records = records_by_key(result);
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
    let records = records_by_key(result);
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
    let records = records_by_key(&retested["result"]);
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

fn gateway(models: Value) -> TestServer {
    TestServer::start(move |_request: HttpRequest| {
        HttpResponse::json(200, &json!({"data": models}).to_string())
    })
    .expect("fake gateway")
}

#[test]
fn context_probe_run_bills_the_selected_models_and_persists_evidence() {
    let temp = temp();
    let mut state = preview_state();
    state["contextProbes"] = json!({});
    write_state(&temp, &state);
    let calls = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
    let recorded = Arc::clone(&calls);
    let gateway = TestServer::start(move |request: HttpRequest| {
        if request.method != "POST" {
            return HttpResponse::json(200, &json!({"data": [{"id": "model-a"}]}).to_string());
        }
        let body: Value = serde_json::from_slice(&request.body).expect("request body");
        recorded.lock().expect("calls").push(body.clone());
        HttpResponse::json(
            200,
            &json!({
                "choices": [{"message": {"content": final_anchor(&body)}}],
                "usage": {"prompt_tokens": 1234}
            })
            .to_string(),
        )
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(
        &console,
        "model.context_probe_run",
        json!({
            "max_requests": 20,
            "max_tokens": 64,
            "models": {"um": ["model-a"]},
        }),
    );

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["requests_used"], json!(4));
    assert_eq!(result["executed_models"], json!(["um|model-a"]));
    assert_eq!(result["budget"]["max_requests"], json!(20));
    assert_eq!(result["budget"]["max_tokens"], json!(64));
    assert_eq!(result["budget"]["requests_used"], json!(4));
    assert_eq!(result["budget"]["remaining_requests"], json!(16));
    assert_eq!(
        result["cost_note"],
        json!("未定价/无法核算：本地网关无计价依据，不编造金额")
    );
    assert_eq!(result["selection"], json!({"um": ["model-a"]}));
    assert_eq!(calls.lock().expect("calls").len(), 4);

    let state = read_state(&temp);
    let record = &state["contextProbes"]["um|model-a"];
    assert_eq!(record["status"], json!("verified"));
    assert_eq!(record["observed_floor"], json!(256000));
    assert_eq!(record["attempts"], json!(4));
    assert_eq!(record["token_evidence"], json!(1234));
    assert_eq!(record["scheme_revision"], json!(1));
    assert!(record["last_run"].as_str().is_some());
    assert!(record["last_success"].as_str().is_some());
    assert!(!temp
        .path()
        .join("data")
        .join("model-sync-probe-recovery.json")
        .exists());
}

#[test]
fn context_probe_run_requires_an_explicit_um_model_selection() {
    let temp = temp();
    let mut state = preview_state();
    state["contextProbes"] = json!({});
    write_state(&temp, &state);
    let console = start_local_console(&temp, 1);

    for params in [
        json!({}),
        json!({"sources": "[\"um\"]"}),
        json!({"models": {"m365": ["model-a"]}}),
    ] {
        let operation = submit(&console, "model.context_probe_run", params.clone());
        assert_eq!(operation["state"], json!("failed"), "{params}: {operation}");
        assert_eq!(operation["error"]["code"], json!("invalid_params"));
    }
}

#[test]
fn context_probe_run_rejects_a_retired_model_without_billing() {
    let temp = temp();
    let mut state = preview_state();
    state["contextProbes"] = json!({});
    write_state(&temp, &state);
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counter = Arc::clone(&calls);
    let gateway = TestServer::start(move |_request: HttpRequest| {
        counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        HttpResponse::json(200, &json!({"data": [{"id": "model-live"}]}).to_string())
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(
        &console,
        "model.context_probe_run",
        json!({"models": {"um": ["model-retired"]}}),
    );

    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("invalid_params"));
    assert!(operation["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("不在当前 um 目录"));
    assert_eq!(
        calls.load(std::sync::atomic::Ordering::SeqCst),
        1,
        "one catalogue read, zero billed requests"
    );
}

#[test]
fn context_probe_run_keeps_completed_evidence_when_cancelled() {
    let temp = temp();
    let mut state = preview_state();
    state["contextProbes"] = json!({});
    write_state(&temp, &state);
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let counter = Arc::clone(&calls);
    let gateway = TestServer::start(move |request: HttpRequest| {
        if request.method != "POST" {
            return HttpResponse::json(200, &json!({"data": [{"id": "model-a"}]}).to_string());
        }
        let _ = counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let body: Value = serde_json::from_slice(&request.body).expect("request body");
        std::thread::sleep(Duration::from_millis(100));
        HttpResponse::json(
            200,
            &json!({
                "choices": [{"message": {"content": final_anchor(&body)}}]
            })
            .to_string(),
        )
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let created = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({
            "action": "model.context_probe_run",
            "target": "model",
            "params": {"models": {"um": ["model-a"]}},
        })),
    );
    assert_eq!(created.status, 201);
    let op_id = created.json()["operation"]["id"]
        .as_str()
        .expect("operation id")
        .to_string();
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let calls_now = calls.load(std::sync::atomic::Ordering::SeqCst);
        if calls_now >= 2 {
            break;
        }
        assert!(Instant::now() < deadline, "probe never sent a request");
        std::thread::sleep(Duration::from_millis(10));
    }
    let cancelled = request(
        &console,
        "POST",
        &format!("/api/console/operations/{op_id}/cancel"),
        None,
    );
    assert_eq!(cancelled.status, 200);
    let operation = wait_terminal(&console, &created.json()["operation"]);
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["stopped"], json!("cancelled"));
    let used = operation["result"]["requests_used"]
        .as_i64()
        .expect("requests_used");
    assert!(used >= 1, "{operation}");

    let state = read_state(&temp);
    let record = &state["contextProbes"]["um|model-a"];
    assert_eq!(record["status"], json!("pending"));
    assert!(record["observed_floor"].as_i64().unwrap_or_default() >= 4000);
    assert_eq!(record["attempts"], json!(used));
}

#[test]
fn context_probe_run_preserves_billed_evidence_when_the_state_save_fails() {
    let temp = temp();
    let mut state = preview_state();
    state["contextProbes"] = json!({});
    write_state(&temp, &state);
    let gateway = gateway(json!([{"id": "model-a"}]));
    let console = start_local_console(&temp, gateway.port());
    // Make the state file's parent a file so the transactional write fails.
    let state_file = state_path(&temp);
    std::fs::remove_file(&state_file).expect("remove state");
    std::fs::create_dir_all(&state_file).expect("state path as directory");

    let operation = submit(
        &console,
        "model.context_probe_run",
        json!({"max_requests": 1, "models": {"um": ["model-a"]}}),
    );

    assert_eq!(operation["state"], json!("failed"), "{operation}");
    assert_eq!(operation["error"]["code"], json!("probe_failed"));
    assert!(operation["error"]["message"]
        .as_str()
        .unwrap_or_default()
        .contains("已计费证据保留到恢复文件"));
    let recovery: Value = serde_json::from_str(
        &std::fs::read_to_string(
            temp.path()
                .join("data")
                .join("model-sync-probe-recovery.json"),
        )
        .expect("recovery file"),
    )
    .expect("recovery json");
    assert_eq!(recovery["persistence"], json!("failed"));
    assert_eq!(recovery["requests_used"], json!(1));
    assert_eq!(recovery["records"]["um|model-a"]["attempts"], json!(1));
}

fn final_anchor(body: &Value) -> String {
    let content = body["messages"][0]["content"].as_str().unwrap_or_default();
    let marker = content
        .split("[ctxprobe ")
        .nth(1)
        .and_then(|rest| rest.split(']').next())
        .unwrap_or("run001");
    format!("[ctxprobe {marker}] FINAL anchor — quote this line verbatim")
}

fn pending_record(model_id: &str) -> Value {
    json!({
        "source": "um", "model_id": model_id,
        "declared_context": 128000, "declared_context_known": true,
        "declared_at": "2026-10-01T00:00:00+00:00",
        "status": "pending", "observed_floor": null,
        "observed_ceiling": null, "attempts": 0,
        "last_run": null, "last_success": null, "last_error": null,
        "evidence_revision": 1, "stale_reason": null,
        "token_evidence": null, "scheme_revision": 1, "history": []
    })
}

fn probe_state(records: Value) -> Value {
    json!({
        "version": 4,
        "catalogCounts": {},
        "contextProbes": records,
        "contextOverrides": {}
    })
}

#[test]
fn context_probe_run_stops_at_the_cumulative_budget_and_reports_the_rest() {
    let temp = temp();
    write_state(
        &temp,
        &probe_state(json!({
            "um|model-a": pending_record("model-a"),
            "um|model-b": pending_record("model-b"),
        })),
    );
    let gateway = TestServer::start(|request: HttpRequest| {
        if request.method != "POST" {
            return HttpResponse::json(
                200,
                &json!({"data": [{"id": "model-a"}, {"id": "model-b"}]}).to_string(),
            );
        }
        let body: Value = serde_json::from_slice(&request.body).expect("request body");
        HttpResponse::json(
            200,
            &json!({"choices": [{"message": {"content": final_anchor(&body)}}]}).to_string(),
        )
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(
        &console,
        "model.context_probe_run",
        json!({"max_requests": 2, "models": {"um": ["model-a", "model-b"]}}),
    );

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["requests_used"], json!(2));
    assert_eq!(result["stopped"], json!("budget"));
    assert_eq!(result["executed_models"], json!(["um|model-a"]));
    assert_eq!(result["not_executed_total"], json!(1));
    assert_eq!(
        result["not_executed"][0],
        json!({
            "key": "um|model-b", "status": "pending",
            "note": "预算耗尽，本轮未执行"
        })
    );
    let state = read_state(&temp);
    assert_eq!(
        state["contextProbes"]["um|model-a"]["status"],
        json!("pending")
    );
    assert_eq!(
        state["contextProbes"]["um|model-a"]["observed_floor"],
        json!(16000)
    );
    assert_eq!(state["contextProbes"]["um|model-a"]["attempts"], json!(2));
    assert_eq!(state["contextProbes"]["um|model-b"]["attempts"], json!(0));
}

#[test]
fn context_probe_run_auto_retests_a_verified_model_and_archives_its_generation() {
    let temp = temp();
    write_state(
        &temp,
        &probe_state(json!({
            "um|model-a": {
                "source": "um", "model_id": "model-a",
                "declared_context": 128000, "declared_context_known": true,
                "status": "verified", "observed_floor": 256000,
                "observed_ceiling": null, "attempts": 4,
                "last_run": "2026-10-02T00:00:00+00:00",
                "last_success": "2026-10-02T00:00:00+00:00",
                "last_error": null, "evidence_revision": 2,
                "stale_reason": null, "token_evidence": 1234,
                "scheme_revision": 1,
                "history": [{"status": "verified", "observed_floor": 64000,
                             "reason": "retest"}]
            }
        })),
    );
    let gateway = TestServer::start(|request: HttpRequest| {
        if request.method != "POST" {
            return HttpResponse::json(200, &json!({"data": [{"id": "model-a"}]}).to_string());
        }
        let body: Value = serde_json::from_slice(&request.body).expect("request body");
        HttpResponse::json(
            200,
            &json!({"choices": [{"message": {"content": final_anchor(&body)}}]}).to_string(),
        )
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(
        &console,
        "model.context_probe_run",
        json!({"models": {"um": ["model-a"]}}),
    );

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["requests_used"], json!(4));
    assert_eq!(result["retest"], json!(true));
    let state = read_state(&temp);
    let record = &state["contextProbes"]["um|model-a"];
    assert_eq!(record["status"], json!("verified"));
    assert_eq!(record["observed_floor"], json!(256000));
    assert_eq!(record["attempts"], json!(4));
    assert_eq!(record["evidence_revision"], json!(3));
    assert_eq!(record["history"].as_array().expect("history").len(), 2);
    assert_eq!(record["history"][1]["observed_floor"], json!(256000));
    assert_eq!(record["history"][1]["reason"], json!("retest"));
}

#[test]
fn context_probe_state_survives_a_console_restart() {
    let temp = temp();
    write_state(
        &temp,
        &probe_state(json!({"um|model-a": pending_record("model-a")})),
    );
    let gateway = TestServer::start(|request: HttpRequest| {
        if request.method != "POST" {
            return HttpResponse::json(200, &json!({"data": [{"id": "model-a"}]}).to_string());
        }
        let body: Value = serde_json::from_slice(&request.body).expect("request body");
        HttpResponse::json(
            200,
            &json!({"choices": [{"message": {"content": final_anchor(&body)}}]}).to_string(),
        )
    })
    .expect("fake gateway");
    {
        let console = start_local_console(&temp, gateway.port());
        let operation = submit(
            &console,
            "model.context_probe_run",
            json!({"max_requests": 1, "models": {"um": ["model-a"]}}),
        );
        assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    }
    let console = start_local_console(&temp, gateway.port());
    let operation = submit(&console, "model.context_probe_preview", json!({}));
    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let record = &operation["result"]["records"][0];
    assert_eq!(record["status"], json!("pending"));
    assert_eq!(record["observed_floor"], json!(4000));
    assert_eq!(record["attempts"], json!(1));
}

#[test]
fn context_probe_run_records_an_explicit_context_limit_as_a_failure_ceiling() {
    let temp = temp();
    write_state(
        &temp,
        &probe_state(json!({"um|model-a": pending_record("model-a")})),
    );
    let gateway = TestServer::start(|request: HttpRequest| {
        if request.method != "POST" {
            return HttpResponse::json(200, &json!({"data": [{"id": "model-a"}]}).to_string());
        }
        HttpResponse::json(
            400,
            &json!({"error": "maximum context length exceeded"}).to_string(),
        )
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit(
        &console,
        "model.context_probe_run",
        json!({"models": {"um": ["model-a"]}}),
    );

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["requests_used"], json!(1));
    let state = read_state(&temp);
    let record = &state["contextProbes"]["um|model-a"];
    assert_eq!(record["status"], json!("failed"));
    assert_eq!(record["observed_ceiling"], json!(4000));
    assert_eq!(record["observed_floor"], Value::Null);
    assert_eq!(record["attempts"], json!(1));
    assert_eq!(record["last_error"], json!("ContextLimitRefused"));
    assert_eq!(record["stale_reason"], Value::Null);
}
