//! Acceptance 5: the S5a read faces never write.
//!
//! These tests snapshot the temporary data directory around the six read
//! faces: no SQLite database is created, an existing database keeps its exact
//! bytes and gains no journal/WAL sidecar, and no ledger or log file appears.

mod support;

use std::collections::BTreeSet;
use std::path::Path;

use rusqlite::types::Value as SqlValue;
use serde_json::Value;

use support::*;

fn entries(root: &Path) -> BTreeSet<String> {
    std::fs::read_dir(root)
        .expect("read temp dir")
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .collect()
}

fn json_to_sql(value: &Value) -> SqlValue {
    match value {
        Value::Null => SqlValue::Null,
        Value::Bool(value) => SqlValue::Integer(i64::from(*value)),
        Value::Number(number) => match number.as_i64() {
            Some(value) => SqlValue::Integer(value),
            None => SqlValue::Real(number.as_f64().unwrap_or_default()),
        },
        Value::String(value) => SqlValue::Text(value.clone()),
        other => SqlValue::Text(other.to_string()),
    }
}

fn seeded_operations(temp: &um_test_harness::TempDataDir, fixture: &Value) -> std::path::PathBuf {
    let path = temp.path().join("seeded-console.sqlite3");
    let connection = rusqlite::Connection::open(&path).expect("open seeded state");
    let seeded = &fixture["operations"]["seeded"];
    for ddl in seeded["schema"].as_array().expect("schema") {
        connection
            .execute_batch(ddl.as_str().expect("ddl"))
            .expect("create schema");
    }
    connection
        .execute(
            "INSERT OR REPLACE INTO console_meta(key, value) VALUES('schema_version', ?)",
            [seeded["metaVersion"].as_str().expect("meta version")],
        )
        .expect("insert meta");
    for row in seeded["rows"].as_array().expect("rows") {
        let values: Vec<SqlValue> = row
            .as_array()
            .expect("row")
            .iter()
            .map(json_to_sql)
            .collect();
        connection
            .execute(
                seeded["insertSql"].as_str().expect("insert sql"),
                rusqlite::params_from_iter(values),
            )
            .expect("insert row");
    }
    path
}

fn get(addr: std::net::SocketAddr, target: &str) -> u64 {
    client_request(addr, &ClientRequest::get(target))
        .expect("request")
        .status
        .into()
}

#[test]
fn read_faces_do_not_create_the_console_state_database() {
    let temp = temp();
    let console = start_console(&temp);
    let before = entries(temp.path());
    for target in [
        "/health",
        "/api/summary",
        "/api/console/services",
        "/api/console/health",
        "/api/console/actions",
        "/api/console/operations",
    ] {
        assert_eq!(get(console.addr, target), 200, "{target}");
    }
    assert_eq!(entries(temp.path()), before);
    assert!(!temp.path().join("console.sqlite3").exists());
    assert!(!temp.path().join("locks").exists());
}

#[test]
fn operations_reader_does_not_modify_an_existing_database() {
    let fixture = fixture();
    let temp = temp();
    let path = seeded_operations(&temp, &fixture);
    let before_bytes = std::fs::read(&path).expect("read seeded bytes");
    let before_entries = entries(temp.path());
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.console_state_file = Some(path.clone());
    });
    assert_eq!(get(console.addr, "/api/console/operations"), 200);
    assert_eq!(get(console.addr, "/api/console/operations/op-a"), 200);
    drop(console);
    assert_eq!(std::fs::read(&path).expect("read bytes"), before_bytes);
    assert_eq!(entries(temp.path()), before_entries);
    for suffix in ["-journal", "-wal", "-shm"] {
        assert!(
            !temp
                .path()
                .join(format!("seeded-console.sqlite3{suffix}"))
                .exists(),
            "read face created a {suffix} sidecar"
        );
    }
}

#[test]
fn read_faces_do_not_write_ledgers_or_logs() {
    let temp = temp();
    let ledger = temp.path().join("usage-share-backend.jsonl");
    std::fs::write(&ledger, b"{\"status\":\"ok\"}\n").expect("write sentinel ledger");
    let before = std::fs::read(&ledger).expect("read sentinel");
    let console = start_console(&temp);
    let before_entries = entries(temp.path());
    for target in [
        "/health",
        "/api/summary",
        "/api/console/services",
        "/api/console/health",
        "/api/console/actions",
        "/api/console/operations",
    ] {
        assert_eq!(get(console.addr, target), 200, "{target}");
    }
    drop(console);
    assert_eq!(std::fs::read(&ledger).expect("read sentinel"), before);
    assert_eq!(entries(temp.path()), before_entries);
}

#[test]
fn operations_reader_does_not_create_a_lock_directory() {
    let temp = temp();
    let console = start_console(&temp);
    assert_eq!(get(console.addr, "/api/console/operations"), 200);
    assert!(!temp.path().join("locks").exists());
    assert!(!temp.path().join("console.sqlite3").exists());
}

fn snapshot(root: &Path) -> BTreeSet<(String, Vec<u8>)> {
    std::fs::read_dir(root)
        .expect("read temp dir")
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.path().is_file())
        .map(|entry| {
            (
                entry.file_name().to_string_lossy().into_owned(),
                std::fs::read(entry.path()).expect("read file"),
            )
        })
        .collect()
}

#[test]
fn share_read_faces_do_not_write_the_store_ledger_or_logs() {
    let fixture = reads_fixture();
    let temp = temp();
    seed_share_store(&temp, &fixture["shareStatus"]["storeSeed"]);
    let backend = temp.path().join("share-backend");
    std::fs::create_dir_all(&backend).expect("backend dir");
    std::fs::write(
        backend.join("usage-share-backend.jsonl"),
        fixture["shareUsage"]["ledger"].as_str().expect("ledger"),
    )
    .expect("ledger");
    let control = temp.path().join("control-token");
    std::fs::write(&control, b"fixture-control-token").expect("control token");
    let security = temp.path().join("security.json");
    std::fs::write(
        &security,
        b"{\"format\":\"um-share-security-state-v1\",\"generation_floor\":1,\"records\":[]}",
    )
    .expect("security state");
    let key = temp.path().join("share-secret.key");
    let hex = fixture["metadata"]["fixtureKeyHex"]
        .as_str()
        .expect("fixture key");
    let mut bytes = Vec::with_capacity(hex.len() / 2);
    for pair in hex.as_bytes().chunks(2) {
        let high = (pair[0] as char).to_digit(16).expect("hex");
        let low = (pair[1] as char).to_digit(16).expect("hex");
        bytes.push((high * 16 + low) as u8);
    }
    std::fs::write(&key, bytes).expect("share secret key");
    let before = snapshot(temp.path());
    let mut seams = StaticShareSeams::new();
    seams.broker_snapshot = Some(serde_json::json!({
        "name": "broker-client",
        "state": "ok",
        "broker": {"version": "2.0.0", "pid": 4242},
    }));
    let console = start_console_with_seams(
        &temp,
        |config| {
            config.share_store_file = Some(temp.path().join("share.db"));
            config.share_backend_dir = Some(backend.clone());
            config.control_token_file = Some(control.clone());
            config.security_state_file = Some(security.clone());
            config.share_secret_key_file = Some(key.clone());
        },
        std::sync::Arc::new(seams),
    );
    console.state().session.issue("fixture-session".to_string());
    let targets = [
        "/api/console/share-management/status",
        "/api/console/share-management/keys",
        "/api/console/share-management/keys/key-active-1/secret",
        "/api/console/share-usage",
        "/api/console/share-usage/requests",
        "/api/console/cloud-ops",
        "/api/console/share-management/display-session",
        "/api/console/share-management/display-state",
    ];
    for target in targets {
        assert_eq!(get(console.addr, target), 200, "{target}");
    }
    for target in [
        "/api/share/status",
        "/api/share/keys",
        "/api/share/keys/key-active-1",
        "/api/share/keys/key-active-1/secret",
        "/api/share/operations/op-1",
        "/api/share/usage",
        "/api/share/usage/requests",
    ] {
        let response = client_request(
            console.addr,
            &ClientRequest::get(target).header("X-Admin-Control", "fixture-control-token"),
        )
        .expect("request");
        assert_eq!(response.status, 200, "{target}");
    }
    drop(console);
    assert_eq!(snapshot(temp.path()), before);
    for sidecar in ["share.db-journal", "share.db-wal", "share.db-shm"] {
        assert!(
            !temp.path().join(sidecar).exists(),
            "read face created a {sidecar} sidecar"
        );
    }
}

#[test]
fn share_read_faces_never_expose_absolute_data_paths() {
    let fixture = reads_fixture();
    let temp = temp();
    seed_share_store(&temp, &fixture["shareStatus"]["storeSeed"]);
    let backend = temp.path().join("share-backend");
    std::fs::create_dir_all(&backend).expect("backend dir");
    std::fs::write(
        backend.join("usage-share-backend.jsonl"),
        fixture["shareUsage"]["ledger"].as_str().expect("ledger"),
    )
    .expect("ledger");
    let console = start_console_with_seams(
        &temp,
        |config| {
            config.share_store_file = Some(temp.path().join("share.db"));
            config.share_backend_dir = Some(backend.clone());
        },
        std::sync::Arc::new(StaticShareSeams::new()),
    );
    let absolute = temp.path().to_string_lossy().to_string();
    for target in [
        "/api/console/share-management/status",
        "/api/console/share-management/keys",
        "/api/console/share-usage",
        "/api/console/share-usage/requests",
        "/api/console/cloud-ops",
        "/api/console/share-management/display-session",
        "/api/console/share-management/display-state",
    ] {
        let response = client_request(console.addr, &ClientRequest::get(target)).expect("request");
        let body = String::from_utf8_lossy(&response.body);
        assert!(!body.contains(&absolute), "{target} leaked a data path");
    }
}
