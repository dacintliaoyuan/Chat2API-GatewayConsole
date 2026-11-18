//! S6a AC6: the retired legacy `/api/share/backend-login-cancel` route is not
//! ported. #106 adds the console facade route
//! (`/api/console/share-management/backend-login-cancel`), which proxies the
//! gateway; the legacy `/api/share/*` surface stays unknown.

use std::sync::Arc;

use um_test_harness::TempDataDir;

mod support;

use support::write::{start_legacy_console, Advance, FakeBroker};
use support::*;

#[test]
fn legacy_share_backend_login_cancel_stays_unknown() {
    let temp = TempDataDir::new("um-console-share-write-").expect("temp dir");
    let broker = Arc::new(FakeBroker::new(Advance::Always));
    let console = start_legacy_console(&temp, broker);

    for method in ["POST", "GET"] {
        let mut request = ClientRequest::get("/api/share/backend-login-cancel");
        request.method = method.to_string();
        request = request.header("X-Admin-Control", "fixture-control-token");
        if method == "POST" {
            request = request.json_body(b"{}".to_vec());
        }
        let response = client_request(console.addr, &request).expect("request");
        assert_eq!(response.status, 404, "{method}");
        assert_eq!(response.json()["error"], "unknown path");
    }
}
