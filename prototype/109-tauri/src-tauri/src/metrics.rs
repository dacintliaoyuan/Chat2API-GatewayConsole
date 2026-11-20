//! Minimal measurement sink for the #109 Tauri prototype.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

static LOCK: Mutex<()> = Mutex::new(());

pub fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or_default()
}

pub fn record(name: &str, at_ms: i64, extra: Value) {
    let path = std::env::var("PROTO109_METRICS")
        .unwrap_or_else(|_| "tmp/109-metrics-tauri.jsonl".to_string());
    let _guard = LOCK.lock();
    if let Some(parent) = PathBuf::from(&path).parent() {
        if !parent.as_os_str().is_empty() {
            let _ = std::fs::create_dir_all(parent);
        }
    }
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(&path) {
        let line = json!({
            "app": "tauri",
            "pid": std::process::id(),
            "name": name,
            "at_ms": at_ms,
            "extra": extra,
        });
        let _ = writeln!(file, "{line}");
    }
}
