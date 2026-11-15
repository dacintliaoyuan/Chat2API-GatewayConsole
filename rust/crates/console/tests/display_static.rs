//! S7 AC1: `/display/` launcher, noVNC static tree and security headers.
//!
//! The expected behavior is `console/display_proxy.py::DisplayProxy._serve_static`
//! and `load_config` (`display_proxy.py:120-146,217-276,1149-1163,1416-1455`):
//! `/display` redirects to `/display/`, the launcher and its two module assets
//! are served from the launcher file's own directory, every other asset comes
//! from the noVNC root with traversal refused, and every response carries the
//! same security headers as the Python console. The fake display listener never
//! binds a governed port.

mod support;

use std::collections::HashMap;
use std::time::Duration;

use serde_json::json;
use um_console::display_relay::DisplayConfig;
use um_test_harness::TempDataDir;

use support::display::*;
use support::*;

const CONTROL_TOKEN: &str = "fixture-control-token";

fn start_display_console(temp: &TempDataDir, assets: &DisplayAssets, vnc_port: u16) -> Console {
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    start_console_with(temp, stopped_runtime(), |config| {
        config.display = display_config(assets, vnc_port);
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

fn assert_security_headers(response: &ClientResponse) {
    let expected = [
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        (
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        ),
        (
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; \
             img-src 'self' data:; connect-src 'self'; font-src 'self'; \
             worker-src 'self' blob:; base-uri 'none'; form-action 'none'; \
             frame-ancestors 'none'",
        ),
    ];
    for (name, value) in expected {
        assert_eq!(response.header(name), Some(value), "{name}");
    }
}

#[test]
fn display_config_defaults_to_disabled_and_loads_valid_values() {
    let default = DisplayConfig::load(|_| None);
    assert!(!default.enabled);
    assert_eq!(default.error, "");

    let temp = temp();
    let assets = display_assets(&temp);
    let values = HashMap::from([
        ("CONSOLE_DISPLAY_ENABLED".to_string(), "1".to_string()),
        (
            "CONSOLE_DISPLAY_NOVNC_ROOT".to_string(),
            assets.novnc_root.to_string_lossy().to_string(),
        ),
        (
            "CONSOLE_DISPLAY_LAUNCHER_FILE".to_string(),
            assets.launcher.to_string_lossy().to_string(),
        ),
    ]);
    let config = DisplayConfig::load(|name| values.get(name).cloned());
    assert!(config.enabled);
    assert_eq!(config.vnc_host, "127.0.0.1");
    assert_eq!(config.vnc_port, 8097);
    assert_eq!(config.vnc_auth_socket, "/run/um-share/vnc-auth.sock");
    assert_eq!(config.vnc_auth_file, "");
    assert_eq!(config.max_connections, 2);
    assert_eq!(config.idle_timeout, Duration::from_secs(900));
    assert_eq!(config.max_session_seconds, Duration::from_secs(14400));

    let legacy = HashMap::from([
        ("CONSOLE_DISPLAY_ENABLED".to_string(), "1".to_string()),
        ("CONSOLE_DISPLAY_VNC_PORT".to_string(), "5901".to_string()),
        (
            "CONSOLE_DISPLAY_NOVNC_ROOT".to_string(),
            assets.novnc_root.to_string_lossy().to_string(),
        ),
        (
            "CONSOLE_DISPLAY_LAUNCHER_FILE".to_string(),
            assets.launcher.to_string_lossy().to_string(),
        ),
    ]);
    let config = DisplayConfig::load(|name| legacy.get(name).cloned());
    assert!(config.enabled);
    assert_eq!(config.vnc_port, 5901);
}

#[test]
fn display_config_invalid_values_and_missing_assets_fail_closed() {
    let temp = temp();
    let assets = display_assets(&temp);
    let root = assets.novnc_root.to_string_lossy().to_string();
    let launcher = assets.launcher.to_string_lossy().to_string();
    let base = |extra: &[(&str, &str)]| {
        let mut values = HashMap::from([
            ("CONSOLE_DISPLAY_ENABLED".to_string(), "1".to_string()),
            ("CONSOLE_DISPLAY_NOVNC_ROOT".to_string(), root.clone()),
            (
                "CONSOLE_DISPLAY_LAUNCHER_FILE".to_string(),
                launcher.clone(),
            ),
        ]);
        for (name, value) in extra {
            values.insert(name.to_string(), value.to_string());
        }
        DisplayConfig::load(|name| values.get(name).cloned())
    };

    for extra in [
        &[("CONSOLE_DISPLAY_VNC_PORT", "0")][..],
        &[("CONSOLE_DISPLAY_MAX_CONNECTIONS", "9")],
        &[("CONSOLE_DISPLAY_IDLE_TIMEOUT", "1")],
        &[("CONSOLE_DISPLAY_MAX_SESSION", "999999")],
        &[("CONSOLE_DISPLAY_VNC_HOST", "0.0.0.0")],
        &[("CONSOLE_DISPLAY_VNC_HOST", "10.0.0.1")],
        &[("CONSOLE_DISPLAY_VNC_HOST", "::")],
        &[("CONSOLE_DISPLAY_VNC_HOST", "example.test")],
        &[("CONSOLE_DISPLAY_VNC_AUTH_SOCKET", "relative.sock")],
        &[
            ("CONSOLE_DISPLAY_VNC_AUTH_SOCKET", "/run/a.sock"),
            ("CONSOLE_DISPLAY_VNC_AUTH_FILE", "/run/a.passwd"),
        ],
    ] {
        let config = base(extra);
        assert!(!config.enabled, "{extra:?}");
        assert_eq!(config.error, "display_config_invalid", "{extra:?}");
    }

    let missing_root = HashMap::from([
        ("CONSOLE_DISPLAY_ENABLED".to_string(), "1".to_string()),
        (
            "CONSOLE_DISPLAY_NOVNC_ROOT".to_string(),
            temp.path().join("missing").to_string_lossy().to_string(),
        ),
        (
            "CONSOLE_DISPLAY_LAUNCHER_FILE".to_string(),
            launcher.clone(),
        ),
    ]);
    let config = DisplayConfig::load(|name| missing_root.get(name).cloned());
    assert!(!config.enabled);
    assert_eq!(config.error, "display_novnc_missing");

    let missing_launcher = HashMap::from([
        ("CONSOLE_DISPLAY_ENABLED".to_string(), "1".to_string()),
        ("CONSOLE_DISPLAY_NOVNC_ROOT".to_string(), root.clone()),
        (
            "CONSOLE_DISPLAY_LAUNCHER_FILE".to_string(),
            temp.path()
                .join("missing.html")
                .to_string_lossy()
                .to_string(),
        ),
    ]);
    let config = DisplayConfig::load(|name| missing_launcher.get(name).cloned());
    assert!(!config.enabled);
    assert_eq!(config.error, "display_launcher_missing");

    let missing_auth = HashMap::from([
        ("CONSOLE_DISPLAY_ENABLED".to_string(), "1".to_string()),
        ("CONSOLE_DISPLAY_NOVNC_ROOT".to_string(), root),
        ("CONSOLE_DISPLAY_LAUNCHER_FILE".to_string(), launcher),
        (
            "CONSOLE_DISPLAY_VNC_AUTH_FILE".to_string(),
            temp.path()
                .join("missing.passwd")
                .to_string_lossy()
                .to_string(),
        ),
    ]);
    let config = DisplayConfig::load(|name| missing_auth.get(name).cloned());
    assert!(!config.enabled);
    assert_eq!(config.error, "display_vnc_auth_missing");
}

#[test]
fn the_launcher_redirects_and_static_assets_are_served_with_security_headers() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets, 9);

    let redirect = get(&console, "/display", &[("X-Admin-Control", CONTROL_TOKEN)]);
    assert_eq!(redirect.status, 308);
    assert_eq!(redirect.header("Location"), Some("/display/"));
    assert_security_headers(&redirect);

    let launcher = get(&console, "/display/", &[("X-Admin-Control", CONTROL_TOKEN)]);
    assert_eq!(launcher.status, 200);
    assert_eq!(
        launcher.header("Content-Type"),
        Some("text/html; charset=utf-8")
    );
    assert_eq!(
        launcher.body,
        std::fs::read(&assets.launcher).expect("launcher bytes")
    );
    assert_security_headers(&launcher);

    let script = get(
        &console,
        "/display/launcher.js",
        &[("X-Admin-Control", CONTROL_TOKEN)],
    );
    assert_eq!(script.status, 200);
    assert_eq!(
        script.header("Content-Type"),
        Some("text/javascript; charset=utf-8")
    );
    assert_eq!(script.body, b"const launcher = 1;\n");
    assert_security_headers(&script);

    let state = get(
        &console,
        "/display/launcher-state.js",
        &[("X-Admin-Control", CONTROL_TOKEN)],
    );
    assert_eq!(state.status, 200);
    assert_eq!(
        state.header("Content-Type"),
        Some("text/javascript; charset=utf-8")
    );
    assert!(String::from_utf8_lossy(&state.body).contains("displayLauncherState"));

    let asset = get(
        &console,
        "/display/core/rfb.js",
        &[("X-Admin-Control", CONTROL_TOKEN)],
    );
    assert_eq!(asset.status, 200);
    assert_eq!(
        asset.header("Content-Type"),
        Some("text/javascript; charset=utf-8")
    );
    assert_eq!(asset.body, b"export default class RFB {}\n");
    assert_security_headers(&asset);
}

#[test]
fn static_assets_refuse_traversal_and_query_strings() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets, 9);
    std::fs::write(temp.path().join("secret"), b"secret").expect("secret");
    let headers = [("X-Admin-Control", CONTROL_TOKEN)];

    for target in [
        "/display/../secret",
        "/display/%2e%2e/secret",
        "/display//core/rfb.js",
        "/display/core/../../secret",
        "/display/package.json",
        "/display/core/missing.js",
    ] {
        let response = get(&console, target, &headers);
        assert_eq!(response.status, 404, "{target}");
        assert_eq!(response.json(), json!({"error": "unknown display asset"}));
    }

    for target in ["/display/?password=secret", "/display/launcher.js?x=1"] {
        let response = get(&console, target, &headers);
        assert_eq!(response.status, 400, "{target}");
        assert_eq!(
            response.json(),
            json!({"error": "display query not allowed"})
        );
    }
}

#[test]
fn display_root_requires_a_management_credential() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets, 9);

    assert_eq!(get(&console, "/display/", &[]).status, 403);
    assert_eq!(
        get(&console, "/display/", &[("Cookie", "usage_session=bad")]).status,
        403
    );
    assert_eq!(
        get(&console, "/display/", &[("Origin", "https://evil.test")]).status,
        403
    );
    assert_eq!(
        get(
            &console,
            "/display/",
            &[("Host", &format!("127.0.0.1:{}", console.addr.port() + 1))]
        )
        .status,
        403
    );
    assert_eq!(
        get(&console, "/display/", &[("X-Admin-Control", CONTROL_TOKEN)]).status,
        200
    );

    console
        .state()
        .session
        .issue("fixture-usage-session".to_string());
    assert_eq!(
        get(
            &console,
            "/display/",
            &[("Cookie", "usage_session=fixture-usage-session")]
        )
        .status,
        200
    );

    // Assets are same-origin only: no management credential is required.
    assert_eq!(get(&console, "/display/launcher.js", &[]).status, 200);
}

#[test]
fn disabled_or_broken_display_is_an_honest_503() {
    let temp = temp();
    let disabled = start_console_with(&temp, stopped_runtime(), |config| {
        config.display = DisplayConfig::default();
    });
    let response = get(
        &disabled,
        "/display/",
        &[("X-Admin-Control", CONTROL_TOKEN)],
    );
    assert_eq!(response.status, 503);
    // `_send_json(handler, 503, self.config.error or "display unavailable")`.
    assert_eq!(response.json(), json!({"error": "display unavailable"}));

    let broken = start_console_with(&temp, stopped_runtime(), |config| {
        config.display = DisplayConfig {
            error: "display_novnc_missing".to_string(),
            ..DisplayConfig::default()
        };
    });
    let response = get(&broken, "/display/", &[("X-Admin-Control", CONTROL_TOKEN)]);
    assert_eq!(response.status, 503);
    assert_eq!(response.json(), json!({"error": "display_novnc_missing"}));
}
