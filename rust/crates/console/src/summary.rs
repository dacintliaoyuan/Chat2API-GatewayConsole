//! Cloud `/api/summary` payload.
//!
//! `usagesrv.py::_build_cloud_summary` reports ONLY the assembled UM share
//! capability. It never reads the private chat2api / DSH ledgers and never
//! probes a local source port. The response shape is the Python dict plus the
//! `summary_cached` bookkeeping fields `_generated_epoch` and
//! `_cached_age_seconds`.

use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use chrono::{FixedOffset, TimeZone};
use serde_json::{json, Value};

use crate::config::{ConsoleConfig, Role};
use crate::local_summary::{LocalSummaryCache, LocalSummaryPaths};

pub const SUMMARY_CACHE_SECONDS: f64 = 300.0;

pub struct SummaryCache {
    value: Mutex<Option<(Value, f64, Instant)>>,
    local: Option<LocalSummaryCache>,
}

impl Default for SummaryCache {
    fn default() -> Self {
        Self::new()
    }
}

impl SummaryCache {
    pub fn new() -> Self {
        Self {
            value: Mutex::new(None),
            local: None,
        }
    }

    /// The role's own summary: local reads the private ledgers, cloud reports
    /// only the assembled UM share capability.
    pub fn for_config(config: &ConsoleConfig) -> Self {
        match config.role {
            Role::Local => Self::local(LocalSummaryPaths {
                usage_log: config.usage_log.clone(),
                dsh_snapshot: config.dsh_snapshot.clone(),
            }),
            Role::Cloud => Self::new(),
        }
    }

    pub fn local(paths: LocalSummaryPaths) -> Self {
        Self {
            value: Mutex::new(None),
            local: Some(LocalSummaryCache::new(paths)),
        }
    }

    pub fn payload(&self) -> Value {
        if let Some(local) = &self.local {
            return local.payload();
        }
        let now = Instant::now();
        let now_epoch = epoch_seconds();
        if let Ok(cache) = self.value.lock() {
            if let Some((value, at_epoch, at)) = cache.as_ref() {
                if now.duration_since(*at).as_secs_f64() < SUMMARY_CACHE_SECONDS {
                    let mut stamped = value.clone();
                    if let Some(object) = stamped.as_object_mut() {
                        object.insert("_generated_epoch".to_string(), json!(at_epoch));
                        object.insert(
                            "_cached_age_seconds".to_string(),
                            json!(round_one_decimal(now_epoch - at_epoch)),
                        );
                    }
                    return stamped;
                }
            }
        }
        let built = build_cloud_summary();
        let at = Instant::now();
        if let Ok(mut cache) = self.value.lock() {
            *cache = Some((built.clone(), now_epoch, at));
        }
        let mut stamped = built;
        if let Some(object) = stamped.as_object_mut() {
            object.insert("_generated_epoch".to_string(), json!(now_epoch));
            object.insert("_cached_age_seconds".to_string(), json!(0.0));
        }
        stamped
    }
}

fn round_one_decimal(value: f64) -> f64 {
    (value * 10.0).round() / 10.0
}

fn epoch_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_secs_f64()
}

/// The exact Python cloud summary payload (without the cache bookkeeping).
pub fn build_cloud_summary() -> Value {
    let offset = FixedOffset::east_opt(8 * 3600).expect("+08:00");
    let generated_at = offset
        .timestamp_opt(chrono::Utc::now().timestamp(), 0)
        .single()
        .expect("local timestamp")
        .format("%Y-%m-%dT%H:%M:%S%:z")
        .to_string();
    json!({
        "generatedAt": generated_at,
        "role": "cloud",
        "assembly": {
            "role": "cloud",
            "services": ["share"],
            "note": "云端角色只装配 UM 分享：不读取本地私人账本；分享用量见 GET /api/console/share-usage",
        },
        "services": {},
        "dsh": {
            "url": null,
            "note": "云端角色未装配 DSH 快照读取",
        },
        "dshMergedIntoTotal": {
            "day": null,
            "note": "云端角色不并入任何本地 DSH 账本",
        },
    })
}
