//! #82: local `/api/console` surfaces.
//!
//! The local role reports exactly one runtime service (the chat2api UM main
//! chain), lists the local UM action catalogue, keeps the original operation
//! records readable, and serves the narrow gateway-connection read face. The
//! cloud role keeps its existing share-gateway payloads.

mod support;

use std::sync::Arc;

use serde_json::json;
use um_console::runtime::{RuntimeSnapshot, StaticRuntimeFacts};

use support::*;

fn local_console(
    temp: &um_test_harness::TempDataDir,
    state_file: Option<std::path::PathBuf>,
) -> Console {
    start_console_with(
        temp,
        Arc::new(StaticRuntimeFacts::new(RuntimeSnapshot::running(vec![42]))),
        |config| {
            config.role = um_console::Role::Local;
            config.port = 8001;
            config.gateway_port = 8000;
            config.console_state_file = state_file;
        },
    )
}

fn seed_operations(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("console.sqlite3");
    let connection = rusqlite::Connection::open(&path).expect("open operations db");
    connection
        .execute_batch(
            "CREATE TABLE console_meta(key TEXT PRIMARY KEY, value TEXT);
             CREATE TABLE operations(
                id TEXT PRIMARY KEY, action TEXT, target TEXT, state TEXT,
                resource TEXT, generation INTEGER, created_at REAL,
                started_at REAL, finished_at REAL, cancel_requested INTEGER,
                steps_json TEXT, result_json TEXT, error_json TEXT,
                idempotency_key TEXT);
             INSERT INTO console_meta(key, value) VALUES('schema_version', '1');
             INSERT INTO operations(id, action, target, state, resource, generation,
                created_at, started_at, finished_at, cancel_requested, steps_json,
                result_json, error_json, idempotency_key)
             VALUES('op-a', 'account.um_status', 'chat2api', 'succeeded',
                'chat2api', 0, 1700000000, 1700000000, NULL, 0, '[]',
                '{\"status\":\"valid\"}', NULL, NULL);",
        )
        .expect("seed operations");
    path
}

#[test]
fn local_services_report_only_the_chat2api_runtime() {
    let temp = temp();
    let console = local_console(&temp, None);
    let response =
        client_request(console.addr, &ClientRequest::get("/api/console/services")).expect("services");
    assert_eq!(response.status, 200);
    let payload = response.json();
    assert_eq!(payload["ok"], json!(true));
    let services = payload["services"].as_array().expect("services");
    assert_eq!(services.len(), 1);
    assert_eq!(services[0]["key"], json!("chat2api"));
    assert_eq!(services[0]["display_name"], json!("chat2api"));
    assert_eq!(services[0]["port"], json!(8000));
    assert_eq!(services[0]["state"], json!("Running"));
    assert_eq!(services[0]["healthy"], json!(true));
    assert_eq!(services[0]["process_ids"], json!([42]));
    assert_eq!(payload["meta"]["source"], json!("local-runtime"));
    assert!(payload["meta"]["generated_epoch"].is_number());

    let health = client_request(console.addr, &ClientRequest::get("/api/console/health"))
        .expect("health");
    assert_eq!(health.status, 200);
    let health_payload = health.json();
    let services = health_payload["services"]
        .as_array()
        .expect("health services");
    assert_eq!(services.len(), 2);
    assert_eq!(services[0]["key"], json!("chat2api"));
    assert_eq!(services[1]["id"], json!("usagesrv"));
    assert_eq!(services[1]["port"], json!(8001));
}

#[test]
fn local_actions_list_the_um_catalogue_without_share_actions() {
    let temp = temp();
    let console = local_console(&temp, None);
    let response =
        client_request(console.addr, &ClientRequest::get("/api/console/actions")).expect("actions");
    assert_eq!(response.status, 200);
    let actions = response.json()["actions"].as_array().expect("actions").clone();
    let names: Vec<&str> = actions
        .iter()
        .filter_map(|row| row["action"].as_str())
        .collect();
    assert!(names.contains(&"account.um_status"));
    assert!(names.contains(&"model.catalog"));
    assert!(!names.iter().any(|name| name.starts_with("share.")));
    assert!(actions.iter().all(|row| row["enabled"] == json!(false)));
}

#[test]
fn local_operations_read_the_original_records() {
    let temp = temp();
    let path = seed_operations(&temp);
    let console = local_console(&temp, Some(path));
    let list = client_request(
        console.addr,
        &ClientRequest::get("/api/console/operations?limit=50"),
    )
    .expect("operations");
    assert_eq!(list.status, 200);
    assert_eq!(list.json()["operations"][0]["id"], json!("op-a"));

    let detail = client_request(
        console.addr,
        &ClientRequest::get("/api/console/operations/op-a"),
    )
    .expect("operation detail");
    assert_eq!(detail.status, 200);
    assert_eq!(detail.json()["operation"]["action"], json!("account.um_status"));

    let missing = client_request(
        console.addr,
        &ClientRequest::get("/api/console/operations/nope"),
    )
    .expect("missing operation");
    assert_eq!(missing.status, 404);
}

#[test]
fn local_gateway_connection_uses_the_runtime_snapshot_and_stays_unknown() {
    let temp = temp();
    let console = local_console(&temp, None);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/gateway-connection?service=chat2api"),
    )
    .expect("gateway connection");
    assert_eq!(response.status, 200);
    let payload = response.json();
    assert_eq!(payload["ok"], json!(true));
    assert_eq!(payload["service"], json!("chat2api"));
    assert_eq!(payload["port"], json!(8000));
    assert_eq!(payload["base_url"], json!("http://127.0.0.1:8000/v1"));
    assert_eq!(payload["health_path"], json!("/health"));
    assert_eq!(payload["protocol"], json!("openai-compatible"));
    assert_eq!(payload["runtime_state"], json!("Running"));
    assert_eq!(payload["runtime_healthy"], json!(true));
    assert_eq!(payload["auth"], json!("unknown"));
    assert_eq!(payload["key_state"], json!("unknown"));
    assert_eq!(payload["key_plaintext"], json!(null));
    assert_eq!(payload["key_in_use"], json!("unknown"));
    assert!(payload["evidence"].is_array());

    let invalid = client_request(
        console.addr,
        &ClientRequest::get("/api/console/gateway-connection?service=share"),
    )
    .expect("invalid service");
    assert_eq!(invalid.status, 400);
    assert_eq!(invalid.json()["error"], json!("invalid_service"));
}

#[test]
fn local_cloud_ops_and_share_usage_reuse_the_existing_readers() {
    let temp = temp();
    let console = local_console(&temp, None);
    let cloud_ops =
        client_request(console.addr, &ClientRequest::get("/api/console/cloud-ops")).expect("cloud ops");
    assert_eq!(cloud_ops.status, 200);
    assert_eq!(cloud_ops.json()["available"], json!(false));

    let share_usage = client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-usage"),
    )
    .expect("share usage");
    assert_eq!(share_usage.status, 503);
    assert_eq!(share_usage.json()["error"], json!("share_ledger_path_unresolved"));
}
