//! #111: production `ShareWriteSeams` over the real broker management channel.
//!
//! The broker side is the REAL `ManagementDispatcher` + `serve_management_cycle`
//! from `um-broker` over a temp AF_UNIX socket, so the seam's HMAC signing,
//! one-shot request id, ACK verification and failure classification run over the
//! same wire path the deployed broker serves. Linux-only by design.

#![cfg(unix)]

use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use um_broker::actions::{Action, ManagementAction};
use um_broker::error::{BrokerResult, ResultCode};
use um_broker::management::{ManagementDispatcher, ManagementHandler};
use um_broker::request::ValidatedRequest;
use um_broker::server::serve_management_cycle;
use um_broker::transport::unix::UnixListener;
use um_console::broker_write::{production_write_seams, BrokerManagementSeams};
use um_console::share_write::{ShareWriteErrorKind, ShareWriteSeams, ShareWriteStore};
use um_test_harness::{HttpResponse, TempDataDir, TestServer};

mod support;

use support::write::{create_over_http, http_request, key_file, open_store};

const BROKER_SECRET: &str = "fixture-broker-control-secret";

#[derive(Clone, Copy)]
enum PublishMode {
    Ack,
    WrongGeneration,
    WrongFingerprint,
    Error(ResultCode),
}

struct ScriptedBroker {
    mode: Mutex<PublishMode>,
    publishes: Mutex<Vec<Map<String, Value>>>,
    emergencies: AtomicUsize,
    host_verify_calls: AtomicUsize,
    host_running: AtomicBool,
}

impl ScriptedBroker {
    fn new(mode: PublishMode) -> Self {
        Self {
            mode: Mutex::new(mode),
            publishes: Mutex::new(Vec::new()),
            emergencies: AtomicUsize::new(0),
            host_verify_calls: AtomicUsize::new(0),
            host_running: AtomicBool::new(true),
        }
    }

    fn set_mode(&self, mode: PublishMode) {
        *self.mode.lock().unwrap() = mode;
    }

    fn publishes(&self) -> Vec<Map<String, Value>> {
        self.publishes.lock().unwrap().clone()
    }

    fn emergencies(&self) -> usize {
        self.emergencies.load(Ordering::SeqCst)
    }
}

impl ManagementHandler for ScriptedBroker {
    fn handle(&self, request: &ValidatedRequest) -> BrokerResult {
        match request.action {
            Action::Management(ManagementAction::Publish) => {
                self.publishes.lock().unwrap().push(request.fields.clone());
                let generation = request
                    .fields
                    .get("generation")
                    .and_then(Value::as_i64)
                    .unwrap_or_default();
                let fingerprint = request
                    .fields
                    .get("fingerprint")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                match *self.mode.lock().unwrap() {
                    PublishMode::Ack => {
                        let mut ack = json!({
                            "generation": generation,
                            "fingerprint": fingerprint,
                            "instance_id": "fixture-instance",
                            "security_generation": generation,
                        });
                        if request.fields.contains_key("terminate_keys") {
                            ack["terminated"] = json!(1);
                            ack["released"] = json!(0);
                            ack["aborted"] = json!(0);
                            ack["upstream_closed"] = json!(1);
                        }
                        BrokerResult::ok(ack)
                    }
                    PublishMode::WrongGeneration => BrokerResult::ok(json!({
                        "generation": generation + 1,
                        "fingerprint": fingerprint,
                        "instance_id": "fixture-instance",
                    })),
                    PublishMode::WrongFingerprint => BrokerResult::ok(json!({
                        "generation": generation,
                        "fingerprint": "0".repeat(64),
                        "instance_id": "fixture-instance",
                    })),
                    PublishMode::Error(code) => BrokerResult::err(code, "scripted broker refusal"),
                }
            }
            Action::Management(ManagementAction::HostVerify) => {
                self.host_verify_calls.fetch_add(1, Ordering::SeqCst);
                BrokerResult::ok(json!({
                    "host_running": self.host_running.load(Ordering::SeqCst),
                }))
            }
            Action::Management(ManagementAction::EmergencyStop) => {
                self.emergencies.fetch_add(1, Ordering::SeqCst);
                BrokerResult::ok(json!({
                    "stopped": true,
                    "notification_failed": false,
                    "host_closed": true,
                    "instance_stopped": true,
                }))
            }
            _ => BrokerResult::err(
                ResultCode::CapabilityMissing,
                "fixture does not implement the verb",
            ),
        }
    }
}

struct SharedHandler(Arc<ScriptedBroker>);

impl ManagementHandler for SharedHandler {
    fn handle(&self, request: &ValidatedRequest) -> BrokerResult {
        self.0.handle(request)
    }
}

struct BrokerServer {
    shutdown: Arc<AtomicBool>,
    handle: Option<std::thread::JoinHandle<()>>,
}

impl BrokerServer {
    fn start(socket: &Path, handler: Arc<ScriptedBroker>) -> Self {
        let mut listener = UnixListener::bind(socket).expect("bind broker socket");
        listener
            .set_nonblocking(true)
            .expect("nonblocking broker listener");
        let shutdown = Arc::new(AtomicBool::new(false));
        let worker_shutdown = Arc::clone(&shutdown);
        let dispatcher = ManagementDispatcher::new(Box::new(SharedHandler(handler)));
        let handle = std::thread::spawn(move || {
            while !worker_shutdown.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok(mut connection) => {
                        let deadline = Instant::now() + Duration::from_secs(10);
                        let _ = serve_management_cycle(
                            &mut connection,
                            &dispatcher,
                            BROKER_SECRET,
                            deadline,
                            epoch_seconds(),
                        );
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
            let _ = listener.close();
        });
        Self {
            shutdown,
            handle: Some(handle),
        }
    }
}

impl Drop for BrokerServer {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

fn epoch_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock after the epoch")
        .as_secs() as i64
}

fn write_control_key(path: &Path) {
    std::fs::write(path, BROKER_SECRET.as_bytes()).expect("write broker control key");
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
        .expect("control key mode");
}

fn seams(socket: &Path) -> Arc<dyn ShareWriteSeams> {
    Arc::new(BrokerManagementSeams::new(
        socket.to_path_buf(),
        BROKER_SECRET.to_string(),
        8003,
    ))
}

fn writer(
    temp: &TempDataDir,
    seams: Arc<dyn ShareWriteSeams>,
) -> (ShareWriteStore, PathBuf, PathBuf) {
    let store_key = key_file(temp);
    let store_path = temp.path().join("share.db");
    (
        ShareWriteStore::new(store_path.clone(), Some(store_key.clone()), seams),
        store_path,
        store_key,
    )
}

#[test]
fn gateway_running_reads_the_broker_host_verify_evidence() {
    let temp = TempDataDir::new("um-console-broker-host-verify-").expect("temp dir");
    let socket = temp.path().join("broker.sock");
    let broker = Arc::new(ScriptedBroker::new(PublishMode::Ack));
    let _server = BrokerServer::start(&socket, Arc::clone(&broker));
    let seams = seams(&socket);

    assert_eq!(seams.gateway_running(), Some(true));
    broker.host_running.store(false, Ordering::SeqCst);
    assert_eq!(seams.gateway_running(), Some(false));
    assert_eq!(broker.host_verify_calls.load(Ordering::SeqCst), 2);
}

#[test]
fn the_private_probe_reads_models_and_returns_a_bounded_outcome() {
    let temp = TempDataDir::new("um-console-probe-private-").expect("temp dir");
    let socket = temp.path().join("broker.sock");
    let broker = Arc::new(ScriptedBroker::new(PublishMode::Ack));
    let _server = BrokerServer::start(&socket, Arc::clone(&broker));
    let upstream = TestServer::start(|request| match request.path.as_str() {
        "/v1/models" => HttpResponse::json(200, r#"{"data":[{"id":"fixture-model"}]}"#),
        _ => HttpResponse::text(404, "not found"),
    })
    .expect("upstream");
    let seams = BrokerManagementSeams::new(
        socket,
        BROKER_SECRET.to_string(),
        upstream.port(),
    );

    let outcome = seams
        .probe_models_private("fixture-secret")
        .expect("probe outcome");
    assert_eq!(outcome["connected"], json!(true), "{outcome}");
    assert_eq!(outcome["stage"], json!("models"));
    assert_eq!(outcome["http_status"], json!(200));
    assert_eq!(outcome["model"], json!("fixture-model"));
    assert_eq!(outcome["reason"], json!("ok"));
}

#[test]
fn publish_success_returns_the_ack_and_require_applied_accepts_it() {
    let temp = TempDataDir::new("um-console-broker-write-").expect("temp dir");
    let socket = temp.path().join("broker.sock");
    let broker = Arc::new(ScriptedBroker::new(PublishMode::Ack));
    let _server = BrokerServer::start(&socket, Arc::clone(&broker));
    let (writer, store_path, store_key) = writer(&temp, seams(&socket));

    let created = writer
        .create_key("brokered", None, None)
        .expect("create key");
    assert_eq!(created["published"], json!(true));
    // A fresh store starts at config_generation 1; the create bumps it to 2.
    assert_eq!(created["applied_generation"], json!(2));
    let key_id = created["key"]["id"]
        .as_str()
        .expect("created key id")
        .to_string();

    let revoked = writer
        .revoke_and_terminate_key(&key_id, 1, None)
        .expect("revoke and terminate");
    assert_eq!(revoked["published"], json!(true));
    assert_eq!(revoked["applied_generation"], json!(3));
    assert_eq!(revoked["terminated"], json!(1));
    assert_eq!(revoked["released"], json!(0));

    let publishes = broker.publishes();
    assert_eq!(publishes.len(), 2);
    assert_eq!(publishes[0]["generation"], json!(2));
    assert_eq!(publishes[1]["generation"], json!(3));
    assert_eq!(publishes[1]["terminate_keys"], json!([key_id]));
    assert_eq!(publishes[1]["projection"]["config_generation"], json!(3));

    let mut store = open_store(&store_path, &store_key);
    let generation = store.generation().expect("generation");
    assert_eq!(generation["applied_generation"], json!(3));
}

#[test]
fn an_ack_mismatch_is_a_503_and_records_the_confirmed_emergency_stop() {
    for (mode, fragment) in [
        (PublishMode::WrongGeneration, "ack generation"),
        (PublishMode::WrongFingerprint, "ack fingerprint"),
    ] {
        let temp = TempDataDir::new("um-console-broker-write-").expect("temp dir");
        let socket = temp.path().join("broker.sock");
        let broker = Arc::new(ScriptedBroker::new(PublishMode::Ack));
        let _server = BrokerServer::start(&socket, Arc::clone(&broker));
        let (writer, store_path, _store_key) = writer(&temp, seams(&socket));

        let created = writer
            .create_key("blocked", None, None)
            .expect("create key");
        assert_eq!(created["published"], json!(true));
        let key_id = created["key"]["id"].as_str().expect("key id").to_string();

        broker.set_mode(mode);
        let error = writer
            .delete_key(&key_id, 1)
            .expect_err("the mismatched ACK must fail closed");
        assert_eq!(error.kind, ShareWriteErrorKind::Broker);
        assert_eq!(error.status(), 503);
        assert!(
            error
                .message
                .contains("revoking operation not applied online"),
            "{}",
            error.message
        );
        assert_eq!(broker.emergencies(), 1);

        let failure = writer.last_failure().expect("recorded publication failure");
        assert!(
            failure["error"].as_str().unwrap().contains(fragment),
            "{failure}"
        );

        let connection = rusqlite::Connection::open(&store_path).expect("open store");
        let payload: String = connection
            .query_row(
                "SELECT payload FROM ops WHERE kind='publish' ORDER BY rowid DESC LIMIT 1",
                [],
                |row| row.get(0),
            )
            .expect("publish intent row");
        let payload: Value = serde_json::from_str(&payload).expect("publish intent payload");
        // The scripted emergency stop confirms closure, so the intent is not
        // blocked; a confirmed stop is the honest fail-closed outcome.
        assert_eq!(payload["blocked"], json!(false));
        assert_eq!(payload["emergency_stop"]["confirmed"], json!(true));
    }
}

#[test]
fn a_broker_error_code_is_a_503_and_keeps_the_code_in_the_message() {
    for code in [ResultCode::Unconfirmed, ResultCode::StaleGeneration] {
        let temp = TempDataDir::new("um-console-broker-write-").expect("temp dir");
        let socket = temp.path().join("broker.sock");
        let broker = Arc::new(ScriptedBroker::new(PublishMode::Error(code)));
        let _server = BrokerServer::start(&socket, broker);
        let projection = json!({
            "schema_version": 2,
            "config_generation": 1,
            "share_max_concurrency": 64,
            "data": [],
        });
        let error = seams(&socket)
            .publish(&projection, None)
            .expect_err("the broker refusal must not become a success");
        assert_eq!(error.kind, ShareWriteErrorKind::Broker);
        assert_eq!(error.status(), 503);
        assert!(error.message.contains(code.as_str()), "{}", error.message);
    }
}

#[test]
fn missing_socket_or_key_keeps_the_unwired_seams_and_the_route_answers_503() {
    let temp = TempDataDir::new("um-console-broker-write-").expect("temp dir");
    let mut config = support::base_config(&temp);
    config.share_store_file = Some(temp.path().join("share.db"));
    config.share_secret_key_file = Some(key_file(&temp));
    let control_key = temp.path().join("broker-control.key");
    write_control_key(&control_key);
    config.broker_socket_path = Some(temp.path().join("missing").join("broker.sock"));
    config.broker_control_key_file = Some(control_key);

    let projection = json!({
        "schema_version": 2,
        "config_generation": 1,
        "share_max_concurrency": 64,
        "data": [],
    });
    let error = production_write_seams(&config)
        .publish(&projection, None)
        .expect_err("an unwired seam must not publish");
    assert_eq!(error.kind, ShareWriteErrorKind::Broker);
    assert_eq!(error.status(), 503);
    assert!(
        error.message.contains("broker 操作门未接线"),
        "{}",
        error.message
    );

    // The key side is checked too: an existing socket with a missing key is
    // still an honest unwired refusal.
    let socket_path = temp.path().join("present.sock");
    let listener = std::os::unix::net::UnixListener::bind(&socket_path).expect("bind socket");
    let mut missing_key = support::base_config(&temp);
    missing_key.broker_socket_path = Some(socket_path.clone());
    missing_key.broker_control_key_file = Some(temp.path().join("missing-control.key"));
    let error = production_write_seams(&missing_key)
        .publish(&projection, None)
        .expect_err("a missing control key must not wire the seam");
    assert_eq!(error.kind, ShareWriteErrorKind::Broker);
    assert!(error.message.contains("broker 操作门未接线"));
    drop(listener);

    let server = um_console::ConsoleServer::start(config).expect("start console");
    let console = support::Console::from_server(server);
    let created = create_over_http(&console, "unwired");
    assert_eq!(created.status, 201);
    assert_eq!(created.json()["published"], json!(false));
    assert!(created.json()["publication_error"]
        .as_str()
        .unwrap()
        .contains("broker 操作门未接线"));
    let key_id = created.json()["key"]["id"]
        .as_str()
        .expect("key id")
        .to_string();
    let deleted = http_request(
        &console,
        "DELETE",
        &format!("/api/console/share-management/keys/{key_id}"),
        Some(json!({"expected_row_version": 1})),
        true,
    );
    assert_eq!(deleted.status, 503);
    assert!(
        deleted.json()["error"]
            .as_str()
            .unwrap()
            .contains("revoking operation not applied online"),
        "{}",
        deleted.json()
    );
}
