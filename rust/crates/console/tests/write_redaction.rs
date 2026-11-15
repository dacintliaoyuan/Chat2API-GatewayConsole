//! S6b AC5: plaintext and management secrets stay out of logs, operation
//! records, URLs and error messages.
//!
//! The only plaintext-bearing responses are the immediate create/rotate
//! replies and the single-key readback (`docs/rust-contract.md` §1.3); the
//! control token and the one-time session code are never echoed by an error.

mod support;

use std::sync::Arc;

use serde_json::{json, Value};

use support::write::{start_legacy_console, FakeBroker};
use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";
const SESSION: &str = "fixture-session";

fn contains(body: &[u8], needle: &str) -> bool {
    !needle.is_empty()
        && body
            .windows(needle.len())
            .any(|window| window == needle.as_bytes())
}

fn request(
    console: &Console,
    method: &str,
    target: &str,
    headers: &[(&str, &str)],
    body: Option<serde_json::Value>,
) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    request.method = method.to_string();
    for (header, value) in headers {
        request = request.header(header, value);
    }
    if let Some(body) = body {
        request = request.json_body(body.to_string().into_bytes());
    }
    client_request(console.addr, &request).expect("request")
}

#[test]
fn plaintext_and_management_secrets_stay_out_of_reads_errors_and_logs() {
    let temp = temp();
    let broker = Arc::new(FakeBroker::new(support::write::Advance::Always));
    let console = start_legacy_console(&temp, Arc::clone(&broker));
    console.state().session.issue(SESSION.to_string());
    let control = [("X-Admin-Control", CONTROL_TOKEN)];
    let session_cookie = format!("usage_session={SESSION}");
    let session = [
        ("Cookie", session_cookie.as_str()),
        ("X-Requested-With", "XMLHttpRequest"),
    ];

    let created = request(
        &console,
        "POST",
        "/api/console/share-management/keys",
        &control,
        Some(json!({"name": "redaction", "idempotency_key": "secret-idem-key"})),
    );
    assert_eq!(created.status, 201);
    let payload = created.json();
    let key_id = payload["key"]["id"].as_str().expect("key id").to_string();
    let plaintext = payload["plaintext"]
        .as_str()
        .expect("plaintext")
        .to_string();
    assert!(plaintext.len() >= 16);
    // The plaintext rides this one response only.
    assert!(contains(&created.body, &plaintext));

    let legacy_created = request(
        &console,
        "POST",
        "/api/share/keys",
        &control,
        Some(json!({"name": "legacy-redaction"})),
    );
    assert_eq!(legacy_created.status, 201);
    let legacy_plaintext = legacy_created.json()["secret"]
        .as_str()
        .expect("legacy secret")
        .to_string();
    assert!(contains(&legacy_created.body, &legacy_plaintext));

    let rotated = request(
        &console,
        "POST",
        &format!("/api/console/share-management/keys/{key_id}/rotate"),
        &control,
        Some(json!({"expected_row_version": 1, "idempotency_key": "rotate-idem"})),
    );
    assert_eq!(rotated.status, 200);
    let rotated_plaintext = rotated.json()["plaintext"]
        .as_str()
        .expect("rotated plaintext")
        .to_string();
    assert!(contains(&rotated.body, &rotated_plaintext));

    let secrets = [
        plaintext.clone(),
        legacy_plaintext.clone(),
        rotated_plaintext.clone(),
    ];
    let read_targets = [
        ("GET", "/api/console/share-management/keys"),
        ("GET", "/api/console/share-management/status"),
        ("GET", "/api/console/operations?limit=200"),
        ("GET", "/api/share/keys"),
        ("GET", "/api/share/status"),
        ("GET", "/api/share/operations/op-1"),
    ];
    for (method, target) in read_targets {
        let response = request(&console, method, target, &control, None);
        for secret in &secrets {
            assert!(
                !contains(&response.body, secret),
                "{target} leaked a plaintext"
            );
        }
        assert!(!contains(&response.body, "secret-idem-key"));
        assert!(!contains(&response.body, "rotate-idem"));
    }

    let error_cases: Vec<(&str, String, Value)> = vec![
        (
            "POST",
            "/api/console/share-management/keys".to_string(),
            json!({"name": "x", "max_concurrency": 49}),
        ),
        (
            "POST",
            "/api/console/share-management/keys".to_string(),
            json!({"name": "x", "bogus": "secret-idem-key"}),
        ),
        (
            "PATCH",
            format!("/api/console/share-management/keys/{key_id}"),
            json!({"name": "again", "expected_row_version": 999}),
        ),
        (
            "DELETE",
            format!("/api/console/share-management/keys/{key_id}"),
            json!({}),
        ),
        (
            "POST",
            "/api/console/share-management/display-operator".to_string(),
            json!({"action": "steal"}),
        ),
        (
            "POST",
            "/api/console/share-management/backend-login-cancel".to_string(),
            json!({}),
        ),
        (
            "POST",
            "/api/session".to_string(),
            json!({"code": "wrong-bootstrap-code"}),
        ),
        ("POST", "/api/session/reissue".to_string(), json!({})),
        (
            "POST",
            "/api/share/keys".to_string(),
            json!({"name": "x", "bogus": 1}),
        ),
        ("GET", "/api/share/not-a-route".to_string(), json!({})),
    ];
    for (method, target, body) in error_cases {
        let headers: &[(&str, &str)] =
            if target == "/api/session" || target == "/api/session/reissue" {
                &[]
            } else {
                &control
            };
        let response = request(&console, method, &target, headers, Some(body));
        assert!(response.status >= 400, "{method} {target}");
        for secret in &secrets {
            assert!(
                !contains(&response.body, secret),
                "{method} {target} leaked a plaintext"
            );
        }
        assert!(!contains(&response.body, CONTROL_TOKEN));
        assert!(!contains(&response.body, "secret-idem-key"));
        assert!(!contains(&response.body, "wrong-bootstrap-code"));
    }

    // Session-gated writes accept the same secret without echoing it.
    let session_created = request(
        &console,
        "POST",
        "/api/share/keys",
        &session,
        Some(json!({"name": "session-redaction"})),
    );
    assert_eq!(session_created.status, 201);
    let session_plaintext = session_created.json()["secret"]
        .as_str()
        .expect("session secret")
        .to_string();
    let listed = request(&console, "GET", "/api/share/keys", &session, None);
    assert!(!contains(&listed.body, &session_plaintext));

    // The candidate write face never creates a log file.
    let logs: Vec<String> = std::fs::read_dir(temp.path())
        .expect("temp dir")
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().to_string())
        .filter(|name| name.contains("log"))
        .collect();
    assert!(logs.is_empty(), "unexpected log files: {logs:?}");
}
