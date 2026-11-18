//! S6a AC2–AC5: the share-management Key write face.
//!
//! The store is the SAME `share-store.db` schema 3 the Python store writes.
//! Publication is an injected seam: these tests use a deterministic fake broker,
//! never a live broker or a real 8003 process.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use um_console::share_write::{
    ShareWriteError, ShareWriteErrorKind, ShareWriteSeams, ShareWriteStore, UnwiredWriteSeams,
};
use um_console::ShareReadStore;
use um_store::ShareStore;
use um_test_harness::TempDataDir;

mod support;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Advance {
    RevokeOnly,
    Always,
}

struct FakeBroker {
    published: Mutex<Vec<Value>>,
    terminate_seen: Mutex<Vec<Vec<String>>>,
    enabled: Mutex<HashSet<String>>,
    security_generation: Mutex<i64>,
    advance: Mutex<Advance>,
    fail: Mutex<Option<String>>,
    ack_override: Mutex<Option<Value>>,
    omit_security: Mutex<bool>,
    omit_counts: Mutex<bool>,
    emergency: Mutex<Value>,
    emergency_calls: Mutex<usize>,
}

impl FakeBroker {
    fn new(advance: Advance) -> Self {
        Self {
            published: Mutex::new(Vec::new()),
            terminate_seen: Mutex::new(Vec::new()),
            enabled: Mutex::new(HashSet::new()),
            security_generation: Mutex::new(0),
            advance: Mutex::new(advance),
            fail: Mutex::new(None),
            ack_override: Mutex::new(None),
            omit_security: Mutex::new(false),
            omit_counts: Mutex::new(false),
            emergency: Mutex::new(json!({"state": "stopped", "stopped": true})),
            emergency_calls: Mutex::new(0),
        }
    }

    fn published(&self) -> Vec<Value> {
        self.published.lock().unwrap().clone()
    }

    fn emergency_calls(&self) -> usize {
        *self.emergency_calls.lock().unwrap()
    }

    fn set_advance(&self, advance: Advance) {
        *self.advance.lock().unwrap() = advance;
    }
}

impl ShareWriteSeams for FakeBroker {
    fn publish(
        &self,
        projection: &Value,
        terminate_keys: Option<&[String]>,
    ) -> Result<Value, ShareWriteError> {
        if let Some(message) = self.fail.lock().unwrap().clone() {
            return Err(ShareWriteError::broker(message));
        }
        self.published.lock().unwrap().push(projection.clone());
        let generation = projection["config_generation"].as_i64().unwrap_or_default();
        let next_enabled: HashSet<String> = projection["data"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .filter(|row| row["enabled"].as_bool().unwrap_or(false))
                    .filter_map(|row| row["key_id"].as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let previous = self.enabled.lock().unwrap().clone();
        let revoked = previous.difference(&next_enabled).next().is_some();
        if revoked || *self.advance.lock().unwrap() == Advance::Always {
            *self.security_generation.lock().unwrap() = generation;
        }
        *self.enabled.lock().unwrap() = next_enabled;
        if let Some(keys) = terminate_keys {
            self.terminate_seen
                .lock()
                .unwrap()
                .push(keys.iter().map(|key| key.to_string()).collect());
        }
        let ack_override = self.ack_override.lock().unwrap().clone();
        let mut reply = json!({
            "applied_generation": ack_override.unwrap_or_else(|| json!(generation)),
            "instance_id": "fixture-instance",
        });
        if !*self.omit_security.lock().unwrap() {
            reply["security_generation"] = json!(*self.security_generation.lock().unwrap());
        }
        if terminate_keys.is_some() && !*self.omit_counts.lock().unwrap() {
            reply["terminated"] = json!(2);
            reply["released"] = json!(3);
            reply["aborted"] = json!(4);
            reply["upstream_closed"] = json!(5);
        }
        Ok(reply)
    }

    fn emergency_stop(&self) -> Result<Value, ShareWriteError> {
        *self.emergency_calls.lock().unwrap() += 1;
        Ok(self.emergency.lock().unwrap().clone())
    }
}

fn key_file(temp: &TempDataDir) -> PathBuf {
    let path = temp.path().join("share-secret.key");
    std::fs::write(&path, (0..32).collect::<Vec<u8>>()).expect("key file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .expect("key file mode");
    }
    path
}

fn make_writer(temp: &TempDataDir, broker: Arc<FakeBroker>) -> (ShareWriteStore, PathBuf, PathBuf) {
    let key_file = key_file(temp);
    let store_path = temp.path().join("share.db");
    let writer = ShareWriteStore::new(store_path.clone(), Some(key_file.clone()), broker);
    (writer, store_path, key_file)
}

fn create(writer: &ShareWriteStore, name: &str) -> Value {
    writer.create_key(name, None, None).expect("create key")
}

fn open_store(path: &Path, key_file: &Path) -> ShareStore {
    ShareStore::open_with_key(
        path,
        um_store::secret::load_key_file(Some(key_file)).unwrap(),
    )
    .expect("open store")
}

#[test]
fn create_list_patch_delete_flow_matches_python() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::RevokeOnly));
    let (writer, store_path, key_file) = make_writer(&temp, broker.clone());

    let created = create(&writer, "alpha");
    let key = created["key"].clone();
    assert_eq!(key["name"], "alpha");
    assert_eq!(key["enabled"], true);
    assert_eq!(key["max_concurrency"], 16);
    assert_eq!(key["row_version"], 1);
    assert!(created["plaintext"]
        .as_str()
        .unwrap()
        .starts_with("um-share-"));
    assert_eq!(created["published"], true);
    assert_eq!(broker.published().len(), 1);

    let key_id = key["id"].as_str().unwrap().to_string();
    let listed = ShareReadStore::with_options(store_path.clone(), None, Some(key_file.clone()))
        .keys()
        .expect("list keys");
    assert_eq!(listed["keys"].as_array().unwrap().len(), 1);
    let plaintext = created["plaintext"].as_str().unwrap();
    assert!(!listed.to_string().contains(plaintext));

    let patched = writer
        .update_key(&key_id, Some("renamed"), None, 1)
        .expect("patch key");
    assert_eq!(patched["name"], "renamed");
    assert_eq!(patched["row_version"], 2);

    let stale = writer
        .update_key(&key_id, Some("again"), None, 1)
        .expect_err("stale row version");
    assert_eq!(stale.kind, ShareWriteErrorKind::Conflict);

    let enabled = writer
        .update_key(&key_id, None, None, 2)
        .expect_err("nothing to update");
    assert_eq!(enabled.kind, ShareWriteErrorKind::Invalid);

    let deleted = writer.delete_key(&key_id, 2).expect("delete key");
    assert!(deleted["deleted_at"].is_string());
    let listed = ShareReadStore::with_options(store_path.clone(), None, Some(key_file.clone()))
        .keys()
        .expect("list keys");
    assert_eq!(listed["keys"], json!([]));

    let store = open_store(&store_path, &key_file);
    let row = store.get_key(&key_id).expect("deleted row");
    assert!(row["deleted_at"].is_string());
    assert_eq!(row["enabled"], false);
    drop(store);

    let already = writer.delete_key(&key_id, 3).expect("already deleted");
    assert_eq!(already["already_deleted"], true);
    assert!(already["deleted_at"].is_string());
}

#[test]
fn create_and_rotate_idempotency_replays_without_plaintext() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (writer, _store_path, _key_file) = make_writer(&temp, broker);

    let first = writer
        .create_key("idem", None, Some("create-1"))
        .expect("create");
    let replay = writer
        .create_key("idem", None, Some("create-1"))
        .expect("create replay");
    assert_eq!(replay["key"]["id"], first["key"]["id"]);
    assert!(replay["plaintext"].is_null());
    assert_eq!(replay["key"]["name"], "idem");

    let key_id = first["key"]["id"].as_str().unwrap().to_string();
    let conflict = writer
        .rotate_key(&key_id, Some("create-1"), 1)
        .expect_err("same idempotency key for a different operation kind");
    assert_eq!(conflict.kind, ShareWriteErrorKind::Conflict);

    let rotated = writer
        .rotate_key(&key_id, Some("rotate-1"), 1)
        .expect("rotate");
    let rotated_secret = rotated["plaintext"].as_str().unwrap().to_string();
    assert!(rotated_secret.starts_with("um-share-"));
    assert_eq!(rotated["key"]["row_version"], 2);

    let replay = writer
        .rotate_key(&key_id, Some("rotate-1"), 2)
        .expect("rotate replay");
    assert_eq!(replay["key"]["id"], key_id);
    assert!(replay["plaintext"].is_null());
    assert_eq!(replay["key"]["row_version"], 2);

    let other = create(&writer, "other");
    let cross_key = writer
        .rotate_key(other["key"]["id"].as_str().unwrap(), Some("rotate-1"), 1)
        .expect_err("idempotency key bound to another key");
    assert_eq!(cross_key.kind, ShareWriteErrorKind::Conflict);
}

#[test]
fn rotate_is_fail_closed_until_the_runtime_confirms_it() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::RevokeOnly));
    let (writer, store_path, key_file) = make_writer(&temp, broker.clone());
    let created = create(&writer, "rot");
    let key_id = created["key"]["id"].as_str().unwrap().to_string();

    let stale = writer
        .rotate_key(&key_id, None, 999)
        .expect_err("stale version");
    assert_eq!(stale.kind, ShareWriteErrorKind::Conflict);

    let failed = writer
        .rotate_key(&key_id, None, 1)
        .expect_err("runtime never confirms a rotate");
    assert_eq!(failed.kind, ShareWriteErrorKind::Broker);
    assert!(failed
        .message
        .contains("revoking operation not applied online"));

    // The rotate transaction still committed the new secret version; the narrow
    // readback reveals it, and the publication stays pending/blocked.
    let read = ShareReadStore::with_options(store_path.clone(), None, Some(key_file.clone()));
    let (status, payload) = read.secret(&key_id, None).expect("readback");
    assert_eq!(status, 200);
    assert!(payload["secret"].as_str().unwrap().starts_with("um-share-"));
    assert_ne!(payload["secret"], created["plaintext"]);
    let status_payload = read.status(&um_console::UnwiredSeams).expect("status");
    assert_eq!(status_payload["security_pending"], 1);
    assert_eq!(status_payload["blocked"], false);

    let store = open_store(&store_path, &key_file);
    let intents = store
        .operations(Some("publish"), None)
        .expect("publish ops");
    let failed_intent = intents
        .iter()
        .find(|operation| operation.status == "failed")
        .expect("failed publish intent");
    assert_eq!(
        failed_intent.payload["target_generation"],
        status_payload["desired_generation"]
    );
    assert_eq!(failed_intent.payload["blocked"], false);
    assert_eq!(broker.emergency_calls(), 1);
}

#[test]
fn revoke_and_terminate_reports_each_gateway_count() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::RevokeOnly));
    let (writer, _store_path, _key_file) = make_writer(&temp, broker.clone());
    let created = create(&writer, "rev");
    let key_id = created["key"]["id"].as_str().unwrap().to_string();

    let revoked = writer
        .revoke_and_terminate_key(&key_id, 1, None)
        .expect("revoke");
    assert_eq!(revoked["key"]["enabled"], false);
    assert_eq!(revoked["revoked"], true);
    assert_eq!(revoked["terminated"], 2);
    assert_eq!(revoked["released"], 3);
    assert_eq!(revoked["aborted"], 4);
    assert_eq!(revoked["upstream_closed"], 5);
    assert!(revoked["note"].as_str().unwrap().contains("不可撤回"));
    assert_eq!(
        broker.terminate_seen.lock().unwrap().clone(),
        vec![vec![key_id.clone()]]
    );

    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::RevokeOnly));
    *broker.omit_counts.lock().unwrap() = true;
    let (writer, _store_path, _key_file) = make_writer(&temp, broker);
    let created = create(&writer, "rev-none");
    let key_id = created["key"]["id"].as_str().unwrap().to_string();
    let revoked = writer
        .revoke_and_terminate_key(&key_id, 1, None)
        .expect("revoke");
    assert!(revoked["terminated"].is_null());
    assert!(revoked["released"].is_null());
    assert!(revoked["note"].as_str().unwrap().contains("在途确认未完成"));
    assert!(!revoked["note"]
        .as_str()
        .unwrap()
        .contains("已撤销并确认终止在途"));
}

#[test]
fn settings_are_one_to_sixty_four_and_conflicts_are_version_mismatches() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (writer, store_path, key_file) = make_writer(&temp, broker.clone());

    let raised = writer.set_share_limits(32, None).expect("lower settings");
    assert_eq!(raised["share_max_concurrency"], 32);
    assert_eq!(raised["changed"], true);
    assert_eq!(raised["published"], true);

    let stale = writer
        .set_share_limits(16, Some(63))
        .expect_err("stale expected_share_max");
    assert_eq!(stale.kind, ShareWriteErrorKind::Conflict);

    let invalid = writer
        .set_share_limits(0, None)
        .expect_err("zero global limit");
    assert_eq!(invalid.kind, ShareWriteErrorKind::Invalid);
    let too_large = writer
        .set_share_limits(65, None)
        .expect_err("too large global limit");
    assert_eq!(too_large.kind, ShareWriteErrorKind::Invalid);

    broker.set_advance(Advance::RevokeOnly);
    let lowered = writer
        .set_share_limits(16, Some(32))
        .expect_err("lowering is revocation-direction");
    assert_eq!(lowered.kind, ShareWriteErrorKind::Broker);
    assert!(lowered
        .message
        .contains("revoking operation not applied online"));

    let store = open_store(&store_path, &key_file);
    assert_eq!(store.share_max_concurrency().unwrap(), 16);
    assert!(store
        .security_records()
        .unwrap()
        .iter()
        .any(|record| record["action"] == "limit_lower"));
    drop(store);

    let read = ShareReadStore::with_options(store_path, None, Some(key_file));
    let status = read.status(&um_console::UnwiredSeams).expect("status");
    assert_eq!(status["security_pending"], 1);
    assert_eq!(status["blocked"], false);
}

#[test]
fn unwired_publication_is_an_honest_failure_not_a_fake_success() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let key_file = key_file(&temp);
    let writer = ShareWriteStore::new(
        temp.path().join("share.db"),
        Some(key_file),
        Arc::new(UnwiredWriteSeams),
    );

    let created = writer
        .create_key("alpha", None, None)
        .expect("safe direction still answers honestly");
    assert_eq!(created["published"], false);
    assert!(created["publication_error"].as_str().is_some());
    assert!(created["publication_error"]
        .as_str()
        .unwrap()
        .contains("未接线"));

    let key_id = created["key"]["id"].as_str().unwrap().to_string();
    let revoked = writer
        .revoke_and_terminate_key(&key_id, 1, None)
        .expect_err("revocation direction fails closed");
    assert_eq!(revoked.kind, ShareWriteErrorKind::Broker);
    assert_eq!(revoked.status(), 503);
}

// ---------------------------------------------------------------- HTTP face

fn start_write_console(
    temp: &TempDataDir,
    broker: Arc<FakeBroker>,
) -> (support::Console, PathBuf, PathBuf) {
    let key_file = key_file(temp);
    let store_path = temp.path().join("share.db");
    let mut config = support::base_config(temp);
    config.share_store_file = Some(store_path.clone());
    config.share_secret_key_file = Some(key_file.clone());
    let server = um_console::ConsoleServer::start_with_wiring(
        config,
        support::stopped_runtime(),
        Arc::new(um_console::UnwiredSeams),
        Arc::new(um_console::operation_manager::UnwiredActionExecutor),
        broker,
    )
    .expect("start candidate console");
    (support::Console::from_server(server), store_path, key_file)
}

fn http_request(
    console: &support::Console,
    method: &str,
    target: &str,
    body: Option<Value>,
    marker: bool,
) -> support::ClientResponse {
    let mut request = support::ClientRequest::get(target);
    request.method = method.to_string();
    if marker {
        request = request.header("X-Requested-With", "XMLHttpRequest");
    }
    if let Some(body) = body {
        request = request.json_body(body.to_string());
    }
    support::client_request(console.addr, &request).expect("request")
}

fn create_over_http(console: &support::Console, name: &str) -> (u16, Value, Option<String>) {
    let response = http_request(
        console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": name})),
        true,
    );
    let cache_control = response.header("Cache-Control").map(str::to_string);
    (response.status, response.json(), cache_control)
}

#[test]
fn http_create_is_no_store_and_plaintext_only_appears_once() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker);

    let first = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "one-shot", "idempotency_key": "create-http"})),
        true,
    );
    assert_eq!(first.status, 201);
    let body = first.json();
    let cache_control = first.header("Cache-Control").map(str::to_string);
    assert_eq!(cache_control.as_deref(), Some("no-store"));
    let plaintext = body["plaintext"].as_str().expect("plaintext").to_string();
    assert!(plaintext.starts_with("um-share-"));
    let key_id = body["key"]["id"].as_str().unwrap().to_string();

    let list = http_request(
        &console,
        "GET",
        "/api/console/share-management/keys",
        None,
        false,
    );
    assert_eq!(list.status, 200);
    assert!(!list
        .body
        .windows(plaintext.len())
        .any(|window| window == plaintext.as_bytes()));

    let status_response = http_request(
        &console,
        "GET",
        "/api/console/share-management/status",
        None,
        false,
    );
    assert!(!status_response
        .body
        .windows(plaintext.len())
        .any(|window| window == plaintext.as_bytes()));

    let replay = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "one-shot", "idempotency_key": "create-http"})),
        true,
    );
    assert_eq!(replay.status, 201);
    assert!(replay.json()["plaintext"].is_null());

    let secret = http_request(
        &console,
        "GET",
        &format!("/api/console/share-management/keys/{key_id}/secret"),
        None,
        false,
    );
    assert_eq!(secret.status, 200);
    assert_eq!(secret.json()["secret"], plaintext);
    assert_eq!(secret.header("Cache-Control"), Some("no-store"));
    assert!(!temp.path().join("console-state.sqlite3").exists());

    let rotated = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/rotate"),
        Some(json!({"expected_row_version": 1, "idempotency_key": "rotate-http"})),
        true,
    );
    assert_eq!(rotated.status, 200);
    assert_eq!(rotated.header("Cache-Control"), Some("no-store"));
    assert!(rotated.json()["plaintext"]
        .as_str()
        .unwrap()
        .starts_with("um-share-"));

    let replay = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/rotate"),
        Some(json!({"expected_row_version": 2, "idempotency_key": "rotate-http"})),
        true,
    );
    assert_eq!(replay.status, 200);
    assert!(replay.json()["plaintext"].is_null());
}

#[test]
fn http_delete_answers_410_and_legacy_readback_is_inline_unavailable() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, store_path, _key_file) = start_write_console(&temp, broker);

    let (_, created, _) = create_over_http(&console, "deleted");
    let key_id = created["key"]["id"].as_str().unwrap().to_string();
    let deleted = http_request(
        &console,
        "DELETE",
        &format!("/api/console/share-management/keys/{key_id}"),
        Some(json!({"expected_row_version": 1})),
        true,
    );
    assert_eq!(deleted.status, 200);
    assert_eq!(deleted.json()["ok"], true);

    let gone = http_request(
        &console,
        "GET",
        &format!("/api/console/share-management/keys/{key_id}/secret"),
        None,
        false,
    );
    assert_eq!(gone.status, 410);
    assert_eq!(gone.json()["error"], "key_deleted");

    let (_, legacy, _) = create_over_http(&console, "legacy");
    let legacy_id = legacy["key"]["id"].as_str().unwrap().to_string();
    let connection = rusqlite::Connection::open(&store_path).expect("open store");
    connection
        .execute(
            "UPDATE keys SET secret_blob=NULL, secret_algo=NULL WHERE key_id=?",
            [&legacy_id],
        )
        .expect("strip ciphertext");
    drop(connection);
    let legacy_read = http_request(
        &console,
        "GET",
        &format!("/api/console/share-management/keys/{legacy_id}/secret"),
        None,
        false,
    );
    assert_eq!(legacy_read.status, 200);
    assert_eq!(legacy_read.json()["secret_unavailable"], true);
    assert_eq!(legacy_read.json()["reason"], "legacy");

    let stale = http_request(
        &console,
        "GET",
        &format!("/api/console/share-management/keys/{legacy_id}/secret?expected_row_version=999"),
        None,
        false,
    );
    assert_eq!(stale.status, 200);
    assert_eq!(stale.json()["reason"], "version_conflict");
}

#[test]
fn http_write_gate_validation_and_revoke_fail_closed() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker.clone());

    let unmarked = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "x"})),
        false,
    );
    assert_eq!(unmarked.status, 403);
    assert_eq!(
        unmarked.json()["error"],
        "same-origin marker or control token required"
    );

    let unknown_field = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "x", "url": "http://evil"})),
        true,
    );
    assert_eq!(unknown_field.status, 400);

    let bad_max = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "x", "max_concurrency": 49})),
        true,
    );
    assert_eq!(bad_max.status, 400);

    let settings_zero = http_request(
        &console,
        "PATCH",
        "/api/console/share-management/settings",
        Some(json!({"share_max_concurrency": 0})),
        true,
    );
    assert_eq!(settings_zero.status, 400);

    let settings_65 = http_request(
        &console,
        "PATCH",
        "/api/console/share-management/settings",
        Some(json!({"share_max_concurrency": 65})),
        true,
    );
    assert_eq!(settings_65.status, 400);

    let settings = http_request(
        &console,
        "PATCH",
        "/api/console/share-management/settings",
        Some(json!({"share_max_concurrency": 32})),
        true,
    );
    assert_eq!(settings.status, 200);
    assert_eq!(settings.json()["share_max_concurrency"], 32);

    let settings_conflict = http_request(
        &console,
        "PATCH",
        "/api/console/share-management/settings",
        Some(json!({"share_max_concurrency": 16, "expected_share_max": 63})),
        true,
    );
    assert_eq!(settings_conflict.status, 409);

    broker.set_advance(Advance::RevokeOnly);
    let (_, created, _) = create_over_http(&console, "rev");
    let key_id = created["key"]["id"].as_str().unwrap().to_string();

    let enabled = http_request(
        &console,
        "PATCH",
        &format!("/api/console/share-management/keys/{key_id}"),
        Some(json!({"enabled": false, "expected_row_version": 1})),
        true,
    );
    assert_eq!(enabled.status, 400);
    assert_eq!(enabled.json()["error"], "enabled_not_writable");

    let patch_conflict = http_request(
        &console,
        "PATCH",
        &format!("/api/console/share-management/keys/{key_id}"),
        Some(json!({"name": "again", "expected_row_version": 999})),
        true,
    );
    assert_eq!(patch_conflict.status, 409);

    *broker.fail.lock().unwrap() = Some("broker down".to_string());
    *broker.emergency.lock().unwrap() = json!({"state": "stopped"});
    let rotate = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/rotate"),
        Some(json!({"expected_row_version": 1})),
        true,
    );
    assert_eq!(rotate.status, 503);
    assert!(rotate.json()["error"]
        .as_str()
        .unwrap()
        .contains("revoking operation not applied online"));

    let revoke = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/revoke-and-terminate"),
        Some(json!({"expected_row_version": 2})),
        true,
    );
    assert_eq!(revoke.status, 503);
    assert!(revoke.json()["error"]
        .as_str()
        .unwrap()
        .contains("revoking operation not applied online"));

    let status = http_request(
        &console,
        "GET",
        "/api/console/share-management/status",
        None,
        false,
    );
    assert_eq!(status.json()["security_pending"], 2);
    assert_eq!(status.json()["blocked"], true);
}

#[test]
fn http_revoke_and_terminate_answers_every_count() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::RevokeOnly));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker);

    let (_, created, _) = create_over_http(&console, "revoke");
    let key_id = created["key"]["id"].as_str().unwrap().to_string();
    let response = http_request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/revoke-and-terminate"),
        Some(json!({"expected_row_version": 1})),
        true,
    );
    assert_eq!(response.status, 200);
    let body = response.json();
    assert_eq!(body["key"]["enabled"], false);
    assert_eq!(body["revoked"], true);
    assert_eq!(body["terminated"], 2);
    assert_eq!(body["released"], 3);
    assert_eq!(body["aborted"], 4);
    assert_eq!(body["upstream_closed"], 5);
    assert!(body["note"].as_str().unwrap().contains("不可撤回"));
}

#[test]
fn http_backend_login_cancel_fails_closed_without_a_control_token() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker);

    let response = http_request(
        &console,
        "POST",
        "/api/console/share-management/backend-login-cancel",
        Some(json!({})),
        true,
    );
    assert_eq!(response.status, 503);
    let body = response.json();
    assert_eq!(body["cancelled"], false);
    assert_eq!(body["state"], Value::Null);
    assert_eq!(body["error"], "control_token_absent");
}

#[test]
fn rust_writes_a_python_created_store_without_migrating_the_data_format() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let reads = support::reads_fixture();
    let store_path = support::seed_share_store(&temp, &reads["shareStatus"]["storeSeed"]);
    let key_file = temp.path().join("share-secret.key");
    let fixture_key = reads["metadata"]["fixtureKeyHex"].as_str().unwrap();
    std::fs::write(&key_file, fixture_key.as_bytes()).expect("fixture key");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&key_file, std::fs::Permissions::from_mode(0o600))
            .expect("key file mode");
    }
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let writer = ShareWriteStore::new(store_path.clone(), Some(key_file.clone()), broker);

    let created = writer
        .create_key("rust-created", None, None)
        .expect("create");
    assert_eq!(created["published"], true);

    let store = open_store(&store_path, &key_file);
    assert_eq!(store.schema_version(), 3);
    assert_eq!(store.projection_schema_version(), 2);
    let rows = store.list_keys(false).expect("list keys");
    assert!(rows.iter().any(|row| row["name"] == "rust-created"));
    assert!(rows.iter().any(|row| row["name"] == "alpha"));
    drop(store);

    let read = ShareReadStore::with_options(store_path, None, Some(key_file));
    let keys = read.keys().expect("read face");
    assert!(keys["keys"]
        .as_array()
        .unwrap()
        .iter()
        .any(|row| row["name"] == "rust-created"));
}
