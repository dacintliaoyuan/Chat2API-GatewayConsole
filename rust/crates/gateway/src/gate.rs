//! Share-key authentication and admission.
//!
//! This is a port of `chat2api/share_auth.py`: the same credential header
//! shapes, fail-closed duplicates/empty/overlong/wrong-prefix rules, per-key and
//! global concurrency, the 20-requests-per-60-seconds key rate limit, and the
//! four `sharing_limit_reached` reasons. The projection is re-read for every
//! authentication and for every admission that carries a generation/version.

use std::collections::{HashMap, VecDeque};
use std::fs;
use std::io::Write;
use std::net::{Shutdown, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

use crate::config::{
    load_share_config, AuthMode, ConfigError, ShareConfig, ShareEntry, LEGACY_KEY_MAX_CONCURRENCY,
};
use crate::http::Headers;

pub const BEARER_PREFIX: &str = "Bearer ";
pub const API_KEY_HEADER: &str = "X-Api-Key";
pub const MAX_CREDENTIAL_HEADER: usize = 256;
pub const RATE_LIMIT_REQUESTS: usize = 20;
pub const RATE_LIMIT_WINDOW: Duration = Duration::from_secs(60);

/// One admission refusal reason, matching the Python `reason_out` dict.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RefusalReason {
    pub reason: Option<String>,
    pub detail: Option<String>,
}

impl RefusalReason {
    fn set(&mut self, reason: &str, detail: String) {
        self.reason = Some(reason.to_string());
        self.detail = Some(detail);
    }
}

/// The gate state, matching the Python `running | draining | stopped`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateState {
    Running,
    Draining,
    Stopped,
}

impl GateState {
    fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Draining => "draining",
            Self::Stopped => "stopped",
        }
    }
}

/// A live request the gate can terminate on a Key revoke.
#[derive(Debug)]
pub struct InflightCtx {
    stream: Mutex<Option<TcpStream>>,
    closed: AtomicBool,
}

impl InflightCtx {
    pub fn new(stream: TcpStream) -> Arc<Self> {
        Arc::new(Self {
            stream: Mutex::new(Some(stream)),
            closed: AtomicBool::new(false),
        })
    }

    pub fn terminated(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }

    /// Close the client socket once; returns True when this call closed it.
    pub fn terminate(&self) -> bool {
        if self.closed.swap(true, Ordering::SeqCst) {
            return false;
        }
        if let Ok(mut guard) = self.stream.lock() {
            if let Some(stream) = guard.take() {
                let _ = stream.shutdown(Shutdown::Both);
            }
        }
        true
    }
}

/// One admitted share request: the verified Key and its in-flight context.
pub struct Admission {
    pub key_id: String,
    pub ctx: Arc<InflightCtx>,
}

struct GateStateData {
    state: GateState,
    config: ShareConfig,
    active: HashMap<String, u32>,
    history: HashMap<String, VecDeque<Instant>>,
    inflight: HashMap<String, Vec<Arc<InflightCtx>>>,
}

/// In-process share-key gate.
pub struct ShareGate {
    config_path: PathBuf,
    auth_mode: AuthMode,
    guard: Mutex<GateStateData>,
}

impl ShareGate {
    pub fn new(config_path: impl AsRef<Path>, auth_mode: AuthMode) -> Result<Self, ConfigError> {
        let config_path = config_path.as_ref().to_path_buf();
        let config = load_share_config(&config_path, auth_mode)?;
        Ok(Self {
            config_path,
            auth_mode,
            guard: Mutex::new(GateStateData {
                state: GateState::Running,
                config,
                active: HashMap::new(),
                history: HashMap::new(),
                inflight: HashMap::new(),
            }),
        })
    }

    pub fn snapshot(&self) -> serde_json::Value {
        let guard = self.guard.lock().expect("share gate poisoned");
        let active: serde_json::Map<String, serde_json::Value> = guard
            .active
            .iter()
            .map(|(key, count)| (key.clone(), serde_json::json!(count)))
            .collect();
        serde_json::json!({
            "state": guard.state.as_str(),
            "config_generation": guard.config.config_generation,
            "share_max_concurrency": guard.config.share_max_concurrency,
            "active": active,
            "keys": guard.config.entries.len(),
        })
    }

    pub fn set_state(&self, state: GateState) {
        let mut guard = self.guard.lock().expect("share gate poisoned");
        guard.state = state;
    }

    /// Resolve one enabled share entry, or `Ok(None)` for an invalid key.
    ///
    /// A configuration read/parse failure is `Err`: it is a configuration or
    /// data-corruption failure, never an invalid key, so callers can fail closed
    /// with a readable 503 instead of folding it into 401.
    pub fn authenticate(
        &self,
        headers: &Headers,
        allow_api_key: bool,
    ) -> Result<Option<(ShareEntry, ShareConfig)>, ConfigError> {
        let Some(credential) = resolve_share_credential(headers, allow_api_key) else {
            return Ok(None);
        };
        let config = load_share_config(&self.config_path, self.auth_mode)?;
        let digest = sha256_hex(credential.as_bytes());
        let entry = config
            .entries
            .iter()
            .find(|entry| {
                entry.enabled && constant_time_eq(entry.secret_hash.as_bytes(), digest.as_bytes())
            })
            .cloned();
        let mut guard = self.guard.lock().expect("share gate poisoned");
        guard.config = config.clone();
        Ok(entry.map(|entry| (entry, config)))
    }

    /// Admit one request; the refusal reason distinguishes the four classes.
    ///
    /// `Err` is a configuration read/parse failure and never a concurrency
    /// refusal; callers answer it with a readable 503.
    pub fn acquire(
        &self,
        key_id: &str,
        generation: Option<u64>,
        secret_version: Option<u32>,
        reason_out: Option<&mut RefusalReason>,
    ) -> Result<bool, ConfigError> {
        let mut reason_out = reason_out;
        let mut guard = self.guard.lock().expect("share gate poisoned");
        if guard.state != GateState::Running {
            if let Some(reason) = reason_out.as_mut() {
                reason.set(
                    "state_not_accepting",
                    "the share is draining or stopped and is not accepting new requests"
                        .to_string(),
                );
            }
            return Ok(false);
        }
        let (key_limit, share_max, config) = if generation.is_some() || secret_version.is_some() {
            let fresh = load_share_config(&self.config_path, self.auth_mode)?;
            if let Some(generation) = generation {
                if fresh.config_generation < generation {
                    return Ok(false);
                }
            }
            let entry = match fresh.entries.iter().find(|entry| entry.key_id == key_id) {
                Some(entry) if entry.enabled => entry.clone(),
                _ => return Ok(false),
            };
            if let Some(secret_version) = secret_version {
                if entry.secret_version != secret_version {
                    return Ok(false);
                }
            }
            (
                entry.max_concurrency,
                fresh.share_max_concurrency,
                Some(fresh),
            )
        } else {
            let key_limit = guard
                .config
                .entries
                .iter()
                .find(|entry| entry.key_id == key_id)
                .map(|entry| entry.max_concurrency)
                .unwrap_or(LEGACY_KEY_MAX_CONCURRENCY);
            (key_limit, guard.config.share_max_concurrency, None)
        };
        let now = Instant::now();
        for entries in guard.history.values_mut() {
            while entries
                .front()
                .is_some_and(|entry| *entry <= now - RATE_LIMIT_WINDOW)
            {
                entries.pop_front();
            }
        }
        guard.history.retain(|_, entries| !entries.is_empty());
        let recent_len = guard.history.get(key_id).map(VecDeque::len).unwrap_or(0);
        if guard.active.get(key_id).copied().unwrap_or(0) >= key_limit {
            if let Some(reason) = reason_out.as_mut() {
                reason.set(
                    "per_key_concurrency",
                    format!(
                        "per-key concurrency limit reached (max {key_limit} in-flight requests for this key)"
                    ),
                );
            }
            return Ok(false);
        }
        if guard.active.values().copied().sum::<u32>() >= share_max {
            if let Some(reason) = reason_out.as_mut() {
                reason.set(
                    "share_concurrency",
                    format!(
                        "global share concurrency limit reached (max {share_max} in-flight requests)"
                    ),
                );
            }
            return Ok(false);
        }
        if recent_len >= RATE_LIMIT_REQUESTS {
            if let Some(reason) = reason_out.as_mut() {
                reason.set(
                    "key_rate_limit",
                    "per-key rate limit reached (max 20 requests per minute)".to_string(),
                );
            }
            return Ok(false);
        }
        guard
            .history
            .entry(key_id.to_string())
            .or_default()
            .push_back(now);
        *guard.active.entry(key_id.to_string()).or_insert(0) += 1;
        if let Some(config) = config {
            guard.config = config;
        }
        Ok(true)
    }

    pub fn release(&self, key_id: &str) {
        let mut guard = self.guard.lock().expect("share gate poisoned");
        let count = guard.active.get(key_id).copied().unwrap_or(0);
        if count <= 1 {
            guard.active.remove(key_id);
        } else {
            guard.active.insert(key_id.to_string(), count - 1);
        }
    }

    pub fn register_inflight(&self, key_id: &str, ctx: Arc<InflightCtx>) {
        let mut guard = self.guard.lock().expect("share gate poisoned");
        let contexts = guard.inflight.entry(key_id.to_string()).or_default();
        if !contexts.iter().any(|existing| Arc::ptr_eq(existing, &ctx)) {
            contexts.push(ctx);
        }
    }

    pub fn unregister_inflight(&self, key_id: &str, ctx: &Arc<InflightCtx>) {
        let mut guard = self.guard.lock().expect("share gate poisoned");
        if let Some(contexts) = guard.inflight.get_mut(key_id) {
            contexts.retain(|existing| !Arc::ptr_eq(existing, ctx));
            if contexts.is_empty() {
                guard.inflight.remove(key_id);
            }
        }
    }

    /// Close every live request held by one revoked Key.
    pub fn terminate_key(&self, key_id: &str) -> u32 {
        let contexts = {
            let mut guard = self.guard.lock().expect("share gate poisoned");
            guard.inflight.remove(key_id).unwrap_or_default()
        };
        contexts
            .iter()
            .filter(|context| context.terminate())
            .count() as u32
    }
}

/// Resolve the ONE share credential from the accepted header shapes.
pub fn resolve_share_credential(headers: &Headers, allow_api_key: bool) -> Option<String> {
    let auth = headers.get_all("Authorization");
    let api_keys = if allow_api_key {
        headers.get_all(API_KEY_HEADER)
    } else {
        Vec::new()
    };
    if auth.len() > 1 || api_keys.len() > 1 {
        return None;
    }
    let bearer = match auth.first() {
        Some(header) => {
            if !header.starts_with(BEARER_PREFIX) || header.len() > MAX_CREDENTIAL_HEADER {
                return None;
            }
            let credential = &header[BEARER_PREFIX.len()..];
            if credential.is_empty() {
                return None;
            }
            Some(credential.to_string())
        }
        None => None,
    };
    let api_key = match api_keys.first() {
        Some(header) => {
            if header.is_empty() || header.len() > MAX_CREDENTIAL_HEADER {
                return None;
            }
            Some(header.to_string())
        }
        None => None,
    };
    match (bearer, api_key) {
        (None, api_key) => api_key,
        (bearer, None) => bearer,
        (Some(bearer), Some(api_key)) => {
            if constant_time_eq(bearer.as_bytes(), api_key.as_bytes()) {
                Some(bearer)
            } else {
                None
            }
        }
    }
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut output = String::with_capacity(64);
    for byte in digest {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}

/// Constant-time equality; length is not secret here.
pub fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut difference = 0u8;
    for (left, right) in left.iter().zip(right.iter()) {
        difference |= left ^ right;
    }
    difference == 0
}

/// Load the control token, creating it on first use like Python.
///
/// An existing readable token wins. A missing or empty file is initialized with
/// a fresh token; a failed initialization fails closed (the caller denies).
pub fn ensure_control_token(path: &Path) -> Option<String> {
    if let Ok(raw) = fs::read_to_string(path) {
        let value = raw.trim();
        if !value.is_empty() {
            return Some(value.to_string());
        }
    }
    let token = generate_control_token();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).ok()?;
    }
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).ok()?;
    file.write_all(token.as_bytes()).ok()?;
    file.flush().ok()?;
    Some(token)
}

fn generate_control_token() -> String {
    use sha2::{Digest, Sha256};
    use std::time::{SystemTime, UNIX_EPOCH};
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let mut hasher = Sha256::new();
    hasher.update(std::process::id().to_le_bytes());
    hasher.update(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0)
            .to_le_bytes(),
    );
    hasher.update(SEQUENCE.fetch_add(1, Ordering::SeqCst).to_le_bytes());
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
