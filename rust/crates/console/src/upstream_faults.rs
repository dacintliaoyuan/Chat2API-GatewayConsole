//! #103: the local 8001 read face of the durable UM upstream fault record.
//!
//! The local 8000 gateway owns the record. This module only reads the JSON
//! document the gateway wrote; it never probes the upstream, never writes the
//! record and never creates a fault entry. A missing file is the legal empty
//! record, while an unreadable or malformed file is an explicit read failure.

use std::path::Path;

use serde_json::{json, Value};

use crate::local_um::UmQueryError;

/// Read one upstream fault record for the local console overview.
pub fn upstream_faults(path: &Path) -> Result<Value, UmQueryError> {
    let snapshot = um_gateway::upstream_fault::read_snapshot(path)
        .map_err(|_| UmQueryError("upstream fault record is unreadable".to_string()))?;
    Ok(json!({
        "status": "ok",
        "source": path
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_else(|| "upstream-faults.json".to_string()),
        "active": snapshot["active"],
        "history": snapshot["history"],
        "last_check": snapshot["last_check"],
        "next_check_epoch": snapshot["next_check_epoch"],
    }))
}
