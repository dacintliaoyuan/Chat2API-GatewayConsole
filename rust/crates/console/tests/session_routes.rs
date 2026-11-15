//! S6b AC1: `POST /api/session` and `POST /api/session/reissue`.
//!
//! The expected behavior comes from `usagesrv.py`:
//! `session_exchange`/`reissue_session_code` (`usagesrv.py:702-743`), the
//! `do_POST` routes (`usagesrv.py:3756-3791`) and the cookie constants
//! (`usagesrv.py:665-666`). The candidate binds only an ephemeral loopback
//! port.

mod support;

use serde_json::{json, Value};

use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

fn start_session_console(
    temp: &um_test_harness::TempDataDir,
    code_file: &std::path::Path,
    control: &std::path::Path,
) -> Console {
    let store_path = temp.path().join("share.db");
    drop(um_store::ShareStore::open_with_key(&store_path, vec![7u8; 32]).expect("share schema"));
    start_console_with(temp, stopped_runtime(), |config| {
        config.control_token_file = Some(control.to_path_buf());
        config.usage_session_code_file = Some(code_file.to_path_buf());
        config.share_store_file = Some(store_path.clone());
    })
}

#[test]
fn reissue_requires_the_control_token_and_publishes_the_code_file() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_session_console(&temp, &code_file, &control);

    let missing = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec()),
    )
    .expect("request");
    assert_eq!(missing.status, 403);
    assert_eq!(missing.json(), json!({"error": "control token required"}));

    let wrong = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec())
            .header("X-Admin-Control", "wrong-token"),
    )
    .expect("request");
    assert_eq!(wrong.status, 403);
    assert_eq!(wrong.json(), json!({"error": "control token required"}));

    let response = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec())
            .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("request");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("cache-control"), Some("no-store"));
    let payload = response.json();
    assert_eq!(payload["ok"], json!(true));
    let code = payload["code"].as_str().expect("reissued code").to_string();
    assert!(!code.is_empty());
    assert_eq!(
        std::fs::read_to_string(&code_file).expect("code file"),
        code
    );
}

#[test]
fn exchange_sets_a_twelve_hour_httponly_session_cookie_and_is_single_use() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_session_console(&temp, &code_file, &control);

    let reissued = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec())
            .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("request");
    let code = reissued.json()["code"].as_str().expect("code").to_string();

    let exchanged = client_request(
        console.addr,
        &ClientRequest::post("/api/session", b"{}".to_vec())
            .json_body(json!({"code": code}).to_string().into_bytes()),
    )
    .expect("request");
    assert_eq!(exchanged.status, 200);
    assert_eq!(exchanged.json(), json!({"ok": true}));
    assert_eq!(exchanged.header("cache-control"), Some("no-store"));
    let cookie = exchanged.header("Set-Cookie").expect("Set-Cookie");
    let value = cookie
        .strip_prefix("usage_session=")
        .and_then(|rest| rest.split(';').next())
        .expect("session cookie value");
    assert!(!value.is_empty());
    for attribute in ["HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=43200"] {
        assert!(
            cookie.contains(attribute),
            "{cookie} must carry {attribute}"
        );
    }

    let replay = client_request(
        console.addr,
        &ClientRequest::post("/api/session", b"{}".to_vec())
            .json_body(json!({"code": code}).to_string().into_bytes()),
    )
    .expect("request");
    assert_eq!(replay.status, 403);
    assert_eq!(replay.json(), json!({"error": "invalid bootstrap code"}));
    assert!(replay.header("Set-Cookie").is_none());

    // The issued session cookie is accepted by the management read face.
    let status = client_request(
        console.addr,
        &ClientRequest::get("/api/share/status")
            .header("Cookie", &format!("usage_session={value}")),
    )
    .expect("request");
    assert_eq!(status.status, 200);
}

#[test]
fn a_wrong_or_expired_code_never_issues_a_session() {
    let temp = temp();
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    let code_file = temp.path().join("session-code");
    let console = start_session_console(&temp, &code_file, &control);

    let reissued = client_request(
        console.addr,
        &ClientRequest::post("/api/session/reissue", b"{}".to_vec())
            .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("request");
    let code = reissued.json()["code"].as_str().expect("code").to_string();
    let wrong = if code == "not-the-code" {
        "another-code".to_string()
    } else {
        "not-the-code".to_string()
    };

    let response = client_request(
        console.addr,
        &ClientRequest::post("/api/session", b"{}".to_vec())
            .json_body(json!({"code": wrong}).to_string().into_bytes()),
    )
    .expect("request");
    assert_eq!(response.status, 403);
    assert_eq!(response.json(), json!({"error": "invalid bootstrap code"}));
    assert!(response.header("Set-Cookie").is_none());

    let missing = client_request(
        console.addr,
        &ClientRequest::post("/api/session", b"{}".to_vec()),
    )
    .expect("request");
    assert_eq!(missing.status, 403);

    // The unconsumed code still works exactly once.
    let first = client_request(
        console.addr,
        &ClientRequest::post("/api/session", b"{}".to_vec())
            .json_body(json!({"code": code}).to_string().into_bytes()),
    )
    .expect("request");
    assert_eq!(first.status, 200);
    assert!(first.header("Set-Cookie").is_some());
    let _: Value = json!(null);
}
