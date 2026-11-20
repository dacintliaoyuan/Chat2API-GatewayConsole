//! S5b: the share-management read face (`usagesrv.py`).
//!
//! The candidate opens the caller-provided `share-store.db` READ-ONLY: it never
//! creates, migrates or writes the store, the ledger or a log. Missing values
//! stay `null`/omitted; `gateway_running` is three-state and is omitted when
//! the broker seam cannot confirm it.

use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{Connection, OpenFlags, Row};
use serde_json::{json, Map, Value};

use crate::error::ConsoleError;

pub const SHARE_STORE_FILE_ENV: &str = "SHARE_STORE_FILE";
pub const SHARE_SECRET_KEY_FILE_ENV: &str = "CHAT2API_SHARE_SECRET_KEY_FILE";
pub const PUBLIC_ORIGIN_ENV: &str = "CONSOLE_PUBLIC_ORIGIN";
pub const LEGACY_SHARE_MAX_CONCURRENCY: i64 = 2;
pub const SHARE_MAX_CONCURRENCY_MAX: i64 = 64;
pub const KEY_MAX_CONCURRENCY_MAX: i64 = 48;

/// Read-only seams the share status face consumes.
///
/// `None` means "not confirmed": the field is omitted rather than folded into a
/// fabricated `false`, `0` or empty list.
pub trait ShareReadSeams: Send + Sync {
    /// The broker status snapshot (the Python `BrokerClientAdapter.snapshot`).
    fn broker_snapshot(&self) -> Option<Value> {
        None
    }

    /// Three-state gateway-instance evidence (`verify_running`).
    fn gateway_running(&self) -> Option<bool> {
        None
    }

    /// The broker's restricted maintenance view.
    fn maintenance_status(&self) -> Option<Value> {
        None
    }

    /// The read-only attribution facts, when this deployment wires them.
    fn attribution(&self) -> Option<Value> {
        None
    }

    /// One read-only auth observation for the display-state face.
    fn auth_observation(&self) -> Option<Value> {
        None
    }

    /// The display relay availability, when this deployment assembles one.
    fn display_availability(&self) -> Option<Value> {
        None
    }
}

pub struct UnwiredSeams;

impl ShareReadSeams for UnwiredSeams {}

/// Production read seam for the S7 display relay: the assembled
/// `DisplayConfig` fixes the availability fact, while the broker and
/// attribution seams stay unconfirmed. The display-state face additionally reads
/// the gateway auth round through the same control-token boundary as the UM auth
/// actions.
pub struct DisplaySeams {
    availability: Value,
    auth: Option<GatewayAuthObservation>,
}

struct GatewayAuthObservation {
    gateway_port: u16,
    control_token_file: Option<PathBuf>,
}

impl DisplaySeams {
    /// Wire the display-state auth observation to the gateway's
    /// `/admin/auth/status` control face.
    pub fn with_auth_observation(
        availability: Value,
        gateway_port: u16,
        control_token_file: Option<PathBuf>,
    ) -> Self {
        Self {
            availability,
            auth: Some(GatewayAuthObservation {
                gateway_port,
                control_token_file,
            }),
        }
    }
}

impl ShareReadSeams for DisplaySeams {
    fn auth_observation(&self) -> Option<Value> {
        self.auth.as_ref().map(|auth| {
            crate::local_um_auth::auth_observation(
                auth.gateway_port,
                auth.control_token_file.as_deref(),
            )
        })
    }

    fn display_availability(&self) -> Option<Value> {
        Some(self.availability.clone())
    }
}

pub struct ShareReadStore {
    path: PathBuf,
    public_origin: Option<String>,
    secret_key_file: Option<PathBuf>,
}

impl ShareReadStore {
    pub fn new(path: PathBuf) -> Self {
        Self {
            path,
            public_origin: std::env::var(PUBLIC_ORIGIN_ENV).ok(),
            secret_key_file: std::env::var_os(SHARE_SECRET_KEY_FILE_ENV).map(PathBuf::from),
        }
    }

    pub fn with_options(
        path: PathBuf,
        public_origin: Option<String>,
        secret_key_file: Option<PathBuf>,
    ) -> Self {
        Self {
            path,
            public_origin,
            secret_key_file,
        }
    }

    fn connection(&self) -> Result<Connection, ConsoleError> {
        if !self.path.is_file() {
            // Read-only discipline: never create or migrate the store.
            return Err(share_management_error());
        }
        Connection::open_with_flags(&self.path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|_| share_management_error())
    }

    /// `ShareManager.status()` reshaped for the console contract.
    pub fn status(&self, seams: &dyn ShareReadSeams) -> Result<Value, ConsoleError> {
        let connection = self.connection()?;
        let (schema_version, config_generation, applied_generation) = connection
            .query_row(
                "SELECT schema_version,config_generation,applied_generation FROM configgen",
                [],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                },
            )
            .map_err(|_| share_management_error())?;
        let security_pending: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM security WHERE status='pending'",
                [],
                |row| row.get(0),
            )
            .map_err(|_| share_management_error())?;
        let stored_share_max = share_max_concurrency(&connection)?;
        let keys = list_key_rows(&connection)?;
        let clamped_keys = keys
            .iter()
            .filter(|row| row.max_concurrency > KEY_MAX_CONCURRENCY_MAX)
            .count() as i64;
        let blocked_publications = blocked_publications(&connection, applied_generation)?;
        let last_publication_failure = last_publication_failure(&connection)?;
        let mut payload = Map::new();
        payload.insert("schema_version".to_string(), json!(schema_version));
        payload.insert(
            "share_max_concurrency".to_string(),
            json!(stored_share_max.min(SHARE_MAX_CONCURRENCY_MAX)),
        );
        payload.insert(
            "share_max_concurrency_stored".to_string(),
            json!(stored_share_max),
        );
        payload.insert(
            "limits_normalization_required".to_string(),
            json!(stored_share_max > SHARE_MAX_CONCURRENCY_MAX || clamped_keys > 0),
        );
        payload.insert("limits_clamped_keys".to_string(), json!(clamped_keys));
        payload.insert("desired_generation".to_string(), json!(config_generation));
        payload.insert("applied_generation".to_string(), json!(applied_generation));
        payload.insert(
            "in_sync".to_string(),
            json!(config_generation == applied_generation && security_pending == 0),
        );
        payload.insert("security_pending".to_string(), json!(security_pending));
        payload.insert("security_in_sync".to_string(), json!(security_pending == 0));
        payload.insert(
            "pending_revocation".to_string(),
            json!(config_generation > applied_generation),
        );
        payload.insert("active_keys".to_string(), json!(keys.len() as i64));
        payload.insert("total_keys".to_string(), json!(keys.len() as i64));
        payload.insert(
            "broker".to_string(),
            map_broker_snapshot(seams.broker_snapshot()),
        );
        payload.insert(
            "last_publication_failure".to_string(),
            last_publication_failure,
        );
        payload.insert(
            "blocked_publications".to_string(),
            Value::Array(blocked_publications),
        );
        payload.insert(
            "blocked".to_string(),
            json!(payload["blocked_publications"]
                .as_array()
                .map(|items| items
                    .iter()
                    .any(|item| item["blocked"].as_bool() == Some(true)))
                .unwrap_or(false)),
        );

        // D18-1: the configured public origin is the real deployment
        // address; an unconfigured/invalid value is never replaced by a
        // different public host.
        let resolved = resolve_public_origin(self.public_origin.as_deref());
        payload.insert(
            "share_url_source".to_string(),
            resolved.source.map(Value::from).unwrap_or(Value::Null),
        );
        payload.insert("share_url_reason".to_string(), json!(resolved.reason));
        if let Some(url) = resolved.url {
            payload.insert("share_url".to_string(), json!(url));
        }

        // The broker's always-on management daemon is NOT the share gateway
        // instance: observe the actual instance through the same read-only seam
        // the share.status action uses. `None` = unconfirmed, so the key is
        // omitted instead of fabricated as a confirmed stop.
        if let Some(running) = seams.gateway_running() {
            payload.insert("gateway_running".to_string(), json!(running));
        }
        if let Some(attribution) = seams.attribution() {
            if attribution
                .as_object()
                .map(|object| object.contains_key("file_ready"))
                .unwrap_or(false)
            {
                payload.insert("attribution".to_string(), attribution);
            }
        }
        payload.insert("last_check_at".to_string(), json!(epoch_seconds()));
        let maintenance = seams.maintenance_status();
        payload.insert(
            "capacity".to_string(),
            share_capacity(payload.get("share_max_concurrency"), maintenance.as_ref()),
        );
        Ok(Value::Object(payload))
    }

    /// `ShareManager.list_keys()` reshaped for the console facade.
    pub fn keys(&self) -> Result<Value, ConsoleError> {
        let connection = self.connection()?;
        let rows = list_key_rows(&connection)?;
        let keys: Vec<Value> = rows.iter().map(console_public_row).collect();
        Ok(json!({ "keys": keys }))
    }

    /// `ShareManager.status()` raw shape (the `/api/share/status` face).
    pub fn raw_status(&self, broker: Option<Value>) -> Result<Value, ConsoleError> {
        let connection = self.connection()?;
        let (schema_version, config_generation, applied_generation) = connection
            .query_row(
                "SELECT schema_version,config_generation,applied_generation FROM configgen",
                [],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                },
            )
            .map_err(|_| share_management_error())?;
        let security_pending: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM security WHERE status='pending'",
                [],
                |row| row.get(0),
            )
            .map_err(|_| share_management_error())?;
        let stored_share_max = share_max_concurrency(&connection)?;
        let keys = list_key_rows(&connection)?;
        let clamped_keys = keys
            .iter()
            .filter(|row| row.max_concurrency > KEY_MAX_CONCURRENCY_MAX)
            .count() as i64;
        let blocked_publications = blocked_publications(&connection, applied_generation)?;
        let last_publication_failure = last_publication_failure(&connection)?;
        let blocked = blocked_publications
            .iter()
            .any(|item| item["blocked"].as_bool() == Some(true));
        Ok(json!({
            "schema_version": schema_version,
            "share_max_concurrency": stored_share_max.min(SHARE_MAX_CONCURRENCY_MAX),
            "share_max_concurrency_stored": stored_share_max,
            "limits_normalization_required":
                stored_share_max > SHARE_MAX_CONCURRENCY_MAX || clamped_keys > 0,
            "limits_clamped_keys": clamped_keys,
            "desired_generation": config_generation,
            "applied_generation": applied_generation,
            "in_sync": config_generation == applied_generation && security_pending == 0,
            "security_pending": security_pending,
            "security_in_sync": security_pending == 0,
            "pending_revocation": config_generation > applied_generation,
            "active_keys": keys.len() as i64,
            "total_keys": keys.len() as i64,
            "broker": broker.unwrap_or_else(|| json!({
                "name": "noop",
                "state": "offline",
                "note": "未安装/未授权高权限 broker；变更已保存但未在线发布",
            })),
            "last_publication_failure": last_publication_failure,
            "blocked_publications": blocked_publications,
            "blocked": blocked,
        }))
    }

    /// `ShareManager.get_operation()` (the `/api/share/operations/{id}` face).
    pub fn operation(&self, operation_id: &str) -> Result<Value, ConsoleError> {
        let connection = self.connection()?;
        let row = connection
            .query_row(
                "SELECT id,kind,status,payload,created_at FROM ops WHERE id=?1",
                [operation_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, Option<String>>(3)?,
                        row.get::<_, String>(4)?,
                    ))
                },
            )
            .map_err(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => {
                    ConsoleError::new(404, json!({"error": "operation not found"}))
                }
                _ => share_management_error(),
            })?;
        let payload = row
            .3
            .as_deref()
            .and_then(|text| serde_json::from_str::<Value>(text).ok())
            .unwrap_or_else(|| json!({}));
        Ok(json!({
            "operation_id": row.0,
            "kind": row.1,
            "status": row.2,
            "payload": payload,
            "created_at": row.4,
        }))
    }

    /// `ShareManager.list_keys()` in the `/api/share/*` row shape.
    pub fn share_keys(&self) -> Result<Value, ConsoleError> {
        let connection = self.connection()?;
        let rows = list_key_rows(&connection)?;
        let keys: Vec<Value> = rows.iter().map(store_public_row).collect();
        Ok(json!({ "keys": keys }))
    }

    /// `ShareManager.get_key()` in the `/api/share/*` row shape.
    pub fn share_key(&self, key_id: &str) -> Result<Value, ConsoleError> {
        let connection = self.connection()?;
        let row = connection
            .query_row(
                "SELECT * FROM keys WHERE key_id=?1",
                [key_id],
                store_public_row_from_row,
            )
            .map_err(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => {
                    ConsoleError::new(404, json!({"error": "key identity not found"}))
                }
                _ => share_management_error(),
            })?;
        Ok(row)
    }

    /// `GET /api/console/share-management/keys/{id}/secret`.
    ///
    /// Four distinct outcomes: 200 plaintext, 200 `secret_unavailable` with a
    /// reason (`legacy`/`undecryptable`/`version_conflict`), 410
    /// `key_deleted`, 404 unknown. The plaintext rides only this immediate
    /// no-store response.
    pub fn secret(
        &self,
        key_id: &str,
        expected_raw: Option<&str>,
    ) -> Result<(u16, Value), ConsoleError> {
        let connection = self.connection()?;
        let row = connection
            .query_row("SELECT * FROM keys WHERE key_id=?1", [key_id], |row| {
                Ok(row_to_key(row))
            })
            .map_err(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => {
                    ConsoleError::new(404, json!({"error": "key identity not found"}))
                }
                _ => share_management_error(),
            })?;
        if row.deleted_at.is_some() {
            return Ok((410, json!({"error": "key_deleted"})));
        }
        let expected = match expected_raw {
            None | Some("") => None,
            Some(raw) => match raw.parse::<i64>() {
                Ok(value) if value >= 1 => Some(value),
                _ => {
                    return Err(ConsoleError::new(
                        400,
                        json!({"error": "invalid expected_row_version"}),
                    ))
                }
            },
        };
        if let Some(expected) = expected {
            if expected != row.row_version {
                return Ok((
                    200,
                    json!({
                        "key_id": key_id,
                        "secret_unavailable": true,
                        "reason": "version_conflict",
                    }),
                ));
            }
        }
        let Some(blob) = row.secret_blob.as_deref() else {
            return Ok(secret_unavailable(key_id, "legacy"));
        };
        let Some(algo) = row.secret_algo.as_deref() else {
            return Ok(secret_unavailable(key_id, "undecryptable"));
        };
        if algo != um_store::secret::SECRET_ALGO_AESGCM_FILE_KEY {
            return Ok(secret_unavailable(key_id, "undecryptable"));
        }
        let key = match um_store::secret::load_key_file(self.secret_key_file.as_deref()) {
            Ok(key) => key,
            Err(_) => return Ok(secret_unavailable(key_id, "undecryptable")),
        };
        let secret = match um_store::secret::unprotect_with_key(blob, algo, &key) {
            Ok(secret) => secret,
            Err(_) => return Ok(secret_unavailable(key_id, "undecryptable")),
        };
        if !um_store::secret::secret_eq(&secret, &row.secret_hash) {
            return Ok(secret_unavailable(key_id, "undecryptable"));
        }
        Ok((
            200,
            json!({
                "key_id": key_id,
                "name": row.name,
                "masked_key": row.masked_key,
                "secret_version": row.secret_version,
                "secret": secret,
            }),
        ))
    }
}

fn secret_unavailable(key_id: &str, reason: &str) -> (u16, Value) {
    (
        200,
        json!({
            "key_id": key_id,
            "secret_unavailable": true,
            "reason": reason,
        }),
    )
}

fn share_management_error() -> ConsoleError {
    ConsoleError::new(500, json!({"error": "share management error"}))
}

fn epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

fn share_max_concurrency(connection: &Connection) -> Result<i64, ConsoleError> {
    let stored: Option<String> = connection
        .query_row(
            "SELECT value FROM meta WHERE key='share_max_concurrency'",
            [],
            |row| row.get(0),
        )
        .map(Some)
        .or_else(|error| match error {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })
        .map_err(|_| share_management_error())?;
    match stored {
        // Only reachable for a pre-v2 file: the legacy hardcode, never an
        // invented limit.
        None => Ok(LEGACY_SHARE_MAX_CONCURRENCY),
        Some(value) => value.parse::<i64>().map_err(|_| share_management_error()),
    }
}

/// `ShareManager._last_failure` for the durable publish journal: the latest
/// failed `publish` operation keeps its recorded error even after a later
/// publication succeeds, matching Python's in-process retention.
fn last_publication_failure(connection: &Connection) -> Result<Value, ConsoleError> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT payload,created_at FROM ops WHERE kind='publish'",
            " AND status='failed' ORDER BY created_at DESC, rowid DESC LIMIT 1",
        ))
        .map_err(|_| share_management_error())?;
    let row = statement.query_row([], |row| {
        Ok((row.get::<_, Option<String>>(0)?, row.get::<_, String>(1)?))
    });
    match row {
        Ok((payload, created_at)) => {
            let payload = payload
                .as_deref()
                .and_then(|text| serde_json::from_str::<Value>(text).ok())
                .unwrap_or_else(|| json!({}));
            let error = payload
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if error.is_empty() {
                return Ok(Value::Null);
            }
            Ok(json!({
                "kind": "publish",
                "error": error,
                "at": created_at,
            }))
        }
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(Value::Null),
        Err(_) => Err(share_management_error()),
    }
}

fn blocked_publications(
    connection: &Connection,
    applied_generation: i64,
) -> Result<Vec<Value>, ConsoleError> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT id,status,payload,created_at FROM ops WHERE kind='publish'",
            " ORDER BY created_at",
        ))
        .map_err(|_| share_management_error())?;
    let rows = statement
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(|_| share_management_error())?;
    let mut blockers = Vec::new();
    for row in rows {
        let (operation_id, status, payload, created_at) =
            row.map_err(|_| share_management_error())?;
        if !matches!(status.as_str(), "pending" | "blocked" | "failed") {
            continue;
        }
        let payload = payload
            .as_deref()
            .and_then(|text| serde_json::from_str::<Value>(text).ok())
            .unwrap_or_else(|| json!({}));
        let Some(target) = payload.get("target_generation").and_then(Value::as_i64) else {
            continue;
        };
        if target <= applied_generation {
            continue;
        }
        let stop = payload
            .get("emergency_stop")
            .cloned()
            .unwrap_or_else(|| json!({}));
        let stop_confirmed = stop.get("confirmed") == Some(&Value::Bool(true));
        let confirmed = stop_confirmed && status != "pending";
        blockers.push(json!({
            "operation_id": operation_id,
            "status": status,
            "target_generation": target,
            "stop_confirmed": stop_confirmed,
            "blocked": payload.get("blocked") == Some(&Value::Bool(true)) && !confirmed,
            "recovered": payload.get("recovered").cloned().unwrap_or(Value::Null),
            "created_at": created_at,
        }));
    }
    Ok(blockers)
}

fn list_key_rows(connection: &Connection) -> Result<Vec<KeyRow>, ConsoleError> {
    let mut statement = connection
        .prepare(concat!(
            "SELECT * FROM keys WHERE enabled=1 AND deleted_at IS NULL",
            " ORDER BY created_at",
        ))
        .map_err(|_| share_management_error())?;
    let rows = statement
        .query_map([], |row| Ok(row_to_key(row)))
        .map_err(|_| share_management_error())?;
    rows.map(|row| row.map_err(|_| share_management_error()))
        .collect()
}

pub struct KeyRow {
    pub key_id: String,
    pub name: String,
    pub secret_hash: String,
    pub secret_version: i64,
    pub masked_key: String,
    pub secret_blob: Option<Vec<u8>>,
    pub secret_algo: Option<String>,
    pub enabled: bool,
    pub max_concurrency: i64,
    pub created_at: String,
    pub updated_at: String,
    pub deleted_at: Option<String>,
    pub row_version: i64,
}

fn row_to_key(row: &Row<'_>) -> KeyRow {
    KeyRow {
        key_id: row.get("key_id").unwrap_or_default(),
        name: row.get("name").unwrap_or_default(),
        secret_hash: row.get("secret_hash").unwrap_or_default(),
        secret_version: row.get("secret_version").unwrap_or_default(),
        masked_key: row.get("masked_key").unwrap_or_default(),
        secret_blob: row.get("secret_blob").ok(),
        secret_algo: row.get("secret_algo").ok(),
        enabled: row.get::<_, i64>("enabled").unwrap_or_default() != 0,
        max_concurrency: row.get("max_concurrency").unwrap_or_default(),
        created_at: row.get("created_at").unwrap_or_default(),
        updated_at: row.get("updated_at").unwrap_or_default(),
        deleted_at: row.get("deleted_at").ok(),
        row_version: row.get("row_version").unwrap_or_default(),
    }
}

fn console_public_row(row: &KeyRow) -> Value {
    json!({
        "id": row.key_id,
        "name": row.name,
        "masked": row.masked_key,
        "plaintext_viewable": row.secret_blob.is_some(),
        "enabled": row.enabled,
        "max_concurrency": row.max_concurrency,
        "row_version": row.row_version,
        "created_at": row.created_at,
    })
}

fn store_public_row(row: &KeyRow) -> Value {
    json!({
        "key_id": row.key_id,
        "name": row.name,
        "secret_version": row.secret_version,
        "masked_key": row.masked_key,
        "plaintext_viewable": row.secret_blob.is_some(),
        "enabled": row.enabled,
        "max_concurrency": row.max_concurrency,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "row_version": row.row_version,
        "deleted_at": row.deleted_at,
    })
}

fn store_public_row_from_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(store_public_row(&row_to_key(row)))
}

/// `usagesrv.py` control-status: the broker control channel answers only
/// when its snapshot is `state=ok` with a broker object. An unassembled seam
/// is `share_broker_unwired`; any other snapshot is
/// `share_broker_unavailable`, never a fabricated responsive `true`.
pub fn control_status(snapshot: Option<Value>) -> Value {
    let Some(snapshot) = snapshot else {
        return json!({"responsive": false, "error_code": "share_broker_unwired"});
    };
    let responsive = snapshot.get("state").and_then(Value::as_str) == Some("ok")
        && snapshot
            .get("broker")
            .map(Value::is_object)
            .unwrap_or(false);
    if responsive {
        json!({"responsive": true})
    } else {
        json!({"responsive": false, "error_code": "share_broker_unavailable"})
    }
}

/// `Handler._console_share_status_payload` broker mapping.
pub fn map_broker_snapshot(snapshot: Option<Value>) -> Value {
    let Some(snapshot) = snapshot else {
        return json!({});
    };
    let Some(snapshot) = snapshot.as_object() else {
        return json!({});
    };
    let mut mapped = Map::new();
    let state = snapshot.get("state").and_then(Value::as_str);
    let inner = snapshot
        .get("broker")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if matches!(state, Some("ok") | Some("running")) {
        mapped.insert("running".to_string(), json!(true));
    } else if matches!(
        state,
        Some("unavailable") | Some("stopped") | Some("conflict")
    ) {
        mapped.insert("running".to_string(), json!(false));
        if let Some(code) = snapshot.get("error_code").and_then(Value::as_str) {
            if !code.is_empty() {
                mapped.insert("error_code".to_string(), json!(code));
            }
        }
    }
    let version = inner.get("version").or_else(|| snapshot.get("version"));
    if let Some(version) = version.and_then(Value::as_str) {
        mapped.insert("version".to_string(), json!(version));
    }
    let pid = inner
        .get("pid")
        .or_else(|| snapshot.get("pid"))
        .or_else(|| {
            snapshot
                .get("process_ids")
                .and_then(Value::as_array)
                .and_then(|ids| ids.first())
        });
    if let Some(pid) = pid.and_then(Value::as_i64) {
        mapped.insert("pid".to_string(), json!(pid));
    }
    if let Some(detail) = snapshot
        .get("detail")
        .and_then(Value::as_str)
        .filter(|detail| !detail.is_empty())
    {
        mapped.insert("detail".to_string(), json!(detail));
    }
    if let Some(diag) = inner.get("diag").and_then(Value::as_array) {
        if !diag.is_empty() {
            mapped.insert(
                "diag".to_string(),
                Value::Array(diag.iter().rev().take(5).rev().cloned().collect()),
            );
        }
    }
    Value::Object(mapped)
}

/// `Handler._console_share_capacity` (r14 overview contract).
pub fn share_capacity(configured: Option<&Value>, maintenance: Option<&Value>) -> Value {
    let configured = configured.and_then(|value| {
        value
            .as_i64()
            .filter(|value| *value >= 1)
            .map(|value| json!(value))
    });
    let mut in_flight = None;
    let mut checked_at = None;
    let mut reason = "maintenance_unwired";
    if let Some(view) = maintenance {
        if let Some(view) = view.as_object() {
            if let Some(checked) = view.get("checked_at").and_then(Value::as_f64) {
                if checked > 0.0 {
                    checked_at = Some(checked as i64);
                }
            }
            match view.get("instance").and_then(Value::as_object) {
                Some(instance) => match instance.get("active_count").and_then(Value::as_i64) {
                    Some(active) if active >= 0 => {
                        in_flight = Some(active);
                        reason = "instance_ok";
                    }
                    _ => reason = "instance_active_count_unavailable",
                },
                None => reason = "instance_unavailable",
            }
        } else {
            reason = "maintenance_unavailable";
        }
    }
    let configured_note = configured
        .as_ref()
        .and_then(Value::as_i64)
        .map(|value| value.to_string())
        .unwrap_or_else(|| "未上报".to_string());
    let reason = if reason == "instance_ok" {
        Value::Null
    } else {
        json!(reason)
    };
    json!({
        "configured": configured.unwrap_or(Value::Null),
        "in_flight": in_flight.map(Value::from).unwrap_or(Value::Null),
        "in_flight_reason": reason,
        "queued": Value::Null,
        "queued_reason": "not_exposed",
        "upstream_capacity": Value::Null,
        "upstream_capacity_measured": false,
        "upstream_capacity_reason": "not_measured",
        "checked_at": checked_at.map(Value::from).unwrap_or(Value::Null),
        "note": format!(
            "configured 是用户配置的准入上限（当前部署 {configured_note}），\
             不是上游实测承载量；in_flight 来自受限后台的实例状态，\
             排队数与上游容量本读面未暴露/未实测"
        ),
    })
}

pub struct ResolvedPublicOrigin {
    pub url: Option<String>,
    pub source: Option<&'static str>,
    pub reason: &'static str,
}

/// `console/public_probe.py::public_origin` + `console_public_url`.
pub fn resolve_public_origin(raw: Option<&str>) -> ResolvedPublicOrigin {
    match raw {
        None => ResolvedPublicOrigin {
            url: None,
            source: None,
            reason: "public_origin_unconfigured",
        },
        Some(value) if value.trim().is_empty() => ResolvedPublicOrigin {
            url: None,
            source: None,
            reason: "public_origin_unconfigured",
        },
        Some(value) => match strict_origin_host(value) {
            Some(host) => ResolvedPublicOrigin {
                url: Some(format!("https://{host}")),
                source: Some("configured"),
                reason: "",
            },
            None => ResolvedPublicOrigin {
                url: None,
                source: None,
                reason: "public_origin_invalid",
            },
        },
    }
}

/// `origin_host`: the host of a STRICT `https://<host>` origin.
pub fn strict_origin_host(value: &str) -> Option<String> {
    if value.is_empty() || value.len() > 253 || !value.is_ascii() {
        return None;
    }
    if value.contains('?') || value.contains('#') || value.contains('\\') {
        return None;
    }
    if value
        .chars()
        .any(|character| character.is_whitespace() || (character as u32) < 32)
    {
        return None;
    }
    let rest = value.strip_prefix("https://")?;
    let (authority, path) = match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, ""),
    };
    if !path.is_empty() && path != "/" {
        return None;
    }
    if authority.is_empty() || authority.contains('@') {
        return None;
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((host, port)) => (host, Some(port)),
        None => (authority, None),
    };
    if host.is_empty() || host.contains(':') {
        return None;
    }
    if let Some(port) = port {
        if port != "443" {
            return None;
        }
    }
    for label in host.split('.') {
        if label.is_empty() || label.len() > 63 || label.starts_with('-') || label.ends_with('-') {
            return None;
        }
        if !label
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        {
            return None;
        }
    }
    let host = host.to_ascii_lowercase();
    if authority != host && authority != format!("{host}:443") {
        return None;
    }
    Some(host)
}

/// Test seam: the store row shape for the `/api/share/*` keys read face.
pub fn share_store_row_for_tests(row: &Row<'_>) -> rusqlite::Result<Value> {
    store_public_row_from_row(row)
}

/// Test seam: the broker snapshot mapping.
pub fn broker_snapshot_for_tests(snapshot: Option<Value>) -> Value {
    map_broker_snapshot(snapshot)
}

/// Test seam: the capacity block.
pub fn capacity_for_tests(configured: Option<&Value>, maintenance: Option<&Value>) -> Value {
    share_capacity(configured, maintenance)
}

/// Read-only whitelisted Key labels for the usage read faces.
///
/// `share/store.py::read_key_labels`: only `key_id`, `name`, `enabled` and
/// `deleted_at` are selected. A missing/damaged store is an error so the
/// caller keeps every label `null` instead of treating it as an empty store.
pub fn read_key_labels(
    path: &std::path::Path,
    wanted: &std::collections::BTreeSet<String>,
) -> Result<std::collections::BTreeMap<String, KeyLabel>, ConsoleError> {
    use std::collections::BTreeMap;
    if wanted.is_empty() {
        return Ok(BTreeMap::new());
    }
    let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|_| share_management_error())?;
    let mut labels = BTreeMap::new();
    let wanted: Vec<&String> = wanted.iter().collect();
    for batch in wanted.chunks(400) {
        let placeholders = vec!["?"; batch.len()].join(",");
        let sql = format!(
            "SELECT key_id,name,enabled,deleted_at FROM keys WHERE key_id IN ({placeholders})"
        );
        let mut statement = connection
            .prepare(&sql)
            .map_err(|_| share_management_error())?;
        let rows = statement
            .query_map(rusqlite::params_from_iter(batch.iter()), |row| {
                let key_id: String = row.get(0)?;
                let name: String = row.get(1)?;
                let enabled: Option<i64> = row.get(2).ok();
                let deleted_at: Option<String> = row.get(3).ok();
                Ok((
                    key_id,
                    KeyLabel {
                        name,
                        deleted: deleted_at.is_some() || enabled == Some(0),
                    },
                ))
            })
            .map_err(|_| share_management_error())?;
        for row in rows {
            let (key_id, label) = row.map_err(|_| share_management_error())?;
            labels.insert(key_id, label);
        }
    }
    Ok(labels)
}

pub struct KeyLabel {
    pub name: String,
    pub deleted: bool,
}
