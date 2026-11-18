//! #94: the local `model.catalog` action runs in Rust, not in a Python sidecar.
//!
//! The executor reads the fixed local gateway catalogue endpoint directly. These
//! tests run a fake gateway HTTP server and point the console at its ephemeral
//! loopback port; the Python interpreter is deliberately unusable so any sidecar
//! fallback fails loudly.

mod support;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use um_console::config::Role;
use um_console::ConsoleServer;
use um_test_harness::{HttpRequest, HttpResponse, TempDataDir, TestServer};

use support::*;

fn start_local_console(temp: &TempDataDir, gateway_port: u16) -> Console {
    let mut config = base_config(temp);
    config.role = Role::Local;
    config.port = 8001;
    config.gateway_port = gateway_port;
    config.repo_root = temp.path().to_path_buf();
    // Any Python sidecar attempt must fail loudly.
    config.python_exe = PathBuf::from("definitely-not-python-94");
    let server =
        ConsoleServer::start_with_runtime(config, stopped_runtime()).expect("start local console");
    Console::from_server(server)
}

fn request(console: &Console, method: &str, target: &str, body: Option<Value>) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    if let Some(body) = body {
        request = request.json_body(body.to_string());
    }
    client_request(console.addr, &request).expect("request")
}

fn wait_terminal(console: &Console, operation: &Value) -> Value {
    let op_id = operation["id"].as_str().expect("operation id").to_string();
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let fetched = request(
            console,
            "GET",
            &format!("/api/console/operations/{op_id}"),
            None,
        );
        assert_eq!(fetched.status, 200);
        let operation = fetched.json()["operation"].clone();
        if matches!(
            operation["state"].as_str(),
            Some("succeeded" | "failed" | "cancelled" | "interrupted")
        ) {
            return operation;
        }
        assert!(
            Instant::now() < deadline,
            "operation never finished: {operation}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn submit_catalog(console: &Console, params: Value) -> Value {
    let created = request(
        console,
        "POST",
        "/api/console/operations",
        Some(json!({
            "action": "model.catalog",
            "target": "model",
            "params": params,
        })),
    );
    assert_eq!(created.status, 201);
    wait_terminal(console, &created.json()["operation"])
}

fn gateway(models: Value) -> TestServer {
    TestServer::start(move |_request: HttpRequest| {
        HttpResponse::json(200, &json!({"data": models}).to_string())
    })
    .expect("fake gateway")
}

#[test]
fn model_catalog_reads_the_gateway_without_a_python_sidecar() {
    let temp = temp();
    let gateway = gateway(json!([
        {"id": "model-a", "max_model_len": 128000},
        {"id": "DeepSeek-V4-Flash-0731"},
        {"id": "model-a-Fast", "max_model_len": 4096},
        {"id": "", "max_model_len": 1},
    ]));
    let console = start_local_console(&temp, gateway.port());

    let operation = submit_catalog(&console, json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["sources"], json!(["um"]));
    assert_eq!(result["warnings"], json!([]));
    let rows = result["catalogs"]["um"].as_array().expect("um rows");
    assert_eq!(rows.len(), 2, "{rows:?}");
    assert_eq!(rows[0]["id"], json!("model-a"));
    assert_eq!(rows[0]["context"], json!(128000));
    assert_eq!(rows[0]["context_state"], json!("declared"));
    assert_eq!(
        rows[0]["output_token_cap"],
        json!({"value": 16384, "source": "compat_default"})
    );
    assert_eq!(rows[1]["id"], json!("DeepSeek-V4-Flash-0731"));
    assert_eq!(rows[1]["context"], Value::Null);
    assert_eq!(rows[1]["context_state"], json!("unknown"));
    assert_eq!(rows[1]["efforts"], json!(["low", "medium", "high", "max"]));
    assert_eq!(rows[1]["default_effort"], json!("high"));
    assert_eq!(rows[1]["efforts_source"], json!("known"));
    assert!(result["checked_epoch"].as_i64().unwrap_or_default() > 0);
}

#[test]
fn model_catalog_scope_reads_only_the_selected_source() {
    let temp = temp();
    let gateway = gateway(json!([{"id": "model-a"}]));
    let console = start_local_console(&temp, gateway.port());

    let operation = submit_catalog(&console, json!({"sources": "[\"um\"]"}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["sources"], json!(["um"]));
}

#[test]
fn model_catalog_rejects_unknown_or_paused_sources_before_reading() {
    let temp = temp();
    let gateway = gateway(json!([{"id": "model-a"}]));
    let console = start_local_console(&temp, gateway.port());

    for sources in [
        json!("[\"m365\"]"),
        json!("[\"um\",\"um\"]"),
        json!("[]"),
        json!("not-json"),
    ] {
        let operation = submit_catalog(&console, json!({"sources": sources}));
        assert_eq!(operation["state"], json!("failed"), "{operation}");
        assert_eq!(operation["error"]["code"], json!("invalid_params"));
    }
}

#[test]
fn model_catalog_reports_an_unreachable_gateway_as_a_warning() {
    let temp = temp();
    // Reserve and immediately drop a listener so the port is closed.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
    let port = listener.local_addr().expect("addr").port();
    drop(listener);
    let console = start_local_console(&temp, port);

    let operation = submit_catalog(&console, json!({}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    assert_eq!(operation["result"]["catalogs"], json!({}));
    let warnings = operation["result"]["warnings"]
        .as_array()
        .expect("warnings");
    assert_eq!(warnings.len(), 1);
    assert!(warnings[0]
        .as_str()
        .unwrap_or_default()
        .starts_with("UM目录不可用："));
}

#[test]
fn strict_fresh_requires_the_raw_fresh_marker() {
    let temp = temp();
    let gateway = TestServer::start(|_request: HttpRequest| {
        HttpResponse::json(200, &json!({"data": [{"id": "cached-model"}]}).to_string())
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit_catalog(&console, json!({"fresh": true, "sources": "[\"um\"]"}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["success"], json!(false));
    assert_eq!(result["fresh"], json!(false));
    assert_eq!(result["stale"], json!(false));
    assert_eq!(result["refresh"]["status"], json!("not_fresh"));
    assert_eq!(result["catalogs"], json!({}));
}

#[test]
fn strict_fresh_with_the_marker_returns_the_models() {
    let temp = temp();
    let gateway = TestServer::start(|request: HttpRequest| {
        assert_eq!(
            request.header("x-chat2api-models"),
            Some("raw-fresh"),
            "{request:?}"
        );
        HttpResponse::json(200, &json!({"data": [{"id": "fresh-model"}]}).to_string())
            .with_header("x-chat2api-raw-models", "1")
    })
    .expect("fake gateway");
    let console = start_local_console(&temp, gateway.port());

    let operation = submit_catalog(&console, json!({"fresh": true, "sources": "[\"um\"]"}));

    assert_eq!(operation["state"], json!("succeeded"), "{operation}");
    let result = &operation["result"];
    assert_eq!(result["success"], json!(true));
    assert_eq!(result["fresh"], json!(true));
    assert_eq!(result["stale"], json!(false));
    assert_eq!(result["refresh"]["status"], json!("fresh"));
    assert_eq!(result["catalogs"]["um"][0]["id"], json!("fresh-model"));
}

#[test]
fn the_local_console_config_defaults_to_the_python_on_path() {
    let config = um_console::config::ConsoleConfig::default();
    assert_eq!(config.python_exe, PathBuf::from("python"));
    assert_eq!(config.repo_root, PathBuf::from("."));
}

#[test]
fn shared_stopped_runtime_is_usable() {
    // Keep the support module linked for the shared helpers.
    let _: Arc<dyn um_console::runtime::RuntimeFacts> = stopped_runtime();
}
