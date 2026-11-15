//! S5b: the cloud-ops read face (`usagesrv.py::cloud_ops_status`).
//!
//! Read-only. A missing configuration is `configured: false` and an unreadable
//! state is `available: false` with a reason — never a fabricated "no
//! revocations" success.

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

pub const SECURITY_FORMAT: &str = "um-share-security-state-v1";
pub const MAX_SECURITY_BYTES: u64 = 16 * 1024 * 1024;
const SECURITY_ACTIONS: [&str; 4] = ["revoke", "rotate", "delete", "limit_lower"];
const SECURITY_STATUSES: [&str; 4] = ["pending", "applied", "remote_confirmed", "superseded"];

pub fn cloud_ops_status(security_state_file: Option<&Path>) -> Value {
    let mut payload = json!({
        "available": false,
        "read_at": epoch_seconds(),
        "security": {"configured": false, "available": false},
        "reasons": [],
        "unresolved": {
            "remote_export_confirmed": "not_checked_here",
            "runtime_generation_ack": "not_checked_here",
            "note": "本读面只透出安全状态文件现有字段；远端导出确认与运行态 generation ACK 未由本读面验证",
        },
    });
    let Some(path) = security_state_file else {
        return payload;
    };
    payload["security"]["configured"] = json!(true);
    match read_security_state(path) {
        Ok(state) => {
            payload["security"] = json!({
                "configured": true,
                "available": true,
                "generation_floor": state["generation_floor"],
                "records": state["records"],
                "pending_remote": state["pending_remote"],
                "exported_at": state["exported_at"],
            });
        }
        Err(reason) => {
            payload["security"] = json!({
                "configured": true,
                "available": false,
                "reason": reason,
            });
            payload["reasons"] = json!(["security_unavailable"]);
        }
    }
    payload["available"] = json!(true);
    payload
}

fn epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or_default()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecurityError(pub String);

/// `um-cloud-deploy/backup_restore/package.py::read_security_state`.
fn read_security_state(path: &Path) -> Result<Value, String> {
    let raw = std::fs::read(path).map_err(|_| {
        format!(
            "BackupSecurityError: security state unreadable: {}",
            path.display()
        )
    })?;
    if raw.len() as u64 > MAX_SECURITY_BYTES {
        return Err(format!(
            "BackupSecurityError: security state is unexpectedly large: {}",
            path.display()
        ));
    }
    let payload: Value = serde_json::from_slice(&raw).map_err(|_| {
        format!(
            "BackupSecurityError: security state is not valid JSON: {}",
            path.display()
        )
    })?;
    validate_security_state(&payload).map_err(|message| format!("BackupSecurityError: {message}"))
}

fn validate_security_state(payload: &Value) -> Result<Value, String> {
    let object = payload
        .as_object()
        .ok_or_else(|| "security state must be an object".to_string())?;
    if object.get("format").and_then(Value::as_str) != Some(SECURITY_FORMAT) {
        return Err("security state format is not supported".to_string());
    }
    let floor = object
        .get("generation_floor")
        .and_then(Value::as_i64)
        .filter(|floor| *floor >= 0)
        .ok_or_else(|| "security generation_floor is invalid".to_string())?;
    let records = object
        .get("records")
        .and_then(Value::as_array)
        .ok_or_else(|| "security records must be a list".to_string())?;
    let mut clean = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for item in records {
        let item = item
            .as_object()
            .ok_or_else(|| "security record is not an object".to_string())?;
        let record_id = item
            .get("record_id")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| "security record_id is invalid".to_string())?;
        if !seen.insert(record_id.to_string()) {
            return Err("security state has a duplicate record_id".to_string());
        }
        if let Some(key_id) = item.get("key_id") {
            if !key_id
                .as_str()
                .map(|value| !value.is_empty())
                .unwrap_or(false)
            {
                return Err("security key_id is invalid".to_string());
            }
        }
        let generation = item
            .get("generation")
            .and_then(Value::as_i64)
            .filter(|generation| *generation >= 1)
            .ok_or_else(|| "security generation is invalid".to_string())?;
        if generation > floor {
            return Err("security record generation is newer than generation_floor".to_string());
        }
        let action = item
            .get("action")
            .and_then(Value::as_str)
            .filter(|action| SECURITY_ACTIONS.contains(action))
            .ok_or_else(|| "security action is invalid".to_string())?;
        let status = item
            .get("status")
            .and_then(Value::as_str)
            .filter(|status| SECURITY_STATUSES.contains(status))
            .ok_or_else(|| "security status is invalid".to_string())?;
        if let Some(secret_version) = item.get("secret_version") {
            if !secret_version
                .as_i64()
                .map(|value| value >= 1)
                .unwrap_or(false)
            {
                return Err("security secret_version is invalid".to_string());
            }
        }
        if !item
            .get("recorded_at")
            .and_then(Value::as_str)
            .map(|value| !value.is_empty())
            .unwrap_or(false)
        {
            return Err("security recorded_at is invalid".to_string());
        }
        clean.push(json!({
            "record_id": record_id,
            "key_id": item.get("key_id").cloned().unwrap_or(Value::Null),
            "generation": generation,
            "action": action,
            "secret_version": item.get("secret_version").cloned().unwrap_or(Value::Null),
            "status": status,
            "recorded_at": item.get("recorded_at").cloned().unwrap_or(Value::Null),
            "applied_at": item.get("applied_at").cloned().unwrap_or(Value::Null),
            "applied_generation": item
                .get("applied_generation")
                .cloned()
                .unwrap_or(Value::Null),
            "instance_id": item.get("instance_id").cloned().unwrap_or(Value::Null),
            "remote_confirmed_at": item
                .get("remote_confirmed_at")
                .cloned()
                .unwrap_or(Value::Null),
            "remote_receipt": item.get("remote_receipt").cloned().unwrap_or(Value::Null),
        }));
    }
    let pending_remote = clean
        .iter()
        .filter(|record| record["status"] != json!("remote_confirmed"))
        .count();
    Ok(json!({
        "format": SECURITY_FORMAT,
        "generation_floor": floor,
        "records": clean.len(),
        "pending_remote": pending_remote,
        "exported_at": object.get("exported_at").cloned().unwrap_or(Value::Null),
    }))
}

/// Test seam: the cloud-ops payload from a security-state file.
pub fn cloud_ops_status_for_tests(path: Option<&Path>) -> Value {
    cloud_ops_status(path)
}
