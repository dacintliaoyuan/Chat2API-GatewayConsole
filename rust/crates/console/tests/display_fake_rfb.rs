//! S7 AC7: the tests use a fake x11vnc/RFB server, never the real display.
//!
//! `console/display_proxy.py` targets x11vnc on 8097, but every Rust display
//! test binds its own fake listener on an ephemeral loopback port and points the
//! console at that port. This test pins that boundary: the fake listener refuses
//! a governed port, the relay dials exactly the fake listener, and no display
//! test file configures the production 8097 port.

mod support;

use std::sync::mpsc;
use std::time::Duration;

use um_console::display_relay::DisplayConfig;
use um_test_harness::TempDataDir;

use support::display::*;
use support::*;

const DISPLAY_TEST_SOURCES: [(&str, &str); 7] = [
    ("display_static.rs", include_str!("display_static.rs")),
    (
        "display_session_grant.rs",
        include_str!("display_session_grant.rs"),
    ),
    (
        "display_grant_exchange.rs",
        include_str!("display_grant_exchange.rs"),
    ),
    ("display_websocket.rs", include_str!("display_websocket.rs")),
    ("display_rfb.rs", include_str!("display_rfb.rs")),
    ("display_redaction.rs", include_str!("display_redaction.rs")),
    ("support/display.rs", include_str!("support/display.rs")),
];

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

#[test]
fn the_relay_dials_only_the_fake_x11vnc_listener() {
    // The production contract target stays 8097; the fake listener never binds it.
    assert_eq!(DisplayConfig::default().vnc_port, 8097);
    assert!(GOVERNED_PORTS.contains(&8097));

    let temp = temp();
    let assets = display_assets(&temp);
    let passwd = password_file(&temp);
    let (sender, receiver) = mpsc::channel();
    let display = FakeDisplay::start(move |mut stream| {
        sender
            .send(stream.peer_addr().expect("fake display peer"))
            .ok();
        use std::io::Write;
        let _ = stream.write_all(b"RFB 003.008\n");
        let _ = recv_exact(&mut stream, 12);
        let _ = stream.write_all(b"\x01\x01");
        let _ = stream.write_all(b"\x00\x00\x00\x00");
        let _ = recv_exact(&mut stream, 1);
        let _ = stream.write_all(&[0u8; 24]);
        // Hold the fake display open until the test closes the viewer.
        let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
        let mut buffer = [0u8; 64];
        let _ = std::io::Read::read(&mut stream, &mut buffer);
    });
    assert!(!GOVERNED_PORTS.contains(&display.port()));
    assert_ne!(display.port(), 8097);

    let control = temp.path().join("control-token");
    std::fs::write(&control, "fixture-control-token").expect("control token");
    let vnc_port = display.port();
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        let mut display = display_config(&assets, vnc_port);
        display.vnc_auth_file = passwd.to_string_lossy().to_string();
        config.display = display;
        config.control_token_file = Some(control);
    });
    // The console dials the fake listener, never the real 8097 display.
    assert_eq!(console.state().display.config().vnc_port, display.port());

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
    let mut client = WsClient::connect(
        console.addr,
        &format!("/display/websockify?operator={generation}"),
        &[
            ("Cookie", &format!("um_display_session={}", session.token)),
            ("Origin", &origin),
        ],
    )
    .expect("websocket");
    assert!(
        client.head().starts_with("HTTP/1.1 101"),
        "{}",
        client.head()
    );
    let _ = client.read_frame().expect("version frame");
    let peer = receiver
        .recv_timeout(Duration::from_secs(3))
        .expect("fake display connection");
    assert!(peer.ip().is_loopback());
    assert!(!GOVERNED_PORTS.contains(&peer.port()));
    drop(client);
    display.join();
}

#[test]
fn no_display_test_configures_the_production_vnc_port() {
    for (name, source) in DISPLAY_TEST_SOURCES {
        assert!(
            !source.contains("vnc_port: 8097"),
            "{name} points at the real display"
        );
    }
}
