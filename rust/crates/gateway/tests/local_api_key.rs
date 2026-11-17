//! #79: `CHAT2API_LOCAL_API_KEY` keeps Python's local-surface semantics.
//!
//! A non-empty key requires `Authorization: Bearer` or `X-Api-Key`; an empty key
//! leaves the local surface open. The general `/api/*` passthrough stays behind
//! the control token instead, exactly like Python.

mod support;

use serde_json::json;

use support::*;

#[test]
fn a_configured_local_api_key_protects_the_local_surface() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.local_api_key = "local-secret".to_string();
    });

    let missing = client_request(gateway.addr, &ClientRequest::get("/v1/models"))
        .expect("missing local key");
    assert_eq!(missing.status, 401);
    assert_eq!(
        body_field(&missing.json(), "code"),
        &json!("local_auth_error")
    );

    let wrong = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer("wrong"),
    )
    .expect("wrong local key");
    assert_eq!(wrong.status, 401);

    let bearer = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").bearer("local-secret"),
    )
    .expect("bearer local key");
    assert_eq!(bearer.status, 200);

    let api_key = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").header("X-Api-Key", "local-secret"),
    )
    .expect("x-api-key local key");
    assert_eq!(api_key.status, 200);

    // The general `/api/*` passthrough uses the control token, not the local
    // API key: a valid control token is enough even when a local key is set.
    let passthrough = client_request(
        gateway.addr,
        &ClientRequest::get("/api/echo").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("general API passthrough");
    assert_eq!(passthrough.status, 200, "{:?}", passthrough.body);
}

#[test]
fn an_empty_local_api_key_leaves_the_local_surface_open() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let open = client_request(gateway.addr, &ClientRequest::get("/v1/models"))
        .expect("open local surface");
    assert_eq!(open.status, 200);
}
