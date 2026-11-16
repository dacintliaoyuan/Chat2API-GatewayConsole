//! S7 AC6: no password, request header or frame content in logs/responses.
//!
//! The expected behavior is `display_proxy.py`'s "no request headers, frame
//! payloads, RFB bytes or credentials are logged" rule: every error is a fixed
//! code, the grant token never reaches a URL or a body, and the relay has no
//! logging call at all.

mod support;

use serde_json::json;
use um_test_harness::TempDataDir;

use support::display::*;
use support::*;

const CONTROL_TOKEN: &str = "secret-control-token";
const SESSION_SECRET: &str = "secret-usage-session";

fn start_display_console(temp: &TempDataDir, assets: &DisplayAssets, vnc_port: u16) -> Console {
    let control = temp.path().join("control-token");
    std::fs::write(&control, CONTROL_TOKEN).expect("control token");
    start_console_with(temp, stopped_runtime(), |config| {
        let mut display = display_config(assets, vnc_port);
        display.vnc_auth_file = temp.path().join("vnc.passwd").to_string_lossy().to_string();
        config.display = display;
        config.control_token_file = Some(control);
    })
}

fn password_file(temp: &TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("vnc.passwd");
    std::fs::write(&path, [0xdb, 0xd8, 0x3c, 0xfd, 0x72, 0x7a, 0x14, 0x58]).expect("password file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .expect("password file mode");
    }
    path
}

fn assert_no_secret(text: &str) {
    for secret in [
        "password",
        "secret-usage-session",
        "secret-control-token",
        "secret-grant-token",
        "0123456789abcdef",
        "um_display_session=",
    ] {
        assert!(!text.contains(secret), "{secret} leaked: {text}");
    }
}

#[test]
fn the_relay_source_has_no_logging_call() {
    let source = include_str!("../src/display_relay.rs");
    for forbidden in ["println!", "eprintln!", "dbg!", "log::", "tracing::"] {
        assert!(!source.contains(forbidden), "{forbidden} in display_relay");
    }
}

#[test]
fn a_failed_vnc_auth_body_carries_only_the_fixed_code() {
    let temp = temp();
    let assets = display_assets(&temp);
    let _passwd = password_file(&temp);
    let display = FakeDisplay::start(|mut stream| {
        use std::io::Write;
        let _ = stream.write_all(b"RFB 003.008\n");
        let _ = recv_exact(&mut stream, 12);
        let _ = stream.write_all(b"\x01\x02");
        let _ = recv_exact(&mut stream, 1);
        let _ = stream.write_all(&(0u8..16).collect::<Vec<u8>>());
        let _ = recv_exact(&mut stream, 16);
        let _ = stream.write_all(b"\x00\x00\x00\x01\x00\x00\x00\x1fpassword is not logged");
    });
    let console = start_display_console(&temp, &assets, display.port());
    let origin = format!("http://127.0.0.1:{}", console.addr.port());
    let (grant, _) = console
        .state()
        .display_grants
        .issue("control-token", &origin)
        .expect("grant");
    let session = console
        .state()
        .display_grants
        .redeem(&grant, &origin)
        .expect("display session");
    let generation =
        console
            .state()
            .display_grants
            .acquire_operator(Some(&session.token), &origin, "acquire")["generation"]
            .as_u64()
            .expect("generation");
    let target = format!("/display/websockify?operator={generation}");
    let cookie = format!(
        "um_display_session={}; usage_session={SESSION_SECRET}",
        session.token
    );
    let mut client = WsClient::connect(
        console.addr,
        &target,
        &[
            ("Cookie", &cookie),
            ("Origin", &origin),
            ("X-Admin-Control", CONTROL_TOKEN),
        ],
    )
    .expect("websocket");
    let head = client.head().to_string();
    let body = String::from_utf8_lossy(&client.read_frame_body()).to_string();
    assert_eq!(
        body,
        json!({"error": "display_vnc_auth_failed"}).to_string()
    );
    assert_no_secret(&head);
    assert_no_secret(&body);
    drop(client);
    display.join();
}

#[test]
fn a_refused_grant_exchange_never_echoes_the_grant_or_the_request_headers() {
    let temp = temp();
    let assets = display_assets(&temp);
    let console = start_display_console(&temp, &assets, 9);
    let origin = format!("http://127.0.0.1:{}", console.addr.port());
    let (grant, _) = console
        .state()
        .display_grants
        .issue("control-token", &origin)
        .expect("grant");
    // A wrong origin is refused; the body stays empty and names no token.
    let response = client_request(
        console.addr,
        &ClientRequest::post(
            "/display/grant",
            json!({"grant": grant}).to_string().into_bytes(),
        )
        .json_body(json!({"grant": grant}).to_string().into_bytes())
        // A wrong console port passes the generic Host gate but is refused by
        // the display origin gate before the body is read.
        .header("Host", &format!("127.0.0.1:{}", console.addr.port() + 1))
        .header("Cookie", &format!("um_display_session={SESSION_SECRET}"))
        .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("request");
    assert_eq!(response.status, 403, "{:?}", response.json());
    // `_send_json(handler, 403, "display access denied")` is a fixed code.
    assert_eq!(response.json(), json!({"error": "display access denied"}));
    for (name, value) in &response.headers {
        assert_no_secret(name);
        assert_no_secret(value);
    }

    // The same one-time grant still exchanges; the 204 body is empty and the
    // cookie carries the session token (not the grant) as its HttpOnly value.
    let response = client_request(
        console.addr,
        &ClientRequest::post(
            "/display/grant",
            json!({"grant": grant}).to_string().into_bytes(),
        )
        .json_body(json!({"grant": grant}).to_string().into_bytes())
        .header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(response.status, 204);
    assert!(response.body.is_empty());
    let cookie = response.header("Set-Cookie").expect("cookie").to_string();
    assert!(!cookie.contains(&grant));
    assert!(!String::from_utf8_lossy(&response.body).contains(SESSION_SECRET));

    // The display-session read reports only the authorization and expiry.
    let token = cookie
        .split(';')
        .next()
        .expect("cookie")
        .trim_start_matches("um_display_session=");
    let response = client_request(
        console.addr,
        &ClientRequest::get("/display/session")
            .header("Cookie", &cookie)
            .header("Origin", &origin),
    )
    .expect("request");
    assert_eq!(response.status, 200);
    let payload = response.json().to_string();
    assert!(!payload.contains(token));
    assert!(!payload.contains(&grant));
}
