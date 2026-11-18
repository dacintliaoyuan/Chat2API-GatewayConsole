//! S7 AC3: one-time display grant exchanged for an HttpOnly cookie.
//!
//! The expected behavior is `display_proxy.py::DisplayProxy._serve_grant_exchange`
//! (`display_proxy.py:1360-1414`): exactly one Content-Length, `application/json`,
//! a bounded body, a canonical grant token, a single redemption bound to the exact
//! origin, and a 204 carrying only the `um_display_session` HttpOnly,
//! SameSite=Strict cookie. Grant TTL is 60 seconds, the session TTL 300 seconds
//! and the relay idle timeout 900 seconds.

mod support;

use serde_json::json;
use um_console::display_grant::{
    DISPLAY_SESSION_TTL_SECONDS, GRANT_TTL_SECONDS, MAX_DISPLAY_SESSIONS, MAX_PENDING_GRANTS,
};
use um_console::display_relay::DisplayConfig;
use um_test_harness::TempDataDir;

use support::display::*;
use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

fn start_display_console(temp: &TempDataDir, assets: &DisplayAssets) -> Console {
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    start_console_with(temp, stopped_runtime(), |config| {
        config.display = display_config(assets, 9);
        config.control_token_file = Some(control);
    })
}

fn origin(console: &Console) -> String {
    format!("http://127.0.0.1:{}", console.addr.port())
}

fn exchange(console: &Console, token: &str, origin: &str) -> ClientResponse {
    client_request(
        console.addr,
        &ClientRequest::post(
            "/display/grant",
            json!({"grant": token}).to_string().into_bytes(),
        )
        .json_body(json!({"grant": token}).to_string().into_bytes())
        .header("Origin", origin),
    )
    .expect("request")
}

fn issue(console: &Console) -> (String, String) {
    let origin = origin(console);
    let (token, _) = console
        .state()
        .display_grants
        .issue("control-token", &origin)
        .expect("grant");
    (token, origin)
}

#[test]
fn the_ttls_are_the_frozen_display_budgets() {
    assert_eq!(GRANT_TTL_SECONDS, 60);
    assert_eq!(DISPLAY_SESSION_TTL_SECONDS, 300);
    assert_eq!(MAX_PENDING_GRANTS, 64);
    assert_eq!(MAX_DISPLAY_SESSIONS, 64);

    let temp = temp();
    let assets = display_assets(&temp);
    let config = display_config(&assets, 9);
    assert_eq!(config.idle_timeout.as_secs(), 900);
    assert_eq!(config.max_session_seconds.as_secs(), 14400);
    assert_eq!(config.max_connections, 2);

    let grants = um_console::DisplayGrants::new();
    let (grant, grant_expires) = grants
        .issue_at("control-token", "http://127.0.0.1:8002", 10)
        .expect("grant");
    assert_eq!(grant_expires, 10 + GRANT_TTL_SECONDS);
    let session = grants
        .redeem_at(&grant, "http://127.0.0.1:8002", 10)
        .expect("session");
    assert_eq!(session.expires_at, 10 + DISPLAY_SESSION_TTL_SECONDS);
}

#[test]
fn a_loopback_grant_reaches_the_operator_from_another_loopback_spelling() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets);
    let port = console.addr.port();

    // The entry helper issues the grant for the console's 127.0.0.1 spelling.
    let grant_origin = format!("http://127.0.0.1:{port}");
    let (grant, _) = console
        .state()
        .display_grants
        .issue("control-token", &grant_origin)
        .expect("grant");

    // The browser opens the private window on localhost, so both the grant
    // exchange and every later request use the localhost spelling.
    let browser_origin = format!("http://localhost:{port}");
    let browser_host = format!("localhost:{port}");
    let redeemed = client_request(
        console.addr,
        &ClientRequest::post(
            "/display/grant",
            json!({"grant": grant}).to_string().into_bytes(),
        )
        .json_body(json!({"grant": grant}).to_string().into_bytes())
        .header("Host", &browser_host)
        .header("Origin", &browser_origin),
    )
    .expect("request");
    assert_eq!(redeemed.status, 204, "{:?}", redeemed.body);
    let cookie = redeemed.header("Set-Cookie").expect("cookie");
    let session = cookie
        .split(';')
        .next()
        .expect("cookie pair")
        .trim_start_matches("um_display_session=")
        .to_string();

    // The same localhost session reads the display session face.
    let read = client_request(
        console.addr,
        &ClientRequest::get("/display/session")
            .header("Host", &browser_host)
            .header("Cookie", &format!("um_display_session={session}")),
    )
    .expect("request");
    assert_eq!(read.status, 200);
    assert_eq!(read.json()["authorized"], json!(true));

    // ... and acquires the single-page operator instead of a 409.
    let acquired = client_request(
        console.addr,
        &ClientRequest::post(
            "/api/console/share-management/display-operator",
            json!({"action": "acquire"}).to_string().into_bytes(),
        )
        .json_body(json!({"action": "acquire"}).to_string().into_bytes())
        .header("Host", &browser_host)
        .header("X-Requested-With", "XMLHttpRequest")
        .header("Cookie", &format!("um_display_session={session}")),
    )
    .expect("request");
    assert_eq!(acquired.status, 200, "{:?}", acquired.json());
    assert_eq!(
        acquired.json(),
        json!({"acquired": true, "generation": 1, "took_over": false})
    );
}

#[test]
fn a_grant_is_exchanged_once_for_an_httponly_cookie() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets);
    let (grant, origin) = issue(&console);

    let response = exchange(&console, &grant, &origin);
    assert_eq!(response.status, 204);
    assert!(response.body.is_empty());
    assert_eq!(response.header("Content-Length"), Some("0"));
    assert_eq!(response.header("Cache-Control"), Some("no-store"));
    let cookie = response.header("Set-Cookie").expect("cookie");
    assert!(cookie.starts_with("um_display_session="));
    assert!(cookie.contains("; HttpOnly"));
    assert!(cookie.contains("; SameSite=Strict"));
    assert!(cookie.contains("; Path=/"));
    assert!(!cookie.contains(&grant));
    let max_age: u64 = cookie
        .split("; ")
        .find_map(|part| part.strip_prefix("Max-Age="))
        .and_then(|value| value.parse().ok())
        .expect("max age");
    assert!((1..=DISPLAY_SESSION_TTL_SECONDS).contains(&max_age));

    let session_token = cookie
        .split(';')
        .next()
        .expect("cookie pair")
        .trim_start_matches("um_display_session=")
        .to_string();
    assert!(console
        .state()
        .display_grants
        .validate_session(&session_token, &origin)
        .is_some());

    // The one-time grant cannot be exchanged twice.
    assert_eq!(exchange(&console, &grant, &origin).status, 403);
}

#[test]
fn the_exchange_rejects_bad_requests_without_consuming_the_grant() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets);
    let (grant, origin) = issue(&console);

    // A wrong origin is refused and does not consume the grant.
    assert_eq!(exchange(&console, &grant, "https://evil.test").status, 403);

    let wrong_type = client_request(
        console.addr,
        &ClientRequest::post(
            "/display/grant",
            json!({"grant": grant}).to_string().into_bytes(),
        )
        .header("Content-Type", "text/plain")
        .header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(wrong_type.status, 415);
    assert!(wrong_type.body.is_empty());

    let too_large = client_request(
        console.addr,
        &ClientRequest::post(
            "/display/grant",
            json!({"grant": grant}).to_string().into_bytes(),
        )
        .json_body(json!({"grant": grant}).to_string().into_bytes())
        .header("Origin", &origin)
        .header("Content-Length", "999999"),
    )
    .expect("request");
    assert_eq!(too_large.status, 413);
    assert!(too_large.body.is_empty());

    let empty = client_request(
        console.addr,
        &ClientRequest::post("/display/grant", Vec::new())
            .header("Content-Type", "application/json")
            .header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(empty.status, 400);

    let invalid_json = client_request(
        console.addr,
        &ClientRequest::post("/display/grant", b"{".to_vec())
            .json_body(b"{".to_vec())
            .header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(invalid_json.status, 400);

    let short_token = exchange(&console, "short", &origin);
    assert_eq!(short_token.status, 400);

    // None of the rejected attempts consumed the grant.
    assert_eq!(exchange(&console, &grant, &origin).status, 204);
}

#[test]
fn the_exchange_is_only_on_the_grant_path() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets);
    let origin = origin(&console);

    let wrong_path = client_request(
        console.addr,
        &ClientRequest::post("/display/", Vec::new()).header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(wrong_path.status, 404);
    assert_eq!(wrong_path.json(), json!({"error": "unknown display path"}));

    let query = client_request(
        console.addr,
        &ClientRequest::post("/display/grant?grant=secret", Vec::new()).header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(query.status, 400);
    assert_eq!(query.json(), json!({"error": "display query not allowed"}));

    // An unknown display POST is a 404 instead of a static 404 page.
    let other = client_request(
        console.addr,
        &ClientRequest::post("/display/websockify", Vec::new()).header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(other.status, 404);
    assert_eq!(other.json(), json!({"error": "unknown display path"}));
}

#[test]
fn a_disabled_display_refuses_the_exchange_before_reading_the_body() {
    let temp = temp();
    let disabled = start_console_with(&temp, stopped_runtime(), |config| {
        config.display = DisplayConfig::default();
    });
    let response = client_request(
        disabled.addr,
        &ClientRequest::post("/display/grant", Vec::new())
            .json_body(json!({"grant": "x".repeat(16)}).to_string().into_bytes()),
    )
    .expect("request");
    assert_eq!(response.status, 503);
    assert_eq!(response.json(), json!({"error": "display unavailable"}));
}
