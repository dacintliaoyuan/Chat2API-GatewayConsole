//! S7 AC4: `/display/websockify` WebSocket relay and operator generation.
//!
//! The expected behavior is `display_proxy.py::DisplayProxy.handle_get` and
//! `_serve_websocket` (`display_proxy.py:1201-1271,1457-1538`) plus
//! `WebSocketRelay` (`display_proxy.py:1674-1978`): only a live display
//! session with the current operator generation may open the relay; the relay
//! forwards binary RFB frames, answers pings, refuses text frames with 1003,
//! drops keyboard/pointer input from a superseded generation, and releases only
//! its own generation. The upstream is a fake echo display on an ephemeral
//! loopback port, never the real display on 8097.

mod support;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::time::{Duration, Instant};

use serde_json::json;
use um_console::display_grant::DisplaySession;
use um_test_harness::TempDataDir;

use support::display::*;
use support::*;

fn echo_display() -> FakeDisplay {
    FakeDisplay::start(|mut stream| {
        let mut buffer = [0u8; 4096];
        loop {
            match stream.read(&mut buffer) {
                Ok(0) => return,
                Ok(read) if &buffer[..read] == b"quit" => return,
                Ok(read) => {
                    if stream.write_all(&buffer[..read]).is_err() {
                        return;
                    }
                }
                Err(_) => return,
            }
        }
    })
}

fn start_relay_console(
    temp: &TempDataDir,
    assets: &DisplayAssets,
    vnc_port: u16,
    max_connections: usize,
) -> Console {
    let control = temp.path().join("control-token");
    std::fs::write(&control, "fixture-control-token").expect("control token");
    start_console_with(temp, stopped_runtime(), |config| {
        let mut display = display_config(assets, vnc_port);
        display.max_connections = max_connections;
        config.display = display;
        config.control_token_file = Some(control);
    })
}

fn origin(console: &Console) -> String {
    format!("http://127.0.0.1:{}", console.addr.port())
}

fn display_session(console: &Console) -> DisplaySession {
    let origin = origin(console);
    let (token, _) = console
        .state()
        .display_grants
        .issue("control-token", &origin)
        .expect("grant");
    console
        .state()
        .display_grants
        .redeem(&token, &origin)
        .expect("display session")
}

fn acquire(console: &Console, session: &DisplaySession) -> u64 {
    let origin = origin(console);
    let result =
        console
            .state()
            .display_grants
            .acquire_operator(Some(&session.token), &origin, "acquire");
    assert_eq!(result["acquired"], json!(true), "{result}");
    result["generation"].as_u64().expect("generation")
}

fn ws_headers(session: &DisplaySession) -> Vec<(String, String)> {
    vec![
        (
            "Cookie".to_string(),
            format!("um_display_session={}", session.token),
        ),
        ("Origin".to_string(), origin_of(session)),
    ]
}

fn origin_of(session: &DisplaySession) -> String {
    session.origin.clone()
}

fn connect(console: &Console, session: &DisplaySession, generation: u64) -> WsClient {
    let mut headers = ws_headers(session);
    let target = format!("/display/websockify?operator={generation}");
    let borrowed: Vec<(&str, &str)> = headers
        .iter_mut()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    WsClient::connect(console.addr, &target, &borrowed).expect("websocket")
}

fn connect_with(
    console: &Console,
    session: &DisplaySession,
    target: &str,
    extra: &[(&str, &str)],
) -> WsClient {
    let mut headers: Vec<(String, String)> = ws_headers(session);
    for (name, value) in extra {
        headers.push((name.to_string(), value.to_string()));
    }
    let borrowed: Vec<(&str, &str)> = headers
        .iter()
        .map(|(name, value)| (name.as_str(), value.as_str()))
        .collect();
    WsClient::connect(console.addr, target, &borrowed).expect("websocket")
}

fn wait_for_connections(console: &Console, expected: usize) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if console.state().display.active_connections() == expected {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!(
        "display relay has {} connections, expected {expected}",
        console.state().display.active_connections()
    );
}

fn wait_for_idle(console: &Console) {
    wait_for_connections(console, 0);
}

#[test]
fn a_binary_relay_answers_ping_and_closes_with_the_client_code() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = echo_display();
    let console = start_relay_console(&temp, &assets, display.port(), 1);
    let session = display_session(&console);
    let generation = acquire(&console, &session);

    let mut client = connect(&console, &session, generation);
    let head = client.head();
    assert!(head.starts_with("HTTP/1.1 101"), "{head}");
    assert!(head.contains("Upgrade: websocket"));
    assert!(head.contains("Connection: Upgrade"));
    assert!(head.contains("Sec-WebSocket-Accept: "));
    assert!(head.contains("Sec-WebSocket-Protocol: binary"));
    assert!(head.contains("Cache-Control: no-store"));

    client.send(0x02, b"hello");
    assert_eq!(
        client.read_frame().expect("binary echo"),
        (0x02, b"hello".to_vec())
    );

    client.send(0x09, b"p");
    assert_eq!(client.read_frame().expect("pong"), (0x0A, b"p".to_vec()));

    client.send(0x08, &1000u16.to_be_bytes());
    let (opcode, payload) = client.read_frame().expect("close");
    assert_eq!(opcode, 0x08);
    assert_eq!(u16::from_be_bytes([payload[0], payload[1]]), 1000);
    drop(client);
    wait_for_idle(&console);
    display.join();
}

#[test]
fn a_second_connection_is_busy_until_the_first_is_released() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = echo_display();
    let console = start_relay_console(&temp, &assets, display.port(), 1);
    let session = display_session(&console);
    let generation = acquire(&console, &session);

    let first = connect(&console, &session, generation);
    assert!(first.head().starts_with("HTTP/1.1 101"));

    let second = connect(&console, &session, generation);
    assert!(
        second.head().starts_with("HTTP/1.1 503"),
        "{}",
        second.head()
    );
    assert_eq!(console.state().display.active_connections(), 1);
    drop(second);
    drop(first);
    wait_for_idle(&console);
    display.join();
}

#[test]
fn an_unavailable_upstream_is_502() {
    let temp = temp();
    let assets = display_assets(&temp);
    let unused = unused_loopback_port();
    let console = start_relay_console(&temp, &assets, unused, 1);
    let session = display_session(&console);
    let generation = acquire(&console, &session);

    let client = connect(&console, &session, generation);
    assert!(
        client.head().starts_with("HTTP/1.1 502"),
        "{}",
        client.head()
    );
    assert_eq!(console.state().display.active_connections(), 0);
}

#[test]
fn bad_handshakes_and_missing_credentials_are_refused() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = echo_display();
    let console = start_relay_console(&temp, &assets, display.port(), 2);
    let session = display_session(&console);
    let generation = acquire(&console, &session);

    for (extra, expected) in [
        (&[("Origin", "https://evil.test")][..], "403"),
        (&[("Sec-WebSocket-Version", "8")], "426"),
        (&[("Sec-WebSocket-Key", "short")], "400"),
        (&[("Sec-WebSocket-Protocol", "chat")], "400"),
    ] {
        let target = format!("/display/websockify?operator={generation}");
        let client = connect_with(&console, &session, &target, extra);
        assert!(
            client.head().starts_with(&format!("HTTP/1.1 {expected}")),
            "{extra:?}: {}",
            client.head()
        );
    }

    // A console session without a display session cannot operate.
    let token = console.state().session.bootstrap_code();
    let response = client_request(
        console.addr,
        &ClientRequest::get(&format!("/display/websockify?operator={generation}"))
            .header("Cookie", &format!("usage_session={token}"))
            .header("Origin", &origin(&console)),
    )
    .expect("request");
    assert_eq!(response.status, 403);
    display.join();
}

#[test]
fn the_query_only_accepts_the_canonical_operator_generation() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = echo_display();
    let console = start_relay_console(&temp, &assets, display.port(), 2);
    let session = display_session(&console);
    let generation = acquire(&console, &session);

    for target in [
        format!("/display/websockify?operator={generation}&path=x"),
        format!("/display/websockify?operator={generation}&operator={generation}"),
        "/display/websockify?operator=abc".to_string(),
        "/display/websockify?operator=0".to_string(),
        "/display/websockify?operator=01".to_string(),
        "/display/websockify?password=x".to_string(),
        format!("/display/launcher.js?operator={generation}"),
    ] {
        let client = connect_with(&console, &session, &target, &[]);
        assert!(
            client.head().starts_with("HTTP/1.1 400"),
            "{target}: {}",
            client.head()
        );
    }
    display.join();
}

#[test]
fn the_relay_requires_the_current_operator_generation() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = echo_display();
    let console = start_relay_console(&temp, &assets, display.port(), 2);
    let session = display_session(&console);

    // No operator query at all: 409 display_operator_stale.
    let no_query = connect_with(&console, &session, "/display/websockify", &[]);
    assert!(
        no_query.head().starts_with("HTTP/1.1 409"),
        "{}",
        no_query.head()
    );

    let generation = acquire(&console, &session);
    let stale = connect(&console, &session, generation + 1);
    assert!(stale.head().starts_with("HTTP/1.1 409"));

    // A forged Tailscale identity without an assembled policy is 403.
    let forged = connect_with(
        &console,
        &session,
        &format!("/display/websockify?operator={generation}"),
        &[
            ("Tailscale-User-Login", "owner@example.com"),
            ("X-Forwarded-For", "100.127.119.98"),
        ],
    );
    assert!(forged.head().starts_with("HTTP/1.1 403"));

    let accepted = connect(&console, &session, generation);
    assert!(accepted.head().starts_with("HTTP/1.1 101"));
    drop(accepted);
    wait_for_idle(&console);
    display.join();
}

#[test]
fn an_old_generation_cannot_operate_but_keeps_viewing() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = echo_display();
    let console = start_relay_console(&temp, &assets, display.port(), 2);
    let first = display_session(&console);
    let second = display_session(&console);
    let origin = origin(&console);
    let first_generation =
        console
            .state()
            .display_grants
            .acquire_operator(Some(&first.token), &origin, "acquire")["generation"]
            .as_u64()
            .expect("generation");
    let mut old_page = connect(&console, &first, first_generation);
    assert!(old_page.head().starts_with("HTTP/1.1 101"));

    let taken =
        console
            .state()
            .display_grants
            .acquire_operator(Some(&second.token), &origin, "takeover");
    assert_eq!(taken["took_over"], json!(true));
    let new_generation = taken["generation"].as_u64().expect("generation");
    let mut new_page = connect(&console, &second, new_generation);
    assert!(new_page.head().starts_with("HTTP/1.1 101"));

    // Keyboard input from the old page is dropped.
    old_page
        .stream
        .set_read_timeout(Some(Duration::from_millis(500)))
        .ok();
    old_page.send(0x02, b"\x04\x01\x00\x00\x00\x00\x00a");
    assert!(
        old_page.read_frame().is_err(),
        "old generation input forwarded"
    );

    // Viewing (FramebufferUpdateRequest, type 3) is not input.
    old_page
        .stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .ok();
    old_page.send(0x02, b"\x03\x00\x00\x00\x00\x00\x00\x00\x00");
    assert_eq!(
        old_page.read_frame().expect("viewer frame"),
        (0x02, b"\x03\x00\x00\x00\x00\x00\x00\x00\x00".to_vec())
    );

    new_page.send(0x02, b"\x04\x01\x00\x00\x00\x00\x00a");
    assert_eq!(
        new_page.read_frame().expect("new owner input"),
        (0x02, b"\x04\x01\x00\x00\x00\x00\x00a".to_vec())
    );

    // Closing the old page releases only its own generation.
    drop(old_page);
    wait_for_connections(&console, 1);
    assert_eq!(
        console
            .state()
            .display_grants
            .operator_state(Some(&second.token), &origin)["owned_by_me"],
        json!(true)
    );
    drop(new_page);
    wait_for_idle(&console);
    assert_eq!(
        console
            .state()
            .display_grants
            .operator_state(Some(&second.token), &origin)["owner_present"],
        json!(false)
    );
    display.join();
}

#[test]
fn a_text_frame_closes_with_1003() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = echo_display();
    let console = start_relay_console(&temp, &assets, display.port(), 1);
    let session = display_session(&console);
    let generation = acquire(&console, &session);

    let mut client = connect(&console, &session, generation);
    client.send(0x01, b"text");
    let (opcode, payload) = client.read_frame().expect("close frame");
    assert_eq!(opcode, 0x08);
    assert_eq!(u16::from_be_bytes([payload[0], payload[1]]), 1003);
    drop(client);
    wait_for_idle(&console);
    display.join();
}

#[test]
fn the_handshake_accept_key_matches_rfc_6455() {
    assert_eq!(
        um_console::display_relay::accept_key_for_tests("dGhlIHNhbXBsZSBub25jZQ=="),
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    );
}

fn unused_loopback_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind unused port");
    let port = listener.local_addr().expect("unused addr").port();
    assert!(!GOVERNED_PORTS.contains(&port));
    drop(listener);
    port
}
