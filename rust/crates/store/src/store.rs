//! AC2/AC3: the single-authority share store.
//!
//! This is the Rust owner of `share/store.py`'s data model: schema 3, the Key
//! lifecycle (create/rename/update/rotate/delete/verify/reveal), the security
//! journal, the publish intent and the projection snapshot. Every mutation runs
//! inside ONE `BEGIN IMMEDIATE` transaction so the key row, the desired
//! generation bump, the security intent and the publish intent commit together.
//! The store NEVER writes the projection file: `publish` does that through the
//! broker (writer -> load -> ACK).

use std::path::Path;

use rusqlite::{params, Connection, Row};
use serde_json::{json, Map, Value};

use crate::error::StoreError;
use crate::ids::{random_uuid, utc_now};
use crate::projection::fingerprint;
use crate::schema::{
    open_connection, KEY_MAX_CONCURRENCY_MAX, KEY_MAX_CONCURRENCY_MIN,
    NEW_KEY_DEFAULT_MAX_CONCURRENCY, PROJECTION_SCHEMA_VERSION, SCHEMA_VERSION,
    SHARE_MAX_CONCURRENCY_MAX, SHARE_MAX_CONCURRENCY_MIN,
};
use crate::secret::{
    load_key_file, mask_secret, protect_with_key, secret_eq, secret_hash, unprotect_with_key,
    SECRET_ALGO_AESGCM_FILE_KEY, SECRET_ALGO_DPAPI_USER,
};

pub const SECURITY_ACTIONS: [&str; 4] = ["revoke", "rotate", "delete", "limit_lower"];
pub const SECURITY_STATUSES: [&str; 4] = ["pending", "applied", "remote_confirmed", "superseded"];

type IdSource = Box<dyn FnMut() -> String>;
type TimeSource = Box<dyn FnMut() -> String>;
type SecretSource = Box<dyn FnMut() -> String>;

pub struct ShareStore {
    connection: Connection,
    next_id: IdSource,
    now: TimeSource,
    next_secret: SecretSource,
    secret_key: Option<Vec<u8>>,
}

impl ShareStore {
    /// Open the store and load the platform secret key from its file.
    pub fn open(path: &Path) -> Result<Self, StoreError> {
        let key = load_key_file(None)?;
        Self::open_with_key(path, key)
    }

    /// Open the store with an explicit key (tests, fixtures, provisioning).
    pub fn open_with_key(path: &Path, key: Vec<u8>) -> Result<Self, StoreError> {
        let connection = open_connection(path)?;
        Ok(Self {
            connection,
            next_id: Box::new(random_uuid),
            now: Box::new(utc_now),
            next_secret: Box::new(|| format!("um-share-{}", random_token_urlsafe(32))),
            secret_key: Some(key),
        })
    }

    /// Test seam: deterministic uuid/timestamp sources.
    pub fn set_sources(&mut self, ids: IdSource, now: TimeSource, secrets: SecretSource) {
        self.next_id = ids;
        self.now = now;
        self.next_secret = secrets;
    }

    pub fn connection(&self) -> &Connection {
        &self.connection
    }

    pub fn close(self) {}

    fn id(&mut self) -> String {
        (self.next_id)()
    }

    fn timestamp(&mut self) -> String {
        (self.now)()
    }

    fn new_secret(&mut self) -> String {
        (self.next_secret)()
    }

    // ---------------------------------------------------------- generation
    pub fn generation(&mut self) -> Result<Value, StoreError> {
        let (schema_version, config_generation, applied_generation) = self.connection.query_row(
            "SELECT schema_version,config_generation,applied_generation FROM configgen",
            [],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            },
        )?;
        let pending_security = self.security_pending()?;
        Ok(json!({
            "schema_version": schema_version,
            "config_generation": config_generation,
            "applied_generation": applied_generation,
            "in_sync": config_generation == applied_generation && pending_security == 0,
            "security_pending": pending_security,
            "security_in_sync": pending_security == 0,
        }))
    }

    fn security_pending(&self) -> Result<i64, StoreError> {
        Ok(self.connection.query_row(
            "SELECT COUNT(*) FROM security WHERE status='pending'",
            [],
            |row| row.get::<_, i64>(0),
        )?)
    }

    /// Share-side ACK: only intents at or below the runtime's OWN watermark
    /// are marked applied; a missing/0 watermark marks NO row.
    pub fn commit_applied(
        &mut self,
        generation: i64,
        instance_id: Option<&str>,
        security_generation: Option<i64>,
    ) -> Result<Value, StoreError> {
        if let Some(security_generation) = security_generation {
            if security_generation < 0 {
                return Err(StoreError::Other(
                    "security generation must be a non-negative integer".to_string(),
                ));
            }
        }
        self.in_transaction(|store| {
            let desired: i64 = store.connection.query_row(
                "SELECT config_generation FROM configgen",
                [],
                |row| row.get(0),
            )?;
            if generation > desired {
                return Err(StoreError::Other(
                    "applied generation newer than desired".to_string(),
                ));
            }
            store.connection.execute(
                "UPDATE configgen SET applied_generation=? WHERE applied_generation < ?",
                params![generation, generation],
            )?;
            let mut security_applied = Vec::new();
            if let Some(security_generation) = security_generation {
                if security_generation > 0 {
                    security_applied = store
                        .mark_security_applied(generation.min(security_generation), instance_id)?;
                }
            }
            let mut result = store.generation()?;
            if let Some(map) = result.as_object_mut() {
                map.insert("security_applied".to_string(), json!(security_applied));
            }
            Ok(result)
        })
    }

    fn mark_security_applied(
        &mut self,
        generation: i64,
        instance_id: Option<&str>,
    ) -> Result<Vec<String>, StoreError> {
        let record_ids: Vec<String> = {
            let mut statement = self.connection.prepare(
                "SELECT record_id FROM security WHERE generation<=? AND status IN ('pending','applied') ORDER BY generation, recorded_at",
            )?;
            let rows = statement.query_map(params![generation], |row| row.get::<_, String>(0))?;
            rows.collect::<Result<_, _>>()?
        };
        if record_ids.is_empty() {
            return Ok(record_ids);
        }
        let now = self.timestamp();
        self.connection.execute(
            "UPDATE security SET status='applied', applied_at=?, applied_generation=?, instance_id=? WHERE generation<=? AND status IN ('pending','applied')",
            params![now, generation, instance_id, generation],
        )?;
        Ok(record_ids)
    }

    // ------------------------------------------------------ security journal
    fn security_record(
        &mut self,
        key_id: Option<&str>,
        action: &str,
        generation: i64,
        secret_version: Option<i64>,
    ) -> Result<String, StoreError> {
        if !SECURITY_ACTIONS.contains(&action) {
            return Err(StoreError::Other(format!(
                "unsupported security action {action:?}"
            )));
        }
        if generation < 1 {
            return Err(StoreError::Other(
                "security generation must be a positive integer".to_string(),
            ));
        }
        if let Some(key_id) = key_id {
            if key_id.is_empty() {
                return Err(StoreError::Other(
                    "security key_id must be a non-empty string".to_string(),
                ));
            }
        }
        if let Some(secret_version) = secret_version {
            if secret_version < 1 {
                return Err(StoreError::Other(
                    "security secret_version must be positive".to_string(),
                ));
            }
        }
        let record_id = self.id();
        let recorded_at = self.timestamp();
        self.connection.execute(
            "INSERT INTO security(record_id,key_id,generation,action,secret_version,status,recorded_at) VALUES(?,?,?,?,?,?,?)",
            params![record_id, key_id, generation, action, secret_version, "pending", recorded_at],
        )?;
        Ok(record_id)
    }

    pub fn security_records(&self) -> Result<Vec<Value>, StoreError> {
        let mut statement = self
            .connection
            .prepare("SELECT * FROM security ORDER BY generation, recorded_at, record_id")?;
        let rows = statement.query_map([], |row| Ok(public_security_row(row)))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(StoreError::from)
    }

    pub fn security_floor(&self) -> Result<i64, StoreError> {
        Ok(self
            .connection
            .query_row("SELECT MAX(generation) FROM security", [], |row| {
                row.get::<_, Option<i64>>(0)
            })?
            .unwrap_or(0))
    }

    pub fn security_state(&mut self) -> Result<Value, StoreError> {
        let records = self.security_records()?;
        let floor = records
            .iter()
            .filter_map(|record| record.get("generation").and_then(Value::as_i64))
            .max()
            .unwrap_or(0);
        let pending_remote = records
            .iter()
            .filter(|record| {
                record.get("status").and_then(Value::as_str) != Some("remote_confirmed")
            })
            .count();
        Ok(json!({
            "format": "um-share-security-state-v1",
            "generation_floor": floor,
            "records": records,
            "pending_remote": pending_remote,
            "exported_at": self.timestamp(),
        }))
    }

    // ------------------------------------------------------------- key ops
    fn in_transaction<T>(
        &mut self,
        function: impl FnOnce(&mut Self) -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        self.connection.execute("BEGIN IMMEDIATE", [])?;
        let result = function(self);
        match result {
            Ok(value) => {
                self.connection.execute("COMMIT", [])?;
                Ok(value)
            }
            Err(error) => {
                let _ = self.connection.execute("ROLLBACK", []);
                Err(error)
            }
        }
    }

    fn bump_desired(&mut self) -> Result<(), StoreError> {
        self.connection.execute(
            "UPDATE configgen SET config_generation = config_generation + 1",
            [],
        )?;
        Ok(())
    }

    fn next_generation(&mut self) -> Result<i64, StoreError> {
        let generation = self.generation()?;
        Ok(generation
            .get("config_generation")
            .and_then(Value::as_i64)
            .unwrap_or_default()
            + 1)
    }

    fn record_publish_intent(
        &mut self,
        revoking: bool,
        action_kind: &str,
        key_id: Option<&str>,
    ) -> Result<(), StoreError> {
        let generation = self.generation()?;
        let target = generation
            .get("config_generation")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        let payload = json!({
            "target_generation": target,
            "revoking": revoking,
            "action_kind": action_kind,
            "key_id": key_id,
        });
        let operation_id = self.id();
        let created_at = self.timestamp();
        self.connection.execute(
            "INSERT INTO ops(id,kind,idempotency_key,status,payload,created_at) VALUES(?,'publish',NULL,'pending',?,?)",
            params![operation_id, payload.to_string(), created_at],
        )?;
        Ok(())
    }

    fn record_operation(
        &mut self,
        kind: &str,
        idempotency_key: Option<&str>,
        payload: &Value,
        status: &str,
    ) -> Result<(String, bool), StoreError> {
        let operation_id = self.id();
        let created_at = self.timestamp();
        if let Some(idempotency_key) = idempotency_key {
            let existing = self.connection.query_row(
                "SELECT id, kind FROM ops WHERE idempotency_key=?",
                params![idempotency_key],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            );
            match existing {
                Ok((id, existing_kind)) => {
                    if existing_kind != kind {
                        return Err(StoreError::Conflict(
                            "idempotency key already used for a different operation kind"
                                .to_string(),
                        ));
                    }
                    return Ok((id, false));
                }
                Err(rusqlite::Error::QueryReturnedNoRows) => {}
                Err(error) => return Err(StoreError::from(error)),
            }
        }
        self.connection.execute(
            "INSERT INTO ops(id,kind,idempotency_key,status,payload,created_at) VALUES(?,?,?,?,?,?)",
            params![
                operation_id,
                kind,
                idempotency_key,
                status,
                payload.to_string(),
                created_at
            ],
        )?;
        Ok((operation_id, true))
    }

    fn find_any_operation(
        &self,
        idempotency_key: &str,
    ) -> Result<Option<OperationRow>, StoreError> {
        let mut statement = self.connection.prepare(
            "SELECT id,kind,idempotency_key,status,payload,created_at FROM ops WHERE idempotency_key=? LIMIT 1",
        )?;
        let mut rows = statement.query(params![idempotency_key])?;
        match rows.next()? {
            Some(row) => Ok(Some(operation_row(row)?)),
            None => Ok(None),
        }
    }

    fn validate_key_max_concurrency(&self, value: i64) -> Result<(), StoreError> {
        if !(KEY_MAX_CONCURRENCY_MIN..=KEY_MAX_CONCURRENCY_MAX).contains(&value) {
            return Err(StoreError::Other(format!(
                "max_concurrency must be an integer in {KEY_MAX_CONCURRENCY_MIN}..{KEY_MAX_CONCURRENCY_MAX}"
            )));
        }
        Ok(())
    }

    fn validate_share_max_concurrency(&self, value: i64) -> Result<(), StoreError> {
        if !(SHARE_MAX_CONCURRENCY_MIN..=SHARE_MAX_CONCURRENCY_MAX).contains(&value) {
            return Err(StoreError::Other(format!(
                "share_max_concurrency must be an integer in {SHARE_MAX_CONCURRENCY_MIN}..{SHARE_MAX_CONCURRENCY_MAX}"
            )));
        }
        Ok(())
    }

    fn protect_for_new_row(
        &self,
        secret: &str,
    ) -> Result<(Option<Vec<u8>>, Option<String>), StoreError> {
        let key = match self.secret_key.as_deref() {
            Some(key) => key,
            None => return Ok((None, None)),
        };
        let blob = protect_with_key(secret, key)?;
        Ok((Some(blob), Some(SECRET_ALGO_AESGCM_FILE_KEY.to_string())))
    }

    fn insert_key(
        &mut self,
        name: &str,
        secret: &str,
        max_concurrency: i64,
    ) -> Result<(String, bool), StoreError> {
        let key_id = self.id();
        let now = self.timestamp();
        let (blob, algo) = self.protect_for_new_row(secret)?;
        self.connection.execute(
            "INSERT INTO keys(key_id,name,secret_hash,secret_version,masked_key,secret_blob,secret_algo,enabled,max_concurrency,created_at,updated_at,deleted_at,row_version) VALUES(?,?,?,1,?,?,?,1,?,?,?,NULL,1)",
            params![
                key_id,
                name,
                secret_hash(secret),
                mask_secret(secret),
                blob,
                algo,
                max_concurrency,
                now,
                now
            ],
        )
        .map_err(|error| match error {
            rusqlite::Error::SqliteFailure(_, Some(message))
                if message.contains("UNIQUE") || message.contains("constraint") =>
            {
                StoreError::Conflict(format!(
                    "name '{name}' already in use as an active identity"
                ))
            }
            other => StoreError::from(other),
        })?;
        self.bump_desired()?;
        Ok((key_id, blob.is_some()))
    }

    pub fn create_key(
        &mut self,
        name: &str,
        secret: Option<&str>,
        idempotency_key: Option<&str>,
        max_concurrency: Option<i64>,
    ) -> Result<Value, StoreError> {
        if name.is_empty() || name.chars().count() > 64 {
            return Err(StoreError::Other("invalid name (1..64 chars)".to_string()));
        }
        let max_concurrency = max_concurrency.unwrap_or(NEW_KEY_DEFAULT_MAX_CONCURRENCY);
        self.validate_key_max_concurrency(max_concurrency)?;
        let secret = match secret {
            Some(secret) => secret.to_string(),
            None => self.new_secret(),
        };
        self.in_transaction(|store| {
            if let Some(idempotency_key) = idempotency_key {
                if let Some(existing) = store.find_any_operation(idempotency_key)? {
                    if existing.kind != "create" {
                        return Err(StoreError::Conflict(
                            "idempotency key already used for a different operation kind"
                                .to_string(),
                        ));
                    }
                    return store.replay_operation(&existing);
                }
            }
            let (key_id, viewable) = store.insert_key(name, &secret, max_concurrency)?;
            store.record_publish_intent(false, "create", Some(&key_id))?;
            if let Some(idempotency_key) = idempotency_key {
                store.record_operation(
                    "create",
                    Some(idempotency_key),
                    &json!({"key_id": key_id, "name": name}),
                    "ok",
                )?;
            }
            let generation = store.generation()?;
            Ok(json!({
                "key_id": key_id,
                "name": name,
                "secret": secret,
                "mk": mask_secret(&secret),
                "plaintext_viewable": viewable,
                "config_generation": generation.get("config_generation").cloned().unwrap_or(Value::Null),
            }))
        })
    }

    fn replay_operation(&mut self, row: &OperationRow) -> Result<Value, StoreError> {
        let payload = row.payload.clone();
        let generation = self.generation()?;
        Ok(json!({
            "key_id": payload.get("key_id").cloned().unwrap_or(Value::Null),
            "secret_version": payload.get("secret_version").cloned().unwrap_or(Value::Null),
            "secret_unavailable": true,
            "replayed": true,
            "config_generation": generation.get("config_generation").cloned().unwrap_or(Value::Null),
        }))
    }

    pub fn get_key(&self, key_id: &str) -> Result<Value, StoreError> {
        let row = self.require_key(key_id)?;
        Ok(public_key_row(&row))
    }

    fn require_key(&self, key_id: &str) -> Result<KeyRow, StoreError> {
        let mut statement = self
            .connection
            .prepare("SELECT * FROM keys WHERE key_id=?")?;
        let mut rows = statement.query(params![key_id])?;
        match rows.next()? {
            Some(row) => Ok(key_row(row)?),
            None => Err(StoreError::NotFound("key identity not found".to_string())),
        }
    }

    pub fn list_keys(&self, include_deleted: bool) -> Result<Vec<Value>, StoreError> {
        let sql = if include_deleted {
            "SELECT * FROM keys ORDER BY created_at"
        } else {
            "SELECT * FROM keys WHERE enabled=1 AND deleted_at IS NULL ORDER BY created_at"
        };
        let mut statement = self.connection.prepare(sql)?;
        let rows = statement.query_map([], key_row)?;
        let rows: Vec<KeyRow> = rows.collect::<Result<Vec<_>, _>>()?;
        Ok(rows.iter().map(public_key_row).collect())
    }

    fn resolve_expected_version(row: &KeyRow, expected: Option<i64>) -> Result<i64, StoreError> {
        let current = row.row_version;
        match expected {
            None => Ok(current),
            Some(expected) => {
                if expected < 1 {
                    return Err(StoreError::Other(
                        "expected_row_version must be a positive integer".to_string(),
                    ));
                }
                if expected != current {
                    return Err(StoreError::VersionMismatch(format!(
                        "row_version conflict: expected {expected}, current {current}"
                    )));
                }
                Ok(current)
            }
        }
    }

    fn update_checked(
        &self,
        key_id: &str,
        expected: i64,
        set_sql: &str,
        values: &[Bind],
    ) -> Result<(), StoreError> {
        let sql = format!("UPDATE keys SET {set_sql} WHERE key_id=? AND row_version=?");
        let mut bound: Vec<Bind> = values.to_vec();
        bound.push(Bind::Text(key_id.to_string()));
        bound.push(Bind::Int(expected));
        let mut statement = self.connection.prepare(&sql)?;
        let changed = statement.execute(rusqlite::params_from_iter(bound.iter()))?;
        if changed == 0 {
            return Err(StoreError::VersionMismatch(
                "row_version conflict".to_string(),
            ));
        }
        Ok(())
    }

    pub fn rename_key(
        &mut self,
        key_id: &str,
        new_name: &str,
        expected_row_version: Option<i64>,
    ) -> Result<Value, StoreError> {
        if new_name.is_empty() || new_name.chars().count() > 64 {
            return Err(StoreError::Other("invalid new name".to_string()));
        }
        self.in_transaction(|store| {
            let row = store.require_key(key_id)?;
            if row.deleted_at.is_some() {
                return Err(StoreError::Disabled(
                    "deleted identity cannot be renamed".to_string(),
                ));
            }
            let expected = Self::resolve_expected_version(&row, expected_row_version)?;
            if row.name == new_name {
                return store.get_key(key_id);
            }
            let now = store.timestamp();
            let result = store.update_checked(
                key_id,
                expected,
                "name=?, updated_at=?, row_version=row_version+1",
                &[Bind::Text(new_name.to_string()), Bind::Text(now)],
            );
            if let Err(error) = result {
                return Err(match error {
                    StoreError::Other(_) => {
                        StoreError::Conflict("display name already in use".to_string())
                    }
                    other => other,
                });
            }
            store.bump_desired()?;
            store.record_publish_intent(false, "rename", Some(key_id))?;
            store.get_key(key_id)
        })
    }

    pub fn update_key(
        &mut self,
        key_id: &str,
        new_name: Option<&str>,
        enabled: Option<bool>,
        max_concurrency: Option<i64>,
        expected_row_version: Option<i64>,
    ) -> Result<Value, StoreError> {
        if let Some(new_name) = new_name {
            if new_name.is_empty() || new_name.chars().count() > 64 {
                return Err(StoreError::Other("invalid new name".to_string()));
            }
        }
        if enabled == Some(true) {
            return Err(StoreError::Other(
                "enabled is not writable: a disabled or revoked identity cannot be re-enabled"
                    .to_string(),
            ));
        }
        if let Some(max_concurrency) = max_concurrency {
            self.validate_key_max_concurrency(max_concurrency)?;
        }
        if new_name.is_none() && enabled.is_none() && max_concurrency.is_none() {
            return Err(StoreError::Other("nothing to update".to_string()));
        }
        self.in_transaction(|store| {
            let row = store.require_key(key_id)?;
            if row.deleted_at.is_some() {
                return Err(StoreError::Disabled(
                    "deleted identity cannot be updated".to_string(),
                ));
            }
            let expected = Self::resolve_expected_version(&row, expected_row_version)?;
            let mut columns: Vec<String> = Vec::new();
            let mut values: Vec<Bind> = Vec::new();
            let mut revoking = false;
            if let Some(new_name) = new_name {
                if new_name != row.name {
                    columns.push("name=?".to_string());
                    values.push(Bind::Text(new_name.to_string()));
                }
            }
            if enabled == Some(false) {
                columns.push("enabled=0".to_string());
                columns.push("deleted_at=?".to_string());
                columns.push("secret_blob=NULL".to_string());
                columns.push("secret_algo=NULL".to_string());
                values.push(Bind::Text(store.timestamp()));
            }
            if let Some(max_concurrency) = max_concurrency {
                if max_concurrency != row.max_concurrency {
                    columns.push("max_concurrency=?".to_string());
                    values.push(Bind::Int(max_concurrency));
                    if max_concurrency < row.max_concurrency {
                        revoking = true;
                    }
                }
            }
            if columns.is_empty() {
                return store.get_key(key_id);
            }
            columns.push("updated_at=?".to_string());
            columns.push("row_version=row_version+1".to_string());
            values.push(Bind::Text(store.timestamp()));
            if enabled == Some(false) || revoking {
                let action = if revoking && enabled != Some(false) {
                    "limit_lower"
                } else {
                    "revoke"
                };
                let generation = store.next_generation()?;
                store.security_record(
                    Some(key_id),
                    action,
                    generation,
                    Some(row.secret_version),
                )?;
            }
            let result = store.update_checked(key_id, expected, &columns.join(", "), &values);
            if let Err(error) = result {
                return Err(match error {
                    StoreError::Other(_) => {
                        StoreError::Conflict("display name already in use".to_string())
                    }
                    other => other,
                });
            }
            store.bump_desired()?;
            store.record_publish_intent(
                revoking || enabled == Some(false),
                "update",
                Some(key_id),
            )?;
            store.get_key(key_id)
        })
    }

    pub fn rotate_key(
        &mut self,
        key_id: &str,
        idempotency_key: Option<&str>,
        expected_row_version: Option<i64>,
    ) -> Result<Value, StoreError> {
        self.in_transaction(|store| {
            if let Some(idempotency_key) = idempotency_key {
                if let Some(existing) = store.find_any_operation(idempotency_key)? {
                    if existing.kind != "rotate" {
                        return Err(StoreError::Conflict(
                            "idempotency key already used for a different operation kind"
                                .to_string(),
                        ));
                    }
                    let prior_key = existing
                        .payload
                        .get("key_id")
                        .and_then(Value::as_str);
                    if prior_key != Some(key_id) {
                        return Err(StoreError::Conflict(
                            "idempotency key already used to rotate a different key".to_string(),
                        ));
                    }
                    return store.replay_operation(&existing);
                }
            }
            let row = store.require_key(key_id)?;
            if row.deleted_at.is_some() {
                return Err(StoreError::Disabled(
                    "deleted identity cannot be rotated".to_string(),
                ));
            }
            let expected = Self::resolve_expected_version(&row, expected_row_version)?;
            let secret = store.new_secret();
            let (blob, algo) = store.protect_for_new_row(&secret)?;
            let new_version = row.secret_version + 1;
            let generation = store.next_generation()?;
            store.security_record(Some(key_id), "rotate", generation, Some(new_version))?;
            let now = store.timestamp();
            store.update_checked(
                key_id,
                expected,
                "secret_hash=?, secret_version=?, masked_key=?, secret_blob=?, secret_algo=?, updated_at=?, row_version=row_version+1",
                &[
                    Bind::Text(secret_hash(&secret)),
                    Bind::Int(new_version),
                    Bind::Text(mask_secret(&secret)),
                    blob.clone().map(Bind::Blob).unwrap_or(Bind::Null),
                    algo.clone().map(Bind::Text).unwrap_or(Bind::Null),
                    Bind::Text(now),
                ],
            )?;
            store.bump_desired()?;
            store.record_publish_intent(true, "rotate", Some(key_id))?;
            if let Some(idempotency_key) = idempotency_key {
                store.record_operation(
                    "rotate",
                    Some(idempotency_key),
                    &json!({"key_id": key_id, "secret_version": new_version}),
                    "ok",
                )?;
            }
            let generation = store.generation()?;
            Ok(json!({
                "key_id": key_id,
                "secret": secret,
                "mk": mask_secret(&secret),
                "secret_version": new_version,
                "plaintext_viewable": blob.is_some(),
                "config_generation": generation.get("config_generation").cloned().unwrap_or(Value::Null),
            }))
        })
    }

    pub fn delete_key(
        &mut self,
        key_id: &str,
        expected_row_version: Option<i64>,
    ) -> Result<Value, StoreError> {
        self.in_transaction(|store| {
            let row = store.require_key(key_id)?;
            let expected = Self::resolve_expected_version(&row, expected_row_version)?;
            if let Some(deleted_at) = row.deleted_at.clone() {
                return Ok(json!({
                    "key_id": key_id,
                    "deleted_at": deleted_at,
                    "already_deleted": true,
                }));
            }
            let now = store.timestamp();
            let generation = store.next_generation()?;
            let secret_version = row.secret_version;
            store.security_record(Some(key_id), "delete", generation, Some(secret_version))?;
            store.update_checked(
                key_id,
                expected,
                "enabled=0, deleted_at=?, secret_blob=NULL, secret_algo=NULL, updated_at=?, row_version=row_version+1",
                &[Bind::Text(now.clone()), Bind::Text(now.clone())],
            )?;
            store.bump_desired()?;
            store.record_publish_intent(true, "delete", Some(key_id))?;
            Ok(json!({"key_id": key_id, "deleted_at": now}))
        })
    }

    pub fn reveal_key_secret(
        &mut self,
        key_id: &str,
        expected_row_version: Option<i64>,
    ) -> Result<Value, StoreError> {
        let row = self.require_key(key_id)?;
        if row.deleted_at.is_some() {
            return Err(StoreError::Disabled(
                "deleted identity has no readable secret".to_string(),
            ));
        }
        if let Some(expected_row_version) = expected_row_version {
            if expected_row_version < 1 {
                return Err(StoreError::Other(
                    "expected_row_version must be a positive integer".to_string(),
                ));
            }
            if expected_row_version != row.row_version {
                return Err(StoreError::VersionMismatch(format!(
                    "row_version conflict: expected {expected_row_version}, current {}",
                    row.row_version
                )));
            }
        }
        let blob = row.secret_blob.clone();
        let blob = match blob {
            Some(blob) if !blob.is_empty() => blob,
            _ => {
                return Err(StoreError::SecretUnavailable(
                    "legacy or unprotected row stores no ciphertext; rotate to enter the reviewable contract"
                        .to_string(),
                ))
            }
        };
        let algo = row.secret_algo.clone();
        let algo = match algo {
            Some(algo) if !algo.is_empty() => algo,
            _ => {
                return Err(StoreError::SecretUndecryptable(
                    "stored ciphertext has no algorithm marker; refusing to guess a scheme"
                        .to_string(),
                ))
            }
        };
        if algo == SECRET_ALGO_DPAPI_USER {
            return Err(StoreError::SecretUndecryptable(
                "stored ciphertext cannot be decrypted under this runtime identity".to_string(),
            ));
        }
        let key = match self.secret_key.as_deref() {
            Some(key) => key,
            None => {
                return Err(StoreError::SecretUndecryptable(
                    "stored ciphertext cannot be decrypted under this runtime identity".to_string(),
                ))
            }
        };
        let secret = unprotect_with_key(&blob, &algo, key).map_err(|_| {
            StoreError::SecretUndecryptable(
                "stored ciphertext cannot be decrypted under this runtime identity".to_string(),
            )
        })?;
        if !secret_eq(&secret, &row.secret_hash) {
            return Err(StoreError::SecretUndecryptable(
                "decrypted secret does not match the stored digest".to_string(),
            ));
        }
        Ok(json!({
            "key_id": key_id,
            "name": row.name,
            "masked_key": row.masked_key,
            "secret_version": row.secret_version,
            "secret": secret,
        }))
    }

    pub fn verify_secret(&self, key_id: &str, secret: &str) -> Result<bool, StoreError> {
        let row = self.require_key(key_id)?;
        if row.deleted_at.is_some() || row.enabled == 0 {
            return Ok(false);
        }
        Ok(secret_eq(secret, &row.secret_hash))
    }

    // --------------------------------------------------------- global limit
    pub fn share_max_concurrency(&self) -> Result<i64, StoreError> {
        let value: Option<String> = self
            .connection
            .query_row(
                "SELECT value FROM meta WHERE key='share_max_concurrency'",
                [],
                |row| row.get(0),
            )
            .map(Some)
            .or_else(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => Ok(None),
                other => Err(other),
            })?;
        match value {
            None => Ok(crate::schema::LEGACY_SHARE_MAX_CONCURRENCY),
            Some(value) => value.parse::<i64>().map_err(|_| {
                StoreError::Other(format!("invalid stored share_max_concurrency {value:?}"))
            }),
        }
    }

    pub fn set_share_max_concurrency(
        &mut self,
        value: i64,
        expected: Option<i64>,
        idempotency_key: Option<&str>,
    ) -> Result<Value, StoreError> {
        self.validate_share_max_concurrency(value)?;
        self.in_transaction(|store| {
            if let Some(idempotency_key) = idempotency_key {
                if let Some(existing) = store.find_any_operation(idempotency_key)? {
                    if existing.kind != "set_share_limits" {
                        return Err(StoreError::Conflict(
                            "idempotency key already used for a different operation kind"
                                .to_string(),
                        ));
                    }
                    return store.replay_share_limit(&existing);
                }
            }
            let current = store.share_max_concurrency()?;
            if let Some(expected) = expected {
                if expected != current {
                    return Err(StoreError::VersionMismatch(format!(
                        "share_max_concurrency conflict: expected {expected}, current {current}"
                    )));
                }
            }
            if value == current {
                return Ok(json!({
                    "share_max_concurrency": current,
                    "previous": current,
                    "changed": false,
                    "replayed": false,
                    "config_generation": store.generation()?.get("config_generation").cloned().unwrap_or(Value::Null),
                }));
            }
            if value < current {
                let generation = store.next_generation()?;
                store.security_record(None, "limit_lower", generation, None)?;
            }
            store.connection.execute(
                "UPDATE meta SET value=? WHERE key='share_max_concurrency'",
                params![value.to_string()],
            )?;
            store.bump_desired()?;
            store.record_publish_intent(value < current, "set_share_limits", None)?;
            if let Some(idempotency_key) = idempotency_key {
                store.record_operation(
                    "set_share_limits",
                    Some(idempotency_key),
                    &json!({"share_max_concurrency": value}),
                    "ok",
                )?;
            }
            Ok(json!({
                "share_max_concurrency": value,
                "previous": current,
                "changed": true,
                "replayed": false,
                "config_generation": store.generation()?.get("config_generation").cloned().unwrap_or(Value::Null),
            }))
        })
    }

    fn replay_share_limit(&mut self, row: &OperationRow) -> Result<Value, StoreError> {
        Ok(json!({
            "share_max_concurrency": row.payload.get("share_max_concurrency").cloned().unwrap_or(Value::Null),
            "replayed": true,
            "changed": false,
            "config_generation": self.generation()?.get("config_generation").cloned().unwrap_or(Value::Null),
        }))
    }

    // -------------------------------------------------------------- ops/IDP
    pub fn operations(
        &self,
        kind: Option<&str>,
        status: Option<&str>,
    ) -> Result<Vec<OperationRow>, StoreError> {
        let mut sql =
            String::from("SELECT id,kind,idempotency_key,status,payload,created_at FROM ops");
        let mut clauses = Vec::new();
        let mut bound: Vec<Bind> = Vec::new();
        if let Some(kind) = kind {
            clauses.push("kind=?");
            bound.push(Bind::Text(kind.to_string()));
        }
        if let Some(status) = status {
            clauses.push("status=?");
            bound.push(Bind::Text(status.to_string()));
        }
        if !clauses.is_empty() {
            sql.push_str(" WHERE ");
            sql.push_str(&clauses.join(" AND "));
        }
        sql.push_str(" ORDER BY created_at");
        let mut statement = self.connection.prepare(&sql)?;
        let rows = statement.query_map(rusqlite::params_from_iter(bound.iter()), |row| {
            operation_row(row)
        })?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(StoreError::from)
    }

    pub fn find_pending_publish_intent(
        &self,
        generation: i64,
    ) -> Result<Option<OperationRow>, StoreError> {
        for operation in self.operations(Some("publish"), Some("pending"))? {
            if operation
                .payload
                .get("target_generation")
                .and_then(Value::as_i64)
                == Some(generation)
            {
                return Ok(Some(operation));
            }
        }
        Ok(None)
    }

    pub fn update_operation(
        &mut self,
        operation_id: &str,
        status: &str,
        payload_merge: Option<&Map<String, Value>>,
    ) -> Result<bool, StoreError> {
        let mut payload: Map<String, Value> = self
            .connection
            .query_row(
                "SELECT payload FROM ops WHERE id=?",
                params![operation_id],
                |row| row.get::<_, Option<String>>(0),
            )
            .map_err(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => error,
                other => other,
            })
            .and_then(|text| {
                serde_json::from_str::<Value>(text.as_deref().unwrap_or("{}"))
                    .map_err(|error| rusqlite::Error::InvalidParameterName(error.to_string()))
            })
            .map(|value| value.as_object().cloned().unwrap_or_default())
            .or_else(|error| match error {
                rusqlite::Error::QueryReturnedNoRows => Ok(Map::new()),
                other => Err(other),
            })?;
        if let Some(payload_merge) = payload_merge {
            for (key, value) in payload_merge {
                payload.insert(key.clone(), value.clone());
            }
        }
        let updated = self.connection.execute(
            "UPDATE ops SET status=?, payload=? WHERE id=?",
            params![status, Value::Object(payload).to_string(), operation_id],
        )?;
        Ok(updated > 0)
    }

    // ----------------------------------------------------------- projection
    pub fn render_projection(&mut self) -> Result<Value, StoreError> {
        self.connection.execute("BEGIN", [])?;
        let result = (|| -> Result<Value, StoreError> {
            let entries = {
                let mut statement = self.connection.prepare(
                    "SELECT key_id, name, secret_hash, secret_version, enabled, max_concurrency FROM keys WHERE deleted_at IS NULL ORDER BY created_at",
                )?;
                let entries: Vec<Value> = statement
                    .query_map([], |row| {
                        Ok(json!({
                            "key_id": row.get::<_, String>(0)?,
                            "name": row.get::<_, String>(1)?,
                            "secret_hash": row.get::<_, String>(2)?,
                            "secret_version": row.get::<_, i64>(3)?,
                            "enabled": row.get::<_, i64>(4)? != 0,
                            "max_concurrency": row.get::<_, i64>(5)?.min(KEY_MAX_CONCURRENCY_MAX),
                        }))
                    })?
                    .collect::<Result<Vec<_>, _>>()?;
                entries
            };
            let share_max = self.share_max_concurrency()?.min(SHARE_MAX_CONCURRENCY_MAX);
            let generation = self.generation()?;
            Ok(json!({
                "schema_version": PROJECTION_SCHEMA_VERSION,
                "config_generation": generation.get("config_generation").cloned().unwrap_or(Value::Null),
                "share_max_concurrency": share_max,
                "data": entries,
            }))
        })();
        self.connection.execute("ROLLBACK", [])?;
        result
    }

    /// Fingerprint the rendered projection with the shared canonical encoder.
    pub fn projection_fingerprint(&mut self) -> Result<String, StoreError> {
        Ok(fingerprint(&self.render_projection()?))
    }

    /// The projection schema marker (2), never the local DB marker (3).
    pub fn projection_schema_version(&self) -> i64 {
        PROJECTION_SCHEMA_VERSION
    }

    /// The local DB schema marker (3).
    pub fn schema_version(&self) -> i64 {
        SCHEMA_VERSION
    }
}

#[derive(Debug, Clone)]
pub struct OperationRow {
    pub id: String,
    pub kind: String,
    pub idempotency_key: Option<String>,
    pub status: String,
    pub payload: Value,
    pub created_at: String,
}

fn operation_row(row: &Row) -> rusqlite::Result<OperationRow> {
    let payload: Option<String> = row.get("payload")?;
    Ok(OperationRow {
        id: row.get("id")?,
        kind: row.get("kind")?,
        idempotency_key: row.get("idempotency_key")?,
        status: row.get("status")?,
        payload: payload
            .as_deref()
            .and_then(|text| serde_json::from_str(text).ok())
            .unwrap_or_else(|| json!({})),
        created_at: row.get("created_at")?,
    })
}

#[derive(Debug, Clone)]
pub struct KeyRow {
    pub key_id: String,
    pub name: String,
    pub secret_hash: String,
    pub secret_version: i64,
    pub masked_key: String,
    pub secret_blob: Option<Vec<u8>>,
    pub secret_algo: Option<String>,
    pub enabled: i64,
    pub max_concurrency: i64,
    pub created_at: String,
    pub updated_at: String,
    pub deleted_at: Option<String>,
    pub row_version: i64,
}

fn key_row(row: &Row) -> rusqlite::Result<KeyRow> {
    Ok(KeyRow {
        key_id: row.get("key_id")?,
        name: row.get("name")?,
        secret_hash: row.get("secret_hash")?,
        secret_version: row.get("secret_version")?,
        masked_key: row.get("masked_key")?,
        secret_blob: row.get("secret_blob")?,
        secret_algo: row.get("secret_algo")?,
        enabled: row.get("enabled")?,
        max_concurrency: row.get("max_concurrency")?,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
        deleted_at: row.get("deleted_at")?,
        row_version: row.get("row_version")?,
    })
}

fn public_key_row(row: &KeyRow) -> Value {
    json!({
        "key_id": row.key_id,
        "name": row.name,
        "secret_version": row.secret_version,
        "masked_key": row.masked_key,
        "plaintext_viewable": row.secret_blob.is_some(),
        "enabled": row.enabled != 0,
        "max_concurrency": row.max_concurrency,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "row_version": row.row_version,
        "deleted_at": row.deleted_at,
    })
}

fn public_security_row(row: &Row) -> Value {
    json!({
        "record_id": row.get::<_, String>("record_id").unwrap_or_default(),
        "key_id": row.get::<_, Option<String>>("key_id").unwrap_or(None),
        "generation": row.get::<_, i64>("generation").unwrap_or_default(),
        "action": row.get::<_, String>("action").unwrap_or_default(),
        "secret_version": row.get::<_, Option<i64>>("secret_version").unwrap_or(None),
        "status": row.get::<_, String>("status").unwrap_or_default(),
        "recorded_at": row.get::<_, String>("recorded_at").unwrap_or_default(),
        "applied_at": row.get::<_, Option<String>>("applied_at").unwrap_or(None),
        "applied_generation": row.get::<_, Option<i64>>("applied_generation").unwrap_or(None),
        "instance_id": row.get::<_, Option<String>>("instance_id").unwrap_or(None),
        "remote_confirmed_at": row.get::<_, Option<String>>("remote_confirmed_at").unwrap_or(None),
        "remote_receipt": row.get::<_, Option<String>>("remote_receipt").unwrap_or(None),
    })
}

/// One SQL-bound value that keeps bytes as a BLOB (never a JSON array/text).
#[derive(Debug, Clone)]
pub enum Bind {
    Null,
    Int(i64),
    Text(String),
    Blob(Vec<u8>),
}

impl rusqlite::ToSql for Bind {
    fn to_sql(&self) -> rusqlite::Result<rusqlite::types::ToSqlOutput<'_>> {
        use rusqlite::types::ValueRef;

        Ok(match self {
            Bind::Null => rusqlite::types::ToSqlOutput::Borrowed(ValueRef::Null),
            Bind::Int(value) => {
                rusqlite::types::ToSqlOutput::Owned(rusqlite::types::Value::Integer(*value))
            }
            Bind::Text(value) => {
                rusqlite::types::ToSqlOutput::Owned(rusqlite::types::Value::Text(value.clone()))
            }
            Bind::Blob(value) => {
                rusqlite::types::ToSqlOutput::Owned(rusqlite::types::Value::Blob(value.clone()))
            }
        })
    }
}

/// Python `secrets.token_urlsafe(32)`: 32 random bytes, base64url, no padding.
pub fn random_token_urlsafe(bytes: usize) -> String {
    let mut raw = vec![0u8; bytes];
    if getrandom::getrandom(&mut raw).is_err() {
        panic!("share store cannot read OS entropy");
    }
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut output = String::new();
    let mut index = 0;
    while index < raw.len() {
        let b0 = raw[index] as usize;
        let b1 = raw.get(index + 1).copied().unwrap_or(0) as usize;
        let b2 = raw.get(index + 2).copied().unwrap_or(0) as usize;
        output.push(ALPHABET[b0 >> 2] as char);
        if index + 1 < raw.len() {
            output.push(ALPHABET[((b0 & 0x03) << 4 | b1 >> 4) & 0x3f] as char);
        }
        if index + 2 < raw.len() {
            output.push(ALPHABET[((b1 & 0x0f) << 2 | b2 >> 6) & 0x3f] as char);
            output.push(ALPHABET[b2 & 0x3f] as char);
        } else if index + 1 < raw.len() {
            output.push(ALPHABET[(b1 & 0x0f) << 2] as char);
        }
        index += 3;
    }
    output
}
