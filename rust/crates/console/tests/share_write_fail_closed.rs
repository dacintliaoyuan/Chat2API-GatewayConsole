//! S6a AC5: revocation direction fails closed; deleted/legacy readback stays distinct.

use std::sync::Arc;

use serde_json::json;
use um_console::ShareReadStore;
use um_test_harness::TempDataDir;

mod support;

use support::write::{create_over_http, http_request, start_write_console, Advance, FakeBroker};
use support::{client_request, ClientRequest, ClientResponse, Console};

fn create_legacy_over_http(console: &Console, name: &str) -> ClientResponse {
    client_request(
        console.addr,
        &ClientRequest::post("/api/share/keys", b"".to_vec())
            .json_body(json!({"name": name}).to_string().into_bytes())
            .header("Cookie", "usage_session=fixture-session")
            .header("X-Requested-With", "XMLHttpRequest"),
    )
    .expect("request")
}

#[test]
fn revoke_failure_stays_blocked_and_never_answers_a_fake_success() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::RevokeOnly));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker.clone());

    let created = create_over_http(&console, "revoke");
    let key_id = created.json()["key"]["id"].as_str().unwrap().to_string();

    *broker.fail.lock().unwrap() = Some("broker down".to_string());
    *broker.emergency.lock().unwrap() = json!({"state": "stopped"});
    let response = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/revoke-and-terminate"),
        Some(json!({"expected_row_version": 1})),
        true,
    );
    assert_eq!(response.status, 503);
    assert!(response.json()["error"]
        .as_str()
        .unwrap()
        .contains("revoking operation not applied online"));
    assert_eq!(broker.emergency_calls(), 1);

    let status = http_request(
        &console,
        "GET",
        "/api/console/share-management/status",
        None,
        false,
    );
    assert_eq!(status.json()["security_pending"], 1);
    assert_eq!(status.json()["blocked"], true);
}

#[test]
fn deleted_and_legacy_readback_keep_the_four_distinct_outcomes() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, store_path, key_file) = start_write_console(&temp, broker);

    let created = create_over_http(&console, "deleted");
    let key_id = created.json()["key"]["id"].as_str().unwrap().to_string();
    let deleted = http_request(
        &console,
        "DELETE",
        &format!("/api/console/share-management/keys/{key_id}"),
        Some(json!({"expected_row_version": 1})),
        true,
    );
    assert_eq!(deleted.status, 200);
    let gone = http_request(
        &console,
        "GET",
        &format!("/api/console/share-management/keys/{key_id}/secret"),
        None,
        false,
    );
    assert_eq!(gone.status, 410);
    assert_eq!(gone.json()["error"], "key_deleted");

    let created = create_over_http(&console, "legacy");
    let legacy_id = created.json()["key"]["id"].as_str().unwrap().to_string();
    let connection = rusqlite::Connection::open(&store_path).expect("open store");
    connection
        .execute(
            "UPDATE keys SET secret_blob=NULL, secret_algo=NULL WHERE key_id=?",
            [&legacy_id],
        )
        .expect("strip ciphertext");
    drop(connection);

    let legacy = http_request(
        &console,
        "GET",
        &format!("/api/console/share-management/keys/{legacy_id}/secret"),
        None,
        false,
    );
    assert_eq!(legacy.status, 200);
    assert_eq!(legacy.json()["secret_unavailable"], true);
    assert_eq!(legacy.json()["reason"], "legacy");

    let stale = http_request(
        &console,
        "GET",
        &format!("/api/console/share-management/keys/{legacy_id}/secret?expected_row_version=999"),
        None,
        false,
    );
    assert_eq!(stale.status, 200);
    assert_eq!(stale.json()["reason"], "version_conflict");

    let read = ShareReadStore::with_options(store_path, None, Some(key_file));
    let (status, payload) = read.secret(&legacy_id, Some("1")).expect("readback");
    assert_eq!(status, 200);
    assert_eq!(payload["reason"], "legacy");
}

#[test]
fn status_reports_the_last_publication_failure_and_keeps_it_after_success() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::RevokeOnly));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker.clone());
    console.state().session.issue("fixture-session".to_string());

    *broker.fail.lock().unwrap() = Some("broker down".to_string());
    let failed = create_legacy_over_http(&console, "failed-publication");
    assert_eq!(failed.status, 201);
    assert_eq!(failed.json()["published"], json!(false));

    let status = http_request(
        &console,
        "GET",
        "/api/console/share-management/status",
        None,
        false,
    );
    assert_eq!(status.status, 200);
    let failure = status.json()["last_publication_failure"].clone();
    assert_eq!(failure["kind"], json!("publish"));
    assert!(failure["error"].as_str().unwrap().contains("broker down"));
    assert!(failure["at"].is_string());

    // Python keeps `_last_failure` after a later successful publication.
    *broker.fail.lock().unwrap() = None;
    let succeeded = create_legacy_over_http(&console, "succeeded-publication");
    assert_eq!(succeeded.status, 201);
    assert_eq!(succeeded.json()["published"], json!(true));

    let after = http_request(
        &console,
        "GET",
        "/api/console/share-management/status",
        None,
        false,
    );
    assert_eq!(after.json()["last_publication_failure"], failure);
}
