//! S6a AC1: the console operations write face.
//!
//! The Rust manager owns the same `console-state.sqlite3` schema 1 and the same
//! submit/cancel/idempotency semantics as `console/operations.py`. Action
//! execution is an injected seam: these tests use a fake executor, never a real
//! service or broker process.

use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::operation_manager::{
    cloud_registry, ActionExecutor, ActionFailure, OperationContext, OperationManager,
};
use um_test_harness::TempDataDir;

fn manager_with(temp: &TempDataDir, executor: Arc<dyn ActionExecutor>) -> Arc<OperationManager> {
    Arc::new(
        OperationManager::new(
            temp.path().join("console.sqlite3"),
            temp.path().join("locks"),
            cloud_registry(),
            executor,
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

struct RecordingExecutor;

impl ActionExecutor for RecordingExecutor {
    fn execute(
        &self,
        action: &str,
        target: &str,
        params: &serde_json::Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        ctx.report_step("probe").expect("report step");
        Ok(json!({
            "action": action,
            "target": target,
            "params": params,
        }))
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
    calls: Mutex<Vec<String>>,
    cancel_aware: bool,
}

impl ActionExecutor for GatedExecutor {
    fn execute(
        &self,
        action: &str,
        _target: &str,
        _params: &serde_json::Map<String, Value>,
        ctx: &OperationContext,
    ) -> Result<Value, ActionFailure> {
        self.calls
            .lock()
            .expect("calls lock")
            .push(action.to_string());
        if self.cancel_aware {
            let deadline = Instant::now() + Duration::from_secs(10);
            while !ctx.is_cancelled() {
                if Instant::now() >= deadline {
                    return Err(ActionFailure::new("test_timeout", "cancel never arrived"));
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            return Err(ActionFailure::cancelled());
        }
        self.gate.wait();
        Ok(json!({"action": action}))
    }
}

fn gated(cancel_aware: bool) -> (Arc<Gate>, Arc<GatedExecutor>) {
    let gate = Arc::new(Gate::new());
    let executor = Arc::new(GatedExecutor {
        gate: Arc::clone(&gate),
        calls: Mutex::new(Vec::new()),
        cancel_aware,
    });
    (gate, executor)
}

#[test]
fn submit_persists_and_lists_one_succeeded_operation() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let manager = manager_with(&temp, Arc::new(RecordingExecutor));

    let (deduplicated, operation) = manager
        .submit("share.status", "share-gateway", None, None)
        .expect("submit");
    assert!(!deduplicated);
    assert_eq!(operation["action"], "share.status");
    assert_eq!(operation["target"], "share-gateway");
    assert_eq!(operation["resource"], "share:orchestration");

    let succeeded = wait_state(&manager, operation["id"].as_str().unwrap(), &["succeeded"]);
    assert_eq!(succeeded["result"]["action"], "share.status");
    assert_eq!(succeeded["steps"][0]["note"], "probe");
    assert!(succeeded["created_at"].as_f64().unwrap() > 0.0);
    assert!(
        succeeded["finished_at"].as_f64().unwrap() >= succeeded["created_at"].as_f64().unwrap()
    );

    let listed = manager.list(50).expect("list");
    let rows = listed["operations"].as_array().expect("operations");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["id"], operation["id"]);

    let fetched = manager
        .get(operation["id"].as_str().unwrap())
        .expect("get")
        .expect("row");
    assert_eq!(fetched, succeeded);
}

#[test]
fn idempotency_dedupes_same_intent_and_conflicts_on_a_different_one() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let manager = manager_with(&temp, Arc::new(RecordingExecutor));

    let params = json!({"restart_budget_s": 30});
    let (_, first) = manager
        .submit(
            "share.gateway_restart",
            "share-gateway",
            params.as_object(),
            Some("idem-1"),
        )
        .expect("first submit");
    let first = wait_state(&manager, first["id"].as_str().unwrap(), &["succeeded"]);

    let (deduplicated, replay) = manager
        .submit(
            "share.gateway_restart",
            "share-gateway",
            params.as_object(),
            Some("idem-1"),
        )
        .expect("replay");
    assert!(deduplicated);
    assert_eq!(replay["id"], first["id"]);
    assert_eq!(replay["deduplicated"], true);

    let conflict = manager
        .submit(
            "share.gateway_restart",
            "share-gateway",
            json!({"restart_budget_s": 60}).as_object(),
            Some("idem-1"),
        )
        .expect_err("different params must conflict");
    assert_eq!(conflict.code, "idempotency_key_conflict");

    let cross_action = manager
        .submit("share.status", "share-gateway", None, Some("idem-1"))
        .expect_err("different action must conflict");
    assert_eq!(cross_action.code, "idempotency_key_conflict");
}

#[test]
fn submit_validation_matches_the_python_manager() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let manager = manager_with(&temp, Arc::new(RecordingExecutor));

    let unknown = manager
        .submit("share.unknown", "share-gateway", None, None)
        .expect_err("unknown action");
    assert_eq!(unknown.code, "invalid_action");

    let empty_target = manager
        .submit("share.status", "", None, None)
        .expect_err("empty target");
    assert_eq!(empty_target.code, "invalid_target");

    let long_target = manager
        .submit("share.status", &"x".repeat(65), None, None)
        .expect_err("long target");
    assert_eq!(long_target.code, "invalid_target");

    let unknown_param = manager
        .submit(
            "share.gateway_restart",
            "share-gateway",
            json!({"budget": 30}).as_object(),
            None,
        )
        .expect_err("unknown param");
    assert_eq!(unknown_param.code, "invalid_params");
    assert_eq!(unknown_param.message, "unknown param(s): [\"budget\"]");

    let wrong_type = manager
        .submit(
            "share.gateway_restart",
            "share-gateway",
            json!({"restart_budget_s": "30"}).as_object(),
            None,
        )
        .expect_err("wrong type");
    assert_eq!(wrong_type.code, "invalid_params");

    let out_of_range = manager
        .submit(
            "share.gateway_restart",
            "share-gateway",
            json!({"restart_budget_s": 181}).as_object(),
            None,
        )
        .expect_err("out of range");
    assert_eq!(out_of_range.code, "invalid_params");

    let too_large = manager
        .submit(
            "share.status",
            "share-gateway",
            json!({"restart_budget_s": "x".repeat(4100)}).as_object(),
            None,
        )
        .expect_err("too large");
    assert_eq!(too_large.code, "invalid_params");

    let long_idem = manager
        .submit(
            "share.status",
            "share-gateway",
            None,
            Some(&"i".repeat(129)),
        )
        .expect_err("long idempotency key");
    assert_eq!(long_idem.code, "invalid_idempotency_key");

    let disabled = manager
        .submit("service.start", "chat2api", None, None)
        .expect_err("unauthorized target");
    assert_eq!(disabled.code, "action_disabled");
    assert!(disabled
        .reason
        .as_deref()
        .unwrap_or_default()
        .contains("share-gateway"));
}

#[test]
fn cancel_of_a_queued_operation_never_executes_it() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let (gate, executor) = gated(false);
    let manager = manager_with(&temp, executor.clone());

    let (_, first) = manager
        .submit("service.start", "share-gateway", None, None)
        .expect("first submit");
    let first_id = first["id"].as_str().unwrap().to_string();
    let deadline = Instant::now() + Duration::from_secs(10);
    while executor.calls.lock().unwrap().len() < 1 {
        assert!(Instant::now() < deadline, "first operation never started");
        std::thread::sleep(Duration::from_millis(5));
    }

    let (_, second) = manager
        .submit("service.start", "share-gateway", None, None)
        .expect("second submit");
    let second_id = second["id"].as_str().unwrap().to_string();
    assert_eq!(second["state"], "queued");

    let cancelled = manager.cancel(&second_id).expect("cancel queued");
    assert_eq!(cancelled["state"], "cancelled");
    assert_eq!(cancelled["cancel_requested"], true);
    assert!(cancelled["result"].is_null());
    assert_eq!(cancelled["error"]["code"], "cancelled");
    assert_eq!(
        cancelled["error"]["message"],
        "cancelled while queued; never executed"
    );

    gate.release();
    let first = wait_state(&manager, &first_id, &["succeeded"]);
    assert_eq!(first["state"], "succeeded");
    assert_eq!(executor.calls.lock().unwrap().len(), 1);
    assert_eq!(
        manager.get(&second_id).unwrap().unwrap()["state"],
        "cancelled"
    );
}

#[test]
fn cancel_of_a_running_operation_is_cooperative() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let (_gate, executor) = gated(true);
    let manager = manager_with(&temp, executor);

    let (_, operation) = manager
        .submit("share.diagnostics", "share-gateway", None, None)
        .expect("submit");
    let op_id = operation["id"].as_str().unwrap().to_string();
    wait_state(&manager, &op_id, &["running"]);

    let requested = manager.cancel(&op_id).expect("cancel running");
    assert_eq!(requested["state"], "running");
    assert_eq!(requested["cancel_requested"], true);

    let cancelled = wait_state(&manager, &op_id, &["cancelled"]);
    assert_eq!(cancelled["error"]["code"], "cancelled");
    assert_eq!(
        cancelled["error"]["message"],
        "adapter honoured the cancel request"
    );
}

#[test]
fn cancel_of_an_unknown_or_finished_operation_is_a_structured_error() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let manager = manager_with(&temp, Arc::new(RecordingExecutor));

    let unknown = manager.cancel("missing").expect_err("unknown operation");
    assert_eq!(unknown.code, "operation_not_found");

    let (_, operation) = manager
        .submit("share.status", "share-gateway", None, None)
        .expect("submit");
    let op_id = operation["id"].as_str().unwrap().to_string();
    wait_state(&manager, &op_id, &["succeeded"]);

    let finished = manager.cancel(&op_id).expect_err("finished operation");
    assert_eq!(finished.code, "operation_already_finished");
    assert!(finished.message.contains("succeeded"));
}

#[test]
fn restart_marks_unfinished_rows_interrupted_without_replay() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let path = temp.path().join("console.sqlite3");
    let lock_dir = temp.path().join("locks");
    let first = Arc::new(
        OperationManager::new(
            path.clone(),
            lock_dir.clone(),
            cloud_registry(),
            Arc::new(RecordingExecutor),
        )
        .expect("first manager"),
    );
    drop(first);

    let connection = rusqlite::Connection::open(&path).expect("open state");
    let insert = "INSERT INTO operations(id, action, target, params_json, state, idempotency_key, resource, generation, created_at) VALUES(?,?,?,?,?,?,?,?,?)";
    connection
        .execute(
            insert,
            rusqlite::params![
                "op-queued",
                "share.status",
                "share-gateway",
                "{}",
                "queued",
                None::<String>,
                "share:orchestration",
                0,
                1.0
            ],
        )
        .expect("insert queued");
    connection
        .execute(
            insert,
            rusqlite::params![
                "op-running",
                "share.status",
                "share-gateway",
                "{}",
                "running",
                None::<String>,
                "share:orchestration",
                0,
                2.0
            ],
        )
        .expect("insert running");
    drop(connection);

    let second = manager_with(&temp, Arc::new(RecordingExecutor));
    let queued = second.get("op-queued").unwrap().unwrap();
    assert_eq!(queued["state"], "interrupted");
    assert_eq!(queued["error"]["code"], "interrupted_by_restart");
    assert!(queued["finished_at"].as_f64().unwrap() > 0.0);
    let running = second.get("op-running").unwrap().unwrap();
    assert_eq!(running["state"], "interrupted");
}

#[test]
fn foreign_tables_or_incompatible_schema_fail_closed() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let path = temp.path().join("foreign.sqlite3");
    let connection = rusqlite::Connection::open(&path).expect("open db");
    connection
        .execute("CREATE TABLE foreign_table(id TEXT PRIMARY KEY)", [])
        .expect("foreign table");
    drop(connection);
    let error = match OperationManager::new(
        path,
        temp.path().join("locks"),
        cloud_registry(),
        Arc::new(RecordingExecutor),
    ) {
        Ok(_) => panic!("foreign tables must fail closed"),
        Err(error) => error,
    };
    assert_eq!(error.status, 503);

    let incompatible = temp.path().join("incompatible.sqlite3");
    let connection = rusqlite::Connection::open(&incompatible).expect("open db");
    connection
        .execute_batch(
            "CREATE TABLE console_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);\
             CREATE TABLE operations(id TEXT PRIMARY KEY);\
             INSERT INTO console_meta(key,value) VALUES('schema_version','99');",
        )
        .expect("schema");
    drop(connection);
    let error = match OperationManager::new(
        incompatible,
        temp.path().join("locks"),
        cloud_registry(),
        Arc::new(RecordingExecutor),
    ) {
        Ok(_) => panic!("incompatible schema must fail closed"),
        Err(error) => error,
    };
    assert_eq!(error.status, 503);
}

#[test]
fn successful_restart_bumps_the_generation_and_cancels_queued_operations() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let (gate, executor) = gated(false);
    let manager = manager_with(&temp, executor.clone());

    let (_, first) = manager
        .submit("share.gateway_restart", "share-gateway", None, None)
        .expect("first restart");
    let first_id = first["id"].as_str().unwrap().to_string();
    let deadline = Instant::now() + Duration::from_secs(10);
    while executor.calls.lock().unwrap().len() < 1 {
        assert!(Instant::now() < deadline, "first restart never started");
        std::thread::sleep(Duration::from_millis(5));
    }
    let (_, second) = manager
        .submit("share.gateway_restart", "share-gateway", None, None)
        .expect("second restart");
    let second_id = second["id"].as_str().unwrap().to_string();
    assert_eq!(second["state"], "queued");

    gate.release();
    wait_state(&manager, &first_id, &["succeeded"]);
    let cancelled = wait_state(&manager, &second_id, &["cancelled"]);
    assert_eq!(cancelled["error"]["code"], "superseded");
}

#[test]
fn an_action_executor_can_observe_cancellation_and_report_steps() {
    struct CheckedExecutor {
        seen: Mutex<Vec<bool>>,
    }
    impl ActionExecutor for CheckedExecutor {
        fn execute(
            &self,
            _action: &str,
            _target: &str,
            _params: &serde_json::Map<String, Value>,
            ctx: &OperationContext,
        ) -> Result<Value, ActionFailure> {
            self.seen.lock().unwrap().push(ctx.is_cancelled());
            ctx.report_step("checked").unwrap();
            Ok(json!({"ok": true}))
        }
    }
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let executor = Arc::new(CheckedExecutor {
        seen: Mutex::new(Vec::new()),
    });
    let manager = manager_with(&temp, executor.clone());
    let (_, operation) = manager
        .submit("share.status", "share-gateway", None, None)
        .expect("submit");
    let operation = wait_state(&manager, operation["id"].as_str().unwrap(), &["succeeded"]);
    assert_eq!(executor.seen.lock().unwrap().as_slice(), &[false]);
    assert_eq!(operation["steps"][0]["note"], "checked");
}

mod support;

fn start_write_console(temp: &TempDataDir, executor: Arc<dyn ActionExecutor>) -> support::Console {
    let mut config = support::base_config(temp);
    config.console_state_file = Some(temp.path().join("console.sqlite3"));
    config.console_lock_dir = Some(temp.path().join("locks"));
    let server = um_console::ConsoleServer::start_with_wiring(
        config,
        support::stopped_runtime(),
        Arc::new(um_console::UnwiredSeams),
        executor,
        Arc::new(um_console::UnwiredWriteSeams),
    )
    .expect("start candidate console");
    support::Console::from_server(server)
}

fn request(
    console: &support::Console,
    method: &str,
    target: &str,
    body: Option<Value>,
) -> support::ClientResponse {
    let mut request = support::ClientRequest::get(target);
    request.method = method.to_string();
    if let Some(body) = body {
        request = request.json_body(body.to_string());
    }
    support::client_request(console.addr, &request).expect("request")
}

#[test]
fn http_submit_answers_201_then_200_for_an_idempotent_replay() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let console = start_write_console(&temp, Arc::new(RecordingExecutor));

    let created = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": "share.status", "target": "share-gateway"})),
    );
    assert_eq!(created.status, 201);
    assert_eq!(created.header("Cache-Control"), Some("no-store"));
    let operation = created.json()["operation"].clone();
    assert_eq!(operation["state"], "queued");
    let op_id = operation["id"].as_str().unwrap().to_string();
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let fetched = request(
            &console,
            "GET",
            &format!("/api/console/operations/{op_id}"),
            None,
        );
        assert_eq!(fetched.status, 200);
        if fetched.json()["operation"]["state"] == "succeeded" {
            break;
        }
        assert!(Instant::now() < deadline, "operation never succeeded");
        std::thread::sleep(Duration::from_millis(10));
    }

    let replay = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({
            "action": "share.status",
            "target": "share-gateway",
            "idempotency_key": "http-idem",
        })),
    );
    assert_eq!(replay.status, 201);

    let replayed = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({
            "action": "share.status",
            "target": "share-gateway",
            "idempotency_key": "http-idem",
        })),
    );
    assert_eq!(replayed.status, 200);
    assert_eq!(replayed.json()["operation"]["deduplicated"], true);
    assert_eq!(
        replayed.json()["operation"]["id"],
        replay.json()["operation"]["id"]
    );
}

#[test]
fn http_submit_validation_and_cancel_errors_match_python() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let console = start_write_console(&temp, Arc::new(RecordingExecutor));

    let unknown_field = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": "share.status", "url": "http://evil"})),
    );
    assert_eq!(unknown_field.status, 400);
    assert!(unknown_field.json()["error"]
        .as_str()
        .unwrap()
        .starts_with("unknown field(s):"));

    let invalid_action = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": "share.unknown"})),
    );
    assert_eq!(invalid_action.status, 404);
    assert_eq!(invalid_action.json()["error"], "invalid_action");

    let disabled = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": "service.start", "target": "chat2api"})),
    );
    assert_eq!(disabled.status, 403);
    assert_eq!(disabled.json()["error"], "action_disabled");
    assert!(disabled.json()["reason"]
        .as_str()
        .unwrap()
        .contains("share-gateway"));

    let invalid_target = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": "share.status", "target": "x".repeat(65)})),
    );
    assert_eq!(invalid_target.status, 400);
    assert_eq!(invalid_target.json()["error"], "invalid target");

    let cancel_unknown = request(
        &console,
        "POST",
        "/api/console/operations/missing/cancel",
        Some(json!({})),
    );
    assert_eq!(cancel_unknown.status, 404);
    assert_eq!(cancel_unknown.json()["error"], "operation_not_found");

    let (_, operation) = {
        let submitted = request(
            &console,
            "POST",
            "/api/console/operations",
            Some(json!({"action": "share.status", "target": "share-gateway"})),
        );
        assert_eq!(submitted.status, 201);
        ((), submitted.json()["operation"].clone())
    };
    let op_id = operation["id"].as_str().unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let fetched = request(
            &console,
            "GET",
            &format!("/api/console/operations/{op_id}"),
            None,
        );
        if fetched.json()["operation"]["state"] == "succeeded" {
            break;
        }
        assert!(Instant::now() < deadline, "operation never succeeded");
        std::thread::sleep(Duration::from_millis(10));
    }
    let cancel_finished = request(
        &console,
        "POST",
        &format!("/api/console/operations/{op_id}/cancel"),
        Some(json!({})),
    );
    assert_eq!(cancel_finished.status, 409);
    assert_eq!(
        cancel_finished.json()["error"],
        "operation_already_finished"
    );
}

#[test]
fn http_operations_write_is_unavailable_without_a_console_state_file() {
    let temp = TempDataDir::new("um-console-operations-").expect("temp dir");
    let mut config = support::base_config(&temp);
    config.console_state_file = None;
    let server = um_console::ConsoleServer::start_with_wiring(
        config,
        support::stopped_runtime(),
        Arc::new(um_console::UnwiredSeams),
        Arc::new(RecordingExecutor),
        Arc::new(um_console::UnwiredWriteSeams),
    )
    .expect("start candidate console");
    let console = support::Console::from_server(server);
    let response = request(
        &console,
        "POST",
        "/api/console/operations",
        Some(json!({"action": "share.status", "target": "share-gateway"})),
    );
    assert_eq!(response.status, 503);
    assert_eq!(response.json()["error"], "console subsystem unavailable");
}
