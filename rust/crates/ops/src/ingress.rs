//! Read-only parity for `um-cloud-deploy/cloudflared/check-ingress.py`.
//!
//! The checker only parses one tunnel ingress file and returns
//! `{ok, problems, rules}`. It never writes, reloads or restarts a tunnel.

use std::fmt;
use std::fs;
use std::path::Path;

use serde_json::{json, Map, Value};

pub const DEFAULT_HOST: &str = "dacintapi.de5.net";
pub const API_PATHS: [&str; 4] = [
    "/v1/models",
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
];
pub const API_SERVICE: &str = "http://127.0.0.1:8100";
pub const CATCH_ALL: &str = "http_status:404";
pub const FORBIDDEN_PORTS: [&str; 4] = ["8096", "8097", "18096", "5901"];

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngressError {
    message: String,
}

impl IngressError {
    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for IngressError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for IngressError {}

pub fn check_ingress(config: &Path, expect_host: &str) -> Value {
    let mut problems = Vec::new();
    let mut rules = Vec::new();
    let text = match fs::read_to_string(config) {
        Ok(text) => text,
        Err(error) => {
            return json!({
                "ok": false,
                "problems": [format!("config unreadable: {error}")],
                "rules": [],
            });
        }
    };
    let payload: serde_yaml::Value = match serde_yaml::from_str(&text) {
        Ok(payload) => payload,
        Err(error) => {
            return json!({
                "ok": false,
                "problems": [format!("invalid YAML: {error}")],
                "rules": [],
            });
        }
    };
    let serde_yaml::Value::Mapping(payload) = payload else {
        return json!({
            "ok": false,
            "problems": ["config must be a mapping"],
            "rules": [],
        });
    };
    let ingress = match payload.get(serde_yaml::Value::String("ingress".to_string())) {
        Some(serde_yaml::Value::Sequence(ingress)) if !ingress.is_empty() => ingress,
        _ => {
            return json!({
                "ok": false,
                "problems": ["ingress must be a non-empty list"],
                "rules": [],
            });
        }
    };

    let mut seen_paths = std::collections::BTreeSet::new();
    let mut catch_all_index: Option<usize> = None;
    for (index, rule) in ingress.iter().enumerate() {
        let serde_yaml::Value::Mapping(rule) = rule else {
            problems.push(format!("rule {index} must be a mapping"));
            continue;
        };
        let service = yaml_string(rule.get(serde_yaml::Value::String("service".to_string())));
        let hostname = yaml_string(rule.get(serde_yaml::Value::String("hostname".to_string())));
        let path = yaml_string(rule.get(serde_yaml::Value::String("path".to_string())));
        let mut entry = Map::new();
        entry.insert(
            "hostname".to_string(),
            hostname.clone().map(Value::String).unwrap_or(Value::Null),
        );
        entry.insert(
            "path".to_string(),
            path.as_deref()
                .and_then(normalise_path)
                .map(Value::String)
                .unwrap_or(Value::Null),
        );
        entry.insert(
            "service".to_string(),
            service.clone().map(Value::String).unwrap_or(Value::Null),
        );
        rules.push(Value::Object(entry));

        let Some(service) = service.filter(|service| !service.is_empty()) else {
            problems.push(format!("rule {index} has no service"));
            continue;
        };
        for port in FORBIDDEN_PORTS {
            if service.contains(port) {
                problems.push(format!(
                    "rule {index} publishes private port {port}: {service}"
                ));
            }
        }
        if service == CATCH_ALL {
            if catch_all_index.is_some() {
                problems.push("more than one catch-all rule".to_string());
            }
            catch_all_index = Some(index);
            if hostname.is_some() || path.is_some() {
                problems.push("the catch-all rule must not name a hostname or path".to_string());
            }
            continue;
        }
        if service != API_SERVICE {
            problems.push(format!(
                "rule {index} service must be {API_SERVICE}: {service}"
            ));
            continue;
        }
        match hostname {
            None => problems.push(format!("rule {index} has no hostname")),
            Some(hostname) if !expect_host.is_empty() && hostname != expect_host => {
                problems.push(format!(
                    "rule {index} hostname is {hostname}, expected {expect_host}"
                ));
            }
            Some(_) => {}
        }
        match path.as_deref().and_then(normalise_path) {
            None => problems.push(format!("rule {index} has no path")),
            Some(normalised) if !API_PATHS.contains(&normalised.as_str()) => {
                problems.push(format!(
                    "rule {index} path is not a shared API path: {}",
                    path.as_deref().unwrap_or_default()
                ));
            }
            Some(normalised) if seen_paths.contains(&normalised) => {
                problems.push(format!("duplicate API path: {normalised}"));
            }
            Some(normalised) => {
                seen_paths.insert(normalised);
            }
        }
    }

    match catch_all_index {
        None => problems.push("missing the catch-all http_status:404 rule".to_string()),
        Some(index) if index != ingress.len() - 1 => {
            problems.push("the catch-all rule must be the last ingress rule".to_string());
        }
        Some(_) => {}
    }
    for path in API_PATHS {
        if !seen_paths.contains(path) {
            problems.push(format!("missing shared API path: {path}"));
        }
    }
    json!({"ok": problems.is_empty(), "problems": problems, "rules": rules})
}

fn yaml_string(value: Option<&serde_yaml::Value>) -> Option<String> {
    match value {
        Some(serde_yaml::Value::String(value)) => Some(value.clone()),
        _ => None,
    }
}

fn normalise_path(path: &str) -> Option<String> {
    let mut text = path.trim();
    if let Some(stripped) = text.strip_prefix('^') {
        text = stripped;
    }
    if let Some(stripped) = text.strip_suffix('$') {
        text = stripped;
    }
    Some(text.to_string())
}
