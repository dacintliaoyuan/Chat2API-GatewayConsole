//! AC3: `publish` writer -> load -> ACK coordination.
//!
//! Mirrors `share/broker_host.py:_publish`: the projection is parsed by the
//! shared parser, its fingerprint is checked, the writer atomically replaces the
//! projection, the trusted current instance is resolved (with one discovery
//! attempt when the registry is empty), the instance loads the generation and the
//! ACK is accepted only when the SAME registration, instance, generation and
//! fingerprint come back. `terminate_keys` is a one-shot list forwarded to the
//! load and never persisted in the projection.

use std::sync::{Arc, Mutex};

use serde_json::{json, Value};

use crate::error::{BrokerResult, ResultCode};
use um_store::projection::parse_projection_payload;

/// The protected projection writer the broker owns.
pub trait ProjectionWriter {
    fn fingerprint(&self, projection: &Value) -> String;
    fn write(
        &mut self,
        projection: &Value,
        generation: i64,
        fingerprint: &str,
    ) -> Result<(), String>;
}

/// The instance channel the broker talks to.
pub trait InstanceChannel {
    fn call(
        &mut self,
        action: &str,
        fields: &Value,
        instance_id: &str,
    ) -> Result<Value, String>;
}

/// Trusted registry of the CURRENT instance with a MONOTONIC version.
#[derive(Default)]
pub struct InstanceRegistry {
    current: Option<String>,
    version: u64,
}

impl InstanceRegistry {
    pub fn register(&mut self, instance_id: impl Into<String>) -> Result<(), String> {
        let instance_id = instance_id.into();
        if instance_id.is_empty() {
            return Err("instance_id must be a non-empty string".to_string());
        }
        self.current = Some(instance_id);
        self.version += 1;
        Ok(())
    }

    pub fn snapshot(&self) -> (Option<String>, u64) {
        (self.current.clone(), self.version)
    }

    pub fn current(&self) -> Option<String> {
        self.current.clone()
    }
}

pub struct PublishCoordinator<W: ProjectionWriter, I: InstanceChannel> {
    writer: W,
    channel: I,
    instances: Arc<Mutex<InstanceRegistry>>,
    discovery: Option<Box<dyn FnMut() -> bool>>,
}

impl<W: ProjectionWriter, I: InstanceChannel> PublishCoordinator<W, I> {
    pub fn new(
        writer: W,
        channel: I,
        instances: Arc<Mutex<InstanceRegistry>>,
    ) -> Self {
        Self {
            writer,
            channel,
            instances,
            discovery: None,
        }
    }

    pub fn with_discovery(mut self, discovery: Box<dyn FnMut() -> bool>) -> Self {
        self.discovery = Some(discovery);
        self
    }

    pub fn register_instance(&self, instance_id: impl Into<String>) -> Result<(), String> {
        self.instances.lock().unwrap().register(instance_id)
    }

    /// writer -> load -> ACK. A malformed projection, a mismatched
    /// fingerprint, a failed write, a replaced instance and a mismatched ACK are
    /// explicit refusals, never a fabricated success.
    pub fn publish(
        &mut self,
        projection: &Value,
        generation: i64,
        fingerprint: &str,
        terminate_keys: Option<&[String]>,
    ) -> BrokerResult {
        if let Err(error) = parse_projection_payload(projection) {
            return BrokerResult::err(ResultCode::BadRequest, error.message);
        }
        let actual = self.writer.fingerprint(projection);
        if actual != fingerprint {
            return BrokerResult::err(
                ResultCode::FingerprintMismatch,
                "projection fingerprint mismatch",
            );
        }
        if let Err(error) = self.writer.write(projection, generation, fingerprint) {
            return BrokerResult::err(ResultCode::InternalError, error);
        }
        let mut load_fields = json!({
            "generation": generation,
            "fingerprint": fingerprint,
        });
        if let Some(terminate_keys) = terminate_keys.filter(|keys| !keys.is_empty()) {
            load_fields["terminate_keys"] = json!(terminate_keys);
        }
        let (mut current_id, mut registration) = self.resolve_instance();
        if current_id.is_none() {
            return BrokerResult::err(ResultCode::Unconfirmed, "no registered instance");
        }
        let mut current = current_id.clone().unwrap_or_default();
        let response = match self.channel.call("load", &load_fields, &current) {
            Ok(response) => response,
            Err(_) => {
                if !self.refresh_discovery() {
                    return BrokerResult::err(ResultCode::Unconfirmed, "instance load failed");
                }
                (current_id, registration) = self.instances.lock().unwrap().snapshot();
                current = match current_id.clone() {
                    Some(current) => current,
                    None => {
                        return BrokerResult::err(
                            ResultCode::Unconfirmed,
                            "no registered instance after refresh",
                        )
                    }
                };
                match self.channel.call("load", &load_fields, &current) {
                    Ok(response) => response,
                    Err(error) => {
                        return BrokerResult::err(
                            ResultCode::Unconfirmed,
                            format!("instance load failed: {error}"),
                        )
                    }
                }
            }
        };
        let result = response
            .get("result")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        if response.get("ok") != Some(&Value::Bool(true)) {
            let code = response
                .get("error_code")
                .and_then(Value::as_str)
                .and_then(ResultCode::parse)
                .unwrap_or(ResultCode::Unconfirmed);
            let detail = result
                .get("detail")
                .and_then(Value::as_str)
                .unwrap_or("");
            return BrokerResult::err(code, detail);
        }
        let (current_now, version_now) = self.instances.lock().unwrap().snapshot();
        if version_now != registration || current_now.as_deref() != Some(current.as_str()) {
            return BrokerResult::err(
                ResultCode::InstanceMismatch,
                "instance was replaced while the load was in flight",
            );
        }
        if result.get("instance_id").and_then(Value::as_str) != current_now.as_deref() {
            return BrokerResult::err(
                ResultCode::InstanceMismatch,
                "ACK from a non-current instance",
            );
        }
        if result.get("generation").and_then(Value::as_i64) != Some(generation)
            || result.get("fingerprint").and_then(Value::as_str) != Some(fingerprint)
        {
            return BrokerResult::err(
                ResultCode::FingerprintMismatch,
                "ACK content differs from publication",
            );
        }
        BrokerResult::ok(Value::Object(result))
    }

    fn resolve_instance(&mut self) -> (Option<String>, u64) {
        let (mut current, mut version) = self.instances.lock().unwrap().snapshot();
        if current.is_none() {
            if let Some(discovery) = self.discovery.as_mut() {
                let _ = discovery();
            }
            (current, version) = self.instances.lock().unwrap().snapshot();
        }
        (current, version)
    }

    fn refresh_discovery(&mut self) -> bool {
        let (current, version) = self.instances.lock().unwrap().snapshot();
        if let Some(discovery) = self.discovery.as_mut() {
            let _ = discovery();
        }
        let (current_after, version_after) = self.instances.lock().unwrap().snapshot();
        current_after != current || version_after != version
    }
}
