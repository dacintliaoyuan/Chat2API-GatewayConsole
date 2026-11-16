//! S6b AC6: a write-face failure falls back to Python and the data format
//! never changes.
//!
//! The Rust candidate writes the SAME `share-store.db` schema 3 / projection
//! schema 2 the Python owner writes, and the session bootstrap code file keeps
//! Python's plain-text 0600 layout. A failed publication leaves the store
//! readable by Python; switching back needs no migration.

mod support;

use std::sync::Arc;

use serde_json::json;
use um_test_harness::TempDataDir;

use support::write::{open_store, start_legacy_console, start_unwired_legacy_console, FakeBroker};
use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

fn request(
    console: &Console,
    method: &str,
    target: &str,
    body: Option<serde_json::Value>,
) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    request = request.header("X-Admin-Control", CONTROL_TOKEN);
    if let Some(body) = body {
        request = request.json_body(body.to_string().into_bytes());
    }
    client_request(console.addr, &request).expect("request")
}

#[test]
fn a_python_created_store_stays_python_readable_after_legacy_writes() {
    let temp = TempDataDir::new("um-console-write-fallback-").expect("temp dir");
    let reads = support::reads_fixture();
    let store_path = support::seed_share_store(&temp, &reads["shareStatus"]["storeSeed"]);
    let key_file = temp.path().join("share-secret.key");
    std::fs::write(
        &key_file,
        reads["metadata"]["fixtureKeyHex"].as_str().unwrap(),
    )
    .expect("fixture key");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600))
            .expect("key file mode");
    }

    let broker = Arc::new(FakeBroker::new(support::write::Advance::Always));
    let console = start_legacy_console(&temp, broker);

    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Some(json!({"name": "python-can-read-me"})),
    );
    assert_eq!(created.status, 201);
    let key_id = created.json()["key_id"].as_str().unwrap().to_string();
    let rotated = request(
        &console,
        "POST",
        &format!("/api/share/keys/{key_id}/rotate"),
        Some(json!({"expected_row_version": 1})),
    );
    assert_eq!(rotated.status, 200);
    let renamed = request(
        &console,
        "PATCH",
        &format!("/api/share/keys/{key_id}"),
        Some(json!({"name": "renamed-by-rust", "expected_row_version": 2})),
    );
    assert_eq!(renamed.status, 200);

    // The Python owner opens the same file: schema 3, projection schema 2,
    // the Python seed key still readable, and no new tables appeared.
    let store = open_store(&store_path, &key_file);
    assert_eq!(store.schema_version(), 3);
    assert_eq!(store.projection_schema_version(), 2);
    let rows = store.list_keys(false).expect("list keys");
    assert!(rows.iter().any(|row| row["name"] == json!("alpha")));
    assert!(rows
        .iter()
        .any(|row| row["name"] == json!("renamed-by-rust")));
    let tables: Vec<String> = store
        .connection()
        .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        .expect("tables")
        .query_map([], |row| row.get::<_, String>(0))
        .expect("query")
        .filter_map(|row| row.ok())
        .collect();
    assert_eq!(tables, vec!["configgen", "keys", "meta", "ops", "security"]);
    drop(store);
}

#[test]
fn a_failed_publication_leaves_the_store_python_readable() {
    let temp = TempDataDir::new("um-console-write-fallback-").expect("temp dir");
    let console = start_unwired_legacy_console(&temp);
    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Some(json!({"name": "unpublished"})),
    );
    // The safe direction answers honestly: the row is saved but not published.
    assert_eq!(created.status, 201);
    assert_eq!(created.json()["published"], json!(false));
    assert!(created.json()["publication_error"].as_str().is_some());
    let key_id = created.json()["key_id"].as_str().unwrap().to_string();

    let revoked = request(
        &console,
        "POST",
        &format!("/api/share/keys/{key_id}/revoke-and-terminate"),
        Some(json!({"expected_row_version": 1})),
    );
    assert_eq!(revoked.status, 503);
    assert!(!String::from_utf8_lossy(&revoked.body).contains("unpublished"));

    let key_file = temp.path().join("share-secret.key");
    let store = open_store(&temp.path().join("share.db"), &key_file);
    assert_eq!(store.schema_version(), 3);
    assert_eq!(store.projection_schema_version(), 2);
    // The revoke direction is tombstoned before its publication fails, exactly
    // like Python: the row leaves the default list but stays in the store.
    assert!(!store
        .list_keys(false)
        .expect("list keys")
        .iter()
        .any(|row| row["name"] == json!("unpublished")));
    assert!(store
        .list_keys(true)
        .expect("list keys")
        .iter()
        .any(|row| row["name"] == json!("unpublished")));
}

#[test]
fn the_session_code_file_keeps_pythons_plain_text_private_layout() {
    let temp = TempDataDir::new("um-console-session-file-").expect("temp dir");
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("nested").join("usage-session-code");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.control_token_file = Some(control.clone());
        config.usage_session_code_file = Some(code_file.clone());
    });
    let response = request(&console, "POST", "/api/session/reissue", None);
    assert_eq!(response.status, 200);
    let code = response.json()["code"].as_str().unwrap().to_string();
    let raw = std::fs::read(&code_file).expect("code file");
    // Python writes the raw token, not a JSON document.
    assert_eq!(String::from_utf8(raw).expect("utf-8 code"), code);
    assert!(!code.starts_with('"'));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&code_file)
            .expect("code metadata")
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o600);
    }

    // A reissue replaces the file atomically from the caller's point of view:
    // the file always carries the current code.
    let response = request(&console, "POST", "/api/session/reissue", None);
    let second = response.json()["code"].as_str().unwrap().to_string();
    assert_ne!(second, code);
    assert_eq!(
        std::fs::read_to_string(&code_file).expect("code file"),
        second
    );
}

#[test]
fn the_candidate_binds_only_an_ephemeral_loopback_port() {
    let temp = TempDataDir::new("um-console-session-file-").expect("temp dir");
    let console = start_console(&temp);
    assert_eq!(console.addr.ip().to_string(), "127.0.0.1");
    assert_ne!(console.addr.port(), 0);
    for port in production_ports() {
        assert_ne!(console.addr.port(), port);
    }
}
