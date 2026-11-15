//! Read-only console operation records.
//!
//! `console/operations.py::OperationManager.list_operations` / `get_operation`
//! read `console-state.sqlite3` and render one row. The Rust read face opens
//! the database READ-ONLY and never creates the schema, marks operations
//! interrupted, writes a lock directory or appends a log. An absent database is
//! the legal first-run case and answers the same empty list Python answers
//! after creating the schema.

use std::path::PathBuf;

use rusqlite::{Connection, OpenFlags, Row};
use serde_json::{json, Map, Value};

use crate::error::ConsoleError;

pub const CONSOLE_STATE_SCHEMA_VERSION: i64 = 1;
pub const DEFAULT_OPERATION_LIMIT: i64 = 50;
pub const MAX_OPERATION_LIMIT: i64 = 200;
#[derive(Debug, Clone)]
pub struct OperationsReader {
    path: Option<PathBuf>,
}

impl OperationsReader {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self { path }
    }

    pub fn list(&self, raw_target: &str) -> Result<Value, ConsoleError> {
        let Some(path) = &self.path else {
            return Ok(json!({"operations": []}));
        };
        if !path.exists() {
            return Ok(json!({"operations": []}));
        }
        let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|_| ConsoleError::console_unavailable())?;
        let tables = table_names(&connection)?;
        if !tables.iter().any(|table| table == "console_meta") {
            if tables.iter().any(|table| table != "operations") {
                return Err(ConsoleError::console_unavailable());
            }
            // Python creates the missing schema on first run. The read face
            // never writes it; the response is the same empty list.
            return Ok(json!({"operations": []}));
        }
        let version: Option<String> = connection
            .query_row(
                "SELECT value FROM console_meta WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .map_err(|_| ConsoleError::console_unavailable())?;
        if version
            .as_deref()
            .and_then(|value| value.parse::<i64>().ok())
            != Some(CONSOLE_STATE_SCHEMA_VERSION)
        {
            return Err(ConsoleError::console_unavailable());
        }
        if !tables.iter().any(|table| table == "operations") {
            return Ok(json!({"operations": []}));
        }
        let limit = operation_limit(raw_target);
        let mut statement = connection
            .prepare(
                "SELECT * FROM operations \
                 ORDER BY created_at DESC, id DESC LIMIT ?",
            )
            .map_err(|_| ConsoleError::console_unavailable())?;
        let rows = statement
            .query_map([limit], operation_view)
            .map_err(|_| ConsoleError::console_unavailable())?;
        let mut operations = Vec::new();
        for row in rows {
            operations.push(row.map_err(|_| ConsoleError::console_unavailable())?);
        }
        Ok(json!({"operations": operations}))
    }

    pub fn get(&self, operation_id: &str) -> Result<Value, ConsoleError> {
        let Some(path) = &self.path else {
            return Err(ConsoleError::unknown_operation());
        };
        if !path.exists() {
            return Err(ConsoleError::unknown_operation());
        }
        let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|_| ConsoleError::console_unavailable())?;
        let tables = table_names(&connection)?;
        if !tables.iter().any(|table| table == "console_meta")
            || !tables.iter().any(|table| table == "operations")
        {
            return Err(ConsoleError::unknown_operation());
        }
        let version: Option<String> = connection
            .query_row(
                "SELECT value FROM console_meta WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .map_err(|_| ConsoleError::console_unavailable())?;
        if version
            .as_deref()
            .and_then(|value| value.parse::<i64>().ok())
            != Some(CONSOLE_STATE_SCHEMA_VERSION)
        {
            return Err(ConsoleError::console_unavailable());
        }
        let row = connection.query_row(
            "SELECT * FROM operations WHERE id=?",
            [operation_id],
            operation_view,
        );
        match row {
            Ok(operation) => Ok(json!({"operation": operation})),
            Err(rusqlite::Error::QueryReturnedNoRows) => Err(ConsoleError::unknown_operation()),
            Err(_) => Err(ConsoleError::console_unavailable()),
        }
    }
}

fn table_names(connection: &Connection) -> Result<Vec<String>, ConsoleError> {
    let mut statement = connection
        .prepare("SELECT name FROM sqlite_master WHERE type='table'")
        .map_err(|_| ConsoleError::console_unavailable())?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|_| ConsoleError::console_unavailable())?;
    let mut tables = Vec::new();
    for row in rows {
        tables.push(row.map_err(|_| ConsoleError::console_unavailable())?);
    }
    Ok(tables)
}

fn operation_view(row: &Row<'_>) -> rusqlite::Result<Value> {
    let load = |column: &str| -> Option<Value> {
        let raw: String = row.get::<_, Option<String>>(column).ok()??;
        serde_json::from_str(&raw).ok()
    };
    let steps = load("steps_json")
        .filter(truthy)
        .unwrap_or_else(|| json!([]));
    let mut operation = Map::new();
    operation.insert("id".to_string(), json!(row.get::<_, String>("id")?));
    operation.insert("action".to_string(), json!(row.get::<_, String>("action")?));
    operation.insert("target".to_string(), json!(row.get::<_, String>("target")?));
    operation.insert("state".to_string(), json!(row.get::<_, String>("state")?));
    operation.insert(
        "resource".to_string(),
        json!(row.get::<_, String>("resource")?),
    );
    operation.insert(
        "generation".to_string(),
        json!(row.get::<_, i64>("generation")?),
    );
    operation.insert(
        "created_at".to_string(),
        json!(row.get::<_, Option<f64>>("created_at")?),
    );
    operation.insert(
        "started_at".to_string(),
        json!(row.get::<_, Option<f64>>("started_at")?),
    );
    operation.insert(
        "finished_at".to_string(),
        json!(row.get::<_, Option<f64>>("finished_at")?),
    );
    operation.insert(
        "cancel_requested".to_string(),
        json!(row.get::<_, i64>("cancel_requested")? != 0),
    );
    operation.insert("steps".to_string(), steps);
    operation.insert(
        "result".to_string(),
        load("result_json").unwrap_or(Value::Null),
    );
    operation.insert(
        "error".to_string(),
        load("error_json").unwrap_or(Value::Null),
    );
    operation.insert(
        "idempotency_key".to_string(),
        json!(row.get::<_, Option<String>>("idempotency_key")?),
    );
    Ok(Value::Object(operation))
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64().is_some_and(|value| value != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

fn operation_limit(raw_target: &str) -> i64 {
    let raw = query_value(raw_target, "limit").unwrap_or_else(|| "50".to_string());
    let parsed = raw.trim().parse::<i128>().unwrap_or(50);
    parsed.clamp(1, MAX_OPERATION_LIMIT as i128) as i64
}

fn query_value(raw_target: &str, name: &str) -> Option<String> {
    let query = raw_target.split_once('?')?.1;
    for pair in query.split('&') {
        let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
        if percent_decode(key) == name {
            return Some(percent_decode(value));
        }
    }
    None
}

fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut output = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => output.push(b' '),
            b'%' if index + 2 < bytes.len() => {
                let hex = &value[index + 1..index + 3];
                match u8::from_str_radix(hex, 16) {
                    Ok(byte) => {
                        output.push(byte);
                        index += 3;
                        continue;
                    }
                    Err(_) => output.push(b'%'),
                }
            }
            byte => output.push(byte),
        }
        index += 1;
    }
    String::from_utf8_lossy(&output).into_owned()
}

/// Test seam: the exact Python `_view` field map for one row.
pub fn operation_view_for_tests(row: &Row<'_>) -> rusqlite::Result<Value> {
    operation_view(row)
}
