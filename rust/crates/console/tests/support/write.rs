#![allow(dead_code)]

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use serde_json::{json, Value};
use um_console::share_write::{ShareWriteError, ShareWriteSeams, ShareWriteStore};
use um_test_harness::TempDataDir;

use super::{base_config, client_request, stopped_runtime, ClientRequest, ClientResponse, Console};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Advance {
    RevokeOnly,
    Always,
}

pub struct FakeBroker {
    pub published: Mutex<Vec<Value>>,
    pub terminate_seen: Mutex<Vec<Vec<String>>>,
    enabled: Mutex<HashSet<String>>,
    security_generation: Mutex<i64>,
    advance: Mutex<Advance>,
    pub fail: Mutex<Option<String>>,
    pub ack_override: Mutex<Option<Value>>,
    pub omit_security: Mutex<bool>,
    pub omit_counts: Mutex<bool>,
    pub emergency: Mutex<Value>,
    pub emergency_calls: Mutex<usize>,
}

impl FakeBroker {
    pub fn new(advance: Advance) -> Self {
        Self {
            published: Mutex::new(Vec::new()),
            terminate_seen: Mutex::new(Vec::new()),
            enabled: Mutex::new(HashSet::new()),
            security_generation: Mutex::new(0),
            advance: Mutex::new(advance),
            fail: Mutex::new(None),
            ack_override: Mutex::new(None),
            omit_security: Mutex::new(false),
            omit_counts: Mutex::new(false),
            emergency: Mutex::new(json!({"state": "stopped", "stopped": true})),
            emergency_calls: Mutex::new(0),
        }
    }

    pub fn set_advance(&self, advance: Advance) {
        *self.advance.lock().unwrap() = advance;
    }

    pub fn published(&self) -> Vec<Value> {
        self.published.lock().unwrap().clone()
    }

    pub fn emergency_calls(&self) -> usize {
        *self.emergency_calls.lock().unwrap()
    }
}

impl ShareWriteSeams for FakeBroker {
    fn publish(
        &self,
        projection: &Value,
        terminate_keys: Option<&[String]>,
    ) -> Result<Value, ShareWriteError> {
        if let Some(message) = self.fail.lock().unwrap().clone() {
            return Err(ShareWriteError::broker(message));
        }
        self.published.lock().unwrap().push(projection.clone());
        let generation = projection["config_generation"].as_i64().unwrap_or_default();
        let next_enabled: HashSet<String> = projection["data"]
            .as_array()
            .map(|rows| {
                rows.iter()
                    .filter(|row| row["enabled"].as_bool().unwrap_or(false))
                    .filter_map(|row| row["key_id"].as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        let previous = self.enabled.lock().unwrap().clone();
        let revoked = previous.difference(&next_enabled).next().is_some();
        if revoked || *self.advance.lock().unwrap() == Advance::Always {
            *self.security_generation.lock().unwrap() = generation;
        }
        *self.enabled.lock().unwrap() = next_enabled;
        if let Some(keys) = terminate_keys {
            self.terminate_seen
                .lock()
                .unwrap()
                .push(keys.iter().map(|key| key.to_string()).collect());
        }
        let ack_override = self.ack_override.lock().unwrap().clone();
        let mut reply = json!({
            "applied_generation": ack_override.unwrap_or_else(|| json!(generation)),
            "instance_id": "fixture-instance",
        });
        if !*self.omit_security.lock().unwrap() {
            reply["security_generation"] = json!(*self.security_generation.lock().unwrap());
        }
        if terminate_keys.is_some() && !*self.omit_counts.lock().unwrap() {
            reply["terminated"] = json!(2);
            reply["released"] = json!(3);
            reply["aborted"] = json!(4);
            reply["upstream_closed"] = json!(5);
        }
        Ok(reply)
    }

    fn emergency_stop(&self) -> Result<Value, ShareWriteError> {
        *self.emergency_calls.lock().unwrap() += 1;
        Ok(self.emergency.lock().unwrap().clone())
    }
}

pub fn key_file(temp: &TempDataDir) -> PathBuf {
    let path = temp.path().join("share-secret.key");
    std::fs::write(&path, (0..32).collect::<Vec<u8>>()).expect("key file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .expect("key file mode");
    }
    path
}

pub fn make_writer(
    temp: &TempDataDir,
    broker: Arc<FakeBroker>,
) -> (ShareWriteStore, PathBuf, PathBuf) {
    let key_file = key_file(temp);
    let store_path = temp.path().join("share.db");
    let writer = ShareWriteStore::new(store_path.clone(), Some(key_file.clone()), broker);
    (writer, store_path, key_file)
}

pub fn start_write_console(
    temp: &TempDataDir,
    broker: Arc<FakeBroker>,
) -> (Console, PathBuf, PathBuf) {
    let key_file = key_file(temp);
    let store_path = temp.path().join("share.db");
    let mut config = base_config(temp);
    config.share_store_file = Some(store_path.clone());
    config.share_secret_key_file = Some(key_file.clone());
    let server = um_console::ConsoleServer::start_with_wiring(
        config,
        stopped_runtime(),
        Arc::new(um_console::UnwiredSeams),
        Arc::new(um_console::operation_manager::UnwiredActionExecutor),
        broker,
    )
    .expect("start candidate console");
    (Console::from_server(server), store_path, key_file)
}

pub fn http_request(
    console: &Console,
    method: &str,
    target: &str,
    body: Option<Value>,
    marker: bool,
) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    if marker {
        request = request.header("X-Requested-With", "XMLHttpRequest");
    }
    if let Some(body) = body {
        request = request.json_body(body.to_string());
    }
    client_request(console.addr, &request).expect("request")
}

pub fn create_over_http(console: &Console, name: &str) -> ClientResponse {
    http_request(
        console,
        "POST",
        "/api/console/share-management/keys",
        Some(json!({"name": name})),
        true,
    )
}

pub fn open_store(path: &Path, key_file: &Path) -> um_store::ShareStore {
    um_store::ShareStore::open_with_key(
        path,
        um_store::secret::load_key_file(Some(key_file)).expect("key file"),
    )
    .expect("open store")
}
