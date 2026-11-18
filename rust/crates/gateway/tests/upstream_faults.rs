//! #103: the local 8000 gateway records UM upstream faults durably.
//!
//! A fault is opened by the first 5xx, transport failure or timeout, updated by
//! later failures of the same upstream, and closed only by contract-valid
//! success evidence. `/health` is a side-effect-free read of the record.

mod support;

use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::{json, Value};
use um_gateway::upstream_fault::{FaultClass, FaultObservation, FaultSource, UpstreamFaultLog};

use support::*;

fn fault_path(temp: &TempDir) -> PathBuf {
    temp.path().join("upstream-faults.json")
}

fn get(addr: std::net::SocketAddr, target: &str) -> ClientResponse {
    client_request(addr, &ClientRequest::get(target)).expect("request")
}

fn stored_faults(path: &PathBuf) -> Value {
    serde_json::from_str(&fs::read_to_string(path).expect("fault file")).expect("fault json")
}

fn no_healthy_upstream() -> StubBehaviour {
    StubBehaviour {
        models_status: 503,
        models_body: Some(json!({"detail": "no healthy upstream"})),
        ..StubBehaviour::default()
    }
}

#[test]
fn a_503_with_no_healthy_upstream_opens_one_fault_and_persists_it() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let path = fault_path(&temp);
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    stub.set(no_healthy_upstream());

    let ready = get(gateway.addr, "/ready");
    assert_eq!(ready.status, 503, "{:?}", ready.body);
    assert_eq!(ready.json()["authentication"], json!("unknown"));

    // The local service stays healthy while the upstream is explicitly abnormal.
    let health = get(gateway.addr, "/health");
    assert_eq!(health.status, 200);
    assert_eq!(health.json()["status"], json!("ok"));
    // Availability and authentication stay separate: a 503 never marks the
    // credential invalid, and this temporary profile has no saved token yet.
    assert_eq!(health.json()["authentication"]["state"], json!("needs_login"));
    assert_eq!(health.json()["upstream_reachability"]["state"], json!("ok"));
    let faults = stored_faults(&path);
    let active = &faults["active"];
    assert_eq!(active["classification"], json!("upstream_5xx"));
    assert_eq!(active["upstream_status"], json!(503));
    assert_eq!(active["summary"], json!("no healthy upstream"));
    assert_eq!(active["failures"], json!(1));
    assert_eq!(active["source"], json!("active_probe"));
    assert!(active["started_epoch"].is_number());
    assert_eq!(active["last_epoch"], active["started_epoch"]);
    assert!(faults["last_check"]["epoch"].is_number());
    assert_eq!(faults["last_check"]["result"], json!("upstream_5xx"));
    assert_eq!(faults["last_check"]["upstream_status"], json!(503));

    // The record is on disk, not only in memory.
    assert_eq!(faults["active"]["classification"], json!("upstream_5xx"));
    assert_eq!(faults["active"]["failures"], json!(1));
}

#[test]
fn continuous_503_timeout_and_network_failures_update_one_active_record() {
    let temp = TempDir::new(unique_temp_prefix());
    let path = fault_path(&temp);
    let log = UpstreamFaultLog::open(Some(path.clone()));

    log.record_failure(FaultObservation {
        class: FaultClass::Upstream5xx,
        upstream_status: Some(503),
        summary: Some("no healthy upstream".to_string()),
        source: FaultSource::ActiveProbe,
    });
    let first = log.snapshot();
    let started = first["active"]["started_epoch"].clone();
    let id = first["active"]["id"].clone();

    log.record_failure(FaultObservation {
        class: FaultClass::Timeout,
        upstream_status: None,
        summary: None,
        source: FaultSource::BackgroundProbe,
    });
    log.record_failure(FaultObservation {
        class: FaultClass::NetworkError,
        upstream_status: None,
        summary: None,
        source: FaultSource::BusinessRequest,
    });

    let faults = log.snapshot();
    let active = &faults["active"];
    assert_eq!(faults["history"], json!([]));
    assert_eq!(active["id"], id);
    assert_eq!(active["started_epoch"], started);
    assert_eq!(active["failures"], json!(3));
    assert_eq!(active["classification"], json!("network_error"));
    assert_eq!(active["source"], json!("business_request"));
    assert_eq!(active["summary"], json!("no healthy upstream"));
    assert!(active["last_epoch"].as_f64().unwrap() >= started.as_f64().unwrap());
}

#[test]
fn valid_catalog_ends_the_fault_with_start_end_and_recovery_source() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let path = fault_path(&temp);
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    stub.set(no_healthy_upstream());
    assert_eq!(get(gateway.addr, "/ready").status, 503);
    let opened = stored_faults(&path);
    let id = opened["active"]["id"].clone();

    // A local `/health` 200 is not upstream recovery evidence.
    assert_eq!(get(gateway.addr, "/health").status, 200);
    assert_eq!(stored_faults(&path)["active"]["id"], id);

    // HTTP 200 with an error shape is not recovery evidence either.
    stub.set(StubBehaviour {
        models_status: 200,
        models_body: Some(json!({"not": "a catalog"})),
        ..StubBehaviour::default()
    });
    assert_eq!(get(gateway.addr, "/ready").status, 503);
    assert_eq!(stored_faults(&path)["active"]["id"], id);

    stub.set(StubBehaviour {
        models_status: 200,
        models_body: Some(json!({"object": "list", "data": []})),
        ..StubBehaviour::default()
    });
    let ready = get(gateway.addr, "/ready");
    assert_eq!(ready.status, 200, "{:?}", ready.body);
    let faults = stored_faults(&path);
    assert_eq!(faults["active"], Value::Null);
    let recovered = &faults["history"][0];
    assert_eq!(recovered["id"], id);
    assert_eq!(recovered["classification"], json!("upstream_5xx"));
    assert_eq!(recovered["upstream_status"], json!(503));
    assert_eq!(recovered["summary"], json!("no healthy upstream"));
    assert_eq!(recovered["recovery_source"], json!("active_probe"));
    assert_eq!(recovered["recovery_status"], json!(200));
    assert!(recovered["recovered_epoch"].as_f64().unwrap() >= recovered["started_epoch"].as_f64().unwrap());

    // A later failure is a new record, never the closed one.
    stub.set(no_healthy_upstream());
    assert_eq!(get(gateway.addr, "/ready").status, 503);
    let faults = stored_faults(&path);
    assert_ne!(faults["active"]["id"], id);
    assert_eq!(faults["history"][0]["id"], id);
}

#[test]
fn fault_state_survives_a_gateway_restart_and_still_matches_new_probes() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let path = fault_path(&temp);
    let first = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    stub.set(no_healthy_upstream());
    assert_eq!(get(first.addr, "/ready").status, 503);
    let opened = stored_faults(&path);
    let id = opened["active"]["id"].clone();
    let started = opened["active"]["started_epoch"].clone();
    drop(first);

    let second = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    let restored = stored_faults(&path);
    assert_eq!(restored["active"]["id"], id);
    assert_eq!(restored["active"]["started_epoch"], started);
    assert_eq!(restored["active"]["failures"], json!(1));

    stub.set(StubBehaviour {
        models_status: 200,
        models_body: Some(json!({"object": "list", "data": []})),
        ..StubBehaviour::default()
    });
    assert_eq!(get(second.addr, "/ready").status, 200);
    let recovered = stored_faults(&path);
    assert_eq!(recovered["active"], Value::Null);
    assert_eq!(recovered["history"][0]["id"], id);
    assert_eq!(recovered["history"][0]["recovery_source"], json!("active_probe"));

    // The closed history is still readable after a restart, and a later failure
    // opens a new record instead of reopening the closed one.
    drop(second);
    let third = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    let restored_history = stored_faults(&path);
    assert_eq!(restored_history["active"], Value::Null);
    assert_eq!(restored_history["history"][0]["id"], id);
    stub.set(no_healthy_upstream());
    assert_eq!(get(third.addr, "/ready").status, 503);
    let reopened = stored_faults(&path);
    assert_ne!(reopened["active"]["id"], id);
    assert_eq!(reopened["history"][0]["id"], id);
}

#[test]
fn read_only_health_and_console_reads_do_not_add_probes_or_faults() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let path = fault_path(&temp);
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    stub.set(no_healthy_upstream());
    assert_eq!(get(gateway.addr, "/ready").status, 503);
    let before = fs::read(&path).expect("fault file");
    let seen_before = stub.seen().len();
    for _ in 0..3 {
        let health = get(gateway.addr, "/health");
        assert_eq!(health.status, 200);
    }
    let after = fs::read(&path).expect("fault file");
    assert_eq!(before, after, "GET /health must not write the fault record");
    assert_eq!(
        stub.seen().len(),
        seen_before,
        "GET /health must not probe the upstream"
    );
}

#[test]
fn an_incomplete_stream_is_not_recovery_evidence() {
    let temp = TempDir::new(unique_temp_prefix());
    let path = fault_path(&temp);
    let log = UpstreamFaultLog::open(Some(path));
    log.record_failure(FaultObservation {
        class: FaultClass::Upstream5xx,
        upstream_status: Some(503),
        summary: Some("no healthy upstream".to_string()),
        source: FaultSource::BusinessRequest,
    });
    log.record_failure(FaultObservation {
        class: FaultClass::StreamError,
        upstream_status: None,
        summary: Some("upstream closed before done".to_string()),
        source: FaultSource::BusinessRequest,
    });
    let faults = log.snapshot();
    assert_eq!(faults["active"]["failures"], json!(2));
    assert_eq!(faults["active"]["classification"], json!("stream_error"));
    assert_eq!(faults["history"], json!([]));
}

#[test]
fn a_503_or_transport_error_does_not_start_reauthentication_but_401_does() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Instant;
    use um_gateway::reauth::{
        ReauthProviderResult, ReauthenticationProvider, ReauthTrigger,
    };

    struct CountingProvider {
        calls: AtomicUsize,
    }
    impl ReauthenticationProvider for CountingProvider {
        fn reauthenticate(
            &self,
            _deadline: Instant,
        ) -> ReauthProviderResult {
            self.reauthenticate_for(_deadline, ReauthTrigger::BusinessRequest)
        }

        fn reauthenticate_for(
            &self,
            _deadline: Instant,
            _trigger: ReauthTrigger,
        ) -> ReauthProviderResult {
            self.calls.fetch_add(1, Ordering::SeqCst);
            ReauthProviderResult::Failed {
                reason: "scripted".to_string(),
            }
        }
    }

    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let provider = Arc::new(CountingProvider {
        calls: AtomicUsize::new(0),
    });
    let gateway = start_chat_gateway_with_reauth(
        &stub,
        &temp,
        false,
        Some(provider.clone()),
        |config| {
            config.retry_delay_base = Duration::ZERO;
            config.max_physical_attempts = 2;
        },
    );

    stub.push(ChatAttempt::HttpStatus {
        status: 503,
        body: "no healthy upstream".to_string(),
        retry_after: None,
    });
    stub.push(ChatAttempt::HttpStatus {
        status: 503,
        body: "no healthy upstream".to_string(),
        retry_after: None,
    });
    let request = ClientRequest::post(
        "/v1/chat/completions",
        r#"{"model":"m","messages":[{"role":"user","content":"hi"}]}"#,
    );
    let response = client_request(gateway.addr, &request).expect("chat");
    assert_eq!(response.status, 503);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 0);

    stub.push(ChatAttempt::HttpStatus {
        status: 401,
        body: "not authenticated".to_string(),
        retry_after: None,
    });
    let response = client_request(gateway.addr, &request).expect("chat 401");
    // The auth contract answers `reauthentication_required` (503), but the
    // provider ran; that is the observable difference from a plain 503.
    assert_eq!(response.status, 503);
    assert_eq!(provider.calls.load(Ordering::SeqCst), 1);
}

#[test]
fn a_business_success_after_a_fault_closes_it_once() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let path = fault_path(&temp);
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    stub.set(no_healthy_upstream());
    assert_eq!(get(gateway.addr, "/ready").status, 503);
    let id = stored_faults(&path)["active"]["id"].clone();

    // A successful business request is valid recovery evidence.
    stub.set(StubBehaviour {
        models_status: 200,
        models_body: Some(json!({"object": "list", "data": []})),
        ..StubBehaviour::default()
    });
    let response = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer(STUB_TOKEN),
    )
    .expect("models");
    assert_eq!(response.status, 200, "{:?}", response.body);

    let faults = stored_faults(&path);
    assert_eq!(faults["active"], Value::Null);
    assert_eq!(faults["history"][0]["id"], id);
    assert_eq!(
        faults["history"][0]["recovery_source"],
        json!("business_request")
    );
    assert_eq!(faults["history"][0]["recovery_detail"], json!("model_catalog"));
}

#[test]
fn an_empty_http_200_business_response_is_not_recovery_evidence() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let path = fault_path(&temp);
    let log = UpstreamFaultLog::open(Some(path.clone()));
    log.record_failure(FaultObservation {
        class: FaultClass::Upstream5xx,
        upstream_status: Some(503),
        summary: Some("no healthy upstream".to_string()),
        source: FaultSource::ActiveProbe,
    });
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    let id = stored_faults(&path)["active"]["id"].clone();
    let request = ClientRequest::post(
        "/v1/chat/completions",
        r#"{"model":"m","messages":[{"role":"user","content":"hi"}]}"#,
    );

    // HTTP 200 with an empty object is not a completed business response.
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({}),
    });
    let response = client_request(gateway.addr, &request).expect("empty chat");
    assert_eq!(response.status, 200);
    assert_eq!(stored_faults(&path)["active"]["id"], id);

    // HTTP 200 with an error shape is not recovery evidence either.
    stub.push(ChatAttempt::Json {
        status: 200,
        body: json!({"error": {"message": "upstream failed"}}),
    });
    let response = client_request(gateway.addr, &request).expect("error shape chat");
    assert_eq!(response.status, 502);
    let faults = stored_faults(&path);
    assert_eq!(faults["active"]["id"], id);
    assert_eq!(faults["history"], json!([]));
}

#[test]
fn a_background_probe_records_the_next_check_time() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let path = fault_path(&temp);
    let _gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
        config.auth_watch_interval = Some(Duration::from_millis(50));
    });
    stub.set(no_healthy_upstream());
    wait_until(Duration::from_secs(5), || {
        stored_faults(&path)["active"]["source"] == json!("background_probe")
    });
    let faults = stored_faults(&path);
    let last = faults["last_check"]["epoch"].as_f64().expect("last check");
    let next = faults["next_check_epoch"].as_f64().expect("next check");
    assert!(next > last);
    let id = faults["active"]["id"].clone();

    // The same background cadence publishes the recovery and closes the record.
    stub.set(StubBehaviour {
        models_status: 200,
        models_body: Some(json!({"object": "list", "data": []})),
        ..StubBehaviour::default()
    });
    wait_until(Duration::from_secs(5), || {
        stored_faults(&path)["active"] == Value::Null
    });
    let recovered = stored_faults(&path);
    assert_eq!(recovered["history"][0]["id"], id);
    assert_eq!(
        recovered["history"][0]["recovery_source"],
        json!("background_probe")
    );
}

#[test]
fn an_active_verify_ends_the_fault_with_credential_probe_evidence() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let path = fault_path(&temp);
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.upstream_fault_log = Some(path.clone());
    });
    stub.set(no_healthy_upstream());
    assert_eq!(get(gateway.addr, "/ready").status, 503);
    let id = stored_faults(&path)["active"]["id"].clone();

    stub.set(StubBehaviour {
        models_status: 200,
        models_body: Some(json!({"object": "list", "data": []})),
        ..StubBehaviour::default()
    });
    let verify = client_request(
        gateway.addr,
        &ClientRequest::get("/admin/auth/verify").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("active verify");
    assert_eq!(verify.status, 200, "{:?}", verify.body);
    assert_eq!(verify.json()["verify"], json!("valid"));
    let faults = stored_faults(&path);
    assert_eq!(faults["active"], Value::Null);
    assert_eq!(faults["history"][0]["id"], id);
    assert_eq!(faults["history"][0]["recovery_source"], json!("active_probe"));
    assert_eq!(
        faults["history"][0]["recovery_detail"],
        json!("credential_probe")
    );
}

#[test]
fn a_completed_stream_is_recovery_evidence_but_an_unfinished_stream_is_not() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = ChatStub::start();
    let path = fault_path(&temp);
    let log = UpstreamFaultLog::open(Some(path.clone()));
    log.record_failure(FaultObservation {
        class: FaultClass::Upstream5xx,
        upstream_status: Some(503),
        summary: Some("no healthy upstream".to_string()),
        source: FaultSource::ActiveProbe,
    });
    let gateway = start_chat_gateway(&stub, &temp, false, |config| {
        config.upstream_fault_log = Some(path.clone());
        config.first_byte_timeout = Duration::from_secs(2);
        config.socket_silence_timeout = Duration::from_secs(2);
        config.request_total = Duration::from_secs(5);
        config.max_fast_retries = 0;
        config.max_slow_retries = 0;
    });
    let id = stored_faults(&path)["active"]["id"].clone();
    let request = ClientRequest::post(
        "/v1/chat/completions",
        r#"{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true}"#,
    )
    .json_body(r#"{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true}"#);

    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}

",
            ),
            ChatChunk::after(
                Duration::from_millis(20),
                b"".to_vec(),
            ),
        ],
        hold: Duration::from_millis(50),
    });
    let response = client_request(gateway.addr, &request).expect("unfinished stream");
    assert_eq!(response.status, 502, "{:?}", response.body);
    assert_eq!(stored_faults(&path)["active"]["id"], id);
    assert_eq!(stored_faults(&path)["history"], json!([]));

    stub.push(ChatAttempt::Sse {
        chunks: vec![
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{\"content\":\"done\"},\"finish_reason\":null}]}

",
            ),
            ChatChunk::now(
                b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}

",
            ),
            ChatChunk::now(b"data: [DONE]

".to_vec()),
        ],
        hold: Duration::from_millis(50),
    });
    let response = client_request(gateway.addr, &request).expect("completed stream");
    assert_eq!(response.status, 200, "{:?}", response.body);
    let faults = stored_faults(&path);
    assert_eq!(faults["active"], Value::Null);
    assert_eq!(faults["history"][0]["id"], id);
    assert_eq!(faults["history"][0]["recovery_detail"], json!("chat_stream"));
}
