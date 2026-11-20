//! #115: the local 8001 read face for the durable UM login-round history.
//!
//! The read parses the file the local 8000 gateway writes. It is read-only: a
//! missing file is a legal empty history, an unreadable file is an error, repeated
//! reads never append a record, and the response keeps a bounded reason summary.

mod support;

use std::fs;

use serde_json::{json, Value};

use support::*;

fn history_line(value: Value) -> String {
    format!("{value}\n")
}

fn history_file(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    temp.path().join("login-history.jsonl")
}

fn write_history(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    let path = history_file(temp);
    let running = history_line(json!({
        "schema": 1,
        "run_id": "run-a",
        "round": 1,
        "state": "running",
        "trigger_source": "business_request",
        "visible": false,
        "started_epoch": 1000.0,
        "ended_epoch": null,
        "submit_count": 0,
        "outcome": null,
        "reason": null,
        "upstream_status": null,
        "evidence_verdict": null,
    }));
    let completed = history_line(json!({
        "schema": 1,
        "run_id": "run-a",
        "round": 1,
        "state": "completed",
        "trigger_source": "business_request",
        "visible": false,
        "started_epoch": 1000.0,
        "ended_epoch": 1060.0,
        "submit_count": 1,
        "outcome": "verification_unknown",
        "reason": "ConnectionError Bearer private-fixture-token",
        "upstream_status": null,
        "evidence_verdict": "unknown",
    }));
    let restored = history_line(json!({
        "schema": 1,
        "run_id": "run-a",
        "round": 2,
        "state": "completed",
        "trigger_source": "explicit",
        "visible": true,
        "started_epoch": 2000.0,
        "ended_epoch": 2060.0,
        "submit_count": 1,
        "outcome": "restored",
        "reason": null,
        "upstream_status": 200,
        "evidence_verdict": "valid",
    }));
    let abandoned = history_line(json!({
        "schema": 1,
        "run_id": "run-b",
        "round": 2,
        "state": "running",
        "trigger_source": "auth_watch",
        "visible": false,
        "started_epoch": 3000.0,
        "ended_epoch": null,
        "submit_count": 1,
        "outcome": null,
        "reason": null,
        "upstream_status": null,
        "evidence_verdict": null,
    }));
    fs::write(&path, [running, completed, restored, abandoned].concat()).expect("write history");
    path
}

fn read_rounds(addr: std::net::SocketAddr, query: &str) -> Value {
    let response = client_request(
        addr,
        &ClientRequest::get(&format!("/api/console/um/login-rounds{query}")),
    )
    .expect("login rounds");
    assert_eq!(response.status, 200, "{:?}", response.body);
    response.json()
}

#[test]
fn the_read_face_returns_retained_rounds_newest_first_with_bounded_reasons() {
    let temp = temp();
    let path = write_history(&temp);
    let before = fs::read(&path).expect("history bytes");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Local;
        config.login_history_file = path.clone();
    });

    let payload = read_rounds(console.addr, "");
    assert_eq!(payload["status"], json!("ok"));
    let rounds = payload["rounds"].as_array().expect("rounds array");
    assert_eq!(rounds.len(), 3, "同一进程运行内同轮次只保留最新一条");
    assert_eq!(rounds[0]["round"], json!(2));
    assert_eq!(rounds[0]["state"], json!("running"));
    assert_eq!(rounds[0]["triggerSource"], json!("auth_watch"));
    assert_eq!(rounds[0]["submitCount"], json!(1));
    assert_eq!(rounds[0]["outcome"], Value::Null);
    assert_eq!(rounds[1]["runId"], json!("run-a"));
    assert_eq!(rounds[1]["outcome"], json!("restored"));
    assert_eq!(rounds[1]["visible"], json!(true));
    assert_eq!(rounds[1]["upstreamStatus"], json!(200));
    assert_eq!(rounds[2]["runId"], json!("run-a"));
    assert_eq!(rounds[2]["startedAt"], json!(1_000_000));
    assert_eq!(rounds[2]["endedAt"], json!(1_060_000));
    assert_eq!(
        rounds[2]["outcome"],
        json!("verification_unknown"),
        "网络核验未知必须与 HTTP 401 分开记录"
    );
    assert_eq!(rounds[2]["evidenceVerdict"], json!("unknown"));
    assert_eq!(rounds[2]["upstreamStatus"], Value::Null);
    assert!(
        !rounds[2]["reason"]
            .as_str()
            .expect("reason")
            .contains("private-fixture-token"),
        "读取面不得带出凭据"
    );
    assert_eq!(
        fs::read(&path).expect("history bytes after read"),
        before,
        "读取不得追加或改写历史"
    );
    assert_eq!(
        read_rounds(console.addr, "?round=2")["rounds"]
            .as_array()
            .expect("filtered rounds")
            .len(),
        2
    );
    let searched = read_rounds(console.addr, "?q=ConnectionError");
    assert_eq!(
        searched["rounds"].as_array().expect("search rounds").len(),
        1
    );
    assert_eq!(
        searched["rounds"][0]["outcome"],
        json!("verification_unknown")
    );
}

#[test]
fn a_missing_history_is_an_empty_read_and_an_unreadable_one_is_an_error() {
    let temp = temp();
    let missing = history_file(&temp);
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Local;
        config.login_history_file = missing;
    });
    let payload = read_rounds(console.addr, "");
    assert_eq!(payload["status"], json!("ok"));
    assert_eq!(payload["rounds"], json!([]));

    let broken = history_file(&temp);
    fs::write(&broken, b"{not json\n").expect("write broken history");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Local;
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
