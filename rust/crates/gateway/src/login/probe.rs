//! Single-shot, side-effect-free credential classification.
//!
//! This is the Rust port of `chat2api/chat2api.py::classify_credential`: one
//! `GET /api/models` probe, never a retry, never a state write. `needs_login`
//! is local (no bearer leaves the box), `valid` requires an upstream 200 with a
//! usable catalog, `rejected` is an upstream 401/403, and everything else is
//! `unknown` so a network failure is never reported as an expired credential.

use serde_json::Value;

use crate::config::{ConfigError, GatewayConfig};
use crate::readonly::UpstreamClient;

/// One credential classification observation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialVerdict {
    pub verdict: &'static str,
    pub reason: Option<&'static str>,
    pub upstream_status: Option<u16>,
}

impl CredentialVerdict {
    pub fn needs_login() -> Self {
        Self {
            verdict: "needs_login",
            reason: Some("no_local_credential"),
            upstream_status: None,
        }
    }

    pub fn valid(status: u16) -> Self {
        Self {
            verdict: "valid",
            reason: None,
            upstream_status: Some(status),
        }
    }

    pub fn unknown(reason: &'static str, status: Option<u16>) -> Self {
        Self {
            verdict: "unknown",
            reason: Some(reason),
            upstream_status: status,
        }
    }

    pub fn rejected(status: u16) -> Self {
        Self {
            verdict: "rejected",
            reason: Some("upstream_rejected"),
            upstream_status: Some(status),
        }
    }
}

/// The credential probe seam.
pub trait CredentialProbe: Send + Sync {
    fn classify(&self, bearer: &str) -> CredentialVerdict;
}

/// The production probe over the configured loopback upstream.
pub struct HttpCredentialProbe {
    client: UpstreamClient,
}

impl HttpCredentialProbe {
    pub fn from_config(config: &GatewayConfig) -> Result<Self, ConfigError> {
        UpstreamClient::from_config(config).map(|client| Self { client })
    }
}

impl CredentialProbe for HttpCredentialProbe {
    fn classify(&self, bearer: &str) -> CredentialVerdict {
        if bearer.is_empty() {
            return CredentialVerdict::needs_login();
        }
        match self.client.get("/api/models", Some(bearer)) {
            Err(failure) => CredentialVerdict::unknown(failure.name(), None),
            Ok(response) => {
                let status = response.status;
                if status == 401 || status == 403 {
                    return CredentialVerdict::rejected(status);
                }
                if (500..=599).contains(&status) {
                    return CredentialVerdict::unknown("upstream_5xx", Some(status));
                }
                if status != 200 {
                    return CredentialVerdict::unknown("unexpected_status", Some(status));
                }
                let payload: Value = match serde_json::from_slice(&response.body) {
                    Ok(payload) => payload,
                    Err(_) => return CredentialVerdict::unknown("non_json_response", Some(status)),
                };
                if payload
                    .as_object()
                    .and_then(|payload| payload.get("data"))
                    .and_then(Value::as_array)
                    .is_none()
                {
                    return CredentialVerdict::unknown("invalid_catalog_shape", Some(status));
                }
                CredentialVerdict::valid(status)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_empty_bearer_is_needs_login_without_a_request() {
        let config = GatewayConfig {
            upstream_base_url: "http://127.0.0.1:9".to_string(),
            ..GatewayConfig::default()
        };
        let probe = HttpCredentialProbe::from_config(&config).expect("probe");
        assert_eq!(
            probe.classify(""),
            CredentialVerdict {
                verdict: "needs_login",
                reason: Some("no_local_credential"),
                upstream_status: None,
            }
        );
    }
}
