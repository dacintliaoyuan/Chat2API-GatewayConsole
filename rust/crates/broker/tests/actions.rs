//! AC3: the fixed action whitelist and closed field vocabularies.

use std::path::Path;

use serde_json::{json, Map, Value};
use um_broker::actions::{
    validate_gateway_restart_fields, validate_host_evidence_fields,
    validate_host_repair_limits_fields, validate_host_stop_fields, validate_host_verify_fields,
    validate_maintenance_begin_fields, validate_maintenance_end_fields,
    validate_maintenance_status_fields, validate_ts_service_control_fields, Action, Channel,
    InstanceAction, ManagementAction, GATEWAY_RESTART_BUDGET_MAX_S, MAINTENANCE_LEASE_MAX_S,
};
use um_broker::error::ResultCode;
use um_broker::request::{validate_management_request, ValidatedRequest};

fn code(error: &um_broker::error::ProtocolError) -> ResultCode {
    error.code()
}

fn minimal_fields(action: ManagementAction) -> Value {
    match action {
        ManagementAction::Status
        | ManagementAction::Start
        | ManagementAction::Drain
        | ManagementAction::EmergencyStop
        | ManagementAction::HostStop
        | ManagementAction::HostRepairLimits
        | ManagementAction::MaintenanceStatus => json!({}),
        ManagementAction::Publish => json!({
            "generation": 1,
            "fingerprint": "a".repeat(64),
            "projection": {"schema_version": 1, "config_generation": 1, "data": []},
        }),
        ManagementAction::TsServiceControl => json!({"action": "start"}),
        ManagementAction::HostVerify => json!({"check": "running"}),
        ManagementAction::MaintenanceBegin => {
            json!({"component": "share_gateway", "lease_s": 30})
        }
        ManagementAction::MaintenanceEnd => {
            json!({"component": "share_gateway", "lease_id": "l-1"})
        }
        ManagementAction::GatewayRestart => {
            json!({"component": "share_gateway", "budget_s": 120})
        }
        ManagementAction::HostEvidence => json!({"check": "limits"}),
    }
}

fn signed_body(action: &str, fields: Value) -> Value {
    let mut body = json!({
        "v": 2,
        "action": action,
        "request_id": "r-1",
        "ts": 1,
        "fields": fields,
    });
    let keys = ["v", "action", "request_id", "ts", "fields"];
    let mut map = body.as_object().expect("object").clone();
    um_broker::hmac::sign_value("secret", &mut map, &keys);
    body = Value::Object(map);
    body
}

fn validate(
    action: &str,
    fields: Value,
) -> Result<ValidatedRequest, um_broker::error::ProtocolError> {
    validate_management_request(&signed_body(action, fields), "secret", 1)
}

#[test]
fn every_management_action_is_typed_and_accepted() {
    assert_eq!(ManagementAction::ALL.len(), 14);
    for action in ManagementAction::ALL {
        let request = validate(action.as_str(), minimal_fields(action))
            .unwrap_or_else(|error| panic!("{}: {error}", action.as_str()));
        assert_eq!(request.action, Action::Management(action));
        assert_eq!(
            Action::from_wire(Channel::Management, action.as_str()),
            Some(Action::Management(action))
        );
    }
}

#[test]
fn every_instance_action_is_typed_and_accepted() {
    assert_eq!(InstanceAction::ALL.len(), 5);
    for action in InstanceAction::ALL {
        assert_eq!(
            Action::from_wire(Channel::Instance, action.as_str()),
            Some(Action::Instance(action))
        );
    }
    assert_eq!(
        Action::from_wire(Channel::Instance, "instance-status"),
        Some(Action::Instance(InstanceAction::InstanceStatus))
    );
    assert_eq!(
        Action::from_wire(Channel::Management, "instance-status"),
        None,
        "the channel, not the name alone, decides the verb set"
    );
}

#[test]
fn the_action_tables_match_the_contract_fixture() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    let mut fixture_management = fixture.broker.management_actions.clone();
    fixture_management.sort();
    let mut management: Vec<&str> = ManagementAction::ALL
        .iter()
        .map(|action| action.as_str())
        .collect();
    management.sort_unstable();
    assert_eq!(management, fixture_management);

    let mut fixture_instance = fixture.broker.instance_actions.clone();
    fixture_instance.sort();
    let mut instance: Vec<&str> = InstanceAction::ALL
        .iter()
        .map(|action| action.as_str())
        .collect();
    instance.sort_unstable();
    assert_eq!(instance, fixture_instance);
}

#[test]
fn arbitrary_actions_are_refused() {
    for action in [
        "shell", "exec", "run", "command", "service", "path", "shutdown", "restart", "unknown",
    ] {
        assert!(
            Action::from_wire(Channel::Management, action).is_none(),
            "action={action}"
        );
        assert!(
            Action::from_wire(Channel::Instance, action).is_none(),
            "action={action}"
        );
        let error = validate(action, json!({})).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest, "action={action}");
        assert!(error.message().contains("unknown action"));
    }
}

#[test]
fn no_action_accepts_a_path_command_service_or_shell_field() {
    for field in [
        "path", "command", "service", "shell", "argv", "pid", "port", "url", "env",
    ] {
        for action in ManagementAction::ALL {
            let mut fields = minimal_fields(action).as_object().expect("object").clone();
            fields.insert(field.to_string(), json!("/bin/sh"));
            let error = validate(action.as_str(), Value::Object(fields)).unwrap_err();
            assert_eq!(
                code(&error),
                ResultCode::BadRequest,
                "action={} field={field}",
                action.as_str()
            );
            assert!(error.message().contains("unknown field"));
        }
    }
}

#[test]
fn no_action_accepts_an_unknown_extra_field() {
    for action in ManagementAction::ALL {
        let mut fields = minimal_fields(action).as_object().unwrap().clone();
        fields.insert("extra".to_string(), json!(1));
        let error = validate(action.as_str(), Value::Object(fields)).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest);
    }
}

#[test]
fn ts_service_control_only_accepts_the_start_stop_enum() {
    for action in ["start", "stop"] {
        let fields = json!({"action": action});
        let normalized =
            validate_ts_service_control_fields(fields.as_object().unwrap()).expect("valid verb");
        assert_eq!(normalized["action"], json!(action));
    }
    for fields in [
        json!({"action": "restart"}),
        json!({"action": "start", "service": "Tailscale"}),
        json!({"service": "Tailscale"}),
        json!({"action": 1}),
    ] {
        let error = validate_ts_service_control_fields(fields.as_object().unwrap()).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest);
    }
}

#[test]
fn host_verify_only_accepts_the_running_closed_enum() {
    for check in ["running", "closed"] {
        let fields = json!({"check": check});
        assert_eq!(
            validate_host_verify_fields(fields.as_object().unwrap()).unwrap()["check"],
            json!(check)
        );
    }
    for fields in [
        json!({"check": "open"}),
        json!({"check": "running", "path": "/usr/bin/true"}),
        json!({"check": true}),
    ] {
        let error = validate_host_verify_fields(fields.as_object().unwrap()).unwrap_err();
        assert_eq!(code(&error), ResultCode::BadRequest);
    }
}

#[test]
fn host_stop_and_repair_limits_have_no_fields() {
    assert!(validate_host_stop_fields(&Map::new()).unwrap().is_empty());
    assert!(validate_host_repair_limits_fields(&Map::new())
        .unwrap()
        .is_empty());
    for fields in [json!({"path": "/bin/sh"}), json!({"command": "kill"})] {
        assert_eq!(
            code(&validate_host_stop_fields(fields.as_object().unwrap()).unwrap_err()),
            ResultCode::BadRequest
        );
        assert_eq!(
            code(&validate_host_repair_limits_fields(fields.as_object().unwrap()).unwrap_err()),
            ResultCode::BadRequest
        );
    }
}

#[test]
fn maintenance_begin_is_component_and_bounded_lease() {
    let valid = json!({"component": "share_gateway", "lease_s": 1});
    assert_eq!(
        validate_maintenance_begin_fields(valid.as_object().unwrap()).unwrap()["lease_s"],
        json!(1)
    );
    let valid = json!({"component": "share_gateway", "lease_s": MAINTENANCE_LEASE_MAX_S});
    assert!(validate_maintenance_begin_fields(valid.as_object().unwrap()).is_ok());
    for fields in [
        json!({"component": "share_gateway", "lease_s": 0}),
        json!({"component": "share_gateway", "lease_s": MAINTENANCE_LEASE_MAX_S + 1}),
        json!({"component": "share_gateway", "lease_s": true}),
        json!({"component": "evil", "lease_s": 30}),
        json!({"component": "share_gateway", "lease_s": 30, "path": "/bin/sh"}),
    ] {
        assert_eq!(
            code(&validate_maintenance_begin_fields(fields.as_object().unwrap()).unwrap_err()),
            ResultCode::BadRequest,
            "fields={fields}"
        );
    }
}

#[test]
fn maintenance_end_is_component_and_bounded_lease_id() {
    let valid = json!({"component": "share_gateway", "lease_id": "l".repeat(64)});
    assert!(validate_maintenance_end_fields(valid.as_object().unwrap()).is_ok());
    for fields in [
        json!({"component": "share_gateway", "lease_id": ""}),
        json!({"component": "share_gateway", "lease_id": "l".repeat(65)}),
        json!({"component": "share_gateway", "lease_id": 1}),
        json!({"component": "evil", "lease_id": "l-1"}),
        json!({"component": "share_gateway", "lease_id": "l-1", "command": "x"}),
    ] {
        assert_eq!(
            code(&validate_maintenance_end_fields(fields.as_object().unwrap()).unwrap_err()),
            ResultCode::BadRequest,
            "fields={fields}"
        );
    }
}

#[test]
fn maintenance_status_has_no_fields() {
    assert!(validate_maintenance_status_fields(&Map::new())
        .unwrap()
        .is_empty());
    assert_eq!(
        code(
            &validate_maintenance_status_fields(json!({"lease_id": "l-1"}).as_object().unwrap())
                .unwrap_err()
        ),
        ResultCode::BadRequest
    );
}

#[test]
fn gateway_restart_is_component_and_bounded_budget() {
    let valid = json!({"component": "share_gateway", "budget_s": GATEWAY_RESTART_BUDGET_MAX_S});
    assert!(validate_gateway_restart_fields(valid.as_object().unwrap()).is_ok());
    for fields in [
        json!({"component": "share_gateway", "budget_s": 0}),
        json!({"component": "share_gateway", "budget_s": GATEWAY_RESTART_BUDGET_MAX_S + 1}),
        json!({"component": "share_gateway", "budget_s": true}),
        json!({"component": "evil", "budget_s": 120}),
        json!({"component": "share_gateway", "budget_s": 120, "service": "x"}),
    ] {
        assert_eq!(
            code(&validate_gateway_restart_fields(fields.as_object().unwrap()).unwrap_err()),
            ResultCode::BadRequest,
            "fields={fields}"
        );
    }
}

#[test]
fn host_evidence_only_accepts_limits_faults_watchdog() {
    for check in ["limits", "faults", "watchdog"] {
        let fields = json!({"check": check});
        assert_eq!(
            validate_host_evidence_fields(fields.as_object().unwrap()).unwrap()["check"],
            json!(check)
        );
    }
    for fields in [
        json!({"check": "shell"}),
        json!({"check": "limits", "path": "/proc/net/tcp"}),
        json!({"check": 1}),
    ] {
        assert_eq!(
            code(&validate_host_evidence_fields(fields.as_object().unwrap()).unwrap_err()),
            ResultCode::BadRequest,
            "fields={fields}"
        );
    }
}

#[test]
fn the_broker_sources_never_spawn_a_process_or_shell() {
    let sources = [
        "src/lib.rs",
        "src/error.rs",
        "src/codec.rs",
        "src/hmac.rs",
        "src/request.rs",
        "src/actions.rs",
        "src/replay.rs",
        "src/management.rs",
        "src/peer.rs",
        "src/socket_policy.rs",
        "src/transport/mod.rs",
        "src/transport/unix.rs",
    ];
    let root = Path::new(env!("CARGO_MANIFEST_DIR"));
    for source in sources {
        let text = std::fs::read_to_string(root.join(source)).expect(source);
        let needles = [
            ["process", "::", "Command"].concat(),
            ["Command", "::", "new"].concat(),
            ["std", "::", "process"].concat(),
            ["sh ", "-c"].concat(),
        ];
        for needle in needles {
            assert!(
                !text.contains(&needle),
                "{source} must not contain {needle:?}"
            );
        }
    }
}
