//! Broker wire result codes.
//!
//! The fixed set is frozen in `docs/rust-contract.md` §7.1 and generated into
//! `tests/fixtures/rust_contract_rev1.json`; `tests/result_codes.rs` asserts the
//! two agree. An unknown code is never accepted as completion.

use std::fmt;

use serde_json::{Map, Value};

/// The frozen result-code table, in the contract's order.
pub const RESULT_CODES: [&str; 18] = [
    "ok",
    "not_installed",
    "stale_generation",
    "fingerprint_mismatch",
    "instance_mismatch",
    "replay",
    "stale_ts",
    "overlimit",
    "deadline_exceeded",
    "unconfirmed",
    "blocked",
    "bad_request",
    "forbidden",
    "internal_error",
    "empty_request",
    "unknown_instance",
    "capability_missing",
    "access_denied",
];

/// One fixed broker result code.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum ResultCode {
    Ok,
    NotInstalled,
    StaleGeneration,
    FingerprintMismatch,
    InstanceMismatch,
    Replay,
    StaleTs,
    Overlimit,
    DeadlineExceeded,
    Unconfirmed,
    Blocked,
    BadRequest,
    Forbidden,
    InternalError,
    EmptyRequest,
    UnknownInstance,
    CapabilityMissing,
    AccessDenied,
}

impl ResultCode {
    pub const ALL: [Self; 18] = [
        Self::Ok,
        Self::NotInstalled,
        Self::StaleGeneration,
        Self::FingerprintMismatch,
        Self::InstanceMismatch,
        Self::Replay,
        Self::StaleTs,
        Self::Overlimit,
        Self::DeadlineExceeded,
        Self::Unconfirmed,
        Self::Blocked,
        Self::BadRequest,
        Self::Forbidden,
        Self::InternalError,
        Self::EmptyRequest,
        Self::UnknownInstance,
        Self::CapabilityMissing,
        Self::AccessDenied,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::NotInstalled => "not_installed",
            Self::StaleGeneration => "stale_generation",
            Self::FingerprintMismatch => "fingerprint_mismatch",
            Self::InstanceMismatch => "instance_mismatch",
            Self::Replay => "replay",
            Self::StaleTs => "stale_ts",
            Self::Overlimit => "overlimit",
            Self::DeadlineExceeded => "deadline_exceeded",
            Self::Unconfirmed => "unconfirmed",
            Self::Blocked => "blocked",
            Self::BadRequest => "bad_request",
            Self::Forbidden => "forbidden",
            Self::InternalError => "internal_error",
            Self::EmptyRequest => "empty_request",
            Self::UnknownInstance => "unknown_instance",
            Self::CapabilityMissing => "capability_missing",
            Self::AccessDenied => "access_denied",
        }
    }

    pub fn parse(code: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|candidate| candidate.as_str() == code)
    }
}

impl fmt::Display for ResultCode {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// A bounded, sanitized protocol rejection: one code and no secrets.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProtocolError {
    code: ResultCode,
    message: String,
}

impl ProtocolError {
    pub fn new(code: ResultCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    pub fn code(&self) -> ResultCode {
        self.code
    }

    pub fn message(&self) -> &str {
        &self.message
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for ProtocolError {}

/// A structured broker result: an arbitrary object is never completion, every
/// outcome carries an explicit result code.
#[derive(Debug, Clone, PartialEq)]
pub struct BrokerResult {
    ok: bool,
    error_code: ResultCode,
    result: Value,
}

impl BrokerResult {
    pub fn ok(result: Value) -> Self {
        Self {
            ok: true,
            error_code: ResultCode::Ok,
            result: if result.is_null() {
                Value::Object(Map::new())
            } else {
                result
            },
        }
    }

    pub fn err(code: ResultCode, detail: impl Into<String>) -> Self {
        if code == ResultCode::Ok {
            panic!("BrokerResult::err requires a non-ok result code");
        }
        let detail = detail.into();
        let result = if detail.is_empty() {
            Value::Object(Map::new())
        } else {
            let mut map = Map::new();
            map.insert("detail".to_string(), Value::String(detail));
            Value::Object(map)
        };
        Self {
            ok: false,
            error_code: code,
            result,
        }
    }

    pub fn ok_flag(&self) -> bool {
        self.ok
    }

    pub fn error_code(&self) -> ResultCode {
        self.error_code
    }

    pub fn result(&self) -> &Value {
        &self.result
    }

    pub fn to_value(&self) -> Value {
        let mut map = Map::new();
        map.insert("ok".to_string(), Value::Bool(self.ok));
        map.insert(
            "error_code".to_string(),
            Value::String(self.error_code.as_str().to_string()),
        );
        map.insert("result".to_string(), self.result.clone());
        Value::Object(map)
    }

    /// Decode a reply. A non-object, a missing/unknown result code or a
    /// non-object `result` is `internal_error`, never a fabricated success.
    pub fn from_value(value: &Value) -> Self {
        let Some(map) = value.as_object() else {
            return Self::err(ResultCode::InternalError, "response must be an object");
        };
        let Some(code) = map.get("error_code").and_then(Value::as_str) else {
            return Self::err(ResultCode::InternalError, "unknown result code None");
        };
        let Some(code) = ResultCode::parse(code) else {
            return Self::err(
                ResultCode::InternalError,
                format!("unknown result code {code:?}"),
            );
        };
        let result = match map.get("result") {
            None | Some(Value::Null) => Value::Object(Map::new()),
            Some(Value::Object(_)) => map.get("result").cloned().unwrap_or_default(),
            Some(_) => {
                return Self::err(
                    ResultCode::InternalError,
                    "response result must be an object",
                )
            }
        };
        Self {
            ok: map.get("ok") == Some(&Value::Bool(true)),
            error_code: code,
            result,
        }
    }
}
