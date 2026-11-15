//! Read-only parity for the public DNS/TLS boundary in `console/public_probe.py`.
//!
//! The state machine only observes DNS answers and TLS handshakes; it never sends
//! an HTTP request and never receives a sharing credential. Tests inject a
//! [`ProbeTransport`]; production keeps using the Python probe.

use std::collections::BTreeMap;
use std::fmt;

use serde_json::{json, Value};

use crate::ledger::Clock;

pub const PUBLIC_ORIGIN_ENV: &str = "CONSOLE_PUBLIC_ORIGIN";
pub const PUBLIC_ORIGIN_UNCONFIGURED: &str = "public_origin_unconfigured";
pub const PUBLIC_ORIGIN_INVALID: &str = "public_origin_invalid";
pub const PUBLIC_CHECK_TIMEOUT_S: f64 = 15.0;
pub const PUBLIC_CHECK_MAX_TIMEOUT_S: f64 = 60.0;
pub const PUBLIC_CHECK_ADDRESS_TIMEOUT_S: f64 = 8.0;
pub const PUBLIC_CHECK_MAX_ADDRESSES: usize = 4;
pub const DNS_TTL_MAX_SECONDS: i64 = 2_147_483_647;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProbeError {
    message: String,
}

impl ProbeError {
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for ProbeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for ProbeError {}

#[derive(Debug, Clone, PartialEq)]
pub enum ProbeFailure {
    DnsTimeout(Value),
    Timeout,
    Transport(String),
}

#[derive(Debug, Clone, PartialEq)]
pub struct DnsResolution {
    pub addresses: Vec<String>,
    pub reason: String,
    pub detail: Value,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TlsCheck {
    pub state: String,
    pub tls: String,
    pub certificate: String,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct DnsSource {
    pub source: String,
    pub state: String,
    pub status: Option<i64>,
    pub classification: String,
    pub addresses: Vec<String>,
    pub addresses_truncated: bool,
    pub ttl_s: Option<i64>,
    pub negative_ttl_s: Option<i64>,
    pub checked_at_epoch: Option<f64>,
    pub elapsed_ms: i64,
}

impl DnsSource {
    pub fn new(source: &str, state: &str) -> Self {
        Self {
            source: source.to_string(),
            state: state.to_string(),
            status: None,
            classification: state.to_string(),
            addresses: Vec::new(),
            addresses_truncated: false,
            ttl_s: None,
            negative_ttl_s: None,
            checked_at_epoch: None,
            elapsed_ms: 0,
        }
    }

    pub fn with_addresses(mut self, addresses: &[&str]) -> Self {
        self.addresses = addresses.iter().map(|value| (*value).to_string()).collect();
        self
    }

    pub fn with_ttl(mut self, ttl: i64) -> Self {
        self.ttl_s = Some(ttl);
        self
    }

    pub fn with_negative_ttl(mut self, ttl: i64) -> Self {
        self.negative_ttl_s = Some(ttl);
        self
    }

    pub fn with_truncated(mut self, truncated: bool) -> Self {
        self.addresses_truncated = truncated;
        self
    }

    pub fn from_json(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        Some(Self {
            source: object.get("source")?.as_str()?.to_string(),
            state: object.get("state")?.as_str()?.to_string(),
            status: object.get("status").and_then(Value::as_i64),
            classification: object
                .get("classification")
                .and_then(Value::as_str)?
                .to_string(),
            addresses: object
                .get("addresses")
                .and_then(Value::as_array)?
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect(),
            addresses_truncated: object
                .get("addresses_truncated")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            ttl_s: object.get("ttl_s").and_then(Value::as_i64),
            negative_ttl_s: object.get("negative_ttl_s").and_then(Value::as_i64),
            checked_at_epoch: object.get("checked_at_epoch").and_then(Value::as_f64),
            elapsed_ms: object
                .get("elapsed_ms")
                .and_then(Value::as_i64)
                .unwrap_or(0),
        })
    }

    pub fn to_json(&self) -> Value {
        json!({
            "source": self.source,
            "state": self.state,
            "status": self.status,
            "classification": self.classification,
            "addresses": self.addresses,
            "addresses_truncated": self.addresses_truncated,
            "ttl_s": self.ttl_s,
            "negative_ttl_s": self.negative_ttl_s,
            "checked_at_epoch": self.checked_at_epoch,
            "elapsed_ms": self.elapsed_ms,
        })
    }
}

pub trait ProbeTransport {
    fn resolve_public_addresses(
        &self,
        host: &str,
        deadline_ms: f64,
    ) -> Result<DnsResolution, ProbeFailure>;

    fn check_tls(
        &self,
        host: &str,
        address: &str,
        deadline_ms: f64,
    ) -> Result<TlsCheck, ProbeFailure>;
}

pub fn origin_host(value: Option<&str>) -> Option<String> {
    let value = value?;
    if value.is_empty() || value.chars().count() > 253 || !value.is_ascii() {
        return None;
    }
    if value
        .chars()
        .any(|character| character.is_whitespace() || (character as u32) < 32)
    {
        return None;
    }
    if value.contains('\\') || value.contains('?') || value.contains('#') {
        return None;
    }
    let lower = value.to_ascii_lowercase();
    let rest = lower.strip_prefix("https://")?;
    if rest.contains('@') {
        return None;
    }
    let (authority, path) = match rest.find('/') {
        Some(index) => (&rest[..index], &rest[index..]),
        None => (rest, ""),
    };
    if !path.is_empty() && path != "/" {
        return None;
    }
    let (host, port) = split_authority(authority)?;
    if host.is_empty() {
        return None;
    }
    if let Some(port) = port {
        if port != "443" {
            return None;
        }
        if authority != format!("{host}:443") {
            return None;
        }
    } else if authority != host {
        return None;
    }
    if host.split('.').any(|label| {
        label.is_empty()
            || label.chars().count() > 63
            || label.starts_with('-')
            || label.ends_with('-')
            || !label
                .chars()
                .all(|character| character.is_ascii_alphanumeric() || character == '-')
    }) {
        return None;
    }
    Some(host.to_string())
}

fn split_authority(authority: &str) -> Option<(&str, Option<&str>)> {
    if authority.starts_with('[') {
        let end = authority.find(']')?;
        let host = &authority[1..end];
        let rest = &authority[end + 1..];
        if rest.is_empty() {
            return Some((host, None));
        }
        let port = rest.strip_prefix(':')?;
        if port.is_empty() {
            return None;
        }
        return Some((host, Some(port)));
    }
    match authority.rsplit_once(':') {
        Some((host, port)) => {
            if host.contains(':') || port.is_empty() {
                return None;
            }
            Some((host, Some(port)))
        }
        None => Some((authority, None)),
    }
}

pub fn public_origin(env: &BTreeMap<String, String>) -> (Option<String>, &'static str) {
    let Some(raw) = env.get(PUBLIC_ORIGIN_ENV) else {
        return (None, PUBLIC_ORIGIN_UNCONFIGURED);
    };
    if raw.trim().is_empty() {
        return (None, PUBLIC_ORIGIN_UNCONFIGURED);
    }
    match origin_host(Some(raw)) {
        Some(host) => (Some(format!("https://{host}")), ""),
        None => (None, PUBLIC_ORIGIN_INVALID),
    }
}

pub fn public_host(url: Option<&str>, env: &BTreeMap<String, String>) -> Option<String> {
    let host = origin_host(url)?;
    let (configured, reason) = public_origin(env);
    if reason == PUBLIC_ORIGIN_INVALID {
        return None;
    }
    if let Some(configured) = configured {
        return (origin_host(Some(&configured))? == host).then_some(host);
    }
    host.ends_with(".ts.net").then_some(host)
}

pub fn public_source(host: &str, env: &BTreeMap<String, String>) -> &'static str {
    let (configured, _reason) = public_origin(env);
    if let Some(configured) = configured {
        if origin_host(Some(&configured)).as_deref() == Some(host) {
            return "configured";
        }
    }
    "tailscale"
}

pub fn aggregate_dns(sources: &[DnsSource], started_ms: f64, clock: &dyn Clock) -> Value {
    let mut addresses: Vec<String> = Vec::new();
    let mut truncated = false;
    let mut ttl: Option<i64> = None;
    let mut negative_ttl: Option<i64> = None;
    for source in sources {
        for address in &source.addresses {
            if !addresses.contains(address) {
                if addresses.len() >= PUBLIC_CHECK_MAX_ADDRESSES {
                    truncated = true;
                    continue;
                }
                addresses.push(address.clone());
            }
        }
        truncated = truncated || source.addresses_truncated;
        if let Some(value) = source.ttl_s {
            ttl = Some(ttl.map_or(value, |current| current.min(value)));
        }
        if let Some(value) = source.negative_ttl_s {
            negative_ttl = Some(negative_ttl.map_or(value, |current| current.min(value)));
        }
    }
    let states: Vec<&str> = sources.iter().map(|source| source.state.as_str()).collect();
    let (state, reason) = if !addresses.is_empty() {
        if states.iter().all(|state| *state == "ok") {
            ("ok", "ok")
        } else if states.contains(&"ok")
            && states
                .iter()
                .any(|state| *state == "empty" || *state == "nxdomain")
        {
            ("disagreement", "dns_disagreement")
        } else {
            ("unconfirmed", "dns_unconfirmed")
        }
    } else if states
        .iter()
        .all(|state| *state == "empty" || *state == "nxdomain")
    {
        if states.contains(&"nxdomain") {
            ("nxdomain", "dns_nxdomain")
        } else {
            ("empty", "dns_empty")
        }
    } else if states.iter().all(|state| *state == "timeout") {
        ("timeout", "dns_timeout")
    } else if states.iter().all(|state| *state == "error") {
        ("error", "dns_failed")
    } else if states.contains(&"not_checked") {
        ("not_checked", "dns_not_checked")
    } else {
        ("unconfirmed", "dns_unconfirmed")
    };
    json!({
        "version": 1,
        "checked_at_utc": clock.utc_iso_seconds(),
        "checked_at_epoch": clock.now_ms() / 1000.0,
        "state": state,
        "reason": reason,
        "addresses": addresses,
        "addresses_truncated": truncated,
        "ttl_s": ttl,
        "negative_ttl_s": negative_ttl,
        "elapsed_ms": (clock.monotonic_ms() - started_ms).max(0.0) as i64,
        "sources": sources.iter().map(DnsSource::to_json).collect::<Vec<_>>(),
    })
}

pub fn addresses_from_answers(data: &Value) -> (Vec<String>, Option<i64>, bool) {
    let mut addresses: Vec<String> = Vec::new();
    let mut ttl: Option<i64> = None;
    let mut truncated = false;
    let answers = data.get("Answer").and_then(Value::as_array);
    for item in answers.into_iter().flatten() {
        if !is_a_record(item) {
            continue;
        }
        let Some(address) = item.get("data").and_then(Value::as_str) else {
            continue;
        };
        let Ok(address) = address.parse::<std::net::Ipv4Addr>() else {
            continue;
        };
        if !is_global_ipv4(address) {
            continue;
        }
        let value = address.to_string();
        if addresses.contains(&value) {
            continue;
        }
        if addresses.len() >= PUBLIC_CHECK_MAX_ADDRESSES {
            truncated = true;
            continue;
        }
        addresses.push(value);
        if let Some(record_ttl) = valid_ttl(item.get("TTL")) {
            ttl = Some(ttl.map_or(record_ttl, |current| current.min(record_ttl)));
        }
    }
    (addresses, ttl, truncated)
}

pub fn negative_ttl(data: &Value) -> Option<i64> {
    let authority = data.get("Authority").and_then(Value::as_array);
    for item in authority.into_iter().flatten() {
        if item.get("type").and_then(Value::as_i64) != Some(6) {
            continue;
        }
        let record_ttl = valid_ttl(item.get("TTL"));
        let fields: Vec<&str> = item
            .get("data")
            .and_then(Value::as_str)
            .map(|data| data.split_whitespace().collect())
            .unwrap_or_default();
        let minimum = if fields.len() >= 7 && fields[6].chars().all(|c| c.is_ascii_digit()) {
            fields[6]
                .parse::<i64>()
                .ok()
                .and_then(|value| (0..=DNS_TTL_MAX_SECONDS).contains(&value).then_some(value))
        } else {
            None
        };
        if let (Some(record_ttl), Some(minimum)) = (record_ttl, minimum) {
            return Some(record_ttl.min(minimum));
        }
    }
    None
}

pub fn summarize_check(result: &mut Value) {
    let entries = result
        .get("entries")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let checked: Vec<Value> = entries
        .iter()
        .filter(|entry| entry.get("state").and_then(Value::as_str) != Some("unchecked"))
        .cloned()
        .collect();
    let has_unchecked = checked.len() != entries.len();
    let (overall, reason) = if checked.is_empty() {
        ("not_checked", "deadline_exceeded")
    } else if checked
        .iter()
        .all(|entry| entry.get("state").and_then(Value::as_str) == Some("ok"))
    {
        if has_unchecked {
            ("degraded", "deadline_exceeded")
        } else {
            ("ok", "ok")
        }
    } else if checked
        .iter()
        .all(|entry| entry.get("state").and_then(Value::as_str) == Some("failed"))
    {
        if has_unchecked {
            ("incomplete", "deadline_exceeded")
        } else {
            ("all_failed", "all_entries_failed")
        }
    } else {
        (
            "degraded",
            if has_unchecked {
                "deadline_exceeded"
            } else {
                "partial_failure"
            },
        )
    };
    if let Value::Object(object) = result {
        object.insert("overall".to_string(), Value::String(overall.to_string()));
        object.insert("reason".to_string(), Value::String(reason.to_string()));
    }
}

pub fn check_exit_code(overall: &str) -> i32 {
    match overall {
        "ok" => 0,
        "not_checked" => 2,
        _ => 1,
    }
}

pub fn bounded_check_deadline(value: Option<f64>) -> f64 {
    let Some(value) = value else {
        return PUBLIC_CHECK_TIMEOUT_S;
    };
    if !value.is_finite() || !(0.1..=PUBLIC_CHECK_MAX_TIMEOUT_S).contains(&value) {
        return PUBLIC_CHECK_TIMEOUT_S;
    }
    value
}

pub fn check_public_connection(
    public_url: Option<&str>,
    deadline_s: f64,
    transport: &dyn ProbeTransport,
    clock: &dyn Clock,
    env: &BTreeMap<String, String>,
) -> Value {
    let started = clock.monotonic_ms();
    let bounded = bounded_check_deadline(Some(deadline_s));
    let mut result = json!({
        "version": 1,
        "checked_at_utc": clock.utc_iso_seconds(),
        "checked_at_epoch": 0.0,
        "public_url": null,
        "host": null,
        "source": null,
        "overall": "not_checked",
        "reason": "public_url_unavailable",
        "deadline_s": bounded,
        "elapsed_ms": 0,
        "entries": [],
        "dns": null,
    });
    let Some(host) = public_host(public_url, env) else {
        return result;
    };
    let source = public_source(&host, env);
    set(&mut result, "public_url", json!(format!("https://{host}")));
    set(&mut result, "host", json!(host));
    set(&mut result, "source", json!(source));
    let deadline = started + bounded * 1000.0;
    match transport.resolve_public_addresses(&host, deadline) {
        Err(ProbeFailure::DnsTimeout(detail)) => {
            set(&mut result, "reason", json!("dns_timeout"));
            set(&mut result, "dns", detail);
        }
        Err(ProbeFailure::Timeout) => {
            set(&mut result, "reason", json!("dns_timeout"));
            set(
                &mut result,
                "dns",
                json!({
                    "version": 1,
                    "checked_at_utc": clock.utc_iso_seconds(),
                    "checked_at_epoch": clock.now_ms() / 1000.0,
                    "state": "timeout",
                    "reason": "dns_timeout",
                    "addresses": [],
                    "addresses_truncated": false,
                    "ttl_s": null,
                    "negative_ttl_s": null,
                    "sources": [],
                }),
            );
        }
        Err(ProbeFailure::Transport(_)) => {
            set(&mut result, "reason", json!("dns_failed"));
        }
        Ok(resolution) => {
            let detail = resolution.detail.clone();
            let addresses = resolution.addresses.clone();
            let reason = resolution.reason.clone();
            set(&mut result, "dns", detail.clone());
            if !addresses.is_empty() {
                let mut entries = Vec::new();
                let mut exhausted = false;
                for address in addresses.iter().take(PUBLIC_CHECK_MAX_ADDRESSES) {
                    if exhausted || clock.monotonic_ms() >= deadline {
                        exhausted = true;
                        entries.push(unchecked_entry(&host, address));
                    } else {
                        let entry_deadline = deadline
                            .min(clock.monotonic_ms() + PUBLIC_CHECK_ADDRESS_TIMEOUT_S * 1000.0);
                        entries.push(check_entry(
                            &host,
                            address,
                            entry_deadline,
                            transport,
                            clock,
                        ));
                    }
                }
                set(&mut result, "entries", Value::Array(entries));
                summarize_check(&mut result);
                let dns_state = detail.get("state").and_then(Value::as_str);
                if result.get("overall").and_then(Value::as_str) == Some("ok")
                    && matches!(dns_state, Some("disagreement" | "unconfirmed"))
                {
                    set(&mut result, "overall", json!("degraded"));
                    set(
                        &mut result,
                        "reason",
                        json!(format!("dns_{}", dns_state.unwrap_or_default())),
                    );
                }
                if detail.get("addresses_truncated").and_then(Value::as_bool) == Some(true) {
                    match result.get("overall").and_then(Value::as_str) {
                        Some("all_failed" | "not_checked") => {
                            set(&mut result, "overall", json!("incomplete"));
                        }
                        Some("ok") => set(&mut result, "overall", json!("degraded")),
                        _ => {}
                    }
                    set(&mut result, "reason", json!("addresses_truncated"));
                }
            } else if reason == "ok" {
                set(&mut result, "reason", json!("dns_empty"));
            } else {
                set(&mut result, "reason", json!(reason));
            }
        }
    }
    set(
        &mut result,
        "checked_at_utc",
        json!(clock.utc_iso_seconds()),
    );
    set(
        &mut result,
        "checked_at_epoch",
        json!(clock.now_ms() / 1000.0),
    );
    set(
        &mut result,
        "elapsed_ms",
        json!((clock.monotonic_ms() - started).max(0.0) as i64),
    );
    result
}

fn check_entry(
    host: &str,
    address: &str,
    deadline: f64,
    transport: &dyn ProbeTransport,
    clock: &dyn Clock,
) -> Value {
    let started = clock.monotonic_ms();
    let mut entry = json!({
        "host": host,
        "ip": address,
        "state": "failed",
        "dns": "ok",
        "tls": "not_checked",
        "certificate": "not_checked",
        "reason": "connection_failed",
        "elapsed_ms": 0,
        "checked_at_epoch": null,
    });
    let check = transport.check_tls(
        host,
        address,
        deadline.min(clock.monotonic_ms() + PUBLIC_CHECK_ADDRESS_TIMEOUT_S * 1000.0),
    );
    match check {
        Ok(check) => {
            set(&mut entry, "state", json!(check.state));
            set(&mut entry, "tls", json!(check.tls));
            set(&mut entry, "certificate", json!(check.certificate));
            set(&mut entry, "reason", json!(check.reason));
        }
        Err(ProbeFailure::Timeout) => {
            set(&mut entry, "state", json!("failed"));
            set(&mut entry, "reason", json!("timeout"));
        }
        Err(_) => {}
    }
    set(
        &mut entry,
        "elapsed_ms",
        json!((clock.monotonic_ms() - started).max(0.0) as i64),
    );
    set(
        &mut entry,
        "checked_at_epoch",
        json!(clock.now_ms() / 1000.0),
    );
    entry
}

fn unchecked_entry(host: &str, address: &str) -> Value {
    json!({
        "host": host,
        "ip": address,
        "state": "unchecked",
        "dns": "not_checked",
        "tls": "not_checked",
        "certificate": "not_checked",
        "reason": "not_checked",
        "elapsed_ms": 0,
        "checked_at_epoch": null,
    })
}

fn set(value: &mut Value, key: &str, item: Value) {
    if let Value::Object(object) = value {
        object.insert(key.to_string(), item);
    }
}

fn is_a_record(item: &Value) -> bool {
    match item.get("type") {
        Some(Value::Number(number)) => number.as_f64() == Some(1.0),
        _ => false,
    }
}

fn valid_ttl(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Number(number)) => number
            .as_i64()
            .filter(|value| (0..=DNS_TTL_MAX_SECONDS).contains(value)),
        _ => None,
    }
}

fn is_global_ipv4(address: std::net::Ipv4Addr) -> bool {
    let octets = address.octets();
    let first = octets[0];
    let second = octets[1];
    let private = first == 0
        || first == 10
        || first == 127
        || (first == 100 && (64..=127).contains(&second))
        || (first == 169 && second == 254)
        || (first == 172 && (16..=31).contains(&second))
        || (first == 192 && second == 168)
        || (first == 192 && second == 0)
        || (first == 192 && second == 2)
        || (first == 198 && (18..=19).contains(&second))
        || (first == 198 && second == 51 && octets[2] == 100)
        || (first == 203 && second == 0 && octets[2] == 113)
        || first >= 240;
    !private
}
