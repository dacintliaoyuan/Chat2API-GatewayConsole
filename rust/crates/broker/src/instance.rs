//! AC5: the instance-channel endpoint.
//!
//! Mirrors the share-side `handle_instance_control`: the endpoint reports its
//! own identity and in-flight state, ACKs a loaded generation, pauses/resumes
//! admission and clears in-flight work. It has NO management ability; an unknown
//! verb is a `bad_request` (the broker maps an old instance's
//! `bad_request`/`unknown action` to `capability_missing`).

use std::collections::BTreeMap;
use std::sync::Mutex;

use serde_json::{json, Map, Value};

use crate::actions::InstanceAction;
use crate::error::{ProtocolError, ResultCode};
use crate::replay::ReplayRegistry;
use crate::request::validate_instance_request;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InstanceStateKind {
    Running,
    Draining,
    Stopped,
}

impl InstanceStateKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Draining => "draining",
            Self::Stopped => "stopped",
        }
    }
}

struct InstanceInner {
    state: InstanceStateKind,
    active: BTreeMap<String, u32>,
    last_seen_generation: i64,
}

pub struct InstanceServer {
    instance_id: String,
    inner: Mutex<InstanceInner>,
    replay: ReplayRegistry,
}

impl InstanceServer {
    pub fn new(instance_id: impl Into<String>) -> Self {
        Self {
            instance_id: instance_id.into(),
            inner: Mutex::new(InstanceInner {
                state: InstanceStateKind::Running,
                active: BTreeMap::new(),
                last_seen_generation: 0,
            }),
            replay: ReplayRegistry::default(),
        }
    }

    pub fn instance_id(&self) -> &str {
        &self.instance_id
    }

    /// Test/deployment seam: register in-flight work for one key.
    pub fn set_active(&self, key_id: &str, count: u32) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.active.insert(key_id.to_string(), count);
        }
    }

    pub fn active_count(&self) -> u32 {
        self.inner
            .lock()
            .map(|inner| inner.active.values().sum())
            .unwrap_or(0)
    }

    pub fn state(&self) -> InstanceStateKind {
        self.inner
            .lock()
            .map(|inner| inner.state)
            .unwrap_or(InstanceStateKind::Stopped)
    }

    pub fn last_seen_generation(&self) -> i64 {
        self.inner
            .lock()
            .map(|inner| inner.last_seen_generation)
            .unwrap_or(0)
    }

    /// Validate, consume the request id, then answer the five instance verbs.
    pub fn handle_frame(&self, body: &Value, secret: &str, now: i64) -> Value {
        let request_id = body
            .get("request_id")
            .and_then(Value::as_str)
            .map(str::to_string);
        let request = match validate_instance_request(body, secret, now) {
            Ok(request) => request,
            Err(error) => {
                return error_frame(request_id.as_deref(), error.code(), error.message(), None)
            }
        };
        if request.instance_id.as_deref() != Some(self.instance_id.as_str()) {
            return error_frame(
                Some(&request.request_id),
                ResultCode::InstanceMismatch,
                "request targets another instance",
                None,
            );
        }
        if let Err(error) = self.replay.consume(&request.request_id, request.ts, now) {
            return error_frame(
                Some(&request.request_id),
                error.code(),
                error.message(),
                None,
            );
        }
        let mut inner = match self.inner.lock() {
            Ok(inner) => inner,
            Err(_) => {
                return error_frame(
                    Some(&request.request_id),
                    ResultCode::InternalError,
                    "instance state poisoned",
                    None,
                )
            }
        };
        let result = match request.action.instance() {
            Some(InstanceAction::InstanceStatus) => Ok(json!({
                "instance_id": self.instance_id,
                "state": inner.state.as_str(),
                "active_count": active_total(&inner.active),
                "last_seen_generation": inner.last_seen_generation,
            })),
            Some(InstanceAction::Load) => Ok(load(&mut inner, &self.instance_id, &request.fields)),
            Some(InstanceAction::Drain) => {
                if inner.state == InstanceStateKind::Stopped {
                    Err(("blocked", "instance is stopped"))
                } else {
                    inner.state = InstanceStateKind::Draining;
                    Ok(json!({
                        "instance_id": self.instance_id,
                        "state": inner.state.as_str(),
                        "active_count": active_total(&inner.active),
                    }))
                }
            }
            Some(InstanceAction::Emergency) => {
                let cleared = active_total(&inner.active);
                inner.active.clear();
                inner.state = InstanceStateKind::Stopped;
                Ok(json!({
                    "instance_id": self.instance_id,
                    "state": inner.state.as_str(),
                    "cleared_in_flight": cleared,
                }))
            }
            Some(InstanceAction::Resume) => {
                if inner.state == InstanceStateKind::Stopped {
                    Err(("blocked", "instance is stopped"))
                } else {
                    inner.state = InstanceStateKind::Running;
                    Ok(json!({
                        "instance_id": self.instance_id,
                        "state": inner.state.as_str(),
                        "active_count": active_total(&inner.active),
                    }))
                }
            }
            None => Err(("bad_request", "unknown instance action")),
        };
        match result {
            Ok(result) => success_frame(&request.request_id, result),
            Err((code, detail)) => {
                let code = ResultCode::parse(code).unwrap_or(ResultCode::BadRequest);
                error_frame(
                    Some(&request.request_id),
                    code,
                    detail,
                    (code == ResultCode::Blocked).then(|| json!({"state": "stopped"})),
                )
            }
        }
    }
}

fn load(inner: &mut InstanceInner, instance_id: &str, fields: &Map<String, Value>) -> Value {
    let generation = fields
        .get("generation")
        .and_then(Value::as_i64)
        .unwrap_or_default();
    let fingerprint = fields
        .get("fingerprint")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let released: u32 = fields
        .get("terminate_keys")
        .and_then(Value::as_array)
        .map(|keys| {
            keys.iter()
                .filter_map(Value::as_str)
                .filter_map(|key| inner.active.remove(key))
                .sum()
        })
        .unwrap_or(0);
    inner.last_seen_generation = inner.last_seen_generation.max(generation);
    json!({
        "instance_id": instance_id,
        "generation": generation,
        "fingerprint": fingerprint,
        "installed": true,
        "state": inner.state.as_str(),
        "active_count": active_total(&inner.active),
        "released": released,
    })
}

fn active_total(active: &BTreeMap<String, u32>) -> u32 {
    active.values().copied().sum()
}

fn success_frame(request_id: &str, result: Value) -> Value {
    json!({
        "v": 2,
        "request_id": request_id,
        "ok": true,
        "error_code": "ok",
        "result": result,
    })
}

fn error_frame(
    request_id: Option<&str>,
    code: ResultCode,
    detail: &str,
    extra: Option<Value>,
) -> Value {
    let mut result = Map::new();
    result.insert("detail".to_string(), Value::String(detail.to_string()));
    if let Some(Value::Object(extra)) = extra {
        result.extend(extra);
    }
    json!({
        "v": 2,
        "request_id": request_id,
        "ok": false,
        "error_code": code.as_str(),
        "result": Value::Object(result),
    })
}

/// Map a validation failure into the reply shape the broker expects. Kept
/// separate so a transport decode failure can reuse it.
pub fn rejection_frame(request_id: Option<&str>, error: &ProtocolError) -> Value {
    error_frame(request_id, error.code(), error.message(), None)
}
