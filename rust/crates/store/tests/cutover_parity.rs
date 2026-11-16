//! #72: cutover data parity against the Python-owned production files.
//!
//! Python writes ONE isolated data directory (schema 3 store, projection, ledger,
//! token and browser profile lock) with the real owners; the Rust side reads that
//! same directory.  The reverse direction is exercised too: Rust appends a ledger
//! row, creates a store and publishes a projection, then the Python probe reads
//! them back.  No production path, real browser or UM endpoint is touched.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Instant;

use rusqlite::Connection;
use serde_json::{json, Map, Value};
use um_gateway::ledger::{Ledger, UsageRecord};
use um_gateway::login::profile::read_profile_lock_owner;
use um_gateway::login::token::TokenStore;
use um_ops::ledger::{read_share_requests, read_share_usage};
use um_ops::{Clock, RequestOptions, UsageOptions};
use um_store::ShareStore;
use um_store::{fingerprint, parse_projection_payload, AllowPath, ProtectedProjectionWriter};

fn fixture_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/cutover_parity")
}

fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/fixtures/rust_cutover_parity_rev1.json")
}

fn fixture() -> Value {
    serde_json::from_str(&fs::read_to_string(fixture_path()).expect("read fixture"))
        .expect("parse fixture")
}

fn data_path(relative: &str) -> PathBuf {
    fixture_dir().join(relative)
}

fn probe_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/cutover_parity_probe.py")
}

fn normalize_checked_at(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut output = Map::new();
            for (key, item) in map {
                output.insert(
                    key.clone(),
                    if key == "checkedAt" {
                        Value::String("<checkedAt>".to_string())
                    } else {
                        normalize_checked_at(item)
                    },
                );
            }
            Value::Object(output)
        }
        Value::Array(items) => Value::Array(items.iter().map(normalize_checked_at).collect()),
        other => other.clone(),
    }
}

fn python_probe(args: &[&str]) -> Value {
    let output = Command::new("python")
        .arg(probe_path())
        .args(args)
        .output()
        .expect("run Python parity probe");
    assert!(
        output.status.success(),
        "Python parity probe failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("parse Python parity probe output")
}

fn python_written_store() -> (tempfile::TempDir, PathBuf) {
    let temp = tempfile::tempdir().expect("temp data dir");
    let db = temp.path().join("share-store.db");
    let sql = fs::read_to_string(data_path("share-store.sql")).expect("read SQL dump");
    let connection = Connection::open(&db).expect("open temp store");
    connection
        .execute_batch(&sql)
        .expect("replay Python SQL dump");
    drop(connection);
    (temp, db)
}

fn rust_store_view(store: &mut ShareStore) -> Value {
    let generation = store.generation().expect("generation");
    let keys = store.list_keys(true).expect("keys");
    let operations: Vec<Value> = store
        .operations(None, None)
        .expect("operations")
        .into_iter()
        .map(|operation| {
            json!({
                "id": operation.id,
                "kind": operation.kind,
                "idempotency_key": operation.idempotency_key,
                "status": operation.status,
                "payload": operation.payload,
                "created_at": operation.created_at,
            })
        })
        .collect();
    let records = store.security_records().expect("security records");
    let floor = store.security_floor().expect("security floor");
    let mut state = store.security_state().expect("security state");
    if let Some(object) = state.as_object_mut() {
        object.remove("exported_at");
    }
    let projection = store.render_projection().expect("projection");
    let share_max = store.share_max_concurrency().expect("share limit");
    json!({
        "generation": generation,
        "keys": keys,
        "operations": operations,
        "security": {"records": records, "floor": floor, "state": state},
        "projection": projection,
        "share_max_concurrency": share_max,
    })
}

fn assert_read_only(path: &Path) {
    let before = fs::read(path).expect("read before");
    let mut store = ShareStore::open_read_only(path).expect("open read-only");
    let _ = rust_store_view(&mut store);
    drop(store);
    let after = fs::read(path).expect("read after");
    assert_eq!(before, after, "the read-only parity path rewrote the store");
}

struct FixedClock;

impl Clock for FixedClock {
    fn now_ms(&self) -> f64 {
        1_794_873_600_000.0
    }

    fn utc_iso_seconds(&self) -> String {
        "2026-11-16T00:00:00+00:00".to_string()
    }
}

fn rust_ledger_views(path: &Path) -> Value {
    let summary = read_share_usage(path, &UsageOptions::default(), &FixedClock).expect("summary");
    let requests =
        read_share_requests(path, &RequestOptions::default(), &FixedClock).expect("requests");
    json!({
        "summary": normalize_checked_at(&summary),
        "requests": normalize_checked_at(&requests),
    })
}

fn python_ledger_views(path: &Path) -> Value {
    python_probe(&[
        "ledger",
        "--path",
        path.to_str().expect("ledger path"),
        "--now",
        "1794873600",
    ])
}

fn stable_ledger_row(row: &Value) -> Value {
    json!({
        "service": row["service"],
        "model": row["model"],
        "stream": row["stream"],
        "status": row["status"],
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cached_input_tokens": row["cached_input_tokens"],
        "cache_report_status": row["cache_report_status"],
        "reasoning_tokens": row["reasoning_tokens"],
        "total_tokens": row["total_tokens"],
        "estimated": row["estimated"],
        "usage_source": row["usage_source"],
        "protocol": row["protocol"],
        "key_id": row["key_id"],
        "request_id": row["request_id"],
        "attribution": row["attribution"],
    })
}

#[test]
fn the_python_written_store_is_read_without_migration() {
    let fixture = fixture();
    let (_temp, db) = python_written_store();
    assert_read_only(&db);
    let mut store = ShareStore::open_read_only(&db).expect("open read-only");
    assert_eq!(rust_store_view(&mut store), fixture["python"]["store"]);
}

#[test]
fn the_python_written_projection_is_parsed_and_rewritable() {
    let fixture = fixture();
    let payload: Value = serde_json::from_str(
        &fs::read_to_string(data_path("run/share-projection.json")).expect("read projection"),
    )
    .expect("parse projection");
    let expected = &fixture["python"]["projection"];
    assert_eq!(
        parse_projection_payload(&payload).expect("parse"),
        expected["parsed"]
    );
    assert_eq!(fingerprint(&payload), expected["fingerprint"]);
    let generation = payload["config_generation"].as_i64().expect("generation");
    let temp = tempfile::tempdir().expect("temp projection dir");
    let path = temp.path().join("share-projection.json");
    let mut writer = ProtectedProjectionWriter::new(path.clone(), AllowPath(path.clone()))
        .expect("writer baseline");
    assert_eq!(
        writer
            .write(
                &payload,
                generation,
                expected["fingerprint"].as_str().expect("fingerprint"),
            )
            .expect("write"),
        expected["fingerprint"]
    );
    let reread: Value = serde_json::from_str(&fs::read_to_string(&path).expect("read projection"))
        .expect("parse written projection");
    assert_eq!(reread, payload);
}

#[test]
fn the_python_written_token_and_profile_are_read_by_rust() {
    let fixture = fixture();
    let token = TokenStore::new(
        data_path("share-backend/token.json"),
        "http://127.0.0.1:8000",
    )
    .load()
    .expect("load Python token");
    assert_eq!(
        json!({
            "url": token.url,
            "token": token.token,
            "api_key": token.api_key,
            "saved_at": token.saved_at,
        }),
        fixture["python"]["token"]
    );

    let temp = tempfile::tempdir().expect("temp profile dir");
    let profile_dir = temp.path().join("edge-profile");
    fs::create_dir_all(&profile_dir).expect("profile dir");
    let lock_path = profile_dir.join("login-browser.lock");
    fs::copy(
        data_path("share-backend/edge-profile/login-browser.lock"),
        &lock_path,
    )
    .expect("copy profile lock");
    let owner = read_profile_lock_owner(&profile_dir).expect("profile owner");
    assert_eq!(owner, fixture["python"]["profile"]["owner"]);
    assert_eq!(
        hex::encode(fs::read(&lock_path).expect("read profile lock")),
        fixture["python"]["profile"]["lock_hex"]
    );
}

#[test]
fn the_python_written_ledger_is_read_by_rust() {
    let fixture = fixture();
    let ledger = data_path("share-backend/usage-share-backend.jsonl");
    assert_eq!(rust_ledger_views(&ledger), fixture["python"]["ledger"]);
}

#[test]
fn a_rust_ledger_append_is_read_by_python_and_survives_a_rollback() {
    let temp = tempfile::tempdir().expect("temp ledger dir");
    let ledger_path = temp
        .path()
        .join("var/lib/um-share-console/share-backend/usage-share-backend.jsonl");
    let ledger = Ledger::new(Some(ledger_path.clone()));
    let usage = json!({
        "prompt_tokens": 21,
        "completion_tokens": 3,
        "total_tokens": 24,
        "prompt_tokens_details": {"cached_tokens": 1},
    });
    ledger.record(UsageRecord {
        model: "cutover-append-model",
        stream: false,
        started_at: Instant::now(),
        usage: Some(&usage),
        input_estimate: 0,
        output_estimate: 0,
        status: "ok",
        protocol: Some("chat"),
        key_id: Some("key-alpha"),
        request_id: Some("rust-append-1"),
        attribution: "verified",
    });
    let bytes = fs::read(&ledger_path).expect("read appended ledger");
    assert_eq!(bytes.iter().filter(|byte| **byte == b'\n').count(), 1);
    let row: Value = serde_json::from_slice(&bytes).expect("parse appended row");
    assert_eq!(
        stable_ledger_row(&row),
        json!({
            "service": "chat2api",
            "model": "cutover-append-model",
            "stream": false,
            "status": "ok",
            "input_tokens": 21,
            "output_tokens": 3,
            "cached_input_tokens": 1,
            "cache_report_status": "reported",
            "reasoning_tokens": 0,
            "total_tokens": 24,
            "estimated": false,
            "usage_source": "upstream",
            "protocol": "chat",
            "key_id": "key-alpha",
            "request_id": "rust-append-1",
            "attribution": "verified",
        })
    );

    let rust = rust_ledger_views(&ledger_path);
    assert_eq!(rust["summary"]["requests"], 1);
    assert_eq!(rust["summary"]["keys"][0]["key_id"], "key-alpha");
    assert_eq!(rust["summary"]["keys"][0]["tokenUsage"]["inputTokens"], 21);

    let python = python_ledger_views(&ledger_path);
    assert_eq!(python["requests"], 1);
    assert_eq!(python["rows"][0]["keyId"], "key-alpha");
    assert_eq!(python["rows"][0]["usageSource"], "upstream");
    assert_eq!(python["rows"][0]["inputTokens"], 21);
    assert_eq!(python["rows"][0]["cacheTokens"], 1);
    assert_eq!(python["keys"][0]["tokenUsage"]["completeRequests"], 1);

    // A rollback keeps the SAME data directory: the row must still be there
    // for both readers, and neither reader may rewrite the ledger.
    let before = fs::read(&ledger_path).expect("read before rollback");
    let rollback = temp.path().join("rollback");
    fs::create_dir_all(&rollback).expect("rollback dir");
    let rollback_ledger = rollback.join("usage-share-backend.jsonl");
    fs::copy(&ledger_path, &rollback_ledger).expect("copy ledger");
    assert_eq!(before, fs::read(&ledger_path).expect("read after rollback"));
    let python_after = python_ledger_views(&rollback_ledger);
    assert_eq!(python_after["requests"], 1);
    assert_eq!(python_after["rows"][0]["requestId"], "rust-append-1");
    let rust_after = rust_ledger_views(&rollback_ledger);
    assert_eq!(rust_after["summary"]["requests"], 1);
}

#[test]
fn a_rust_written_store_and_projection_are_read_by_python() {
    let temp = tempfile::tempdir().expect("temp store dir");
    let db = temp.path().join("share-store.db");
    let mut store = ShareStore::open_with_key(&db, (0..32).collect()).expect("create Rust store");
    let created = store
        .create_key(
            "rust-created",
            Some("um-share-rust-0123456789abcdef"),
            None,
            Some(16),
        )
        .expect("create Rust key");
    assert_eq!(created["plaintext_viewable"], true);
    let projection = store.render_projection().expect("Rust projection");
    let generation = projection["config_generation"]
        .as_i64()
        .expect("projection generation");
    let projection_path = temp.path().join("share-projection.json");
    let mut writer =
        ProtectedProjectionWriter::new(projection_path.clone(), AllowPath(projection_path.clone()))
            .expect("projection writer");
    let written = writer
        .write(&projection, generation, &fingerprint(&projection))
        .expect("write Rust projection");
    assert_eq!(written, fingerprint(&projection));
    drop(store);

    let python_store = python_probe(&["store", "--path", db.to_str().expect("store path")]);
    let keys = python_store["keys"].as_array().expect("Python keys");
    assert_eq!(keys.len(), 1);
    assert_eq!(keys[0]["name"], "rust-created");
    assert_eq!(keys[0]["plaintext_viewable"], true);
    assert_eq!(keys[0]["max_concurrency"], 16);
    assert_eq!(python_store["generation"]["config_generation"], 2);

    let python_projection = python_probe(&[
        "projection",
        "--path",
        projection_path.to_str().expect("projection path"),
    ]);
    assert_eq!(python_projection["fingerprint"], written);
    assert_eq!(python_projection["parsed"], projection);
}
