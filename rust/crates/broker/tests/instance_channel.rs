//! AC5: the instance channel answers only its five verbs.
//!
//! The instance endpoint mirrors `_ShareInstanceControl`: it reports its own
//! identity/in-flight state, ACKs a loaded generation, pauses/resumes admission
//! and clears in-flight work. It never runs a management action.

use std::sync::atomic::{AtomicUsize, Ordering};

use serde_json::{json, Value};
use um_broker::actions::{Action, Channel, InstanceAction, ManagementAction};
use um_broker::error::ResultCode;
use um_broker::instance::{InstanceServer, InstanceStateKind};
use um_broker::management::map_instance_refusal;

const SECRET: &str = "instance-secret";

fn signed(action: &str, fields: Value, request_id: &str) -> Value {
    let mut body = json!({
        "v": 2,
        "action": action,
        "request_id": request_id,
        "ts": 100,
        "instance_id": "i-1",
        "fields": fields,
    });
    let keys = ["v", "action", "request_id", "ts", "instance_id", "fields"];
    let mut map = body.as_object().unwrap().clone();
    um_broker::hmac::sign_value(SECRET, &mut map, &keys);
    body = Value::Object(map);
    body
}

fn server() -> InstanceServer {
    let server = InstanceServer::new("i-1");
    server.set_active("k-1", 2);
    server.set_active("k-2", 1);
    server
}

static NEXT_REQUEST: AtomicUsize = AtomicUsize::new(0);

fn reply(server: &InstanceServer, action: &str, fields: Value) -> Value {
    let request_id = format!("r-{}", NEXT_REQUEST.fetch_add(1, Ordering::SeqCst));
    server.handle_frame(&signed(action, fields, &request_id), SECRET, 100)
}

#[test]
fn instance_status_answers_identity_and_in_flight() {
    let server = server();
    let reply = reply(&server, "instance-status", json!({}));
    assert_eq!(reply["ok"], json!(true));
    assert_eq!(reply["error_code"], json!("ok"));
    assert_eq!(reply["result"]["instance_id"], json!("i-1"));
    assert_eq!(reply["result"]["state"], json!("running"));
    assert_eq!(reply["result"]["active_count"], json!(3));
    assert_eq!(reply["result"]["last_seen_generation"], json!(0));
}

#[test]
fn load_acks_the_generation_and_terminates_one_shot_keys() {
    let server = server();
    let reply = reply(
        &server,
        "load",
        json!({
            "generation": 7,
            "fingerprint": "f".repeat(64),
            "terminate_keys": ["k-1", "missing"],
        }),
    );
    assert_eq!(reply["ok"], json!(true), "{reply}");
    assert_eq!(reply["result"]["instance_id"], json!("i-1"));
    assert_eq!(reply["result"]["generation"], json!(7));
    assert_eq!(reply["result"]["fingerprint"], json!("f".repeat(64)));
    assert_eq!(reply["result"]["installed"], json!(true));
    assert_eq!(reply["result"]["released"], json!(2));
    assert_eq!(reply["result"]["state"], json!("running"));
    assert_eq!(server.active_count(), 1);
    assert_eq!(server.last_seen_generation(), 7);
}

#[test]
fn drain_pauses_acceptance_and_resume_restores_it() {
    let server = server();
    let drained = reply(&server, "drain", json!({}));
    assert_eq!(drained["ok"], json!(true));
    assert_eq!(drained["result"]["state"], json!("draining"));
    assert_eq!(drained["result"]["active_count"], json!(3));
    assert_eq!(server.state(), InstanceStateKind::Draining);

    let resumed = reply(&server, "resume", json!({}));
    assert_eq!(resumed["ok"], json!(true));
    assert_eq!(resumed["result"]["state"], json!("running"));
    assert_eq!(server.state(), InstanceStateKind::Running);
}

#[test]
fn emergency_clears_in_flight_and_stops() {
    let server = server();
    let reply = reply(&server, "emergency", json!({}));
    assert_eq!(reply["ok"], json!(true));
    assert_eq!(reply["result"]["state"], json!("stopped"));
    assert_eq!(reply["result"]["cleared_in_flight"], json!(3));
    assert_eq!(server.active_count(), 0);
    assert_eq!(server.state(), InstanceStateKind::Stopped);
}

#[test]
fn a_stopped_instance_refuses_drain_and_resume() {
    let server = server();
    reply(&server, "emergency", json!({}));
    for action in ["drain", "resume"] {
        let reply = reply(&server, action, json!({}));
        assert_eq!(reply["ok"], json!(false), "{action}");
        assert_eq!(reply["error_code"], json!("blocked"), "{action}");
        assert_eq!(reply["result"]["state"], json!("stopped"), "{action}");
    }
    assert_eq!(server.state(), InstanceStateKind::Stopped);
}

#[test]
fn only_five_instance_verbs_are_answered() {
    assert_eq!(InstanceAction::ALL.len(), 5);
    for action in InstanceAction::ALL {
        let server = server();
        let fields = match action {
            InstanceAction::Load => json!({
                "generation": 1,
                "fingerprint": "f".repeat(64),
            }),
            _ => json!({}),
        };
        let reply = reply(&server, action.as_str(), fields);
        assert_eq!(reply["ok"], json!(true), "{}: {reply}", action.as_str());
    }
}

#[test]
fn management_verbs_are_refused_on_the_instance_channel() {
    let server = server();
    for action in ManagementAction::ALL {
        // `drain` is the one shared name: on this channel it is the
        // instance verb, never the management one.
        if action == ManagementAction::Drain {
            continue;
        }
        let fields = match action {
            ManagementAction::Publish => json!({
                "generation": 1,
                "fingerprint": "f".repeat(64),
                "projection": {},
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
            _ => json!({}),
        };
        let reply = reply(&server, action.as_str(), fields);
        assert_eq!(
            reply["error_code"],
            json!("bad_request"),
            "{} must be refused: {reply}",
            action.as_str()
        );
        assert_eq!(
            reply["result"]["detail"],
            json!("unknown action"),
            "{}",
            action.as_str()
        );
        assert_eq!(
            map_instance_refusal(&reply).error_code(),
            ResultCode::CapabilityMissing,
            "{}",
            action.as_str()
        );
    }
    assert_eq!(server.state(), InstanceStateKind::Running);
}

#[test]
fn a_frame_for_another_instance_is_refused() {
    let server = server();
    let mut body = signed("instance-status", json!({}), "r-1");
    body["instance_id"] = json!("i-2");
    let mut map = body.as_object().unwrap().clone();
    um_broker::hmac::sign_value(
        SECRET,
        &mut map,
        &["v", "action", "request_id", "ts", "instance_id", "fields"],
    );
    body = Value::Object(map);
    let reply = server.handle_frame(&body, SECRET, 100);
    assert_eq!(reply["error_code"], json!("instance_mismatch"));
    assert_eq!(reply["ok"], json!(false));
}

#[test]
fn a_replayed_instance_frame_changes_nothing() {
    let server = server();
    let body = signed("emergency", json!({}), "r-1");
    let first = server.handle_frame(&body, SECRET, 100);
    assert_eq!(first["ok"], json!(true));
    assert_eq!(server.state(), InstanceStateKind::Stopped);

    let second = server.handle_frame(&body, SECRET, 100);
    assert_eq!(second["error_code"], json!("replay"));
    assert_eq!(second["ok"], json!(false));
}

#[test]
fn a_forged_instance_frame_changes_nothing() {
    let server = server();
    let mut body = signed("emergency", json!({}), "r-1");
    body["mac"] = json!("0".repeat(64));
    let reply = server.handle_frame(&body, SECRET, 100);
    assert_eq!(reply["error_code"], json!("forbidden"));
    assert_eq!(server.state(), InstanceStateKind::Running);
    assert_eq!(server.active_count(), 3);
}

#[test]
fn the_instance_actions_are_typed_on_the_instance_channel_only() {
    for action in InstanceAction::ALL {
        assert_eq!(
            Action::from_wire(Channel::Instance, action.as_str()),
            Some(Action::Instance(action))
        );
        if action != InstanceAction::Drain {
            assert_eq!(
                Action::from_wire(Channel::Management, action.as_str()),
                None,
                "{}",
                action.as_str()
            );
        }
    }
    assert!(Action::from_wire(Channel::Instance, "status").is_none());
    assert!(Action::from_wire(Channel::Instance, "publish").is_none());
}

#[test]
fn an_unknown_instance_verb_is_a_bad_request() {
    let server = server();
    let reply = reply(&server, "shell", json!({}));
    assert_eq!(reply["error_code"], json!("bad_request"));
    assert_eq!(reply["result"]["detail"], json!("unknown action"));
}

#[test]
fn a_non_object_instance_frame_is_a_bad_request() {
    let server = server();
    let reply = server.handle_frame(&json!([1, 2]), SECRET, 100);
    assert_eq!(reply["error_code"], json!("bad_request"));
    assert_eq!(reply["request_id"], Value::Null);
}

#[test]
fn the_instance_action_table_matches_the_fixture() {
    let fixture = um_contract::ContractFixture::load_default().expect("contract fixture");
    let mut fixture_instance = fixture.broker.instance_actions.clone();
    fixture_instance.sort();
    let mut instance: Vec<&str> = InstanceAction::ALL
        .iter()
        .map(|action| action.as_str())
        .collect();
    instance.sort_unstable();
    assert_eq!(instance, fixture_instance);
}
