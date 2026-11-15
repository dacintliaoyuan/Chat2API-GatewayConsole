//! S6b AC3: the legacy `/api/share/*` write face.
//!
//! The expected behavior comes from `usagesrv.py::_share_api` (`usagesrv.py:2013-2278`)
//! and `_management_auth(write=True)` (`usagesrv.py:1767-1783`): writes require
//! the local control token, or the usage session plus the same-origin CSRF
//! marker. The candidate binds only an ephemeral loopback port.

mod support;

use std::sync::Arc;

use serde_json::{json, Value};

use support::write::{
    start_legacy_console, start_legacy_console_with_public_origin, start_unwired_legacy_console,
    FakeBroker,
};
use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";
const SESSION: &str = "fixture-session";

fn session_console(temp: &um_test_harness::TempDataDir) -> (Console, Arc<FakeBroker>) {
    let broker = Arc::new(FakeBroker::new(support::write::Advance::Always));
    let console = start_legacy_console(temp, Arc::clone(&broker));
    console.state().session.issue(SESSION.to_string());
    (console, broker)
}

fn request(
    console: &Console,
    method: &str,
    target: &str,
    auth: Auth,
    body: Option<Value>,
) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    match auth {
        Auth::Control => {
            request = request.header("X-Admin-Control", CONTROL_TOKEN);
        }
        Auth::Session => {
            request = request
                .header("Cookie", &format!("usage_session={SESSION}"))
                .header("X-Requested-With", "XMLHttpRequest");
        }
        Auth::SessionNoCsrf => {
            request = request.header("Cookie", &format!("usage_session={SESSION}"));
        }
        Auth::None => {}
    }
    if let Some(body) = body {
        request = request.json_body(body.to_string().into_bytes());
    }
    client_request(console.addr, &request).expect("request")
}

#[derive(Clone, Copy)]
enum Auth {
    Control,
    Session,
    SessionNoCsrf,
    None,
}

#[test]
fn legacy_writes_require_the_control_token_or_session_plus_csrf() {
    let temp = temp();
    let (console, _broker) = session_console(&temp);

    let missing = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::None,
        Some(json!({"name": "no-credentials"})),
    );
    assert_eq!(missing.status, 401);
    assert_eq!(
        missing.json(),
        json!({"error": "share management credentials required"})
    );

    let no_csrf = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::SessionNoCsrf,
        Some(json!({"name": "no-csrf"})),
    );
    assert_eq!(no_csrf.status, 403);
    assert_eq!(
        no_csrf.json(),
        json!({"error": "session CSRF marker required"})
    );

    let session = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Session,
        Some(json!({"name": "session-key"})),
    );
    assert_eq!(session.status, 201);
    assert_eq!(session.json()["name"], json!("session-key"));

    let control = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Control,
        Some(json!({"name": "control-key"})),
    );
    assert_eq!(control.status, 201);
    assert_eq!(control.json()["name"], json!("control-key"));

    let wrong = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::None,
        Some(json!({"name": "wrong-token"})),
    );
    assert_eq!(wrong.status, 401);

    // Reads keep the S5b session/control-token rule.
    let read = request(&console, "GET", "/api/share/keys", Auth::None, None);
    assert_eq!(read.status, 401);
    let read = request(&console, "GET", "/api/share/keys", Auth::Session, None);
    assert_eq!(read.status, 200);
}

#[test]
fn legacy_create_rotate_patch_delete_and_limits_match_the_manager() {
    let temp = temp();
    let (console, _broker) = session_console(&temp);

    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Control,
        Some(json!({"name": "legacy-one", "max_concurrency": 8})),
    );
    assert_eq!(created.status, 201);
    assert_eq!(created.header("cache-control"), Some("no-store"));
    let payload = created.json();
    for field in [
        "key_id",
        "name",
        "secret",
        "mk",
        "plaintext_viewable",
        "config_generation",
        "published",
        "desired_generation",
        "applied_generation",
        "in_sync",
        "security_pending",
        "security_in_sync",
    ] {
        assert!(payload.get(field).is_some(), "missing {field}: {payload}");
    }
    // The legacy manager result never carries the facade-only `key`/`plaintext`.
    assert!(payload.get("key").is_none());
    assert!(payload.get("plaintext").is_none());
    let key_id = payload["key_id"].as_str().expect("key id").to_string();
    let plaintext = payload["secret"].as_str().expect("secret").to_string();

    let listed = request(&console, "GET", "/api/share/keys", Auth::Control, None);
    assert_eq!(listed.status, 200);
    assert!(!String::from_utf8_lossy(&listed.body).contains(&plaintext));

    let rotated = request(
        &console,
        "POST",
        &format!("/api/share/keys/{key_id}/rotate"),
        Auth::Control,
        Some(json!({"expected_row_version": 1})),
    );
    assert_eq!(rotated.status, 200);
    assert_eq!(rotated.json()["key_id"], json!(key_id));
    assert_eq!(rotated.json()["secret_version"], json!(2));
    assert!(rotated.json().get("key").is_none());

    let patched = request(
        &console,
        "PATCH",
        &format!("/api/share/keys/{key_id}"),
        Auth::Control,
        Some(json!({"name": "renamed", "expected_row_version": 2})),
    );
    assert_eq!(patched.status, 200);
    assert_eq!(patched.json()["name"], json!("renamed"));

    let settings = request(
        &console,
        "PATCH",
        "/api/share/settings",
        Auth::Control,
        Some(json!({"share_max_concurrency": 32, "expected_share_max": 64})),
    );
    assert_eq!(settings.status, 200);
    assert_eq!(settings.json()["share_max_concurrency"], json!(32));

    let normalized = request(
        &console,
        "POST",
        "/api/share/limits/normalize",
        Auth::Control,
        Some(json!({})),
    );
    assert_eq!(normalized.status, 200);
    let normalized = normalized.json();
    for field in ["changed", "global", "keys", "blocked"] {
        assert!(normalized.get(field).is_some(), "missing {field}");
    }
    assert!(normalized["global"].get("before").is_some());
    assert!(normalized["global"].get("after").is_some());

    let deleted = request(
        &console,
        "DELETE",
        &format!("/api/share/keys/{key_id}"),
        Auth::Control,
        Some(json!({"expected_row_version": 3})),
    );
    assert_eq!(deleted.status, 200);
    assert_eq!(deleted.json()["key_id"], json!(key_id));
    assert!(deleted.json()["deleted_at"].is_string());
    assert_eq!(deleted.json()["published"], json!(true));

    let again = request(
        &console,
        "DELETE",
        &format!("/api/share/keys/{key_id}"),
        Auth::Control,
        Some(json!({"expected_row_version": 4})),
    );
    assert_eq!(again.status, 200);
    assert_eq!(again.json()["already_deleted"], json!(true));
}

#[test]
fn legacy_revoke_and_terminate_reports_the_ack_counts() {
    let temp = temp();
    let (console, broker) = session_console(&temp);
    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Control,
        Some(json!({"name": "to-revoke"})),
    );
    let key_id = created.json()["key_id"].as_str().unwrap().to_string();
    let response = request(
        &console,
        "POST",
        &format!("/api/share/keys/{key_id}/revoke-and-terminate"),
        Auth::Control,
        Some(json!({"expected_row_version": 1})),
    );
    assert_eq!(response.status, 200);
    assert_eq!(response.json()["key_id"], json!(key_id));
    assert_eq!(response.json()["terminated"], json!(2));
    assert_eq!(
        broker.terminate_seen.lock().unwrap().last(),
        Some(&vec![key_id])
    );
}

#[test]
fn legacy_lifecycle_actions_are_accepted_with_202() {
    let wired_temp = temp();
    let (console, broker) = session_console(&wired_temp);
    for (path, expected) in [
        ("/api/share/start", "op-start"),
        ("/api/share/stop", "op-stop"),
        ("/api/share/emergency-stop", "op-emergency"),
    ] {
        let response = request(&console, "POST", path, Auth::Control, Some(json!({})));
        assert_eq!(response.status, 202, "{path}");
        assert_eq!(response.json()["operation_id"], json!(expected), "{path}");
        assert_eq!(response.json()["status"], json!("pending"), "{path}");
    }
    assert_eq!(broker.start_calls.lock().unwrap().len(), 1);
    assert_eq!(*broker.stop_calls.lock().unwrap(), 1);
    assert_eq!(*broker.emergency_action_calls.lock().unwrap(), 1);

    let unwired_temp = temp();
    let console = start_unwired_legacy_console(&unwired_temp);
    let response = request(
        &console,
        "POST",
        "/api/share/start",
        Auth::Control,
        Some(json!({})),
    );
    assert_eq!(response.status, 503);
    assert!(!response.json()["error"].as_str().unwrap().is_empty());
}

#[test]
fn legacy_write_validation_matches_python() {
    let temp = temp();
    let (console, _broker) = session_console(&temp);

    let cases: Vec<(&str, &str, Value, u16, &str)> = vec![
        (
            "POST",
            "/api/share/keys",
            json!({"name": "x", "bogus": 1}),
            400,
            "unknown field(s): ['bogus']",
        ),
        (
            "POST",
            "/api/share/keys",
            json!({"name": ""}),
            400,
            "invalid name",
        ),
        (
            "POST",
            "/api/share/keys",
            json!({"name": "x", "max_concurrency": 49}),
            400,
            "max_concurrency must be an integer in 1..48",
        ),
        (
            "POST",
            "/api/share/keys",
            json!({"name": "x", "idempotency_key": "x".repeat(129)}),
            400,
            "invalid idempotency_key",
        ),
        (
            "PATCH",
            "/api/share/settings",
            json!({"share_max_concurrency": 65}),
            400,
            "share_max_concurrency must be an integer in 1..64",
        ),
        (
            "POST",
            "/api/share/limits/normalize",
            json!({"unexpected": true}),
            400,
            "unknown field(s): ['unexpected']",
        ),
        (
            "POST",
            "/api/share/keys/missing-key/rotate",
            json!({}),
            400,
            "expected_row_version required",
        ),
        (
            "DELETE",
            "/api/share/keys/missing-key",
            json!({}),
            400,
            "expected_row_version required",
        ),
        (
            "POST",
            "/api/share/keys/missing-key/test",
            json!({}),
            400,
            "invalid key_id or expected_row_version",
        ),
        (
            "POST",
            "/api/share/start",
            json!({"bogus": 1}),
            400,
            "unknown field(s): ['bogus']",
        ),
        (
            "POST",
            "/api/share/not-a-route",
            json!({}),
            404,
            "unknown path",
        ),
    ];
    for (method, target, body, status, error) in cases {
        let response = request(&console, method, target, Auth::Control, Some(body));
        assert_eq!(response.status, status, "{method} {target}");
        assert_eq!(response.json()["error"], json!(error), "{method} {target}");
    }

    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Control,
        Some(json!({"name": "patch-me"})),
    );
    let key_id = created.json()["key_id"].as_str().unwrap().to_string();
    let patch = request(
        &console,
        "PATCH",
        &format!("/api/share/keys/{key_id}"),
        Auth::Control,
        Some(json!({"enabled": false, "expected_row_version": 1})),
    );
    assert_eq!(patch.status, 400);
    assert_eq!(patch.json()["error"], json!("enabled_not_writable"));

    let conflict = request(
        &console,
        "PATCH",
        &format!("/api/share/keys/{key_id}"),
        Auth::Control,
        Some(json!({"name": "stale", "expected_row_version": 99})),
    );
    assert_eq!(conflict.status, 409);
}

#[test]
fn legacy_key_test_never_leaks_the_plaintext_and_stays_honest() {
    let first_temp = temp();
    let (console, broker) = session_console(&first_temp);
    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Control,
        Some(json!({"name": "probe-me"})),
    );
    let payload = created.json();
    let key_id = payload["key_id"].as_str().unwrap().to_string();
    let plaintext = payload["secret"].as_str().unwrap().to_string();

    // No configured public origin: the probe never sends the credential.
    let response = request(
        &console,
        "POST",
        &format!("/api/share/keys/{key_id}/test"),
        Auth::Control,
        Some(json!({"expected_row_version": 1})),
    );
    assert_eq!(response.status, 200);
    let body = response.json();
    assert_eq!(body["connected"], json!(false));
    assert_eq!(body["reason"], json!("public_url_unavailable"));
    assert_eq!(body["tested_from"], json!("public"));
    assert!(!String::from_utf8_lossy(&response.body).contains(&plaintext));
    assert!(broker.probed.lock().unwrap().is_empty());

    // With a verified gateway and a probe seam the credential reaches the
    // probe exactly once, and the response still carries no plaintext.
    let probe_temp = temp();
    let broker = Arc::new(FakeBroker::new(support::write::Advance::Always));
    *broker.gateway_running.lock().unwrap() = Some(true);
    *broker.probe.lock().unwrap() = Some(json!({
        "connected": true,
        "reason": "ok",
        "stage": "inference",
        "http_status": 200,
        "model": "fixture-model",
        "elapsed_ms": 42,
    }));
    let console = start_legacy_console_with_public_origin(
        &probe_temp,
        Arc::clone(&broker),
        Some("https://share.example.test"),
    );
    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Control,
        Some(json!({"name": "probe-ok"})),
    );
    let payload = created.json();
    let key_id = payload["key_id"].as_str().unwrap().to_string();
    let plaintext = payload["secret"].as_str().unwrap().to_string();
    let response = request(
        &console,
        "POST",
        &format!("/api/share/keys/{key_id}/test"),
        Auth::Control,
        Some(json!({"expected_row_version": 1})),
    );
    assert_eq!(response.status, 200);
    let body = response.json();
    assert_eq!(body["connected"], json!(true));
    assert_eq!(body["reason"], json!("ok"));
    assert_eq!(body["public_url"], json!("https://share.example.test"));
    assert!(!String::from_utf8_lossy(&response.body).contains(&plaintext));
    let probed = broker.probed.lock().unwrap();
    assert_eq!(probed.len(), 1);
    assert_eq!(probed[0].0, plaintext);
    assert_eq!(probed[0].1, "https://share.example.test");
}

#[test]
fn legacy_unconfigured_public_origin_never_guesses_a_probe_host() {
    let temp = temp();
    let (console, broker) = session_console(&temp);
    *broker.gateway_running.lock().unwrap() = Some(true);
    let created = request(
        &console,
        "POST",
        "/api/share/keys",
        Auth::Control,
        Some(json!({"name": "no-origin"})),
    );
    let key_id = created.json()["key_id"].as_str().unwrap().to_string();
    let response = request(
        &console,
        "POST",
        &format!("/api/share/keys/{key_id}/test"),
        Auth::Control,
        Some(json!({"expected_row_version": 1})),
    );
    assert_eq!(response.status, 200);
    assert_eq!(response.json()["connected"], json!(false));
    assert_eq!(response.json()["reason"], json!("public_url_unavailable"));
    assert!(broker.probed.lock().unwrap().is_empty());
}
