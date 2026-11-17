//! #82: the local share-management faces reuse the existing share modules.
//!
//! `/api/share/*` keeps its session/control-token auth; the
//! `/api/console/share-management/*` facade keeps the console trust surface
//! (credential-free reads, same-origin marker on writes). The local role does
//! not assemble a second share implementation.

mod support;

use std::sync::Arc;

use serde_json::json;

use support::write::{key_file, FakeBroker, Advance};
use support::*;
use um_console::operation_manager::UnwiredActionExecutor;
use um_console::{ConsoleServer, UnwiredSeams};

const CONTROL_TOKEN: &str = "fixture-control-token";

fn start_local_share(temp: &um_test_harness::TempDataDir) -> Console {
    let key_file = key_file(temp);
    let store = temp.path().join("share.db");
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let backend = temp.path().join("share-backend");
    std::fs::create_dir_all(&backend).expect("backend dir");
    let key = um_store::secret::load_key_file(Some(&key_file)).expect("share key");
    drop(um_store::ShareStore::open_with_key(&store, key).expect("share schema"));
    let mut config = base_config(temp);
    config.role = um_console::Role::Local;
    config.port = 8001;
    config.share_store_file = Some(store);
    config.share_secret_key_file = Some(key_file);
    config.control_token_file = Some(control);
    config.share_backend_dir = Some(backend);
    let server = ConsoleServer::start_with_wiring(
        config,
        stopped_runtime(),
        Arc::new(UnwiredSeams),
        Arc::new(UnwiredActionExecutor),
        Arc::new(FakeBroker::new(Advance::Always)),
    )
    .expect("start local console");
    Console::from_server(server)
}

#[test]
fn local_console_share_reads_are_credential_free_and_legacy_reads_keep_auth() {
    let temp = temp();
    let console = start_local_share(&temp);

    let missing = client_request(console.addr, &ClientRequest::get("/api/share/keys"))
        .expect("legacy keys");
    assert_eq!(missing.status, 401);

    let with_token = client_request(
        console.addr,
        &ClientRequest::get("/api/share/keys").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("legacy keys with token");
    assert_eq!(with_token.status, 200);
    assert!(with_token.json()["keys"].is_array());

    let console_keys = client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-management/keys"),
    )
    .expect("console keys");
    assert_eq!(console_keys.status, 200);
    assert!(console_keys.json()["keys"].is_array());

    let status = client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-management/status"),
    )
    .expect("console status");
    assert_eq!(status.status, 200);

    let unresolved = client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-usage"),
    )
    .expect("console share usage");
    assert_eq!(unresolved.status, 200);
    assert!(unresolved.json()["keys"].is_array());
}

#[test]
fn local_console_share_writes_keep_the_same_origin_marker() {
    let temp = temp();
    let console = start_local_share(&temp);

    let refused = client_request(
        console.addr,
        &ClientRequest::post("/api/console/share-management/keys", b"{\"name\":\"local-key\"}".to_vec())
            .json_body(json!({"name": "local-key"}).to_string().into_bytes()),
    )
    .expect("refused write");
    assert_eq!(refused.status, 403);

    let created = client_request(
        console.addr,
        &ClientRequest::post(
            "/api/console/share-management/keys",
            json!({"name": "local-key"}).to_string().into_bytes(),
        )
        .json_body(json!({"name": "local-key"}).to_string().into_bytes())
        .header("X-Requested-With", "XMLHttpRequest"),
    )
    .expect("created key");
    assert_eq!(created.status, 201);
    assert_eq!(created.json()["key"]["name"], json!("local-key"));

    let keys = client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-management/keys"),
    )
    .expect("keys");
    assert_eq!(keys.status, 200);
    assert_eq!(keys.json()["keys"][0]["name"], json!("local-key"));
}

#[test]
fn local_console_share_usage_reuses_the_share_ledger() {
    let temp = temp();
    let fixture = reads_fixture();
    let backend = temp.path().join("share-backend");
    std::fs::create_dir_all(&backend).expect("backend dir");
    std::fs::write(
        backend.join("usage-share-backend.jsonl"),
        fixture["shareUsage"]["ledger"].as_str().expect("ledger"),
    )
    .expect("write ledger");
    let key_file = key_file(&temp);
    let store = temp.path().join("share.db");
    let key = um_store::secret::load_key_file(Some(&key_file)).expect("share key");
    drop(um_store::ShareStore::open_with_key(&store, key).expect("share schema"));
    let mut config = base_config(&temp);
    config.role = um_console::Role::Local;
    config.port = 8001;
    config.share_store_file = Some(store);
    config.share_secret_key_file = Some(key_file);
    config.share_backend_dir = Some(backend);
    let server = ConsoleServer::start_with_wiring(
        config,
        stopped_runtime(),
        Arc::new(UnwiredSeams),
        Arc::new(UnwiredActionExecutor),
        Arc::new(FakeBroker::new(Advance::Always)),
    )
    .expect("start local console");
    let console = Console::from_server(server);

    let usage = client_request(console.addr, &ClientRequest::get("/api/console/share-usage"))
        .expect("share usage");
    assert_eq!(usage.status, 200);
    assert!(!usage.json()["keys"].as_array().expect("usage keys").is_empty());
    assert!(usage.json()["coverage"].is_object());

    let requests = client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-usage/requests"),
    )
    .expect("share usage requests");
    assert_eq!(requests.status, 200);
    assert!(requests.json()["rows"].is_array());
    assert!(requests.json()["coverage"].is_object());
}
