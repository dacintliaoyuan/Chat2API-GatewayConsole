//! AC2: schema-v2 request validation for both control channels.
//!
//! The check order mirrors `share/broker_protocol.py::_check_common`: version,
//! action whitelist, timestamp window, request id, channel identity, HMAC, fields
//! shape, field whitelist and field types. MAC verification therefore happens
//! BEFORE any field is interpreted, and every rejection is one fixed code.

use serde_json::{Map, Value};

use crate::actions::{validate_management_fields, Action, InstanceAction, ManagementAction};
use crate::error::{ProtocolError, ResultCode};
use crate::hmac::verify_value;

pub const SCHEMA_VERSION: i64 = 2;
pub const TS_WINDOW_SECONDS: i64 = 300;
pub const MAX_REQUEST_ID_CHARS: usize = 128;
pub const MAX_INSTANCE_ID_CHARS: usize = 64;
pub const MAX_STRING_FIELD_CHARS: usize = 128;
pub const MAX_KEY_ID_CHARS: usize = 64;
pub const MAX_TERMINATE_KEYS: usize = 16;

pub const MANAGEMENT_SIGNED_KEYS: [&str; 5] = ["v", "action", "request_id", "ts", "fields"];
pub const INSTANCE_SIGNED_KEYS: [&str; 6] =
    ["v", "action", "request_id", "ts", "instance_id", "fields"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FieldKind {
    Int,
    Str,
    Dict,
    Bool,
    List,
}

#[derive(Debug, Clone, Copy)]
pub struct FieldSpec {
    pub name: &'static str,
    pub kind: FieldKind,
    pub optional: bool,
}

#[derive(Debug, Clone, Copy)]
pub struct ActionSpec {
    pub action: Action,
    pub fields: &'static [FieldSpec],
}

impl ActionSpec {
    pub fn name(&self) -> &'static str {
        self.action.as_str()
    }
}

const fn field(name: &'static str, kind: FieldKind) -> FieldSpec {
    FieldSpec {
        name,
        kind,
        optional: false,
    }
}

const fn optional_field(name: &'static str, kind: FieldKind) -> FieldSpec {
    FieldSpec {
        name,
        kind,
        optional: true,
    }
}

const NONE: &[FieldSpec] = &[];

pub const MANAGEMENT_ACTIONS: &[ActionSpec] = &[
    ActionSpec {
        action: Action::Management(ManagementAction::Status),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Management(ManagementAction::Publish),
        fields: &[
            field("generation", FieldKind::Int),
            field("fingerprint", FieldKind::Str),
            field("projection", FieldKind::Dict),
            optional_field("terminate_keys", FieldKind::List),
        ],
    },
    ActionSpec {
        action: Action::Management(ManagementAction::Start),
        fields: &[optional_field("explicit", FieldKind::Bool)],
    },
    ActionSpec {
        action: Action::Management(ManagementAction::Drain),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Management(ManagementAction::EmergencyStop),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Management(ManagementAction::TsServiceControl),
        fields: &[field("action", FieldKind::Str)],
    },
    ActionSpec {
        action: Action::Management(ManagementAction::HostVerify),
        fields: &[field("check", FieldKind::Str)],
    },
    ActionSpec {
        action: Action::Management(ManagementAction::HostStop),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Management(ManagementAction::HostRepairLimits),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Management(ManagementAction::MaintenanceBegin),
        fields: &[
            field("component", FieldKind::Str),
            field("lease_s", FieldKind::Int),
        ],
    },
    ActionSpec {
        action: Action::Management(ManagementAction::MaintenanceEnd),
        fields: &[
            field("component", FieldKind::Str),
            field("lease_id", FieldKind::Str),
        ],
    },
    ActionSpec {
        action: Action::Management(ManagementAction::MaintenanceStatus),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Management(ManagementAction::GatewayRestart),
        fields: &[
            field("component", FieldKind::Str),
            field("budget_s", FieldKind::Int),
        ],
    },
    ActionSpec {
        action: Action::Management(ManagementAction::HostEvidence),
        fields: &[field("check", FieldKind::Str)],
    },
];

pub const INSTANCE_ACTIONS: &[ActionSpec] = &[
    ActionSpec {
        action: Action::Instance(InstanceAction::InstanceStatus),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Instance(InstanceAction::Load),
        fields: &[
            field("generation", FieldKind::Int),
            field("fingerprint", FieldKind::Str),
            optional_field("terminate_keys", FieldKind::List),
        ],
    },
    ActionSpec {
        action: Action::Instance(InstanceAction::Drain),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Instance(InstanceAction::Emergency),
        fields: NONE,
    },
    ActionSpec {
        action: Action::Instance(InstanceAction::Resume),
        fields: NONE,
    },
];

/// One authenticated request after schema validation.
#[derive(Debug, Clone, PartialEq)]
pub struct ValidatedRequest {
    pub action: Action,
    pub request_id: String,
    pub instance_id: Option<String>,
    pub ts: i64,
    pub fields: Map<String, Value>,
}

/// Validate + authenticate a management-channel request (8001 -> broker).
pub fn validate_management_request(
    body: &Value,
    secret: &str,
    now: i64,
) -> Result<ValidatedRequest, ProtocolError> {
    validate_request(
        body,
        secret,
        MANAGEMENT_ACTIONS,
        &MANAGEMENT_SIGNED_KEYS,
        false,
        now,
    )
}

/// Validate + authenticate an instance-channel request (broker -> share).
pub fn validate_instance_request(
    body: &Value,
    secret: &str,
    now: i64,
) -> Result<ValidatedRequest, ProtocolError> {
    validate_request(
        body,
        secret,
        INSTANCE_ACTIONS,
        &INSTANCE_SIGNED_KEYS,
        true,
        now,
    )
}

fn validate_request(
    body: &Value,
    secret: &str,
    actions: &[ActionSpec],
    signed_keys: &[&str],
    require_instance: bool,
    now: i64,
) -> Result<ValidatedRequest, ProtocolError> {
    let map = body
        .as_object()
        .ok_or_else(|| bad_request("request must be an object"))?;
    if map.get("v").and_then(Value::as_i64) != Some(SCHEMA_VERSION) {
        return Err(bad_request("unsupported protocol version"));
    }
    let action_name = map
        .get("action")
        .and_then(Value::as_str)
        .ok_or_else(|| bad_request("unknown action"))?;
    let spec = actions
        .iter()
        .find(|candidate| candidate.name() == action_name)
        .ok_or_else(|| bad_request("unknown action"))?;
    let ts = map
        .get("ts")
        .and_then(Value::as_i64)
        .ok_or_else(|| bad_request("request must include integer ts"))?;
    if ts.abs_diff(now) > TS_WINDOW_SECONDS as u64 {
        return Err(ProtocolError::new(
            ResultCode::StaleTs,
            "request timestamp outside window",
        ));
    }
    let request_id = map
        .get("request_id")
        .and_then(Value::as_str)
        .filter(|value| {
            let length = value.chars().count();
            (1..=MAX_REQUEST_ID_CHARS).contains(&length)
        })
        .ok_or_else(|| bad_request("invalid request_id"))?;
    let instance_id = match map.get("instance_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(value))
            if (1..=MAX_INSTANCE_ID_CHARS).contains(&value.chars().count()) =>
        {
            Some(value.clone())
        }
        Some(_) => return Err(bad_request("invalid instance_id")),
    };
    if require_instance && instance_id.is_none() {
        return Err(bad_request("invalid instance_id"));
    }
    if !require_instance && instance_id.is_some() {
        return Err(bad_request("management request must not carry instance_id"));
    }
    verify_value(secret, body, signed_keys)?;
    let fields = map
        .get("fields")
        .and_then(Value::as_object)
        .ok_or_else(|| bad_request("fields must be an object"))?;
    let unknown: Vec<&str> = fields
        .keys()
        .map(String::as_str)
        .filter(|key| !spec.fields.iter().any(|field| field.name == *key))
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
    let mut normalized = Map::new();
    for field in spec.fields {
        let Some(value) = fields.get(field.name) else {
            if field.optional {
                continue;
            }
            return Err(bad_request(format!("missing field: {}", field.name)));
        };
        validate_field(field, value)?;
        normalized.insert(field.name.to_string(), value.clone());
    }
    if let Action::Management(action) = spec.action {
        normalized = validate_management_fields(action, &normalized)?;
    }
    Ok(ValidatedRequest {
        action: spec.action,
        request_id: request_id.to_string(),
        instance_id,
        ts,
        fields: normalized,
    })
}

fn validate_field(field: &FieldSpec, value: &Value) -> Result<(), ProtocolError> {
    match field.kind {
        FieldKind::List => {
            let Some(items) = value.as_array() else {
                return Err(bad_request(format!(
                    "field {} must be a list of 1..{MAX_TERMINATE_KEYS} key ids \
                     (each 1..{MAX_KEY_ID_CHARS} chars)",
                    field.name
                )));
            };
            let valid = (1..=MAX_TERMINATE_KEYS).contains(&items.len())
                && items.iter().all(|item| {
                    item.as_str()
                        .is_some_and(|text| (1..=MAX_KEY_ID_CHARS).contains(&text.chars().count()))
                });
            if !valid {
                return Err(bad_request(format!(
                    "field {} must be a list of 1..{MAX_TERMINATE_KEYS} key ids \
                     (each 1..{MAX_KEY_ID_CHARS} chars)",
                    field.name
                )));
            }
        }
        FieldKind::Int => {
            if !value.as_i64().is_some_and(|number| number >= 1) {
                return Err(bad_request(format!(
                    "field {} must be a positive integer",
                    field.name
                )));
            }
        }
        FieldKind::Str => {
            let valid = value
                .as_str()
                .is_some_and(|text| (1..=MAX_STRING_FIELD_CHARS).contains(&text.chars().count()));
            if !valid {
                return Err(bad_request(format!(
                    "field {} must be a 1..{MAX_STRING_FIELD_CHARS} char string",
                    field.name
                )));
            }
        }
        FieldKind::Dict => {
            if !value.is_object() {
                return Err(bad_request(format!(
                    "field {} must be an object",
                    field.name
                )));
            }
        }
        FieldKind::Bool => {
            if !value.is_boolean() {
                return Err(bad_request(format!(
                    "field {} must be a boolean",
                    field.name
                )));
            }
        }
    }
    Ok(())
}

fn bad_request(message: impl Into<String>) -> ProtocolError {
    ProtocolError::new(ResultCode::BadRequest, message)
}
