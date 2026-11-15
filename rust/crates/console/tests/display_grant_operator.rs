//! S6b AC2: one-time display grants and the single-page operator.
//!
//! The expected behavior comes from `console/display_proxy.py`:
//! `DisplayGrants.issue/redeem` (`display_proxy.py:366-470`), the operator
//! state machine (`display_proxy.py:434-560`) and the console routes
//! `_console_display_grant_payload` / `_console_display_operator_payload`
//! (`usagesrv.py:2671-2743`). The candidate binds only an ephemeral loopback
//! port and assembles no display relay.

mod support;

use std::sync::Arc;

use serde_json::{json, Value};

use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

struct DisplaySeams {
    availability: Option<Value>,
}

impl um_console::ShareReadSeams for DisplaySeams {
    fn display_availability(&self) -> Option<Value> {
        self.availability.clone()
    }
}

fn start_display_console(
    temp: &um_test_harness::TempDataDir,
    availability: Option<Value>,
) -> Console {
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    start_console_with_seams(
        temp,
        |config| config.control_token_file = Some(control.clone()),
        Arc::new(DisplaySeams { availability }),
    )
}

fn issue_grant(console: &Console) -> Value {
    let response = client_request(
        console.addr,
        &ClientRequest::post("/api/console/share-management/display-grant", b"".to_vec())
            .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("request");
    assert_eq!(response.status, 200, "{:?}", response.json());
    response.json()
}

fn operator(console: &Console, session: Option<&str>, body: Value) -> (u16, Value) {
    let mut request = ClientRequest::post(
        "/api/console/share-management/display-operator",
        b"".to_vec(),
    )
    .json_body(body.to_string().into_bytes())
    .header("X-Requested-With", "XMLHttpRequest");
    if let Some(session) = session {
        request = request.header("Cookie", &format!("um_display_session={session}"));
    }
    let response = client_request(console.addr, &request).expect("request");
    (response.status, response.json())
}

#[test]
fn display_grant_requires_a_management_subject_and_is_single_use() {
    let temp = temp();
    let console = start_display_console(&temp, Some(json!({"available": true, "reason": null})));

    let denied = client_request(
        console.addr,
        &ClientRequest::post("/api/console/share-management/display-grant", b"".to_vec())
            .header("X-Requested-With", "XMLHttpRequest"),
    )
    .expect("request");
    assert_eq!(denied.status, 403);
    assert_eq!(
        denied.json(),
        json!({"error": "display_authorization_required"})
    );

    let payload = issue_grant(&console);
    let grant = payload["grant"].as_str().expect("grant").to_string();
    assert!(!grant.is_empty());
    assert!(payload["expires_at"].is_number());
    let origin = payload["origin"].as_str().expect("origin").to_string();
    assert_eq!(origin, format!("http://127.0.0.1:{}", console.addr.port()));
    let url = payload["url"].as_str().expect("url").to_string();
    let (path, fragment) = url.split_once('#').expect("fragment");
    assert_eq!(path, "/display/grant");
    assert!(!path.contains(&grant));
    assert_eq!(fragment, format!("grant={grant}"));

    let session = console
        .state()
        .display_grants
        .redeem(&grant, &origin)
        .expect("redeemed session");
    assert!(
        console
            .state()
            .display_grants
            .redeem(&grant, &origin)
            .is_none(),
        "a grant is single-use"
    );
    assert!(console
        .state()
        .display_grants
        .validate_session(&session.token, &origin)
        .is_some());
}

#[test]
fn display_grant_is_bound_to_its_exact_origin() {
    let temp = temp();
    let console = start_display_console(&temp, Some(json!({"available": true, "reason": null})));
    let payload = issue_grant(&console);
    let grant = payload["grant"].as_str().expect("grant").to_string();
    let origin = payload["origin"].as_str().expect("origin").to_string();
    let wrong_origin = format!("{origin}.evil.example");
    assert!(console
        .state()
        .display_grants
        .redeem(&grant, &wrong_origin)
        .is_none());
    // A wrong origin does not consume the grant.
    assert!(console
        .state()
        .display_grants
        .redeem(&grant, &origin)
        .is_some());
}

#[test]
fn display_operator_acquire_and_takeover() {
    let temp = temp();
    let console = start_display_console(&temp, Some(json!({"available": true, "reason": null})));

    let first = issue_grant(&console);
    let first_session = console
        .state()
        .display_grants
        .redeem(
            first["grant"].as_str().expect("grant"),
            first["origin"].as_str().expect("origin"),
        )
        .expect("first session");
    let second = issue_grant(&console);
    let second_session = console
        .state()
        .display_grants
        .redeem(
            second["grant"].as_str().expect("grant"),
            second["origin"].as_str().expect("origin"),
        )
        .expect("second session");

    let (status, body) = operator(
        &console,
        Some(&first_session.token),
        json!({"action": "acquire"}),
    );
    assert_eq!(status, 200);
    assert_eq!(
        body,
        json!({"acquired": true, "generation": 1, "took_over": false})
    );

    // The same owner re-acquires idempotently.
    let (status, body) = operator(
        &console,
        Some(&first_session.token),
        json!({"action": "acquire"}),
    );
    assert_eq!(status, 200);
    assert_eq!(
        body,
        json!({"acquired": true, "generation": 1, "took_over": false})
    );

    let (status, body) = operator(
        &console,
        Some(&second_session.token),
        json!({"action": "acquire"}),
    );
    assert_eq!(status, 409);
    assert_eq!(
        body,
        json!({
            "error": "display_operator_exists",
            "generation": 1,
            "owner_label": "display-session",
        })
    );

    let (status, body) = operator(
        &console,
        Some(&second_session.token),
        json!({"action": "takeover"}),
    );
    assert_eq!(status, 200);
    assert_eq!(
        body,
        json!({"acquired": true, "generation": 2, "took_over": true})
    );

    let (status, body) = operator(&console, None, json!({"action": "acquire"}));
    assert_eq!(status, 409);
    assert_eq!(body, json!({"error": "display_session_required"}));
}

#[test]
fn display_operator_validates_the_action_the_write_gate_and_the_body() {
    let temp = temp();
    let console = start_display_console(&temp, Some(json!({"available": true, "reason": null})));

    // No same-origin marker and no control token: the write gate refuses.
    let response = client_request(
        console.addr,
        &ClientRequest::post(
            "/api/console/share-management/display-operator",
            b"".to_vec(),
        )
        .json_body(json!({"action": "acquire"}).to_string().into_bytes()),
    )
    .expect("request");
    assert_eq!(response.status, 403);
    assert_eq!(
        response.json(),
        json!({"error": "same-origin marker or control token required"})
    );

    let (status, body) = operator(&console, None, json!({"action": "steal"}));
    assert_eq!(status, 400);
    assert_eq!(body, json!({"error": "display_operator_action_invalid"}));

    let (status, body) = operator(&console, None, json!({"action": "acquire", "force": true}));
    assert_eq!(status, 400);
    assert_eq!(body, json!({"error": "unknown field(s): ['force']"}));

    let (status, body) = operator(&console, None, json!({}));
    assert_eq!(status, 400);
    assert_eq!(body, json!({"error": "display_operator_action_invalid"}));
}

#[test]
fn an_unassembled_or_disabled_display_is_an_honest_503() {
    let unwired_temp = temp();
    let unwired = start_display_console(&unwired_temp, None);
    for target in [
        "/api/console/share-management/display-grant",
        "/api/console/share-management/display-operator",
    ] {
        let response = client_request(
            unwired.addr,
            &ClientRequest::post(target, b"".to_vec()).header("X-Admin-Control", CONTROL_TOKEN),
        )
        .expect("request");
        assert_eq!(response.status, 503, "{target}");
        assert_eq!(
            response.json(),
            json!({"error": "display_proxy_unavailable"}),
            "{target}"
        );
    }

    let disabled_temp = temp();
    let disabled = start_display_console(
        &disabled_temp,
        Some(json!({"available": false, "reason": "display_disabled"})),
    );
    for target in [
        "/api/console/share-management/display-grant",
        "/api/console/share-management/display-operator",
    ] {
        let response = client_request(
            disabled.addr,
            &ClientRequest::post(target, b"".to_vec()).header("X-Admin-Control", CONTROL_TOKEN),
        )
        .expect("request");
        assert_eq!(response.status, 503, "{target}");
        assert_eq!(
            response.json(),
            json!({"error": "display_disabled"}),
            "{target}"
        );
    }
}

#[test]
fn a_foreign_tailnet_identity_is_refused_instead_of_trusted() {
    let temp = temp();
    let console = start_display_console(&temp, Some(json!({"available": true, "reason": null})));
    let response = client_request(
        console.addr,
        &ClientRequest::post("/api/console/share-management/display-grant", b"".to_vec())
            .header("X-Admin-Control", CONTROL_TOKEN)
            .header("Tailscale-User-Login", "owner@example.com"),
    )
    .expect("request");
    assert_eq!(response.status, 403);
    assert_eq!(
        response.json(),
        json!({"error": "display_authorization_required"})
    );
}
