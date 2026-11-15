#![allow(dead_code)]

use std::path::PathBuf;

use serde_json::{Map, Value};
use um_ops::Clock;
use um_test_harness::TempDataDir;

pub fn fixture_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/rust_ops_rev1.json")
}

pub fn fixture() -> Value {
    serde_json::from_str(&std::fs::read_to_string(fixture_path()).expect("read ops fixture"))
        .expect("parse ops fixture")
}

pub fn temp() -> TempDataDir {
    TempDataDir::new("um-rust-ops-test-").expect("temp data dir")
}

pub struct FixedClock {
    pub now_ms: f64,
    pub iso: String,
}

impl Clock for FixedClock {
    fn now_ms(&self) -> f64 {
        self.now_ms
    }

    fn utc_iso_seconds(&self) -> String {
        self.iso.clone()
    }
}

pub fn fixed_clock() -> FixedClock {
    FixedClock {
        now_ms: 1_790_341_200_000.0,
        iso: "2026-09-25T13:00:00+00:00".to_string(),
    }
}

pub fn normalize_checked_at(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut output = Map::new();
            for (key, item) in map {
                if key == "checkedAt" {
                    output.insert(key.clone(), Value::String("<checkedAt>".to_string()));
                } else {
                    output.insert(key.clone(), normalize_checked_at(item));
                }
            }
            Value::Object(output)
        }
        Value::Array(items) => Value::Array(items.iter().map(normalize_checked_at).collect()),
        other => other.clone(),
    }
}

pub fn strip_timing(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut output = Map::new();
            for (key, item) in map {
                if matches!(
                    key.as_str(),
                    "checked_at_utc" | "checked_at_epoch" | "elapsed_ms"
                ) {
                    continue;
                }
                output.insert(key.clone(), strip_timing(item));
            }
            Value::Object(output)
        }
        Value::Array(items) => Value::Array(items.iter().map(strip_timing).collect()),
        other => other.clone(),
    }
}

pub fn env_map(pairs: &[(&str, &str)]) -> std::collections::BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(key, value)| ((*key).to_string(), (*value).to_string()))
        .collect()
}

pub fn replace_tmp(value: &Value, root: &std::path::Path) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| (key.clone(), replace_tmp(item, root)))
                .collect(),
        ),
        Value::Array(items) => {
            Value::Array(items.iter().map(|item| replace_tmp(item, root)).collect())
        }
        Value::String(text) => {
            Value::String(text.replace("<tmp>", root.to_string_lossy().as_ref()))
        }
        other => other.clone(),
    }
}
