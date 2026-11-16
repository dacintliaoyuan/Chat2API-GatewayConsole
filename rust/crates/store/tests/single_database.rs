//! AC6/AC7: one SQLite file, no second database, no data migration.
//!
//! The Rust store opens the caller's file in place: it creates no
//! `share-store-rust.db`, no second schema and no data copy, so a rollback to
//! the Python store reads the same bytes.

use std::path::PathBuf;

use um_store::ShareStore;

fn test_key() -> Vec<u8> {
    (0..32).collect()
}

#[test]
fn the_store_uses_only_the_caller_chosen_database_file() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("share-store.db");
    {
        let store = ShareStore::open_with_key(&path, test_key()).unwrap();
        store
            .connection()
            .execute(
                "INSERT INTO keys(key_id,name,secret_hash,secret_version,masked_key,\
                 enabled,created_at,updated_at,deleted_at,row_version) \
                 VALUES('python-row','legacy','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',\
                 1,'legacy-****',1,'2026-11-15T00:00:00.000000Z','2026-11-15T00:00:00.000000Z',NULL,1)",
                [],
            )
            .unwrap();
    }
    // Re-opening the SAME file (the rollback reader) sees the Python-shaped
    // row: the Rust store never copied or migrated the data elsewhere.
    let store = ShareStore::open_with_key(&path, test_key()).unwrap();
    assert_eq!(store.get_key("python-row").unwrap()["name"], "legacy");
    assert!(
        store
            .connection()
            .query_row(
                "SELECT COUNT(*) FROM keys WHERE key_id='python-row'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap()
            == 1
    );
    drop(store);
    let names: Vec<String> = std::fs::read_dir(directory.path())
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| {
            !name.ends_with("-journal") && !name.ends_with("-wal") && !name.ends_with("-shm")
        })
        .collect();
    assert_eq!(names, vec!["share-store.db".to_string()]);
    assert!(!PathBuf::from(directory.path())
        .join("share-store-rust.db")
        .exists());
}

#[test]
fn opening_the_same_database_twice_keeps_one_schema() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("share-store.db");
    let first = ShareStore::open_with_key(&path, test_key()).unwrap();
    drop(first);
    let second = ShareStore::open_with_key(&path, test_key()).unwrap();
    let count: i64 = second
        .connection()
        .query_row("SELECT COUNT(*) FROM configgen", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 1);
}
