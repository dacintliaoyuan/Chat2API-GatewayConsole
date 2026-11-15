//! S6a: the 8002 share-management Key write face (`usagesrv.py` facade +
//! `share/management.py`).
//!
//! The Rust owner opens the SAME `share-store.db` schema 3 the Python store
//! writes and never migrates it. Mutations run in the store's single
//! `BEGIN IMMEDIATE` transaction; the projection is published through an
//! injected broker seam. Revoking directions (rotate/delete/revoke/lower limit)
//! are fail-closed: an unconfirmed publication answers 503 and records a
//! blocked publish intent, never a fake success. Plaintext rides only the
//! immediate create/rotate response and never an operation row.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use um_store::secret::load_key_file;
use um_store::{fingerprint, ShareStore, StoreError};

pub const KEY_MAX_CONCURRENCY_MIN: i64 = 1;
pub const KEY_MAX_CONCURRENCY_MAX: i64 = 48;
pub const SHARE_MAX_CONCURRENCY_MIN: i64 = 1;
pub const SHARE_MAX_CONCURRENCY_MAX: i64 = 64;

#[derive(Debug, Clone, PartialEq)]
pub struct ShareWriteError {
    pub kind: ShareWriteErrorKind,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShareWriteErrorKind {
    NotFound,
    Conflict,
    Invalid,
    Broker,
}

impl ShareWriteError {
    pub fn store(error: StoreError) -> Self {
        let kind = match error {
            StoreError::NotFound(_) => ShareWriteErrorKind::NotFound,
            StoreError::Conflict(_) | StoreError::Disabled(_) | StoreError::VersionMismatch(_) => {
                ShareWriteErrorKind::Conflict
            }
            StoreError::MigrationConflict(_)
            | StoreError::SecretUnavailable(_)
            | StoreError::SecretUndecryptable(_)
            | StoreError::Other(_) => ShareWriteErrorKind::Invalid,
        };
        Self {
            kind,
            message: error.message().to_string(),
        }
    }

    pub fn broker(message: impl Into<String>) -> Self {
        Self {
            kind: ShareWriteErrorKind::Broker,
            message: message.into(),
        }
    }

    pub fn status(&self) -> u16 {
        match self.kind {
            ShareWriteErrorKind::NotFound => 404,
            ShareWriteErrorKind::Conflict => 409,
            ShareWriteErrorKind::Invalid => 400,
            ShareWriteErrorKind::Broker => 503,
        }
    }
}

impl From<StoreError> for ShareWriteError {
    fn from(error: StoreError) -> Self {
        Self::store(error)
    }
}

/// The broker publication seam. An unwired seam is an honest 503; it never
/// pretends a mutation was published.
pub trait ShareWriteSeams: Send + Sync {
    fn publish(
        &self,
        projection: &Value,
        terminate_keys: Option<&[String]>,
    ) -> Result<Value, ShareWriteError>;

    fn emergency_stop(&self) -> Result<Value, ShareWriteError>;

    /// `ShareManager.start`: a lifecycle action is a broker/host operation, so
    /// an unwired seam refuses instead of inventing an operation id.
    fn start(
        &self,
        _explicit: bool,
        _idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        Err(ShareWriteError::broker(
            "share host lifecycle seam is not wired; refusing to fake an accepted action",
        ))
    }

    fn graceful_stop(&self, _idempotency_key: Option<&str>) -> Result<Value, ShareWriteError> {
        Err(ShareWriteError::broker(
            "share host lifecycle seam is not wired; refusing to fake an accepted action",
        ))
    }

    fn emergency_stop_action(
        &self,
        _idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        Err(ShareWriteError::broker(
            "share host lifecycle seam is not wired; refusing to fake an accepted action",
        ))
    }

    /// `BrokerClientAdapter.host_verify("running")`: three-state evidence.
    fn gateway_running(&self) -> Option<bool> {
        None
    }

    /// The explicit version-bound public probe. `None` means the probe seam is
    /// not wired; the caller reports an honest failure, never a success.
    fn probe_models(&self, _secret: &str, _public_url: &str) -> Option<Value> {
        None
    }

    fn probe_models_private(&self, _secret: &str) -> Option<Value> {
        None
    }
}

pub struct UnwiredWriteSeams;

impl ShareWriteSeams for UnwiredWriteSeams {
    fn publish(
        &self,
        _projection: &Value,
        _terminate_keys: Option<&[String]>,
    ) -> Result<Value, ShareWriteError> {
        Err(ShareWriteError::broker(
            "broker 操作门未接线（部署授权包装配后可用）；不冒充可用",
        ))
    }

    fn emergency_stop(&self) -> Result<Value, ShareWriteError> {
        Err(ShareWriteError::broker(
            "broker 操作门未接线（部署授权包装配后可用）；不冒充可用",
        ))
    }
}

#[derive(Debug, Default, Clone)]
struct AckCounts {
    terminated: Option<i64>,
    released: Option<i64>,
    aborted: Option<i64>,
    upstream_closed: Option<i64>,
}

pub struct ShareWriteStore {
    path: Option<PathBuf>,
    secret_key_file: Option<PathBuf>,
    seams: Arc<dyn ShareWriteSeams>,
    last_published: Mutex<Option<(i64, String)>>,
    last_failure: Mutex<Option<Value>>,
    last_counts: Mutex<AckCounts>,
    active_probes: Mutex<HashSet<String>>,
}

impl ShareWriteStore {
    pub fn new(
        path: PathBuf,
        secret_key_file: Option<PathBuf>,
        seams: Arc<dyn ShareWriteSeams>,
    ) -> Self {
        Self::from_config(Some(path), secret_key_file, seams)
    }

    pub fn from_config(
        path: Option<PathBuf>,
        secret_key_file: Option<PathBuf>,
        seams: Arc<dyn ShareWriteSeams>,
    ) -> Self {
        Self {
            path,
            secret_key_file,
            seams,
            last_published: Mutex::new(None),
            last_failure: Mutex::new(None),
            last_counts: Mutex::new(AckCounts::default()),
            active_probes: Mutex::new(HashSet::new()),
        }
    }

    fn open(&self) -> Result<ShareStore, ShareWriteError> {
        let Some(path) = &self.path else {
            return Err(ShareWriteError::store(StoreError::Other(
                "share store path is not configured".to_string(),
            )));
        };
        let key = load_key_file(self.secret_key_file.as_deref())?;
        ShareStore::open_with_key(path, key).map_err(ShareWriteError::store)
    }

    pub fn create_key(
        &self,
        name: &str,
        max_concurrency: Option<i64>,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        if name.is_empty() || name.chars().count() > 64 {
            return Err(ShareWriteError::store(StoreError::Other(
                "invalid name".to_string(),
            )));
        }
        if let Some(max_concurrency) = max_concurrency {
            if !(KEY_MAX_CONCURRENCY_MIN..=KEY_MAX_CONCURRENCY_MAX).contains(&max_concurrency) {
                return Err(ShareWriteError::store(StoreError::Other(format!(
                    "max_concurrency must be an integer in {KEY_MAX_CONCURRENCY_MIN}..{KEY_MAX_CONCURRENCY_MAX}"
                ))));
            }
        }
        let mut store = self.open()?;
        let result = store
            .create_key(name, None, idempotency_key, max_concurrency)
            .map_err(ShareWriteError::store)?;
        let mut payload = self.publish_response(&mut store, &result)?;
        let key_id = result
            .get("key_id")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let key = store.get_key(key_id).map_err(ShareWriteError::store)?;
        let secret = result.get("secret").cloned().unwrap_or(Value::Null);
        payload["key"] = console_public_row(&key);
        payload["plaintext"] = secret;
        Ok(payload)
    }

    pub fn update_key(
        &self,
        key_id: &str,
        name: Option<&str>,
        max_concurrency: Option<i64>,
        expected_row_version: i64,
    ) -> Result<Value, ShareWriteError> {
        if let Some(name) = name {
            if name.is_empty() || name.chars().count() > 64 {
                return Err(ShareWriteError::store(StoreError::Other(
                    "invalid name".to_string(),
                )));
            }
        }
        if let Some(max_concurrency) = max_concurrency {
            if !(KEY_MAX_CONCURRENCY_MIN..=KEY_MAX_CONCURRENCY_MAX).contains(&max_concurrency) {
                return Err(ShareWriteError::store(StoreError::Other(format!(
                    "max_concurrency must be an integer in {KEY_MAX_CONCURRENCY_MIN}..{KEY_MAX_CONCURRENCY_MAX}"
                ))));
            }
        }
        let mut store = self.open()?;
        let before = generation_of(&mut store)?;
        let result = store
            .update_key(
                key_id,
                name,
                None,
                max_concurrency,
                Some(expected_row_version),
            )
            .map_err(ShareWriteError::store)?;
        let after = generation_of(&mut store)?;
        let revoking = if after > before {
            store
                .find_pending_publish_intent(after)
                .map_err(ShareWriteError::store)?
                .map(|operation| {
                    operation
                        .payload
                        .get("revoking")
                        .and_then(Value::as_bool)
                        .unwrap_or(false)
                })
                .unwrap_or(false)
        } else {
            false
        };
        if revoking {
            return self.require_applied(&mut store, result, None);
        }
        self.publish_response(&mut store, &result)
    }

    pub fn set_share_limits(
        &self,
        share_max_concurrency: i64,
        expected_share_max: Option<i64>,
    ) -> Result<Value, ShareWriteError> {
        if !(SHARE_MAX_CONCURRENCY_MIN..=SHARE_MAX_CONCURRENCY_MAX).contains(&share_max_concurrency)
        {
            return Err(ShareWriteError::store(StoreError::Other(format!(
                "share_max_concurrency must be an integer in {SHARE_MAX_CONCURRENCY_MIN}..{SHARE_MAX_CONCURRENCY_MAX}"
            ))));
        }
        let mut store = self.open()?;
        let result = store
            .set_share_max_concurrency(share_max_concurrency, expected_share_max, None)
            .map_err(ShareWriteError::store)?;
        let changed = result
            .get("changed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let previous = result
            .get("previous")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        if changed && share_max_concurrency < previous {
            return self.require_applied(&mut store, result, None);
        }
        self.publish_response(&mut store, &result)
    }

    pub fn rotate_key(
        &self,
        key_id: &str,
        idempotency_key: Option<&str>,
        expected_row_version: i64,
    ) -> Result<Value, ShareWriteError> {
        let mut store = self.open()?;
        let result = store
            .rotate_key(key_id, idempotency_key, Some(expected_row_version))
            .map_err(ShareWriteError::store)?;
        let mut payload = self.require_applied(&mut store, result, None)?;
        let key = store.get_key(key_id).map_err(ShareWriteError::store)?;
        payload["key"] = console_public_row(&key);
        payload["plaintext"] = payload.get("secret").cloned().unwrap_or(Value::Null);
        Ok(payload)
    }

    pub fn delete_key(
        &self,
        key_id: &str,
        expected_row_version: i64,
    ) -> Result<Value, ShareWriteError> {
        let mut store = self.open()?;
        let result = store
            .delete_key(key_id, Some(expected_row_version))
            .map_err(ShareWriteError::store)?;
        self.require_applied(&mut store, result, None)
    }

    pub fn revoke_and_terminate_key(
        &self,
        key_id: &str,
        expected_row_version: i64,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        let mut payload =
            self.revoke_and_terminate(key_id, expected_row_version, idempotency_key)?;
        let store = self.open()?;
        let key = store.get_key(key_id).map_err(ShareWriteError::store)?;
        payload["key"] = console_public_row(&key);
        payload["revoked"] = json!(true);
        Ok(payload)
    }

    /// The legacy `/api/share/*` answer: the manager result without the
    /// facade-only `key`/`plaintext` fields.
    fn revoke_and_terminate(
        &self,
        key_id: &str,
        expected_row_version: i64,
        _idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        let mut store = self.open()?;
        let result = store
            .update_key(key_id, None, Some(false), None, Some(expected_row_version))
            .map_err(ShareWriteError::store)?;
        let terminate_keys = [key_id.to_string()];
        let disabled = self.require_applied(&mut store, result, Some(&terminate_keys))?;
        let counts = self
            .last_counts
            .lock()
            .map(|counts| counts.clone())
            .unwrap_or_default();
        let confirmed = counts.terminated.is_some();
        let mut payload = disabled.as_object().cloned().unwrap_or_default();
        payload.insert("terminated".to_string(), json!(counts.terminated));
        payload.insert("released".to_string(), json!(counts.released));
        payload.insert("aborted".to_string(), json!(counts.aborted));
        payload.insert("upstream_closed".to_string(), json!(counts.upstream_closed));
        payload.insert(
            "note".to_string(),
            json!(if confirmed {
                "已撤销并确认终止在途请求；已提交上游的消耗不可撤回"
            } else {
                "已撤销；在途确认未完成；已提交上游的消耗不可撤回"
            }),
        );
        Ok(Value::Object(payload))
    }

    /// The legacy `/api/share/keys` answer: the manager result without the
    /// facade-only `key`/`plaintext` fields.
    pub fn create_key_legacy(
        &self,
        name: &str,
        max_concurrency: Option<i64>,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        let mut payload = self.create_key(name, max_concurrency, idempotency_key)?;
        strip_facade_fields(&mut payload);
        Ok(payload)
    }

    pub fn rotate_key_legacy(
        &self,
        key_id: &str,
        idempotency_key: Option<&str>,
        expected_row_version: i64,
    ) -> Result<Value, ShareWriteError> {
        let mut payload = self.rotate_key(key_id, idempotency_key, expected_row_version)?;
        strip_facade_fields(&mut payload);
        Ok(payload)
    }

    /// `ShareManager.start`: the lifecycle action is a broker/host operation.
    pub fn start(
        &self,
        explicit: bool,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        self.seams.start(explicit, idempotency_key)
    }

    pub fn stop(&self, idempotency_key: Option<&str>) -> Result<Value, ShareWriteError> {
        self.seams.graceful_stop(idempotency_key)
    }

    pub fn emergency_stop_legacy(
        &self,
        idempotency_key: Option<&str>,
    ) -> Result<Value, ShareWriteError> {
        self.seams.emergency_stop_action(idempotency_key)
    }

    /// `ShareManager.normalize_limits`: the explicit operator normalization
    /// action, never a silent startup migration. A lowering whose publication
    /// was not confirmed is reported through `blocked`.
    pub fn normalize_limits(&self) -> Result<Value, ShareWriteError> {
        let store = self.open()?;
        let stored = store
            .share_max_concurrency()
            .map_err(ShareWriteError::store)?;
        let global_before = stored;
        let mut global_after = stored;
        let mut blocked = false;
        let mut changed = false;
        if stored > SHARE_MAX_CONCURRENCY_MAX {
            if self
                .set_share_limits(SHARE_MAX_CONCURRENCY_MAX, Some(stored))
                .is_err()
            {
                blocked = true;
            }
            global_after = SHARE_MAX_CONCURRENCY_MAX;
            changed = true;
        } else if stored < SHARE_MAX_CONCURRENCY_MAX {
            self.set_share_limits(SHARE_MAX_CONCURRENCY_MAX, None)?;
            global_after = SHARE_MAX_CONCURRENCY_MAX;
            changed = true;
        }
        let rows = store.list_keys(false).map_err(ShareWriteError::store)?;
        let mut keys = Vec::new();
        for row in rows {
            let before = row
                .get("max_concurrency")
                .and_then(Value::as_i64)
                .unwrap_or_default();
            if before <= KEY_MAX_CONCURRENCY_MAX {
                continue;
            }
            let key_id = row
                .get("key_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let row_version = row
                .get("row_version")
                .and_then(Value::as_i64)
                .unwrap_or_default();
            if self
                .update_key(key_id, None, Some(KEY_MAX_CONCURRENCY_MAX), row_version)
                .is_err()
            {
                blocked = true;
            }
            keys.push(json!({
                "id": key_id,
                "before": before,
                "after": KEY_MAX_CONCURRENCY_MAX,
            }));
            changed = true;
        }
        Ok(json!({
            "changed": changed,
            "global": {"before": global_before, "after": global_after},
            "keys": keys,
            "blocked": blocked,
        }))
    }

    /// `console/share_probe.py::test_key`: the explicit version-bound probe.
    /// The plaintext reaches only the injected probe seam and never the
    /// response, an operation record, a URL or an error.
    pub fn test_key(
        &self,
        key_id: &str,
        expected_row_version: i64,
        private: bool,
        public_origin: Option<&str>,
    ) -> Value {
        let mut result = json!({
            "key_id": key_id,
            "row_version": expected_row_version,
            "connected": false,
            "reason": "unconfirmed",
            "stage": "configuration",
            "tested_from": if private { "private" } else { "public" },
            "public_url": Value::Null,
            "dns": Value::Null,
            "checked_at": epoch_seconds(),
        });
        let Some(_guard) = self.begin_probe(key_id) else {
            result["reason"] = json!("test_in_progress");
            return result;
        };
        let mut store = match self.open() {
            Ok(store) => store,
            Err(error) => {
                result["reason"] = json!(if error.kind == ShareWriteErrorKind::NotFound {
                    "key_changed"
                } else {
                    "secret_unavailable"
                });
                return result;
            }
        };
        let row = match store.get_key(key_id) {
            Ok(row) => row,
            Err(_) => {
                result["reason"] = json!("key_changed");
                return result;
            }
        };
        if row.get("row_version").and_then(Value::as_i64) != Some(expected_row_version) {
            result["reason"] = json!("key_changed");
            return result;
        }
        if !row.get("deleted_at").map(Value::is_null).unwrap_or(true)
            || row.get("enabled").and_then(Value::as_bool) != Some(true)
        {
            result["reason"] = json!("key_disabled");
            return result;
        }
        if !private {
            // The old Funnel fallback is retired (#33): an unconfigured or
            // invalid public origin is never replaced by a guessed host.
            let resolved = crate::share_read::resolve_public_origin(public_origin);
            result["public_url"] = if resolved.source == Some("configured") {
                resolved.url.map(Value::String).unwrap_or(Value::Null)
            } else {
                Value::Null
            };
            if result["public_url"].is_null() {
                result["reason"] = json!("public_url_unavailable");
                return result;
            }
        }
        if self.seams.gateway_running() != Some(true) {
            result["reason"] = json!("gateway_unconfirmed");
            return result;
        }
        let secret = match store.reveal_key_secret(key_id, Some(expected_row_version)) {
            Ok(payload) => payload
                .get("secret")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string(),
            Err(error) => {
                result["reason"] = json!(match error.kind() {
                    "NotFound" | "Conflict" | "Disabled" | "VersionMismatch" => {
                        "key_changed"
                    }
                    _ => "secret_unavailable",
                });
                return result;
            }
        };
        let outcome = if private {
            self.seams.probe_models_private(&secret)
        } else {
            self.seams
                .probe_models(&secret, result["public_url"].as_str().unwrap_or_default())
        };
        match outcome {
            Some(outcome) => {
                if let (Value::Object(target), Value::Object(extra)) = (&mut result, outcome) {
                    for (key, value) in extra {
                        target.insert(key, value);
                    }
                }
            }
            None => {
                result["connected"] = json!(false);
                result["reason"] = json!("connection_failed");
                result["stage"] = json!("models");
            }
        }
        match store.get_key(key_id) {
            Ok(current) => {
                if current.get("row_version").and_then(Value::as_i64) != Some(expected_row_version)
                    || current.get("enabled").and_then(Value::as_bool) != Some(true)
                    || !current
                        .get("deleted_at")
                        .map(Value::is_null)
                        .unwrap_or(true)
                {
                    result["connected"] = json!(false);
                    result["reason"] = json!("key_changed");
                }
            }
            Err(_) => {
                result["connected"] = json!(false);
                result["reason"] = json!("key_changed");
            }
        }
        result["checked_at"] = json!(epoch_seconds());
        result
    }

    fn begin_probe(&self, key_id: &str) -> Option<ProbeGuard<'_>> {
        let mut active = self
            .active_probes
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if active.contains(key_id) || active.len() >= 2 {
            return None;
        }
        active.insert(key_id.to_string());
        Some(ProbeGuard {
            store: self,
            key_id: key_id.to_string(),
        })
    }

    fn publish_response(
        &self,
        store: &mut ShareStore,
        payload: &Value,
    ) -> Result<Value, ShareWriteError> {
        let outcome = self.publish(store, None);
        let mut output = self.response(store, payload)?;
        output["published"] = json!(outcome.published);
        output["publication_error"] = outcome.error.map(Value::String).unwrap_or(Value::Null);
        Ok(output)
    }

    fn response(&self, store: &mut ShareStore, payload: &Value) -> Result<Value, ShareWriteError> {
        let generation = store.generation().map_err(ShareWriteError::store)?;
        let mut output = payload.as_object().cloned().unwrap_or_default();
        output.insert(
            "desired_generation".to_string(),
            generation
                .get("config_generation")
                .cloned()
                .unwrap_or(Value::Null),
        );
        output.insert(
            "applied_generation".to_string(),
            generation
                .get("applied_generation")
                .cloned()
                .unwrap_or(Value::Null),
        );
        output.insert(
            "in_sync".to_string(),
            generation.get("in_sync").cloned().unwrap_or(Value::Null),
        );
        output.insert(
            "security_pending".to_string(),
            generation
                .get("security_pending")
                .cloned()
                .unwrap_or(Value::Null),
        );
        output.insert(
            "security_in_sync".to_string(),
            generation
                .get("security_in_sync")
                .cloned()
                .unwrap_or(Value::Null),
        );
        Ok(Value::Object(output))
    }

    fn require_applied(
        &self,
        store: &mut ShareStore,
        mut payload: Value,
        terminate_keys: Option<&[String]>,
    ) -> Result<Value, ShareWriteError> {
        if let Ok(mut counts) = self.last_counts.lock() {
            *counts = AckCounts::default();
        }
        let outcome = self.publish(store, terminate_keys);
        let mut object = payload.as_object().cloned().unwrap_or_default();
        object.insert("published".to_string(), json!(outcome.published));
        object.insert(
            "publication_error".to_string(),
            outcome
                .error
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        payload = Value::Object(object);
        if !outcome.published {
            let stop = self.fail_closed(
                store,
                &outcome.op_id,
                outcome.error.as_deref().unwrap_or_default(),
            );
            let desired = generation_of(store).unwrap_or_default();
            let state = if stop
                .get("confirmed")
                .and_then(Value::as_bool)
                .unwrap_or(false)
            {
                "confirmed"
            } else {
                "NOT confirmed (blocked)"
            };
            return Err(ShareWriteError::broker(format!(
                "revoking operation not applied online (desired={desired}); \
                 existing projection may still authorize the old key; \
                 emergency stop {state}"
            )));
        }
        self.response(store, &payload)
    }

    fn publish(&self, store: &mut ShareStore, terminate_keys: Option<&[String]>) -> PublishOutcome {
        let projection = match store.render_projection() {
            Ok(projection) => projection,
            Err(error) => {
                return PublishOutcome::failed(ShareWriteError::store(error).message, String::new())
            }
        };
        let generation = projection
            .get("config_generation")
            .and_then(Value::as_i64)
            .unwrap_or_default();
        let digest = fingerprint(&projection);
        let op_id = match store.find_pending_publish_intent(generation) {
            Ok(Some(intent)) => intent.id,
            Ok(None) => match store.record_publish_intent_for_current(false, "publish", None) {
                Ok(op_id) => op_id,
                Err(error) => {
                    return PublishOutcome::failed(
                        ShareWriteError::store(error).message,
                        String::new(),
                    )
                }
            },
            Err(error) => {
                return PublishOutcome::failed(ShareWriteError::store(error).message, String::new())
            }
        };
        if let Ok(last) = self.last_published.lock() {
            if let Some((last_generation, last_fingerprint)) = last.as_ref() {
                if generation < *last_generation {
                    let error = format!(
                        "publication generation regression refused (already published generation {last_generation} > target {generation})"
                    );
                    self.record_failure(store, &op_id, &error, json!({"refused": "regression"}));
                    return PublishOutcome::failed(error, op_id);
                }
                if generation == *last_generation && digest != *last_fingerprint {
                    let error = "same-generation projection content mismatch refused".to_string();
                    self.record_failure(
                        store,
                        &op_id,
                        &error,
                        json!({"refused": "content_mismatch"}),
                    );
                    return PublishOutcome::failed(error, op_id);
                }
            }
        }
        let reply = match self.seams.publish(&projection, terminate_keys) {
            Ok(reply) => reply,
            Err(error) => {
                let message = format!("projection publication failed: {}", error.message);
                self.record_failure(store, &op_id, &message, Value::Null);
                return PublishOutcome::failed(message, op_id);
            }
        };
        let acked = acked_generation(&reply);
        if acked != Some(generation) {
            let error = format!(
                "broker ack does not confirm target generation {generation} (ack {acked:?}); the share may still run an older projection"
            );
            self.record_failure(store, &op_id, &error, json!({"ack": acked}));
            return PublishOutcome::failed(error, op_id);
        }
        let security_field = reply.get("security_generation").cloned();
        let security_generation = security_field.as_ref().and_then(security_watermark);
        let security_invalid = security_field.is_some() && security_generation.is_none();
        let pending_security = match store.security_records() {
            Ok(records) => records
                .into_iter()
                .filter(|record| {
                    record.get("status").and_then(Value::as_str) == Some("pending")
                        && record
                            .get("generation")
                            .and_then(Value::as_i64)
                            .map(|record_generation| record_generation <= generation)
                            .unwrap_or(false)
                })
                .collect::<Vec<_>>(),
            Err(error) => {
                return PublishOutcome::failed(ShareWriteError::store(error).message, op_id)
            }
        };
        let pending_generations: Vec<i64> = {
            let mut generations: Vec<i64> = pending_security
                .iter()
                .filter_map(|record| record.get("generation").and_then(Value::as_i64))
                .collect();
            generations.sort_unstable();
            generations.dedup();
            generations
        };
        let mut security_error = None;
        if security_invalid {
            security_error = Some(format!(
                "broker ack security_generation {security_field:?} is not a non-negative integer watermark; the ACK cannot confirm any security journal intent"
            ));
        } else if !pending_security.is_empty() {
            match security_generation {
                None => {
                    security_error = Some(format!(
                        "broker ack carries no security_generation; security journal intent(s) at generation(s) {pending_generations:?} cannot be confirmed applied (never assumed 0)"
                    ));
                }
                Some(watermark) => {
                    let mut uncovered: Vec<i64> = pending_security
                        .iter()
                        .filter_map(|record| record.get("generation").and_then(Value::as_i64))
                        .filter(|generation| *generation > watermark)
                        .collect();
                    uncovered.sort_unstable();
                    uncovered.dedup();
                    if !uncovered.is_empty() {
                        security_error = Some(format!(
                            "broker ack security_generation {watermark} does not cover security journal intent(s) at generation(s) {uncovered:?}; refusing to mark them applied"
                        ));
                    }
                }
            }
        }
        if let Some(error) = security_error {
            let _ = store.update_operation(
                &op_id,
                "failed",
                Some(&object(&[
                    ("error", json!(error)),
                    ("security_unconfirmed", json!(true)),
                    (
                        "ack_security_generation",
                        security_field.clone().unwrap_or(Value::Null),
                    ),
                    ("pending_security_generations", json!(pending_generations)),
                ])),
            );
            self.note_failure(&error);
            return PublishOutcome::failed(error, op_id);
        }
        let instance_id = reply
            .get("instance_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty());
        if let Err(error) = store.commit_applied(generation, instance_id, security_generation) {
            return PublishOutcome::failed(ShareWriteError::store(error).message, op_id);
        }
        if let Ok(mut last) = self.last_published.lock() {
            *last = Some((generation, digest));
        }
        let _ = store.update_operation(
            &op_id,
            "applied",
            Some(&object(&[
                ("generation", json!(generation)),
                (
                    "security_generation",
                    security_generation
                        .map(|value| json!(value))
                        .unwrap_or(Value::Null),
                ),
            ])),
        );
        if let Ok(mut counts) = self.last_counts.lock() {
            *counts = AckCounts {
                terminated: reply_count(&reply, "terminated"),
                released: reply_count(&reply, "released"),
                aborted: reply_count(&reply, "aborted"),
                upstream_closed: reply_count(&reply, "upstream_closed"),
            };
        }
        PublishOutcome {
            published: true,
            op_id,
            error: None,
        }
    }

    fn fail_closed(&self, store: &mut ShareStore, op_id: &str, publish_error: &str) -> Value {
        let stop = match self.seams.emergency_stop() {
            Ok(result) => {
                let confirmed = result
                    .get("stopped")
                    .and_then(Value::as_bool)
                    .unwrap_or(false);
                json!({"attempted": true, "confirmed": confirmed, "result": result})
            }
            Err(error) => json!({
                "attempted": true,
                "confirmed": false,
                "error": error.message,
            }),
        };
        let confirmed = stop
            .get("confirmed")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let _ = store.update_operation(
            op_id,
            "failed",
            Some(&object(&[
                ("error", json!(publish_error)),
                ("blocked", json!(!confirmed)),
                ("emergency_stop", stop.clone()),
            ])),
        );
        stop
    }

    fn record_failure(&self, store: &mut ShareStore, op_id: &str, error: &str, extra: Value) {
        let mut payload = object(&[("error", json!(error))]);
        if let Value::Object(extra) = extra {
            for (key, value) in extra {
                payload.insert(key, value);
            }
        }
        let _ = store.update_operation(op_id, "failed", Some(&payload));
        self.note_failure(error);
    }

    fn note_failure(&self, error: &str) {
        if let Ok(mut failure) = self.last_failure.lock() {
            *failure = Some(json!({
                "kind": "publish",
                "error": error,
                "at": um_store::ids::utc_now(),
            }));
        }
    }

    /// Test seam: the last publication failure, when one occurred.
    pub fn last_failure(&self) -> Option<Value> {
        self.last_failure
            .lock()
            .ok()
            .and_then(|value| value.clone())
    }
}

#[derive(Debug, Clone)]
struct PublishOutcome {
    published: bool,
    op_id: String,
    error: Option<String>,
}

impl PublishOutcome {
    fn failed(error: String, op_id: String) -> Self {
        Self {
            published: false,
            op_id,
            error: Some(error),
        }
    }
}

fn generation_of(store: &mut ShareStore) -> Result<i64, ShareWriteError> {
    Ok(store
        .generation()
        .map_err(ShareWriteError::store)?
        .get("config_generation")
        .and_then(Value::as_i64)
        .unwrap_or_default())
}

fn acked_generation(reply: &Value) -> Option<i64> {
    if reply.get("ack").and_then(Value::as_bool) == Some(true) {
        if let Some(generation) = reply.get("generation").and_then(Value::as_i64) {
            return Some(generation);
        }
    }
    reply.get("applied_generation").and_then(Value::as_i64)
}

fn security_watermark(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(_) => None,
        Value::Number(number) => number.as_i64().filter(|value| *value >= 0),
        Value::Object(map) => map
            .get("generation")
            .and_then(Value::as_i64)
            .filter(|value| *value >= 0),
        _ => None,
    }
}

fn reply_count(reply: &Value, name: &str) -> Option<i64> {
    reply.get(name).and_then(Value::as_i64)
}

fn object(pairs: &[(&str, Value)]) -> Map<String, Value> {
    let mut map = Map::new();
    for (key, value) in pairs {
        map.insert((*key).to_string(), value.clone());
    }
    map
}

/// A bounded per-key probe slot: one probe per key, two globally.
struct ProbeGuard<'a> {
    store: &'a ShareWriteStore,
    key_id: String,
}

impl Drop for ProbeGuard<'_> {
    fn drop(&mut self) {
        if let Ok(mut active) = self.store.active_probes.lock() {
            active.remove(&self.key_id);
        }
    }
}

fn strip_facade_fields(payload: &mut Value) {
    if let Value::Object(map) = payload {
        map.remove("key");
        map.remove("plaintext");
    }
}

fn epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

/// Test seam: `usagesrv.py::_console_share_public_row`.
pub fn console_public_row(row: &Value) -> Value {
    json!({
        "id": row.get("key_id").cloned().unwrap_or(Value::Null),
        "name": row.get("name").cloned().unwrap_or(Value::Null),
        "masked": row
            .get("masked_key")
            .or_else(|| row.get("mk"))
            .cloned()
            .unwrap_or(Value::Null),
        "plaintext_viewable": row
            .get("plaintext_viewable")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        "enabled": row.get("enabled").cloned().unwrap_or(Value::Null),
        "max_concurrency": row.get("max_concurrency").cloned().unwrap_or(Value::Null),
        "row_version": row.get("row_version").cloned().unwrap_or(Value::Null),
        "created_at": row.get("created_at").cloned().unwrap_or(Value::Null),
    })
}

/// Test seam: whether a path is a known share-management write route.
pub fn is_write_route(method: &str, rest: &[&str]) -> bool {
    matches!(
        (method, rest),
        ("POST", ["keys"])
            | ("PATCH", ["settings"])
            | ("PATCH", ["keys", _])
            | ("DELETE", ["keys", _])
            | ("POST", ["keys", _, "rotate"])
            | ("POST", ["keys", _, "revoke-and-terminate"])
    )
}

/// Test seam: the key-file path default used by the write face.
pub fn key_file_for(path: Option<&Path>) -> Option<PathBuf> {
    path.map(Path::to_path_buf)
}
