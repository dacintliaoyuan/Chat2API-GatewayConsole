//! AC3: the fixed action whitelist and its closed field vocabularies.
//!
//! Actions are a Rust enum, not free-form strings: a handler receives a typed
//! [`Action`], so a shell/path/service name cannot reach an implementation.
//! The only wire values are the frozen management and instance action names, and
//! every action's fields are a fixed whitelist with specialized enums where the
//! Python protocol restricts them.

use serde_json::{Map, Value};

use crate::error::{ProtocolError, ResultCode};
use crate::request::{INSTANCE_ACTIONS, MANAGEMENT_ACTIONS};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ManagementAction {
    Status,
    Publish,
    Start,
    Drain,
    EmergencyStop,
    TsServiceControl,
    HostVerify,
    HostStop,
    HostRepairLimits,
    MaintenanceBegin,
    MaintenanceEnd,
    MaintenanceStatus,
    GatewayRestart,
    HostEvidence,
}

impl ManagementAction {
    pub const ALL: [Self; 14] = [
        Self::Status,
        Self::Publish,
        Self::Start,
        Self::Drain,
        Self::EmergencyStop,
        Self::TsServiceControl,
        Self::HostVerify,
        Self::HostStop,
        Self::HostRepairLimits,
        Self::MaintenanceBegin,
        Self::MaintenanceEnd,
        Self::MaintenanceStatus,
        Self::GatewayRestart,
        Self::HostEvidence,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Status => "status",
            Self::Publish => "publish",
            Self::Start => "start",
            Self::Drain => "drain",
            Self::EmergencyStop => "emergency-stop",
            Self::TsServiceControl => "ts-service-control",
            Self::HostVerify => "host-verify",
            Self::HostStop => "host-stop",
            Self::HostRepairLimits => "host-repair-limits",
            Self::MaintenanceBegin => "maintenance-begin",
            Self::MaintenanceEnd => "maintenance-end",
            Self::MaintenanceStatus => "maintenance-status",
            Self::GatewayRestart => "gateway-restart",
            Self::HostEvidence => "host-evidence",
        }
    }

    pub fn from_wire(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|action| action.as_str() == name)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum InstanceAction {
    InstanceStatus,
    Load,
    Drain,
    Emergency,
    Resume,
}

impl InstanceAction {
    pub const ALL: [Self; 5] = [
        Self::InstanceStatus,
        Self::Load,
        Self::Drain,
        Self::Emergency,
        Self::Resume,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::InstanceStatus => "instance-status",
            Self::Load => "load",
            Self::Drain => "drain",
            Self::Emergency => "emergency",
            Self::Resume => "resume",
        }
    }

    pub fn from_wire(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|action| action.as_str() == name)
    }
}

/// The control channel an action belongs to. The two channels share some
/// names (`drain`), so the channel is part of the typed identity.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Channel {
    Management,
    Instance,
}

/// The typed action a validated request carries.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Action {
    Management(ManagementAction),
    Instance(InstanceAction),
}

impl Action {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Management(action) => action.as_str(),
            Self::Instance(action) => action.as_str(),
        }
    }

    pub fn management(self) -> Option<ManagementAction> {
        match self {
            Self::Management(action) => Some(action),
            Self::Instance(_) => None,
        }
    }

    pub fn instance(self) -> Option<InstanceAction> {
        match self {
            Self::Instance(action) => Some(action),
            Self::Management(_) => None,
        }
    }

    pub fn from_wire(channel: Channel, name: &str) -> Option<Self> {
        match channel {
            Channel::Management => ManagementAction::from_wire(name).map(Self::Management),
            Channel::Instance => InstanceAction::from_wire(name).map(Self::Instance),
        }
    }
}

pub const TS_SERVICE_CONTROL_ACTIONS: [&str; 2] = ["start", "stop"];
pub const HOST_VERIFY_CHECKS: [&str; 2] = ["running", "closed"];
pub const HOST_EVIDENCE_CHECKS: [&str; 3] = ["limits", "faults", "watchdog"];
pub const MAINTENANCE_COMPONENTS: [&str; 1] = ["share_gateway"];
pub const GATEWAY_RESTART_COMPONENTS: [&str; 1] = ["share_gateway"];
pub const MAINTENANCE_LEASE_MAX_S: i64 = 600;
pub const GATEWAY_RESTART_BUDGET_MAX_S: i64 = 180;
pub const MAINTENANCE_LEASE_DEFAULT_S: i64 = 300;
pub const GATEWAY_RESTART_BUDGET_DEFAULT_S: i64 = 120;

/// Apply the action-specific field vocabulary BEFORE the handler runs. The
/// generic schema only checks presence/types; this closes the enums the Python
/// coordinator validates inside its action branches.
pub fn validate_management_fields(
    action: ManagementAction,
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    match action {
        ManagementAction::TsServiceControl => validate_ts_service_control_fields(fields),
        ManagementAction::HostVerify => validate_host_verify_fields(fields),
        ManagementAction::HostStop => validate_host_stop_fields(fields),
        ManagementAction::HostRepairLimits => validate_host_repair_limits_fields(fields),
        ManagementAction::MaintenanceBegin => validate_maintenance_begin_fields(fields),
        ManagementAction::MaintenanceEnd => validate_maintenance_end_fields(fields),
        ManagementAction::MaintenanceStatus => validate_maintenance_status_fields(fields),
        ManagementAction::GatewayRestart => validate_gateway_restart_fields(fields),
        ManagementAction::HostEvidence => validate_host_evidence_fields(fields),
        ManagementAction::Status
        | ManagementAction::Publish
        | ManagementAction::Start
        | ManagementAction::Drain
        | ManagementAction::EmergencyStop => Ok(fields.clone()),
    }
}

/// `ts-service-control` carries the fixed verb enum only: the service name is
/// a broker-side protocol constant and can never cross the wire.
pub fn validate_ts_service_control_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &["action"])?;
    let action = enum_field(fields, "action", &TS_SERVICE_CONTROL_ACTIONS)?;
    let mut normalized = Map::new();
    normalized.insert("action".to_string(), Value::String(action.to_string()));
    Ok(normalized)
}

/// `host-verify` carries the fixed check enum only; the task/port/image stays
/// broker-side deployment configuration.
pub fn validate_host_verify_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &["check"])?;
    let check = enum_field(fields, "check", &HOST_VERIFY_CHECKS)?;
    let mut normalized = Map::new();
    normalized.insert("check".to_string(), Value::String(check.to_string()));
    Ok(normalized)
}

/// `host-stop` has NO fields at all.
pub fn validate_host_stop_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &[])
}

/// `host-repair-limits` has NO fields at all.
pub fn validate_host_repair_limits_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &[])
}

/// `maintenance-begin` is exactly {component enum, lease_s 1..600}; out-of-range
/// values are rejected, never clamped.
pub fn validate_maintenance_begin_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &["component", "lease_s"])?;
    let component = enum_field(fields, "component", &MAINTENANCE_COMPONENTS)?;
    let lease_s = bounded_int(fields, "lease_s", MAINTENANCE_LEASE_MAX_S)?;
    let mut normalized = Map::new();
    normalized.insert(
        "component".to_string(),
        Value::String(component.to_string()),
    );
    normalized.insert("lease_s".to_string(), Value::from(lease_s));
    Ok(normalized)
}

/// `maintenance-end` is exactly {component enum, lease_id 1..64 chars}.
pub fn validate_maintenance_end_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &["component", "lease_id"])?;
    let component = enum_field(fields, "component", &MAINTENANCE_COMPONENTS)?;
    let lease_id = fields
        .get("lease_id")
        .and_then(Value::as_str)
        .ok_or_else(|| bad_request("lease_id must be a 1..64 char string"))?;
    if !(1..=64).contains(&lease_id.chars().count()) {
        return Err(bad_request("lease_id must be a 1..64 char string"));
    }
    let mut normalized = Map::new();
    normalized.insert(
        "component".to_string(),
        Value::String(component.to_string()),
    );
    normalized.insert("lease_id".to_string(), Value::String(lease_id.to_string()));
    Ok(normalized)
}

/// `maintenance-status` has NO fields at all.
pub fn validate_maintenance_status_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &[])
}

/// `gateway-restart` is exactly {component enum, budget_s 1..180}.
pub fn validate_gateway_restart_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &["component", "budget_s"])?;
    let component = enum_field(fields, "component", &GATEWAY_RESTART_COMPONENTS)?;
    let budget_s = bounded_int(fields, "budget_s", GATEWAY_RESTART_BUDGET_MAX_S)?;
    let mut normalized = Map::new();
    normalized.insert(
        "component".to_string(),
        Value::String(component.to_string()),
    );
    normalized.insert("budget_s".to_string(), Value::from(budget_s));
    Ok(normalized)
}

/// `host-evidence` is exactly {check enum}; the object it reads stays
/// broker-side deployment configuration.
pub fn validate_host_evidence_fields(
    fields: &Map<String, Value>,
) -> Result<Map<String, Value>, ProtocolError> {
    reject_unknown(fields, &["check"])?;
    let check = enum_field(fields, "check", &HOST_EVIDENCE_CHECKS)?;
    let mut normalized = Map::new();
    normalized.insert("check".to_string(), Value::String(check.to_string()));
    Ok(normalized)
}

fn reject_unknown(
    fields: &Map<String, Value>,
    allowed: &[&str],
) -> Result<Map<String, Value>, ProtocolError> {
    let unknown: Vec<&str> = fields
        .keys()
        .map(String::as_str)
        .filter(|key| !allowed.contains(key))
        .collect();
    if !unknown.is_empty() {
        let mut unknown = unknown;
        unknown.sort_unstable();
        let listed: Vec<String> = unknown.iter().map(|name| format!("'{name}'")).collect();
        return Err(bad_request(format!(
            "unknown field(s): [{}]",
            listed.join(", ")
        )));
    }
    Ok(Map::new())
}

fn enum_field<'a>(
    fields: &'a Map<String, Value>,
    key: &str,
    allowed: &[&str],
) -> Result<&'a str, ProtocolError> {
    let value = fields
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| bad_request(format!("{key} must be one of {allowed:?}")))?;
    if !allowed.contains(&value) {
        return Err(bad_request(format!("{key} must be one of {allowed:?}")));
    }
    Ok(value)
}

fn bounded_int(fields: &Map<String, Value>, key: &str, maximum: i64) -> Result<i64, ProtocolError> {
    let value = fields
        .get(key)
        .and_then(Value::as_i64)
        .filter(|value| (1..=maximum).contains(value))
        .ok_or_else(|| bad_request(format!("{key} must be an integer in 1..{maximum}")))?;
    Ok(value)
}

fn bad_request(message: impl Into<String>) -> ProtocolError {
    ProtocolError::new(ResultCode::BadRequest, message)
}

/// The fixed action tables are shared with [`crate::request`]; this assertion
/// lives here so the typed enum and the wire tables can never drift.
pub fn action_spec_matches(action: Action) -> bool {
    MANAGEMENT_ACTIONS
        .iter()
        .chain(INSTANCE_ACTIONS.iter())
        .any(|spec| spec.action == action)
}
