//! S6a AC7: the data format is unchanged and the write face falls back to Python.

use std::sync::Arc;

use serde_json::json;
use um_console::ShareReadStore;
use um_test_harness::TempDataDir;

mod support;

use support::write::{
    http_request, make_writer, open_store, start_write_console, Advance, FakeBroker,
};

#[test]
fn rust_writes_a_python_created_store_without_migrating_or_adding_operations() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
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
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let writer =
        um_console::ShareWriteStore::new(store_path.clone(), Some(key_file.clone()), broker);

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

    // The same file remains readable by the read-only Python-shaped face.
    let read = ShareReadStore::with_options(store_path, None, Some(key_file));
    assert!(read
        .keys()
        .expect("read face")
        .to_string()
        .contains("rust-created"));

    // The write face never creates the console operation database.
    assert!(!temp.path().join("console-state.sqlite3").exists());
}

#[test]
fn an_unwired_publication_is_honest_and_the_candidate_never_switches_production() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let (writer, _store_path, _key_file) =
        make_writer(&temp, Arc::new(FakeBroker::new(Advance::Always)));
    let created = writer.create_key("alpha", None, None).expect("create");
    assert_eq!(created["published"], true);

    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let key_file = support::write::key_file(&temp);
    let writer = um_console::ShareWriteStore::new(
        temp.path().join("share.db"),
        Some(key_file),
        Arc::new(um_console::UnwiredWriteSeams),
    );
    let created = writer
        .create_key("alpha", None, None)
        .expect("safe direction answers honestly");
    assert_eq!(created["published"], false);
    assert!(created["publication_error"].as_str().is_some());
    let key_id = created["key"]["id"].as_str().unwrap().to_string();
    let failed = writer
        .revoke_and_terminate_key(&key_id, 1, None)
        .expect_err("revocation direction fails closed");
    assert_eq!(failed.status(), 503);

    // A candidate write face serves the same JSON over its ephemeral loopback
    // port; it never binds 8002 or any governed production port.
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker);
    assert_ne!(console.addr.port(), 8002);
    let response = http_request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": "alpha"})),
        true,
    );
    assert_eq!(response.status, 201);
    assert!(response.json()["plaintext"].as_str().is_some());
}
