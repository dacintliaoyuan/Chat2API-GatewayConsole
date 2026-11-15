//! AC1: SQLite schema 3, byte-compatible with `share/store.py`.
//!
//! The DDL text is the Python store's own DDL: a Rust store and a Python store
//! must open each other's file without a migration step. The local DB marker
//! (`SCHEMA_VERSION = 3`) is deliberately separate from the share-side
//! projection marker (`PROJECTION_SCHEMA_VERSION = 2`), exactly like Python.
//!
//! Migration v1 -> v2 -> v3 runs inside ONE transaction and never rewrites key
//! data: v1 rows keep the legacy per-key limit 1, migrated stores keep the
//! legacy global limit 2, and the ciphertext columns stay NULL (hash-only rows
//! stay under the legacy contract until a manual rotation).

use std::path::Path;

use rusqlite::Connection;

use crate::error::StoreError;

pub const SCHEMA_VERSION: i64 = 3;
pub const PROJECTION_SCHEMA_VERSION: i64 = 2;

pub const LEGACY_KEY_MAX_CONCURRENCY: i64 = 1;
pub const LEGACY_SHARE_MAX_CONCURRENCY: i64 = 2;
pub const NEW_DEPLOYMENT_SHARE_MAX_CONCURRENCY: i64 = 64;
pub const NEW_KEY_DEFAULT_MAX_CONCURRENCY: i64 = 16;
pub const KEY_MAX_CONCURRENCY_MIN: i64 = 1;
pub const KEY_MAX_CONCURRENCY_MAX: i64 = 48;
pub const SHARE_MAX_CONCURRENCY_MIN: i64 = 1;
pub const SHARE_MAX_CONCURRENCY_MAX: i64 = 64;

/// The Python store's DDL, statement for statement.
pub const SCHEMA_DDL: &str = "
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
    key_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    secret_version INTEGER NOT NULL DEFAULT 1,
    masked_key TEXT NOT NULL,
    secret_blob BLOB,
    secret_algo TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(name, deleted_at)
);
CREATE TABLE IF NOT EXISTS ops (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(idempotency_key)
);
CREATE TABLE IF NOT EXISTS configgen (
    schema_version INTEGER NOT NULL,
    config_generation INTEGER NOT NULL,
    applied_generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS security (
    record_id TEXT PRIMARY KEY,
    key_id TEXT,
    generation INTEGER NOT NULL,
    action TEXT NOT NULL,
    secret_version INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    recorded_at TEXT NOT NULL,
    applied_at TEXT,
    applied_generation INTEGER,
    instance_id TEXT,
    remote_confirmed_at TEXT,
    remote_receipt TEXT
);
CREATE INDEX IF NOT EXISTS idx_security_generation
    ON security(generation);
CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_active_name
    ON keys(name) WHERE deleted_at IS NULL;
";

/// Open a connection and bring its schema to [`SCHEMA_VERSION`].
pub fn open_connection(path: &Path) -> Result<Connection, StoreError> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)
                .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
        }
    }
    let connection = Connection::open(path)
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    connection
        .execute("PRAGMA foreign_keys=ON", [])
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    initialize(&connection)?;
    Ok(connection)
}

/// Create/migrate the schema inside ONE transaction.
pub fn initialize(connection: &Connection) -> Result<(), StoreError> {
    connection
        .execute_batch("BEGIN")
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    let result = initialize_inside(connection);
    match result {
        Ok(()) => connection
            .execute_batch("COMMIT")
            .map_err(|error| StoreError::Other(format!("cannot open share store: {error}"))),
        Err(error) => {
            let _ = connection.execute_batch("ROLLBACK");
            Err(error)
        }
    }
}

fn initialize_inside(connection: &Connection) -> Result<(), StoreError> {
    connection
        .execute_batch(SCHEMA_DDL)
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    let version: Option<String> = connection
        .query_row(
            "SELECT value FROM meta WHERE key='schema_version'",
            [],
            |row| row.get(0),
        )
        .map(Some)
        .or_else(|error| match error {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    match version.as_deref().and_then(|value| value.parse::<i64>().ok()) {
        None => {
            connection
                .execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    [SCHEMA_VERSION.to_string()],
                )
                .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
            connection
                .execute(
                    "INSERT INTO meta(key,value) VALUES('share_max_concurrency',?)",
                    [NEW_DEPLOYMENT_SHARE_MAX_CONCURRENCY.to_string()],
                )
                .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
        }
        Some(SCHEMA_VERSION) => {}
        Some(2) => upgrade_v2_to_v3(connection)?,
        Some(1) => {
            upgrade_v1_to_v2(connection)?;
            upgrade_v2_to_v3(connection)?;
        }
        Some(version) => {
            return Err(StoreError::Other(format!(
                "unsupported schema_version {version} (supported {SCHEMA_VERSION})"
            )))
        }
    }
    let has_configgen: bool = connection
        .query_row("SELECT COUNT(*) FROM configgen", [], |row| {
            row.get::<_, i64>(0)
        })
        .map(|count| count > 0)
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    if !has_configgen {
        connection
            .execute(
                "INSERT INTO configgen(schema_version,config_generation,applied_generation) VALUES(?,?,0)",
                [SCHEMA_VERSION, 1],
            )
            .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    }
    Ok(())
}

fn upgrade_v1_to_v2(connection: &Connection) -> Result<(), StoreError> {
    let columns = table_columns(connection, "keys")?;
    if !columns.iter().any(|name| name == "max_concurrency") {
        connection
            .execute(
                &format!(
                    "ALTER TABLE keys ADD COLUMN max_concurrency INTEGER NOT NULL DEFAULT {LEGACY_KEY_MAX_CONCURRENCY}"
                ),
                [],
            )
            .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    }
    connection
        .execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('share_max_concurrency',?)",
            [LEGACY_SHARE_MAX_CONCURRENCY.to_string()],
        )
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    connection
        .execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            [SCHEMA_VERSION.to_string()],
        )
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    connection
        .execute(
            "UPDATE configgen SET schema_version=?",
            [SCHEMA_VERSION],
        )
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    Ok(())
}

fn upgrade_v2_to_v3(connection: &Connection) -> Result<(), StoreError> {
    let columns = table_columns(connection, "keys")?;
    if !columns.iter().any(|name| name == "secret_blob") {
        connection
            .execute("ALTER TABLE keys ADD COLUMN secret_blob BLOB", [])
            .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    }
    if !columns.iter().any(|name| name == "secret_algo") {
        connection
            .execute("ALTER TABLE keys ADD COLUMN secret_algo TEXT", [])
            .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    }
    connection
        .execute(
            "UPDATE meta SET value=? WHERE key='schema_version'",
            [SCHEMA_VERSION.to_string()],
        )
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    connection
        .execute(
            "UPDATE configgen SET schema_version=?",
            [SCHEMA_VERSION],
        )
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    Ok(())
}

/// Column names of one table, in declaration order.
pub fn table_columns(connection: &Connection, table: &str) -> Result<Vec<String>, StoreError> {
    let mut statement = connection
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| StoreError::Other(format!("cannot open share store: {error}")))
}
