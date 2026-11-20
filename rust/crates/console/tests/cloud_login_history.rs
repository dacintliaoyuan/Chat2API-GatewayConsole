//! #116: the cloud 8002 read face for the durable UM login-round history.
//!
//! The cloud console exposes exactly `GET /api/console/um/login-rounds` from the
//! shared read implementation. It only parses the file the cloud 8003 gateway
//! writes: a missing file is a legal empty history, an unreadable file is an error,
//! and repeated reads never append or rewrite a record. Every other `um/*` face
//! stays unassembled on the cloud role.

mod support;

use std::fs;

use serde_json::{json, Value};

use support::*;

fn history_line(value: Value) -> String {
    format!("{value}\n")
}

fn write_history(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("login-history.jsonl");
    let running = history_line(json!({
        "schema": 1,
        "run_id": "cloud-run-a",
        "round": 1,
        "state": "running",
        "trigger_source": "business_request",
        "visible": false,
        "started_epoch": 1_791_000_000.0,
        "ended_epoch": null,
        "submit_count": 0,
        "outcome": null,
        "reason": null,
        "upstream_status": null,
        "evidence_verdict": null,
    }));
    let completed = history_line(json!({
        "schema": 1,
        "run_id": "cloud-run-a",
        "round": 1,
        "state": "completed",
        "trigger_source": "business_request",
        "visible": false,
        "started_epoch": 1_791_000_000.0,
        "ended_epoch": 1_791_000_060.0,
        "submit_count": 1,
        "outcome": "verification_unknown",
        "reason": "ConnectionError Bearer cloud-private-fixture-token",
        "upstream_status": null,
        "evidence_verdict": "unknown",
    }));
    let restored = history_line(json!({
        "schema": 1,
        "run_id": "cloud-run-a",
        "round": 2,
        "state": "completed",
        "trigger_source": "auth_watch",
        "visible": true,
        "started_epoch": 1_791_001_000.0,
        "ended_epoch": 1_791_001_060.0,
        "submit_count": 2,
        "outcome": "restored",
        "reason": null,
        "upstream_status": 200,
        "evidence_verdict": "valid",
    }));
    fs::write(&path, [running, completed, restored].concat()).expect("write history");
    path
}

fn read_rounds(addr: std::net::SocketAddr, query: &str) -> Value {
    let response = client_request(
        addr,
        &ClientRequest::get(&format!("/api/console/um/login-rounds{query}")),
    )
    .expect("cloud login rounds");
    assert_eq!(response.status, 200, "{:?}", response.body);
    response.json()
}

#[test]
fn cloud_console_reads_retained_rounds_without_writing_the_history() {
    let temp = temp();
    let path = write_history(&temp);
    let before = fs::read(&path).expect("history bytes");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Cloud;
        config.login_history_file = path.clone();
    });

    let payload = read_rounds(console.addr, "");
    assert_eq!(payload["status"], json!("ok"));
    let rounds = payload["rounds"].as_array().expect("rounds array");
    assert_eq!(rounds.len(), 2, "同一运行内同轮次只保留最新一条");
    assert_eq!(rounds[0]["round"], json!(2));
    assert_eq!(rounds[0]["triggerSource"], json!("auth_watch"));
    assert_eq!(rounds[0]["submitCount"], json!(2));
    assert_eq!(rounds[0]["outcome"], json!("restored"));
    assert_eq!(rounds[0]["upstreamStatus"], json!(200));
    assert_eq!(rounds[0]["evidenceVerdict"], json!("valid"));
    assert_eq!(rounds[1]["outcome"], json!("verification_unknown"));
    assert_eq!(rounds[1]["evidenceVerdict"], json!("unknown"));
    assert_eq!(rounds[1]["upstreamStatus"], Value::Null);
    assert!(
        !rounds[1]["reason"]
            .as_str()
            .expect("reason")
            .contains("cloud-private-fixture-token"),
        "云端读面不得带出凭据"
    );

    assert_eq!(
        read_rounds(console.addr, "?round=2")["rounds"]
            .as_array()
            .expect("round filter")
            .len(),
        1
    );
    let searched = read_rounds(console.addr, "?q=ConnectionError");
    assert_eq!(
        searched["rounds"].as_array().expect("search filter").len(),
        1
    );
    assert_eq!(
        searched["rounds"][0]["outcome"],
        json!("verification_unknown")
    );

    assert_eq!(
        fs::read(&path).expect("history bytes after reads"),
        before,
        "读取不得追加或改写历史"
    );
}

#[test]
fn cloud_console_keeps_every_other_um_face_unassembled() {
    let temp = temp();
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Cloud;
    });
    for path in [
        "/api/console/um/today",
        "/api/console/um/trend",
        "/api/console/um/requests",
        "/api/console/um/logs",
        "/api/console/um/maintenance",
        "/api/console/um/upstream-faults",
    ] {
        let response =
            client_request(console.addr, &ClientRequest::get(path)).expect("cloud um face");
        assert_eq!(response.status, 503, "{path}");
        assert_eq!(response.json()["error"], json!("capability_not_assembled"));
        assert_eq!(response.json()["capability"], json!("um"));
    }
    let post = client_request(
        console.addr,
        &ClientRequest::post("/api/console/um/login-rounds", b"{}")
            .header("Content-Type", "application/json"),
    )
    .expect("cloud um write");
    assert_eq!(post.status, 503);
    assert_eq!(post.json()["error"], json!("capability_not_assembled"));
}

#[test]
fn a_missing_history_is_an_empty_read_and_an_unreadable_one_is_an_error() {
    let temp = temp();
    let missing = temp.path().join("missing-login-history.jsonl");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Cloud;
        config.login_history_file = missing;
    });
    let payload = read_rounds(console.addr, "");
    assert_eq!(payload["status"], json!("ok"));
    assert_eq!(payload["rounds"], json!([]));

    let broken = temp.path().join("broken-login-history.jsonl");
    fs::write(&broken, b"{not json\n").expect("write broken history");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Cloud;
        config.login_history_file = broken;
    });
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/login-rounds"),
    )
    .expect("unreadable history");
    assert_eq!(response.status, 500);
    assert_eq!(response.json()["error"], json!("login_history_unreadable"));
}
