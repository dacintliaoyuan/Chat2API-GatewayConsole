//! S7 AC2: `/display/session` and the GET `/display/grant` page.
//!
//! The expected behavior is `display_proxy.py::DisplayProxy._serve_session`
//! (`display_proxy.py:1342-1358`) and the grant-page branch of `handle_get`
//! (`display_proxy.py:1235-1245`): the grant page is static code that loads
//! without a session so its module script can exchange a fragment, and the
//! session read answers `{authorized, expires_at}` without ever echoing a token.

mod support;

use serde_json::json;
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

fn get(console: &Console, target: &str, headers: &[(&str, &str)]) -> ClientResponse {
    let mut request = ClientRequest::get(target);
    for (name, value) in headers {
        request = request.header(name, value);
    }
    client_request(console.addr, &request).expect("request")
}

fn session(console: &Console, headers: &[(&str, &str)]) -> serde_json::Value {
    let response = get(console, "/display/session", headers);
    assert_eq!(response.status, 200, "{:?}", response.body);
    response.json()
}

#[test]
fn the_grant_page_is_static_and_needs_only_the_same_origin() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets);

    // `session_valid=lambda _cookie: False` in the Python route test: the page
    // still loads because it is static code, not a secret.
    let response = get(&console, "/display/grant", &[("Cookie", "")]);
    assert_eq!(response.status, 200);
    assert_eq!(
        response.header("Content-Type"),
        Some("text/html; charset=utf-8")
    );
    assert_eq!(
        response.body,
        std::fs::read(&assets.launcher).expect("launcher bytes")
    );
    assert!(String::from_utf8_lossy(&response.body).contains("/display/launcher.js"));
    let csp = response.header("Content-Security-Policy").expect("csp");
    assert!(csp.contains("script-src 'self'"));
    assert!(!csp.contains("script-src 'self' 'unsafe-inline'"));
    assert_eq!(response.header("Cache-Control"), Some("no-store"));

    // A query string is never accepted on the fragment-exchange page.
    let response = get(&console, "/display/grant?grant=secret", &[]);
    assert_eq!(response.status, 400);
    assert_eq!(
        response.json(),
        json!({"error": "display query not allowed"})
    );

    // A cross-origin request is refused before the static page is read.
    let response = get(
        &console,
        "/display/grant",
        &[("Origin", "https://evil.test")],
    );
    assert_eq!(response.status, 403);
}

#[test]
fn the_session_read_reports_authorization_without_echoing_tokens() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets);
    let origin = format!("http://127.0.0.1:{}", console.addr.port());

    assert_eq!(
        session(&console, &[]),
        json!({"authorized": false, "expires_at": null})
    );
    assert_eq!(
        session(&console, &[("Cookie", "usage_session=bad")]),
        json!({"authorized": false, "expires_at": null})
    );

    // The admin control token authorizes the launcher but has no display
    // session, so there is no expiry to report.
    assert_eq!(
        session(&console, &[("X-Admin-Control", CONTROL_TOKEN)]),
        json!({"authorized": true, "expires_at": null})
    );

    console
        .state()
        .session
        .issue("fixture-usage-session".to_string());
    assert_eq!(
        session(
            &console,
            &[("Cookie", "usage_session=fixture-usage-session")]
        ),
        json!({"authorized": true, "expires_at": null})
    );

    let (grant, _) = console
        .state()
        .display_grants
        .issue("control-token", &origin)
        .expect("grant");
    let display_session = console
        .state()
        .display_grants
        .redeem(&grant, &origin)
        .expect("display session");
    let payload = session(
        &console,
        &[(
            "Cookie",
            &format!("um_display_session={}", display_session.token),
        )],
    );
    assert_eq!(payload["authorized"], json!(true));
    assert_eq!(payload["expires_at"], json!(display_session.expires_at));
    let body = payload.to_string();
    assert!(!body.contains(&display_session.token));
    assert!(!body.contains(&grant));

    // The same cookie on another origin is not a display session.
    let response = get(
        &console,
        "/display/session",
        &[
            (
                "Cookie",
                &format!("um_display_session={}", display_session.token),
            ),
            ("Origin", "https://evil.test"),
        ],
    );
    assert_eq!(response.status, 403);
}

#[test]
fn a_disabled_or_broken_display_answers_503_on_both_reads() {
    let temp = temp();
    let disabled = start_console_with(&temp, stopped_runtime(), |config| {
        config.display = DisplayConfig::default();
    });
    for target in ["/display/session", "/display/grant"] {
        let response = get(&disabled, target, &[]);
        assert_eq!(response.status, 503, "{target}");
        assert_eq!(response.json(), json!({"error": "display unavailable"}));
    }

    let broken = start_console_with(&temp, stopped_runtime(), |config| {
        config.display = DisplayConfig {
            error: "display_launcher_missing".to_string(),
            ..DisplayConfig::default()
        };
    });
    for target in ["/display/session", "/display/grant"] {
        let response = get(&broken, target, &[]);
        assert_eq!(response.status, 503, "{target}");
        assert_eq!(
            response.json(),
            json!({"error": "display_launcher_missing"}),
            "{target}"
        );
    }
}
