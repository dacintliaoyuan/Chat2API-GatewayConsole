//! Review fix: `GET /api/console/share-management/control-status`.
//!
//! `usagesrv.py:2802-2819` answers 200 `{responsive, error_code}` from the
//! broker snapshot: `state=ok` plus a broker object is responsive, an unwired
//! broker is `share_broker_unwired`, and any other snapshot is
//! `share_broker_unavailable`. A non-GET method is 405.

mod support;

use serde_json::json;

use support::*;

fn start_control_console(
    temp: &um_test_harness::TempDataDir,
    snapshot: Option<serde_json::Value>,
) -> Console {
    let mut seams = StaticShareSeams::new();
    seams.broker_snapshot = snapshot;
    start_console_with_seams(temp, |_| {}, std::sync::Arc::new(seams))
}

fn get_control_status(console: &Console) -> ClientResponse {
    client_request(
        console.addr,
        &ClientRequest::get("/api/console/share-management/control-status"),
    )
    .expect("request")
}

#[test]
fn control_status_reports_a_responsive_broker() {
    let temp = temp();
    let console = start_control_console(
        &temp,
        Some(json!({
            "state": "ok",
            "broker": {"version": "2.0.0", "pid": 4242},
        })),
    );
    let response = get_control_status(&console);
    assert_eq!(response.status, 200);
    assert_eq!(response.json(), json!({"responsive": true}));
    assert_eq!(response.header("cache-control"), Some("no-store"));
}

#[test]
fn control_status_reports_unavailable_and_unwired_brokers() {
    let temp = temp();
    let console = start_control_console(
        &temp,
        Some(json!({"state": "unavailable", "error_code": "deadline_exceeded"})),
    );
    let response = get_control_status(&console);
    assert_eq!(response.status, 200);
    assert_eq!(
        response.json(),
        json!({"responsive": false, "error_code": "share_broker_unavailable"})
    );

    let temp = support::temp();
    let console = start_control_console(&temp, None);
    let response = get_control_status(&console);
    assert_eq!(response.status, 200);
    assert_eq!(
        response.json(),
        json!({"responsive": false, "error_code": "share_broker_unwired"})
    );
}

#[test]
fn control_status_rejects_non_get_with_method_not_allowed() {
    let temp = temp();
    let console = start_control_console(&temp, Some(json!({"state": "ok", "broker": {}})));
    let response = client_request(
        console.addr,
        &ClientRequest::post("/api/console/share-management/control-status", b"".to_vec())
            .header("X-Requested-With", "XMLHttpRequest"),
    )
    .expect("request");
    assert_eq!(response.status, 405);
    assert_eq!(response.json(), json!({"error": "method not allowed"}));
}
