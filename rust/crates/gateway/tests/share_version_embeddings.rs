//! Acceptance 5: `/v1/version` and `/v1/embeddings` are not shared.
//!
//! The share mode route gate refuses both with `404 route_not_shared` before
//! authentication or any upstream call, matching the real Python handler.

mod support;

use serde_json::json;

use support::*;

#[test]
fn version_is_route_not_shared_in_share_mode() {
    let fixture = fixture();
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let response = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/version").bearer(SHARE_SECRET),
    )
    .expect("share version");
    assert_eq!(
        u64::from(response.status),
        fixture["readOnly"]["versionShare"]["status"]
            .as_u64()
            .unwrap()
    );
    assert_eq!(
        response.json(),
        json!({"error": {
            "message": fixture["readOnly"]["versionShare"]["body"]["error"]["message"],
            "type": "not_found_error",
            "code": "route_not_shared",
            "param": null,
            "detail": null,
        }})
    );
    assert_eq!(response.header("connection"), Some("close"));
    assert!(
        !stub
            .seen()
            .iter()
            .any(|request| request.path == "/api/version"),
        "share mode must not reach the upstream version"
    );
}

#[test]
fn embeddings_is_route_not_shared_in_share_mode() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);

    let expected = json!({"error": {
        "message": "route_not_shared",
        "type": "not_found_error",
        "code": "route_not_shared",
        "param": null,
        "detail": null,
    }});
    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/embeddings", "{}").bearer(SHARE_SECRET),
    )
    .expect("share embeddings");
    assert_eq!(response.status, 404);
    assert_eq!(response.json(), expected);
    assert_eq!(response.header("connection"), Some("close"));
    assert!(
        !stub
            .seen()
            .iter()
            .any(|request| request.path == "/api/models"),
        "share mode must not forward embeddings"
    );
}

#[test]
fn the_route_gate_runs_before_authentication() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, true);
    for target in ["/v1/version", "/v1/embeddings"] {
        let request = if target == "/v1/version" {
            ClientRequest::get(target)
        } else {
            ClientRequest::post(target, "{}")
        };
        let response = client_request(gateway.addr, &request).expect("route gate");
        assert_eq!(response.status, 404, "{target}");
        assert_eq!(
            response.json()["error"]["type"],
            json!("not_found_error"),
            "{target}"
        );
    }
}
