//! S6a AC4: plaintext rides only the immediate create/rotate no-store response.
//!
//! No operation row, list, status, error or log carries the plaintext.

use std::sync::Arc;

use serde_json::{json, Value};

use um_test_harness::TempDataDir;

mod support;

use support::write::{http_request, start_write_console, Advance, FakeBroker};

fn body_contains(body: &[u8], secret: &str) -> bool {
    body.windows(secret.len())
        .any(|window| window == secret.as_bytes())
}

#[test]
fn plaintext_is_only_in_the_immediate_create_and_rotate_responses() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, store_path, _key_file) = start_write_console(&temp, broker);

    let created = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "one-shot", "idempotency_key": "create-http"})),
        true,
    );
    assert_eq!(created.status, 201);
    assert_eq!(created.header("Cache-Control"), Some("no-store"));
    let plaintext = created.json()["plaintext"]
        .as_str()
        .expect("plaintext")
        .to_string();
    let key_id = created.json()["key"]["id"].as_str().unwrap().to_string();

    let list = http_request(
        &console,
        "GET",
        "/api/console/share-management/keys",
        None,
        false,
    );
    assert!(!body_contains(&list.body, &plaintext));
    let status = http_request(
        &console,
        "GET",
        "/api/console/share-management/status",
        None,
        false,
    );
    assert!(!body_contains(&status.body, &plaintext));
    let operations = http_request(
        &console,
        "GET",
        "/api/console/operations?limit=200",
        None,
        false,
    );
    assert!(!body_contains(&operations.body, &plaintext));
    assert!(!temp.path().join("console-state.sqlite3").exists());

    let replay = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "one-shot", "idempotency_key": "create-http"})),
        true,
    );
    assert_eq!(replay.status, 201);
    assert!(replay.json()["plaintext"].is_null());

    let rotated = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/rotate"),
        Some(json!({"expected_row_version": 1, "idempotency_key": "rotate-http"})),
        true,
    );
    assert_eq!(rotated.status, 200);
    assert_eq!(rotated.header("Cache-Control"), Some("no-store"));
    let rotated_plaintext = rotated.json()["plaintext"]
        .as_str()
        .expect("rotate plaintext")
        .to_string();
    assert!(!body_contains(&rotated.body, &plaintext));
    let list = http_request(
        &console,
        "GET",
        "/api/console/share-management/keys",
        None,
        false,
    );
    assert!(!body_contains(&list.body, &rotated_plaintext));

    let replay = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/rotate"),
        Some(json!({"expected_row_version": 2, "idempotency_key": "rotate-http"})),
        true,
    );
    assert_eq!(replay.status, 200);
    assert!(replay.json()["plaintext"].is_null());

    let invalid = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "x".repeat(65)})),
        true,
    );
    assert_eq!(invalid.status, 400);
    assert!(!body_contains(&invalid.body, &plaintext));

    let errors = [
        (
            "POST",
            "/api/console/share-management/keys",
            json!({"name": "x", "max_concurrency": 49}),
        ),
        (
            "POST",
            &format!("/api/console/share-management/keys/{key_id}/rotate"),
            json!({"expected_row_version": 999}),
        ),
        (
            "PATCH",
            &format!("/api/console/share-management/keys/{key_id}"),
            json!({"name": "again", "expected_row_version": 999}),
        ),
        (
            "DELETE",
            &format!("/api/console/share-management/keys/{key_id}"),
            json!({}),
        ),
    ];
    for (method, target, body) in errors {
        let response = http_request(&console, method, target, Some(body), true);
        assert!(response.status >= 400);
        assert!(!body_contains(&response.body, &plaintext));
        assert!(!body_contains(&response.body, &rotated_plaintext));
    }

    // No log file is written by the candidate write face.
    let logs: Vec<String> = std::fs::read_dir(temp.path())
        .expect("temp dir")
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .filter(|name| name.contains("log"))
        .collect();
    assert!(logs.is_empty(), "unexpected log files: {logs:?}");
    let _ = store_path;
    let _: Value = json!(null);
}
