//! #79: the local role assembles the general `/api/*` GET/POST passthrough.
//!
//! The passthrough keeps the Python 8000 boundary: the control token, loopback
//! Host, no Origin, the upstream bearer and a JSON-object POST body. The cloud
//! role keeps the same surface unassembled and answers 501
//! `local_api_surface_s1a`.

mod support;

use serde_json::{json, Value};

use support::*;

fn local_get(target: &str) -> ClientRequest {
    ClientRequest::get(target).header("X-Admin-Control", CONTROL_TOKEN)
}

fn local_post(target: &str, body: &str) -> ClientRequest {
    ClientRequest::post(target, body)
        .header("X-Admin-Control", CONTROL_TOKEN)
        .json_body(body)
}

#[test]
fn local_role_proxies_general_api_get_and_post() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let get = client_request(gateway.addr, &local_get("/api/echo?probe=1")).expect("local GET");
    assert_eq!(get.status, 200, "{:?}", get.body);
    assert_eq!(get.json()["method"], json!("GET"));
    assert_eq!(get.json()["path"], json!("/api/echo"));

    let seen = stub.seen();
    let get_seen = seen
        .iter()
        .find(|request| request.path == "/api/echo" && request.method == "GET")
        .expect("GET /api/echo reached the upstream");
    assert_eq!(get_seen.raw_target, "/api/echo", "query must not be forwarded");
    assert_eq!(
        get_seen.authorization.as_deref(),
        Some(format!("Bearer {STUB_TOKEN}").as_str())
    );

    let post = client_request(
        gateway.addr,
        &local_post("/api/echo", r#"{"ping":true}"#),
    )
    .expect("local POST");
    assert_eq!(post.status, 200, "{:?}", post.body);
    assert_eq!(post.json()["method"], json!("POST"));
    assert_eq!(post.json()["path"], json!("/api/echo"));
    assert_eq!(post.json()["body"], json!({"ping": true}));

    let seen = stub.seen();
    let post_seen = seen
        .iter()
        .find(|request| request.path == "/api/echo" && request.method == "POST")
        .expect("POST /api/echo reached the upstream");
    assert_eq!(
        serde_json::from_slice::<Value>(&post_seen.body).expect("forwarded body"),
        json!({"ping": true})
    );
}

#[test]
fn local_passthrough_requires_the_control_token_and_local_boundary() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let missing = client_request(gateway.addr, &ClientRequest::get("/api/echo"))
        .expect("missing control token");
    assert_eq!(missing.status, 403);
    assert_eq!(body_field(&missing.json(), "code"), &json!("control_denied_token"));

    let wrong = client_request(
        gateway.addr,
        &ClientRequest::get("/api/echo").header("X-Admin-Control", "wrong"),
    )
    .expect("wrong control token");
    assert_eq!(wrong.status, 403);
    assert_eq!(body_field(&wrong.json(), "code"), &json!("control_denied_token"));

    let origin = client_request(
        gateway.addr,
        &ClientRequest::get("/api/echo")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .header("Origin", ALLOWED_ORIGIN),
    )
    .expect("control origin");
    assert_eq!(origin.status, 403);
    assert_eq!(
        body_field(&origin.json(), "code"),
        &json!("control_denied_endpoint")
    );

    let host = client_request(
        gateway.addr,
        &ClientRequest::get("/api/echo")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .header("Host", "example.com"),
    )
    .expect("foreign host");
    assert_eq!(host.status, 403);
    assert_eq!(
        body_field(&host.json(), "code"),
        &json!("control_denied_endpoint")
    );

    assert!(
        !stub
            .seen()
            .iter()
            .any(|request| request.path == "/api/echo"),
        "a denied passthrough must never reach the upstream"
    );
}

#[test]
fn local_passthrough_post_requires_a_json_object_body() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let invalid_json = client_request(
        gateway.addr,
        &local_post("/api/echo", "{"),
    )
    .expect("invalid JSON");
    assert_eq!(invalid_json.status, 400);
    assert_eq!(
        body_field(&invalid_json.json(), "code"),
        &json!("invalid_json")
    );

    let non_object = client_request(gateway.addr, &local_post("/api/echo", "[]"))
        .expect("non-object body");
    assert_eq!(non_object.status, 400);
    assert_eq!(
        body_field(&non_object.json(), "code"),
        &json!("invalid_body")
    );

    // Python `_read_body` accepts a missing Content-Type and turns an empty body
    // into `{}` before the upstream call.
    let empty = client_request(
        gateway.addr,
        &ClientRequest::post("/api/echo", "").header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("empty body");
    assert_eq!(empty.status, 200, "{:?}", empty.body);
    let seen = stub.seen();
    let empty_seen = seen
        .iter()
        .find(|request| request.path == "/api/echo" && request.method == "POST")
        .expect("empty POST reached the upstream");
    assert_eq!(
        serde_json::from_slice::<Value>(&empty_seen.body).expect("empty forwarded body"),
        json!({})
    );
}
