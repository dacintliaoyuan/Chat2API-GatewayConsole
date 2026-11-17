//! #82: local console auth boundary and the `usage_session` cookie.
//!
//! The local console read faces are credential-free behind the loopback
//! Host/Origin gate and reject any proxy metadata. The `usage_session` cookie is
//! issued only by the `/api/session` exchange; it authenticates the share
//! management faces, never the console read faces.

mod support;

use serde_json::json;

use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

fn start_local(
    temp: &um_test_harness::TempDataDir,
    code_file: &std::path::Path,
    control: &std::path::Path,
) -> Console {
    let store_path = temp.path().join("share.db");
    drop(um_store::ShareStore::open_with_key(&store_path, vec![7u8; 32]).expect("share schema"));
    start_console_with(temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Local;
        config.port = 8001;
        config.control_token_file = Some(control.to_path_buf());
        config.usage_session_code_file = Some(code_file.to_path_buf());
        config.share_store_file = Some(store_path.clone());
        config.usage_log = temp.path().join("usage.jsonl");
    })
}

#[test]
fn console_faces_are_credential_free_and_ignore_the_session_cookie() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_local(&temp, &code_file, &control);

    for target in [
        "/api/summary",
        "/api/console/um/today",
        "/api/console/services",
        "/api/console/gateway-connection?service=chat2api",
        "/api/console/cloud-ops",
        "/api/console/actions",
        "/api/console/operations",
    ] {
        let response = client_request(console.addr, &ClientRequest::get(target)).expect(target);
        assert_eq!(response.status, 200, "{target}");
    }

    let bogus = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/today").header("Cookie", "usage_session=bogus"),
    )
    .expect("bogus cookie");
    assert_eq!(bogus.status, 200);
}

#[test]
fn share_management_requires_the_session_or_control_token() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_local(&temp, &code_file, &control);

    let missing = client_request(console.addr, &ClientRequest::get("/api/share/status"))
        .expect("share status");
    assert_eq!(missing.status, 401);

    let reissued = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec())
            .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("reissue");
    let code = reissued.json()["code"].as_str().expect("code").to_string();
    let exchanged = client_request(
        console.addr,
        &ClientRequest::post("/api/session", b"{}".to_vec())
            .json_body(json!({"code": code}).to_string().into_bytes()),
    )
    .expect("exchange");
    assert_eq!(exchanged.status, 200);
    let cookie = exchanged
        .header("Set-Cookie")
        .expect("Set-Cookie")
        .split(';')
        .next()
        .expect("cookie pair")
        .to_string();
    assert!(cookie.starts_with("usage_session="));

    let accepted = client_request(
        console.addr,
        &ClientRequest::get("/api/share/status").header("Cookie", &cookie),
    )
    .expect("share status with session");
    assert_eq!(accepted.status, 200);

    let invalid = client_request(
        console.addr,
        &ClientRequest::get("/api/share/status").header("Cookie", "usage_session=bogus"),
    )
    .expect("share status with bogus session");
    assert_eq!(invalid.status, 401);
}

#[test]
fn local_rejects_non_loopback_host_and_cross_origin_requests() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_local(&temp, &code_file, &control);

    let foreign_host = client_request(
        console.addr,
        &ClientRequest::get("/api/summary").header("Host", "192.0.2.10:8001"),
    )
    .expect("foreign host");
    assert_eq!(foreign_host.status, 403);
    assert_eq!(foreign_host.json(), json!({"error": "local-only service"}));

    let cross_origin = client_request(
        console.addr,
        &ClientRequest::get("/api/summary").header("Origin", "http://evil.example"),
    )
    .expect("cross origin");
    assert_eq!(cross_origin.status, 403);

    let same_origin = client_request(
        console.addr,
        &ClientRequest::get("/api/summary")
            .header("Origin", &format!("http://127.0.0.1:{}", console.addr.port())),
    )
    .expect("same origin");
    assert_eq!(same_origin.status, 200);
}

#[test]
fn local_rejects_proxy_metadata_even_with_a_loopback_host() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_local(&temp, &code_file, &control);

    for (name, value) in [
        ("Tailscale-User-Login", "owner@example.com"),
        ("X-Forwarded-For", "100.64.0.1"),
        ("X-Forwarded-Host", "console.example.ts.net"),
        ("Forwarded", "for=100.64.0.1"),
    ] {
        let response = client_request(
            console.addr,
            &ClientRequest::get("/api/summary").header(name, value),
        )
        .expect(name);
        assert_eq!(response.status, 403, "{name}");
        assert_eq!(response.json(), json!({"error": "local-only service"}));
    }
}

#[test]
fn session_reissue_needs_the_control_token() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_local(&temp, &code_file, &control);

    let missing = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec()),
    )
    .expect("missing token");
    assert_eq!(missing.status, 403);
    assert_eq!(missing.json(), json!({"error": "control token required"}));

    let accepted = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec())
            .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("accepted token");
    assert_eq!(accepted.status, 200);
    assert!(accepted.json()["code"].is_string());
}
