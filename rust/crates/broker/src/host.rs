//! AC4: bounded host lifecycle and maintenance semantics.
//!
//! Mirrors `share/broker_host.py`: the restricted start has ONE bounded
//! readiness confirmation, drain waits for in-flight work and then stops the host
//! and verifies closure, emergency stop trusts the VERIFIED host closure, the
//! maintenance lease is bound to `(component, instance, round)`, and gateway
//! restart is a single-layer stop/start gated on three host proofs. Every verb
//! draws on a caller-visible budget; an unreadable evidence is never ready.

use std::collections::BTreeMap;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::error::{BrokerResult, ResultCode};
use crate::publish::{InstanceChannel, InstanceRegistry};

pub const DRAIN_WINDOW_SECONDS: f64 = 60.0;
pub const STOP_BUDGET_SECONDS: f64 = 20.0;
pub const START_READY_TIMEOUT_SECONDS: f64 = 6.0;
pub const START_POLL_INTERVAL_SECONDS: f64 = 0.25;
pub const GATEWAY_POLL_INTERVAL_SECONDS: f64 = 0.25;
pub const MAINTENANCE_COMPONENT: &str = "share_gateway";

/// The clock the coordinator uses; production is monotonic, tests advance it.
pub trait Clock {
    fn now(&self) -> f64;
    fn sleep(&self, seconds: f64);
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_secs_f64())
            .unwrap_or_default()
    }

    fn sleep(&self, seconds: f64) {
        if seconds > 0.0 {
            std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
        }
    }
}

/// The OS-facing host actions. A missing/unreadable verdict is `None`, never a
/// fabricated `true`.
pub trait HostOps {
    /// Every host verb receives the caller's remaining budget and must not
    /// block past it.
    fn start_host(&mut self, explicit: bool, deadline_s: f64) -> Option<bool>;
    fn stop_host(&mut self, deadline_s: f64) -> Option<bool>;
    fn verify_running(&mut self, deadline_s: f64) -> Option<bool>;
    fn verify_closed(&mut self, deadline_s: f64) -> Option<bool>;
    fn verify_limits_evidence(&mut self) -> Option<(Option<Value>, String)>;
    fn verify_limits(&mut self) -> Option<Option<bool>>;
    fn read_watchdog(&mut self) -> Option<Value>;
    fn read_faults(&mut self) -> Option<Value>;
    fn repair_limits(&mut self) -> Option<Value>;
}

/// One maintenance lease bound to `(component, instance, round, expires_at)`.
#[derive(Debug, Clone, PartialEq)]
pub struct MaintenanceLease {
    pub component: String,
    pub lease_id: String,
    pub instance_id: String,
    pub round: u64,
    pub created_at: i64,
    pub expires_at: i64,
}

#[derive(Default)]
pub struct MaintenanceLeaseStore {
    current: Option<MaintenanceLease>,
    counter: u64,
    now: Option<i64>,
}

impl MaintenanceLeaseStore {
    pub fn now(&mut self) -> i64 {
        match self.now {
            Some(now) => now,
            None => SystemClock.now() as i64,
        }
    }

    pub fn set_now(&mut self, now: i64) {
        self.now = Some(now);
    }

    pub fn current(&self) -> Option<&MaintenanceLease> {
        self.current.as_ref()
    }

    pub fn begin(
        &mut self,
        component: &str,
        instance_id: &str,
        round: u64,
        lease_s: i64,
    ) -> MaintenanceLease {
        let now = self.now();
        self.counter += 1;
        let lease = MaintenanceLease {
            component: component.to_string(),
            lease_id: format!("{:032x}", self.counter),
            instance_id: instance_id.to_string(),
            round,
            created_at: now,
            expires_at: now + lease_s,
        };
        self.current = Some(lease.clone());
        lease
    }

    pub fn end(&mut self, component: &str, lease_id: &str) -> (bool, Option<MaintenanceLease>) {
        let Some(lease) = self.current.clone() else {
            return (false, None);
        };
        if lease.component != component || lease.lease_id != lease_id {
            return (false, None);
        }
        self.current = None;
        (true, Some(lease))
    }

    pub fn expire_due(&mut self, now: i64) -> Option<MaintenanceLease> {
        let lease = self.current.clone()?;
        if now >= lease.expires_at {
            self.current = None;
            return Some(lease);
        }
        None
    }

    pub fn clear(&mut self) {
        self.current = None;
    }
}

/// The bounded lifecycle coordinator.
pub struct HostCoordinator<H: HostOps, I: InstanceChannel> {
    host: H,
    channel: I,
    instances: InstanceRegistry,
    clock: Box<dyn Clock>,
    leases: MaintenanceLeaseStore,
    drain_timeout: f64,
    stop_timeout: f64,
    start_ready_timeout: f64,
    start_poll_interval: f64,
    closure_epoch: u64,
    maintenance: Value,
}

impl<H: HostOps, I: InstanceChannel> HostCoordinator<H, I> {
    pub fn new(host: H, channel: I, instances: InstanceRegistry, clock: Box<dyn Clock>) -> Self {
        Self {
            host,
            channel,
            instances,
            clock,
            leases: MaintenanceLeaseStore::default(),
            drain_timeout: DRAIN_WINDOW_SECONDS,
            stop_timeout: STOP_BUDGET_SECONDS,
            start_ready_timeout: START_READY_TIMEOUT_SECONDS,
            start_poll_interval: START_POLL_INTERVAL_SECONDS,
            closure_epoch: 0,
            maintenance: idle_maintenance_record(),
        }
    }

    pub fn with_leases(mut self, leases: MaintenanceLeaseStore) -> Self {
        self.leases = leases;
        self
    }

    pub fn leases_mut(&mut self) -> &mut MaintenanceLeaseStore {
        &mut self.leases
    }

    pub fn maintenance_record(&self) -> &Value {
        &self.maintenance
    }

    fn now(&self) -> f64 {
        self.clock.now()
    }

    fn sleep(&self, seconds: f64) {
        self.clock.sleep(seconds);
    }

    /// Restricted start with ONE bounded readiness confirmation.
    pub fn start(&mut self, explicit: bool) -> BrokerResult {
        let deadline = self.now() + self.start_ready_timeout;
        if self.host.start_host(explicit, deadline - self.now()) != Some(true) {
            return BrokerResult::err(
                ResultCode::Unconfirmed,
                "start did not confirm the host task",
            );
        }
        if self.now() >= deadline {
            return BrokerResult::err(
                ResultCode::Unconfirmed,
                "start consumed the whole readiness budget before the host could be verified",
            );
        }
        let epoch = self.closure_epoch;
        let mut running = self.host.verify_running(deadline - self.now());
        while running != Some(true) && self.now() < deadline {
            let remaining = deadline - self.now();
            self.sleep(self.start_poll_interval.min(remaining));
            if self.now() >= deadline {
                break;
            }
            running = self.host.verify_running(deadline - self.now());
        }
        if running != Some(true) {
            return BrokerResult::err(
                ResultCode::Unconfirmed,
                "start did not verify a running host",
            );
        }
        if self.now() >= deadline {
            return BrokerResult::err(
                ResultCode::Unconfirmed,
                "readiness was confirmed only after the readiness budget expired",
            );
        }
        if self.closure_epoch != epoch {
            return BrokerResult::err(
                ResultCode::Unconfirmed,
                "a verified stop landed while the start waited for readiness",
            );
        }
        BrokerResult::ok(json!({"started": true}))
    }

    /// Bounded drain with a REAL final stop and closure verification.
    pub fn drain(&mut self) -> BrokerResult {
        let mut notification_failed = false;
        let mut active_left: Option<i64> = None;
        let instance_id = self.instances.current();
        let deadline = self.now() + self.drain_timeout;
        if let Some(instance_id) = instance_id.as_deref() {
            match self
                .channel
                .call("drain", &json!({}), instance_id, deadline - self.now())
            {
                Ok(response) if response.get("ok") == Some(&Value::Bool(true)) => {}
                _ => notification_failed = true,
            }
        } else {
            notification_failed = true;
        }
        while self.now() < deadline {
            let remaining = deadline - self.now();
            if remaining <= 0.0 {
                break;
            }
            let Some(instance_id) = instance_id.as_deref() else {
                break;
            };
            match self
                .channel
                .call("instance-status", &json!({}), instance_id, remaining)
            {
                Ok(response) => {
                    active_left = response
                        .get("result")
                        .and_then(|result| result.get("active_count"))
                        .and_then(Value::as_i64);
                    if active_left == Some(0) {
                        break;
                    }
                }
                Err(_) => {
                    notification_failed = true;
                    break;
                }
            }
            self.sleep(0.05_f64.min(deadline - self.now()));
        }
        let timed_out = active_left != Some(0);
        let stop_deadline = self.now() + self.stop_timeout;
        if let Some(instance_id) = instance_id.as_deref() {
            let budget = (self.stop_timeout / 2.0).min(stop_deadline - self.now());
            match self
                .channel
                .call("emergency", &json!({}), instance_id, budget)
            {
                Ok(response) if response.get("ok") == Some(&Value::Bool(true)) => {}
                _ => notification_failed = true,
            }
        } else {
            notification_failed = true;
        }
        let host_stopped = self.host.stop_host(stop_deadline - self.now()) == Some(true);
        let closed = host_stopped
            && self.host.verify_closed(stop_deadline - self.now()) == Some(true);
        if closed {
            self.closure_epoch += 1;
            self.leases.clear();
            self.maintenance = idle_maintenance_record();
        }
        BrokerResult::ok(json!({
            "drained": !timed_out,
            "timed_out": timed_out,
            "stopped": closed,
            "notification_failed": notification_failed,
            "in_flight": active_left,
        }))
    }

    /// Emergency: refuse new + clear in-flight, stop the host, VERIFY closure.
    pub fn emergency_stop(&mut self) -> BrokerResult {
        let mut notification_failed = false;
        let mut cleared: Option<i64> = None;
        let mut instance_stopped = false;
        let instance_id = self.instances.current();
        let stop_deadline = self.now() + self.stop_timeout;
        if let Some(instance_id) = instance_id.as_deref() {
            let budget = (self.stop_timeout / 2.0).min(stop_deadline - self.now());
            match self
                .channel
                .call("emergency", &json!({}), instance_id, budget)
            {
                Ok(response) => {
                    if response.get("ok") != Some(&Value::Bool(true)) {
                        notification_failed = true;
                    }
                    instance_stopped = response
                        .get("result")
                        .and_then(|result| result.get("state"))
                        .and_then(Value::as_str)
                        == Some("stopped");
                    cleared = response
                        .get("result")
                        .and_then(|result| result.get("cleared_in_flight"))
                        .and_then(Value::as_i64);
                }
                Err(_) => notification_failed = true,
            }
        } else {
            notification_failed = true;
        }
        let host_stopped = self.host.stop_host(stop_deadline - self.now()) == Some(true);
        let closed = host_stopped
            && self.host.verify_closed(stop_deadline - self.now()) == Some(true);
        let confirmed = closed;
        if confirmed {
            self.closure_epoch += 1;
            self.leases.clear();
            self.maintenance = idle_maintenance_record();
        }
        let mut result = json!({
            "stopped": confirmed,
            "notification_failed": notification_failed,
            "host_closed": closed,
            "instance_stopped": instance_stopped,
        });
        if let Some(cleared) = cleared {
            result["cleared_in_flight"] = json!(cleared);
        }
        BrokerResult::ok(result)
    }

    /// Pause acceptance with ONE lease bound to the CURRENT instance/round.
    pub fn maintenance_begin(&mut self, component: &str, lease_s: i64) -> BrokerResult {
        if self.leases.current().is_some() {
            return BrokerResult::err(ResultCode::Blocked, "maintenance lease already in force");
        }
        let (current, version) = self.instances.snapshot();
        let Some(current) = current else {
            return BrokerResult::err(ResultCode::Unconfirmed, "no registered instance");
        };
        let status = match self.channel.call("instance-status", &json!({}), &current) {
            Ok(response) if response.get("ok") == Some(&Value::Bool(true)) => {
                response.get("result").cloned().unwrap_or_else(|| json!({}))
            }
            Ok(response) => {
                return BrokerResult::err(
                    ResultCode::Unconfirmed,
                    response
                        .get("result")
                        .and_then(|result| result.get("detail"))
                        .and_then(Value::as_str)
                        .unwrap_or("instance status failed"),
                )
            }
            Err(error) => {
                return BrokerResult::err(
                    ResultCode::Unconfirmed,
                    format!("instance status failed: {error}"),
                )
            }
        };
        if status.get("state").and_then(Value::as_str) == Some("stopped") {
            return BrokerResult::err(ResultCode::Blocked, "instance is stopped");
        }
        let active = status.get("active_count").and_then(Value::as_i64);
        match self.channel.call("drain", &json!({}), &current) {
            Ok(response) if response.get("ok") == Some(&Value::Bool(true)) => {
                let drain_result = response.get("result").cloned().unwrap_or_else(|| json!({}));
                if drain_result.get("state").and_then(Value::as_str) != Some("draining") {
                    return BrokerResult::err(
                        ResultCode::Unconfirmed,
                        "instance drain state unconfirmed",
                    );
                }
            }
            Ok(response) => {
                let detail = response
                    .get("result")
                    .and_then(|result| result.get("detail"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if response.get("error_code").and_then(Value::as_str) == Some("bad_request")
                    && detail == "unknown action"
                {
                    return BrokerResult::err(
                        ResultCode::CapabilityMissing,
                        "instance does not support the maintenance drain",
                    );
                }
                return BrokerResult::err(
                    response
                        .get("error_code")
                        .and_then(Value::as_str)
                        .and_then(ResultCode::parse)
                        .unwrap_or(ResultCode::Unconfirmed),
                    detail,
                );
            }
            Err(error) => {
                return BrokerResult::err(
                    ResultCode::Unconfirmed,
                    format!("instance drain failed: {error}"),
                )
            }
        }
        let lease = self.leases.begin(component, &current, version, lease_s);
        self.maintenance = json!({
            "state": "leased",
            "component": component,
            "lease_id": lease.lease_id,
            "instance_id": current,
            "round": version,
            "created_at": lease.created_at,
            "expires_at": lease.expires_at,
            "by": "maintenance-begin",
            "last_outcome": Value::Null,
        });
        BrokerResult::ok(json!({
            "accepted": true,
            "component": component,
            "lease_id": lease.lease_id,
            "lease_expires_at": lease.expires_at,
            "instance_id": current,
            "round": version,
            "state": "draining",
            "active_count": active,
        }))
    }

    /// Resume acceptance for the CURRENT lease.
    pub fn maintenance_end(&mut self, component: &str, lease_id: &str) -> BrokerResult {
        let Some(lease) = self.leases.current().cloned() else {
            return BrokerResult::err(ResultCode::Unconfirmed, "no maintenance lease in force");
        };
        if lease.component != component || lease.lease_id != lease_id {
            return BrokerResult::err(ResultCode::InstanceMismatch, "lease does not match");
        }
        if self.leases.now() >= lease.expires_at {
            return BrokerResult::err(ResultCode::Unconfirmed, "maintenance lease expired");
        }
        let (current, version) = self.instances.snapshot();
        if current.as_deref() != Some(lease.instance_id.as_str()) || version != lease.round {
            return BrokerResult::err(ResultCode::InstanceMismatch, "instance or round changed");
        }
        let current = current.unwrap();
        match self.channel.call("resume", &json!({}), &current) {
            Ok(response) if response.get("ok") == Some(&Value::Bool(true)) => {
                let (ok, ended) = self.leases.end(component, lease_id);
                if !ok || ended.is_none() {
                    return BrokerResult::err(
                        ResultCode::InstanceMismatch,
                        "lease changed while resuming",
                    );
                }
                let result = response.get("result").cloned().unwrap_or_else(|| json!({}));
                self.maintenance = idle_maintenance_record();
                BrokerResult::ok(json!({
                    "resumed": true,
                    "component": component,
                    "lease_id": lease_id,
                    "instance_id": result.get("instance_id").cloned().unwrap_or(json!(current)),
                    "round": version,
                    "state": result.get("state").cloned().unwrap_or(json!("running")),
                    "active_count": result.get("active_count").cloned().unwrap_or(Value::Null),
                }))
            }
            Ok(response) => {
                let code = response
                    .get("error_code")
                    .and_then(Value::as_str)
                    .and_then(ResultCode::parse)
                    .unwrap_or(ResultCode::Unconfirmed);
                let detail = response
                    .get("result")
                    .and_then(|result| result.get("detail"))
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if code == ResultCode::CapabilityMissing {
                    return BrokerResult::err(
                        ResultCode::CapabilityMissing,
                        "instance does not support resume",
                    );
                }
                BrokerResult::err(code, detail)
            }
            Err(error) => BrokerResult::err(
                ResultCode::Unconfirmed,
                format!("instance resume failed: {error}"),
            ),
        }
    }

    /// Read-only maintenance view: unavailable evidence is `null`.
    pub fn maintenance_status(&mut self) -> BrokerResult {
        let now = self.leases.now();
        let lease = self.leases.current().cloned();
        let mut instance = Value::Null;
        if let Some(current) = self.instances.current() {
            if let Ok(response) = self.channel.call("instance-status", &json!({}), &current) {
                if let Some(result) = response.get("result") {
                    instance = json!({
                        "instance_id": result.get("instance_id").cloned().unwrap_or(json!(current)),
                        "state": result.get("state").cloned().unwrap_or(Value::Null),
                        "active_count": result.get("active_count").cloned().unwrap_or(Value::Null),
                        "last_seen_generation": result.get("last_seen_generation").cloned().unwrap_or(Value::Null),
                    });
                }
            }
        }
        BrokerResult::ok(json!({
            "checked_at": now,
            "lease": lease.as_ref().map(lease_value).unwrap_or(Value::Null),
            "lease_expired": lease.as_ref().map(|lease| now >= lease.expires_at).unwrap_or(false),
            "instance": instance,
            "last_outcome": self.maintenance.get("last_outcome").cloned().unwrap_or(Value::Null),
        }))
    }

    /// ONE bounded 8100 single-layer restart gated on the three host proofs.
    pub fn gateway_restart(&mut self, component: &str, budget_s: i64) -> BrokerResult {
        let started = self.now();
        if self.host.verify_running() != Some(true) {
            return BrokerResult::err(
                ResultCode::Blocked,
                "gateway identity unconfirmed or stopped",
            );
        }
        let lease_before = self.leases.current().cloned();
        let now = self.leases.now();
        self.maintenance = json!({
            "state": "restarting",
            "component": component,
            "lease_id": lease_before.as_ref().map(|lease| lease.lease_id.clone()),
            "instance_id": lease_before.as_ref().map(|lease| lease.instance_id.clone()),
            "round": lease_before.as_ref().map(|lease| lease.round),
            "created_at": now,
            "expires_at": now + budget_s + 30,
            "by": "gateway-restart",
            "last_outcome": Value::Null,
        });
        let mut stopped_confirmed = false;
        self.host.stop_host();
        let deadline = self.now() + budget_s as f64;
        while self.now() < deadline {
            if self.host.verify_closed() == Some(true) {
                stopped_confirmed = true;
                break;
            }
            self.sleep(GATEWAY_POLL_INTERVAL_SECONDS);
        }
        let mut running_confirmed = false;
        if stopped_confirmed {
            self.host.start_host(true);
            let deadline = self.now() + budget_s as f64;
            while self.now() < deadline {
                if self.host.verify_running() == Some(true) {
                    running_confirmed = true;
                    break;
                }
                self.sleep(GATEWAY_POLL_INTERVAL_SECONDS);
            }
        }
        let restarted = stopped_confirmed && running_confirmed;
        let elapsed = ((self.now() - started) * 1000.0).round() / 1000.0;
        let mut result = json!({
            "restarted": restarted,
            "component": component,
            "stopped_confirmed": stopped_confirmed,
            "running_confirmed": running_confirmed,
            "elapsed_s": elapsed,
            "maintenance_state": self.maintenance.get("state").cloned().unwrap_or(Value::Null),
        });
        if restarted {
            self.sync_maintenance_record("gateway-restart");
            if let Some(map) = result.as_object_mut() {
                map.insert(
                    "maintenance_state".to_string(),
                    self.maintenance
                        .get("state")
                        .cloned()
                        .unwrap_or(Value::Null),
                );
            }
            BrokerResult::ok(result)
        } else {
            if !stopped_confirmed {
                self.sync_maintenance_record("gateway-restart");
            }
            self.record_maintenance_outcome("gateway-restart", false, "unconfirmed");
            if let Some(map) = result.as_object_mut() {
                map.insert(
                    "maintenance_state".to_string(),
                    self.maintenance
                        .get("state")
                        .cloned()
                        .unwrap_or(Value::Null),
                );
            }
            BrokerResult::err_with_result(ResultCode::Unconfirmed, result)
        }
    }

    /// Read-only host evidence: the verdict is passed through VERBATIM.
    pub fn host_evidence(&mut self, check: &str) -> BrokerResult {
        let checked_at = self.clock.now() as i64;
        let (verdict, detail) = match check {
            "watchdog" => {
                let verdict = self.host.read_watchdog();
                let detail = verdict
                    .as_ref()
                    .and_then(|verdict| verdict.get("query_error"))
                    .and_then(Value::as_str)
                    .unwrap_or(if verdict.is_some() {
                        "ok"
                    } else {
                        "watchdog_capability_missing"
                    })
                    .to_string();
                (verdict, detail)
            }
            "limits" => match self.host.verify_limits_evidence() {
                Some((verdict, detail)) => (verdict, detail),
                None => {
                    let verdict = self.host.verify_limits().flatten();
                    let detail = if verdict.is_some() {
                        "ok".to_string()
                    } else {
                        "limits_unavailable".to_string()
                    };
                    (verdict.map(Value::Bool), detail)
                }
            },
            _ => {
                let verdict = self.host.read_faults();
                let detail = if verdict.is_some() {
                    "read tail ok".to_string()
                } else {
                    "fault_log_absent".to_string()
                };
                (verdict, detail)
            }
        };
        BrokerResult::ok(json!({
            "check": check,
            "verdict": verdict,
            "detail": detail,
            "checked_at": checked_at,
        }))
    }

    /// `host-verify`: the fixed running/closed evidence.
    pub fn host_verify(&mut self, check: &str) -> BrokerResult {
        let verdict = if check == "running" {
            self.host.verify_running()
        } else {
            self.host.verify_closed()
        };
        let key = if check == "running" {
            "host_running"
        } else {
            "host_closed"
        };
        BrokerResult::ok(json!({key: verdict}))
    }

    /// `host-stop`: stop then verify closure, never only the stop's return.
    pub fn host_stop(&mut self) -> BrokerResult {
        if self.host.stop_host() != Some(true) {
            return BrokerResult::err(ResultCode::Unconfirmed, "host stop did not confirm success");
        }
        if self.host.verify_closed() != Some(true) {
            return BrokerResult::err(ResultCode::Unconfirmed, "host stop did not verify closure");
        }
        BrokerResult::ok(json!({"host_stopped": true}))
    }

    /// `host-repair-limits`: a missing helper is `capability_missing`.
    pub fn host_repair_limits(&mut self) -> BrokerResult {
        let Some(result) = self.host.repair_limits() else {
            return BrokerResult::err(ResultCode::CapabilityMissing, "repair helper unavailable");
        };
        if result.get("repaired") != Some(&Value::Bool(true)) {
            let code = result
                .get("code")
                .and_then(Value::as_str)
                .and_then(ResultCode::parse)
                .unwrap_or(ResultCode::Unconfirmed);
            return BrokerResult::err(code, "limits repair not confirmed");
        }
        BrokerResult::ok(json!({"repaired": true, "code": "ok"}))
    }

    fn sync_maintenance_record(&mut self, by: &str) {
        let lease = self.leases.current().cloned();
        self.maintenance = match lease {
            None => idle_maintenance_record(),
            Some(lease) => json!({
                "state": "leased",
                "component": lease.component,
                "lease_id": lease.lease_id,
                "instance_id": lease.instance_id,
                "round": lease.round,
                "created_at": lease.created_at,
                "expires_at": lease.expires_at,
                "by": by,
                "last_outcome": Value::Null,
            }),
        };
    }

    fn record_maintenance_outcome(&mut self, action: &str, ok: bool, code: &str) {
        if ok {
            return;
        }
        self.maintenance["last_outcome"] = json!({
            "at": self.leases.now(),
            "action": action,
            "ok": false,
            "code": code.chars().take(40).collect::<String>(),
            "detail": "",
        });
    }

    /// Lazy lease expiry: only the still-current instance/round is resumed.
    pub fn expire_maintenance_due(&mut self) {
        let Some(lease) = self.leases.current().cloned() else {
            return;
        };
        if self.leases.now() < lease.expires_at {
            return;
        }
        let (current, version) = self.instances.snapshot();
        if current.as_deref() != Some(lease.instance_id.as_str()) || version != lease.round {
            return;
        }
        let current = current.unwrap();
        match self.channel.call("resume", &json!({}), &current) {
            Ok(response) if response.get("ok") == Some(&Value::Bool(true)) => {
                let now = self.leases.now();
                self.leases.expire_due(now);
                self.maintenance = idle_maintenance_record();
            }
            Ok(response) => {
                let code = response
                    .get("error_code")
                    .and_then(Value::as_str)
                    .unwrap_or("unconfirmed")
                    .to_string();
                self.record_maintenance_outcome("maintenance-end", false, &code);
            }
            Err(_) => {
                self.record_maintenance_outcome("maintenance-end", false, "unconfirmed");
            }
        }
    }

    pub fn adopt_maintenance_record(&mut self, record: Value) {
        if !record.is_object() {
            return;
        }
        let state = record.get("state").and_then(Value::as_str);
        if state == Some("leased") {
            if let (
                Some(component),
                Some(lease_id),
                Some(instance_id),
                Some(round),
                Some(created_at),
                Some(expires_at),
            ) = (
                record.get("component").and_then(Value::as_str),
                record.get("lease_id").and_then(Value::as_str),
                record.get("instance_id").and_then(Value::as_str),
                record.get("round").and_then(Value::as_u64),
                record.get("created_at").and_then(Value::as_i64),
                record.get("expires_at").and_then(Value::as_i64),
            ) {
                self.leases.adopt(MaintenanceLease {
                    component: component.to_string(),
                    lease_id: lease_id.to_string(),
                    instance_id: instance_id.to_string(),
                    round,
                    created_at,
                    expires_at,
                });
            }
        }
        self.maintenance = record;
    }
}

impl MaintenanceLeaseStore {
    fn adopt(&mut self, lease: MaintenanceLease) {
        self.current = Some(lease);
    }
}

fn lease_value(lease: &MaintenanceLease) -> Value {
    json!({
        "component": lease.component,
        "lease_id": lease.lease_id,
        "instance_id": lease.instance_id,
        "round": lease.round,
        "created_at": lease.created_at,
        "expires_at": lease.expires_at,
    })
}

fn idle_maintenance_record() -> Value {
    json!({
        "state": "idle",
        "component": Value::Null,
        "lease_id": Value::Null,
        "instance_id": Value::Null,
        "round": Value::Null,
        "created_at": Value::Null,
        "expires_at": Value::Null,
        "by": Value::Null,
        "last_outcome": Value::Null,
    })
}

/// A sorted map of instance id -> verified creation time, for the ACK evidence.
pub fn instance_creation_map() -> BTreeMap<String, f64> {
    BTreeMap::new()
}
