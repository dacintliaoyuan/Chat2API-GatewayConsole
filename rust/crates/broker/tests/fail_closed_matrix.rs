//! AC7: the fail-closed matrix over the complete management cycle.
//!
//! Each refusal is driven through the real read -> decode -> validate -> replay
//! -> dispatch path, so a handler can never be reached by a forged MAC, stale
//! timestamp, replay, overlimit frame, missing capability or denied endpoint.

use std::collections::VecDeque;
use std::io;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_broker::codec::{encode_frame, MAX_MESSAGE};
use um_broker::error::{BrokerResult, ResultCode};
use um_broker::management::{classify_connect_error, ManagementDispatcher, ManagementHandler};
use um_broker::peer::{
    require_peer_credentials, verify_peer_evidence, PeerEvidence, PeerExpectation,
};
use um_broker::request::ValidatedRequest;
use um_broker::server::{serve_management_cycle, CycleOutcome, FrameIo};
use um_broker::socket_policy::{plan_connect_endpoint, EndpointFacts, SocketPolicyError};

const SECRET: &str = "test-broker-secret";

struct MemoryIo {
    incoming: VecDeque<Vec<u8>>,
    written: Vec<Vec<u8>>,
}

impl MemoryIo {
    fn with(frames: Vec<Vec<u8>>) -> Self {
        Self {
            incoming: frames.into(),
            written: Vec::new(),
        }
    }

    fn written_value(&self, index: usize) -> Value {
        serde_json::from_slice(&self.written[index]).expect("written frame is JSON")
    }
}

impl FrameIo for MemoryIo {
    fn read_frame(
        &mut self,
        _deadline: Instant,
    ) -> Result<Vec<u8>, um_broker::error::ProtocolError> {
        self.incoming.pop_front().ok_or_else(|| {
            um_broker::error::ProtocolError::new(
                ResultCode::DeadlineExceeded,
                "request deadline exceeded",
            )
        })
    }

    fn write_frame(&mut self, frame: &[u8]) -> io::Result<()> {
        self.written.push(frame.to_vec());
        Ok(())
    }
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

fn signed_body(action: &str, fields: Value, request_id: &str, ts: i64) -> Value {
    let mut body = json!({
        "v": 2,
        "action": action,
        "request_id": request_id,
        "ts": ts,
        "fields": fields,
    });
    let keys = ["v", "action", "request_id", "ts", "fields"];
    let mut map = body.as_object().unwrap().clone();
    um_broker::hmac::sign_value(SECRET, &mut map, &keys);
    body = Value::Object(map);
    body
}

fn run(
    dispatcher: &ManagementDispatcher,
    frames: Vec<Value>,
    now: i64,
) -> (MemoryIo, Vec<CycleOutcome>) {
    let mut io = MemoryIo::with(
        frames
            .iter()
            .map(|frame| encode_frame(frame).expect("frame"))
            .collect(),
    );
    let deadline = Instant::now() + Duration::from_secs(10);
    let outcome = serve_management_cycle(&mut io, dispatcher, SECRET, deadline, now);
    (io, vec![outcome])
}

#[test]
fn a_forged_mac_is_refused_before_the_handler() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let mut body = signed_body("status", json!({}), "r-1", 100);
    body["mac"] = json!("0".repeat(64));
    let (io, outcomes) = run(&dispatcher, vec![body], 100);
    assert_eq!(outcomes[0].code(), Some(ResultCode::Forbidden));
    assert_eq!(io.written_value(0)["error_code"], json!("forbidden"));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[test]
fn a_stale_timestamp_is_refused_before_the_handler() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let body = signed_body("status", json!({}), "r-1", 100);
    let (io, outcomes) = run(&dispatcher, vec![body], 401);
    assert_eq!(outcomes[0].code(), Some(ResultCode::StaleTs));
    assert_eq!(io.written_value(0)["error_code"], json!("stale_ts"));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[test]
fn a_replayed_frame_is_refused_before_the_second_side_effect() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let body = signed_body("status", json!({}), "r-1", 100);
    let mut io = MemoryIo::with(vec![
        encode_frame(&body).expect("frame"),
        encode_frame(&body).expect("frame"),
    ]);
    let deadline = Instant::now() + Duration::from_secs(10);
    let first = serve_management_cycle(&mut io, &dispatcher, SECRET, deadline, 100);
    let second = serve_management_cycle(&mut io, &dispatcher, SECRET, deadline, 100);
    assert_eq!(first.code(), Some(ResultCode::Ok));
    assert_eq!(second.code(), Some(ResultCode::Replay));
    assert_eq!(io.written_value(1)["error_code"], json!("replay"));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[test]
fn an_overlimit_frame_is_answered_explicitly_and_never_dispatched() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let mut io = MemoryIo::with(vec![{
        let mut frame = vec![b'x'; MAX_MESSAGE + 1];
        frame.push(b'\n');
        frame
    }]);
    let deadline = Instant::now() + Duration::from_secs(10);
    let outcome = serve_management_cycle(&mut io, &dispatcher, SECRET, deadline, 100);
    assert_eq!(outcome.code(), Some(ResultCode::Overlimit));
    assert_eq!(io.written_value(0)["error_code"], json!("overlimit"));
    assert_eq!(io.written_value(0)["request_id"], Value::Null);
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}

#[test]
fn a_missing_capability_is_refused_without_a_handler() {
    let dispatcher = ManagementDispatcher::without_capabilities();
    let body = signed_body(
        "maintenance-begin",
        json!({"component": "share_gateway", "lease_s": 30}),
        "r-1",
        100,
    );
    let (io, outcomes) = run(&dispatcher, vec![body], 100);
    assert_eq!(outcomes[0].code(), Some(ResultCode::CapabilityMissing));
    assert_eq!(
        io.written_value(0)["error_code"],
        json!("capability_missing")
    );
}

#[test]
fn a_denied_endpoint_is_access_denied_not_not_installed() {
    assert_eq!(
        classify_connect_error(io::ErrorKind::PermissionDenied),
        ResultCode::AccessDenied
    );
    let missing = require_peer_credentials(None).unwrap_err();
    assert_eq!(missing.code(), ResultCode::AccessDenied);
    let wide = plan_connect_endpoint(&EndpointFacts {
        path: PathBuf::from("/run/um-share/broker.sock"),
        exists: true,
        is_socket: true,
        mode: 0o666,
    })
    .unwrap_err();
    assert_eq!(wide.result_code(), ResultCode::AccessDenied);
    assert_eq!(
        verify_peer_evidence(
            &PeerEvidence {
                pid: 1,
                uid: Some(65534),
                cmdline: vec!["/usr/bin/python".to_string(), "chat2api.py".to_string()],
                image: Some(PathBuf::from("/usr/bin/python")),
                creation_epoch: Some(1.0),
            },
            &PeerExpectation {
                expected_uid: Some(1000),
                ..PeerExpectation::default()
            },
        )
        .unwrap_err()
        .code(),
        ResultCode::AccessDenied
    );
    assert!(matches!(wide, SocketPolicyError::AccessDenied(_)));
}

#[test]
fn a_poisoned_cycle_keeps_serving() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let mut io = MemoryIo::with(vec![
        b"not-json\n".to_vec(),
        encode_frame(&signed_body("status", json!({}), "r-1", 100)).expect("frame"),
    ]);
    let deadline = Instant::now() + Duration::from_secs(10);
    let first = serve_management_cycle(&mut io, &dispatcher, SECRET, deadline, 100);
    assert_eq!(first.code(), Some(ResultCode::BadRequest));
    let second = serve_management_cycle(&mut io, &dispatcher, SECRET, deadline, 100);
    assert_eq!(second.code(), Some(ResultCode::Ok));
    assert_eq!(calls.load(Ordering::SeqCst), 1);
}

#[test]
fn a_deadline_without_a_frame_gets_no_reply() {
    let calls = Arc::new(AtomicUsize::new(0));
    let dispatcher = ManagementDispatcher::new(Box::new(RecordingHandler {
        calls: calls.clone(),
    }));
    let mut io = MemoryIo::with(vec![]);
    let deadline = Instant::now();
    let outcome = serve_management_cycle(&mut io, &dispatcher, SECRET, deadline, 100);
    assert_eq!(outcome.code(), Some(ResultCode::DeadlineExceeded));
    assert!(io.written.is_empty(), "no fabricated reply on a deadline");
    assert_eq!(calls.load(Ordering::SeqCst), 0);
}
