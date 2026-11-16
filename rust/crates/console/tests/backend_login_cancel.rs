//! S6b AC4: the retired `backend-login-cancel` route is not ported.
//!
//! `web/src/cloud/cloud-writes.ts` still calls the route, but the real
//! `usagesrv.py` has no such path: both the legacy `/api/share/*` surface and
//! the `share-management` facade answer `404 unknown path`. The candidate keeps
//! that behavior; it never simulates a login action.

mod support;

use serde_json::json;

use support::write::FakeBroker;
use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

fn request(console: &Console, method: &str, target: &str) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    request = request.header("X-Admin-Control", CONTROL_TOKEN);
    if method != "GET" {
        request = request
            .header("X-Requested-With", "XMLHttpRequest")
            .json_body(b"{}".to_vec());
    }
    client_request(console.addr, &request).expect("request")
}

#[test]
fn backend_login_cancel_is_not_a_route_on_either_8002_surface() {
    let temp = temp();
    let broker = std::sync::Arc::new(FakeBroker::new(support::write::Advance::Always));
    let console = support::write::start_legacy_console(&temp, broker);

    for target in [
        "/api/console/share-management/backend-login-cancel",
        "/api/share/backend-login-cancel",
    ] {
        for method in ["GET", "POST", "PATCH", "DELETE"] {
            let response = request(&console, method, target);
            assert_eq!(response.status, 404, "{method} {target}");
            assert_eq!(response.json(), json!({"error": "unknown path"}));
        }
    }
}
