//! #80: `POST /admin/shutdown` asks for the graceful stop.
//!
//! Python answers 202 `{"status": "stopping"}` and stops the process. The Rust
//! entry keeps the same contract: the control token is required, the request marks
//! the process for a graceful stop, and the listener/login/instance teardown runs
//! in the process entry instead of inside the request thread.

mod support;

use std::sync::atomic::Ordering;

use serde_json::json;

use support::*;

#[test]
fn admin_shutdown_requires_the_control_token_and_requests_the_stop() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let denied = client_request(
        gateway.addr,
        &ClientRequest::post("/admin/shutdown", "{}").json_body("{}"),
    )
    .expect("shutdown without token");
    assert_eq!(denied.status, 403);
    assert_eq!(
        body_field(&denied.json(), "code"),
        &json!("control_denied_token")
    );
    assert!(!gateway
        .server
        .state()
        .shutdown_request
        .load(Ordering::SeqCst));

    let accepted = client_request(
        gateway.addr,
        &ClientRequest::post("/admin/shutdown", "{}")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .json_body("{}"),
    )
    .expect("shutdown with token");
    assert_eq!(accepted.status, 202);
    assert_eq!(accepted.json()["status"], json!("stopping"));
    assert!(gateway
        .server
        .state()
        .shutdown_request
        .load(Ordering::SeqCst));
}
