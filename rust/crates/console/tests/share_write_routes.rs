//! S6a AC6: the retired backend-login-cancel route is not ported.

use std::sync::Arc;

use serde_json::json;
use um_test_harness::TempDataDir;

mod support;

use support::write::{http_request, start_write_console, Advance, FakeBroker};

#[test]
fn backend_login_cancel_answers_404_and_never_a_simulated_action() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let (console, _store_path, _key_file) = start_write_console(&temp, broker);

    for method in ["POST", "GET"] {
        let response = http_request(
            &console,
            method,
            "/api/console/share-management/backend-login-cancel",
            (method == "POST").then(|| json!({})),
            method == "POST",
        );
        assert_eq!(response.status, 404, "{method}");
        assert_eq!(response.json()["error"], "unknown path");
    }
}
