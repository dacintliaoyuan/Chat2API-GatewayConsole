//! AC2/AC6: one-shot `request_id` consumption before any side effect.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use serde_json::{json, Value};
use um_broker::error::{BrokerResult, ProtocolError, ResultCode};
use um_broker::management::{map_instance_refusal, ManagementDispatcher, ManagementHandler};
use um_broker::replay::ReplayRegistry;
use um_broker::request::ValidatedRequest;

fn code(error: &ProtocolError) -> ResultCode {
    error.code()
}

#[test]
fn a_request_id_is_consumed_once() {
    let registry = ReplayRegistry::new(20_000);
    registry.consume("r-1", 100, 100).expect("first use");
    let error = registry.consume("r-1", 100, 100).unwrap_err();
    assert_eq!(code(&error), ResultCode::Replay);
    assert!(error.message().contains("replayed request_id"));
}

#[test]
fn a_consumed_id_is_reclaimed_only_after_the_window() {
    let registry = ReplayRegistry::new(20_000);
    registry.consume("r-1", 100, 100).expect("first use");
    assert_eq!(
        code(&registry.consume("r-1", 100, 400).unwrap_err()),
        ResultCode::Replay,
        "now - ts == 300 is still retained"
    );
    registry
        .consume("r-1", 500, 801)
        .expect("now - ts == 701 expires the old id");
    assert_eq!(registry.size(), 1);
}

#[test]
fn a_full_table_of_valid_ids_refuses_new_requests() {
    let registry = ReplayRegistry::new(2);
    registry.consume("r-1", 100, 100).expect("first");
    registry.consume("r-2", 100, 100).expect("second");
    let error = registry.consume("r-3", 100, 100).unwrap_err();
    assert_eq!(code(&error), ResultCode::Overlimit);
    assert!(error.message().contains("replay table capacity"));
    // An EXPIRED slot is reclaimed before capacity is enforced.
    registry
        .consume("r-3", 100, 401)
        .expect("expired slots are reclaimed");
}

#[test]
fn an_invalid_request_id_is_a_bad_request() {
    let registry = ReplayRegistry::new(20_000);
    let error = registry.consume("", 100, 100).unwrap_err();
    assert_eq!(code(&error), ResultCode::BadRequest);
}

struct RecordingHandler {
    calls: Arc<AtomicUsize>,
}

impl ManagementHandler for RecordingHandler {
    fn handle(&self, _request: &ValidatedRequest) -> BrokerResult {
        self.calls.fetch_add(1, Ordering::SeqCst);
        BrokerResult::ok(json!({"handled": true}))
    }
}

fn body() -> Value {
    json!({
        "v": 2,
        "action": "status",
        "request_id": "r-1",
        "ts": 100,
        "fields": {},
    })
}

fn sign(body: &mut Value) {
    let keys = ["v", "action", "request_id", "ts", "fields"];
    let mut map = body.as_object().expect("object").clone();
    um_broker::hmac::sign_value("secret", &mut map, &keys);
    *body = Value::Object(map);
}

#[test]
fn a_replayed_frame_never_reaches_the_handler() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let mut body = body();
    sign(&mut body);
    let first = dispatcher.handle_frame(&body, "secret", 100);
    assert!(first.result().ok_flag(), "{}", first.to_value());
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    let second = dispatcher.handle_frame(&body, "secret", 100);
    assert_eq!(second.result().error_code(), ResultCode::Replay);
    assert_eq!(calls.load(Ordering::SeqCst), 1, "replay must not dispatch");
    assert_eq!(second.request_id(), Some("r-1"));
}

#[test]
fn a_forged_frame_never_reaches_the_handler() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let mut body = body();
    sign(&mut body);
    body["mac"] = json!("0".repeat(64));
    let reply = dispatcher.handle_frame(&body, "secret", 100);
    assert_eq!(reply.result().error_code(), ResultCode::Forbidden);
    assert_eq!(reply.request_id(), Some("r-1"));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[test]
fn a_stale_frame_never_reaches_the_handler() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let mut body = body();
    sign(&mut body);
    let reply = dispatcher.handle_frame(&body, "secret", 401);
    assert_eq!(reply.result().error_code(), ResultCode::StaleTs);
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[test]
fn an_unknown_verb_without_a_capability_is_capability_missing() {
    let dispatcher = ManagementDispatcher::without_capabilities();
    let mut body = body();
    body["action"] = json!("maintenance-begin");
    body["fields"] = json!({"component": "share_gateway", "lease_s": 30});
    sign(&mut body);
    let reply = dispatcher.handle_frame(&body, "secret", 100);
    assert_eq!(reply.result().error_code(), ResultCode::CapabilityMissing);
    assert!(!reply.result().ok_flag());
}

#[test]
fn an_old_instance_refusal_maps_to_capability_missing() {
    let reply = map_instance_refusal(&json!({
        "ok": false,
        "error_code": "bad_request",
        "result": {"detail": "unknown action"},
    }));
    assert_eq!(reply.error_code(), ResultCode::CapabilityMissing);
    assert!(!reply.ok_flag());
}

#[test]
fn other_instance_refusals_keep_their_code() {
    let blocked = map_instance_refusal(&json!({
        "ok": false,
        "error_code": "blocked",
        "result": {"detail": "instance is stopped"},
    }));
    assert_eq!(blocked.error_code(), ResultCode::Blocked);
    assert_eq!(blocked.result()["detail"], json!("instance is stopped"));

    let unknown = map_instance_refusal(&json!({
        "ok": false,
        "error_code": "not_a_code",
        "result": {"detail": "whatever"},
    }));
    assert_eq!(unknown.error_code(), ResultCode::Unconfirmed);

    let ok = map_instance_refusal(&json!({
        "ok": true,
        "error_code": "ok",
        "result": {},
    }));
    assert_eq!(ok.error_code(), ResultCode::Unconfirmed);
}
