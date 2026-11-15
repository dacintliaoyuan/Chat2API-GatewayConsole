//! S7 AC5: RFB 3.8/3.7/3.3 handshake and VNC challenge-response.
//!
//! The expected behavior is `display_proxy.py::perform_vnc_auth` and the pure
//! DES helpers (`display_proxy.py:601-948`): the console proxy completes the
//! upstream security handshake with x11vnc and hands the browser an RFB 3.8 None
//! greeting; the password never enters the browser, a URL or a log. The upstream
//! is a fake x11vnc/RFB server on an ephemeral loopback port, never 8097.

mod support;

use std::io::Write;

use serde_json::json;
use um_console::display_relay::{
    des_encrypt_block_for_tests, load_vnc_password_file_for_tests, vnc_auth_response_for_tests,
    vnc_password_key_for_tests,
};
use um_test_harness::TempDataDir;

use support::display::*;
use support::*;

/// The x11vnc `-storepasswd` file for `password`, from the Python test.
const STOREPASSWD: [u8; 8] = [0xdb, 0xd8, 0x3c, 0xfd, 0x72, 0x7a, 0x14, 0x58];
/// The Python d3des reference response for the challenge `bytes(range(16))`.
const RESPONSE: [u8; 16] = [
    0xb8, 0x66, 0x92, 0x41, 0x25, 0xc8, 0xee, 0xbb, 0x9d, 0xeb, 0xc1, 0xdb, 0x61, 0xc5, 0x38, 0xe2,
];
const SERVER_INIT: [u8; 24] = [
    0x00, 0x05, 0x00, 0x03, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
];

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn password_file(temp: &TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("vnc.passwd");
    std::fs::write(&path, STOREPASSWD).expect("password file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
            .expect("password file mode");
    }
    path
}

fn start_rfb_console(
    temp: &TempDataDir,
    assets: &DisplayAssets,
    vnc_port: u16,
    password: Option<&std::path::Path>,
) -> Console {
    let control = temp.path().join("control-token");
    std::fs::write(&control, "fixture-control-token").expect("control token");
    let password = password
        .map(std::path::Path::to_path_buf)
        .unwrap_or_else(|| password_file(temp));
    start_console_with(temp, stopped_runtime(), |config| {
        let mut display = display_config(assets, vnc_port);
        display.vnc_auth_file = password.to_string_lossy().to_string();
        config.display = display;
        config.control_token_file = Some(control);
    })
}

fn connect_operator(console: &Console) -> WsClient {
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
    WsClient::connect(
        console.addr,
        &format!("/display/websockify?operator={generation}"),
        &[
            ("Cookie", &format!("um_display_session={}", session.token)),
            ("Origin", &origin),
        ],
    )
    .expect("websocket")
}

fn read_greeting(client: &mut WsClient) -> (Vec<u8>, Vec<u8>) {
    let (opcode, version) = client.read_frame().expect("version frame");
    assert_eq!(opcode, 0x02);
    assert_eq!(version.len(), 12, "noVNC reads the version alone");
    let mut rest = Vec::new();
    for _ in 0..3 {
        let (opcode, payload) = client.read_frame().expect("greeting frame");
        assert_eq!(opcode, 0x02);
        rest.extend_from_slice(&payload);
    }
    (version, rest)
}

#[test]
fn the_des_and_vnc_vectors_match_python() {
    assert_eq!(
        hex(&des_encrypt_block_for_tests(
            [0x4e, 0x6f, 0x77, 0x20, 0x69, 0x73, 0x20, 0x74],
            [0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef],
        )),
        "3fa40e8a984d4815"
    );
    assert_eq!(
        hex(&vnc_auth_response_for_tests(
            &[0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef,],
            &[
                0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0x01, 0x23, 0x45, 0x67, 0x89, 0xab,
                0xcd, 0xef,
            ],
        )
        .expect("response")),
        "6e09a37726dd560c6e09a37726dd560c"
    );
    assert_eq!(
        vnc_password_key_for_tests(&STOREPASSWD).expect("password"),
        *b"password"
    );
    assert_eq!(
        hex(
            &vnc_auth_response_for_tests(b"password", &(0u8..16).collect::<Vec<u8>>())
                .expect("response")
        ),
        "b866924125c8eebb9debc1db61c538e2"
    );
}

#[test]
fn a_bad_password_file_fails_closed() {
    let temp = temp();
    let short = temp.path().join("short.passwd");
    std::fs::write(&short, b"short").expect("short file");
    assert!(load_vnc_password_file_for_tests(&short).is_err());

    let missing = temp.path().join("missing.passwd");
    assert!(load_vnc_password_file_for_tests(&missing).is_err());
}

#[cfg(unix)]
#[test]
fn a_loose_password_file_is_refused_on_posix() {
    use std::os::unix::fs::PermissionsExt;
    let temp = temp();
    let path = temp.path().join("loose.passwd");
    std::fs::write(&path, STOREPASSWD).expect("password file");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).expect("loose mode");
    assert!(load_vnc_password_file_for_tests(&path).is_err());
}

#[test]
fn rfb_38_vnc_auth_completes_upstream_and_hides_the_password() {
    let temp = temp();
    let assets = display_assets(&temp);
    let passwd = password_file(&temp);
    let display = FakeDisplay::start(|mut stream| {
        stream.write_all(b"RFB 003.008\n").expect("version");
        assert_eq!(
            recv_exact(&mut stream, 12).expect("client version"),
            b"RFB 003.008\n"
        );
        stream.write_all(b"\x01\x02").expect("security types");
        assert_eq!(recv_exact(&mut stream, 1).expect("security type"), b"\x02");
        stream
            .write_all(&(0u8..16).collect::<Vec<u8>>())
            .expect("challenge");
        assert_eq!(recv_exact(&mut stream, 16).expect("response"), RESPONSE);
        stream.write_all(b"\x00\x00\x00\x00").expect("result");
        assert_eq!(recv_exact(&mut stream, 1).expect("client init"), b"\x01");
        stream.write_all(&SERVER_INIT).expect("server init");
        let message = recv_exact(&mut stream, 9).expect("relayed message");
        assert_eq!(message, b"\x03\x00\x00\x00\x00\x00\x00\x00\x00");
        stream.write_all(&message).expect("echo");
    });
    let console = start_rfb_console(&temp, &assets, display.port(), Some(&passwd));
    let mut client = connect_operator(&console);
    assert!(
        client.head().starts_with("HTTP/1.1 101"),
        "{}",
        client.head()
    );

    let (version, rest) = read_greeting(&mut client);
    assert_eq!(version, b"RFB 003.008\n");
    let mut expected = vec![0x01, 0x01, 0x00, 0x00, 0x00, 0x00];
    expected.extend_from_slice(&SERVER_INIT);
    assert_eq!(rest, expected);
    assert!(!hex(&version).contains("70617373776f7264"));

    // noVNC's own version, security type and ClientInit are discarded, then
    // the first real RFB message reaches x11vnc.
    let mut client_prefix = b"RFB 003.008\n".to_vec();
    client_prefix.push(0x01);
    client_prefix.push(0x01);
    client.send(0x02, &client_prefix);
    client.send(0x02, b"\x03\x00\x00\x00\x00\x00\x00\x00\x00");
    assert_eq!(
        client.read_frame().expect("echo"),
        (0x02, b"\x03\x00\x00\x00\x00\x00\x00\x00\x00".to_vec())
    );
    drop(client);
    display.join();
}

#[test]
fn rfb_37_and_33_none_handshakes_are_accepted() {
    for (version, response) in [
        (b"RFB 003.007\n".to_vec(), b"RFB 003.007\n".to_vec()),
        (b"RFB 003.003\n".to_vec(), b"RFB 003.003\n".to_vec()),
    ] {
        let temp = temp();
        let assets = display_assets(&temp);
        let expected_version = response.clone();
        let display = FakeDisplay::start(move |mut stream| {
            stream.write_all(&version).expect("version");
            assert_eq!(
                recv_exact(&mut stream, 12).expect("client version"),
                expected_version
            );
            if version == b"RFB 003.007\n" {
                stream.write_all(b"\x01\x01").expect("security types");
                stream.write_all(b"\x00\x00\x00\x00").expect("result");
            } else {
                stream
                    .write_all(b"\x00\x00\x00\x01")
                    .expect("security type");
            }
            assert_eq!(recv_exact(&mut stream, 1).expect("client init"), b"\x01");
            stream.write_all(&SERVER_INIT).expect("server init");
        });
        let console = start_rfb_console(&temp, &assets, display.port(), None);
        let mut client = connect_operator(&console);
        assert!(
            client.head().starts_with("HTTP/1.1 101"),
            "{}",
            client.head()
        );
        let (greeting, rest) = read_greeting(&mut client);
        assert_eq!(greeting, b"RFB 003.008\n");
        let mut expected = vec![0x01, 0x01, 0x00, 0x00, 0x00, 0x00];
        expected.extend_from_slice(&SERVER_INIT);
        assert_eq!(rest, expected);
        drop(client);
        display.join();
    }
}

#[test]
fn an_upstream_auth_failure_is_502_without_upgrade() {
    let temp = temp();
    let assets = display_assets(&temp);
    let passwd = password_file(&temp);
    let display = FakeDisplay::start(|mut stream| {
        stream.write_all(b"RFB 003.008\n").expect("version");
        let _ = recv_exact(&mut stream, 12).expect("client version");
        stream.write_all(b"\x01\x02").expect("security types");
        let _ = recv_exact(&mut stream, 1).expect("security type");
        stream
            .write_all(&(0u8..16).collect::<Vec<u8>>())
            .expect("challenge");
        let _ = recv_exact(&mut stream, 16).expect("response");
        stream
            .write_all(b"\x00\x00\x00\x01\x00\x00\x00\x04fail")
            .expect("failure");
    });
    let console = start_rfb_console(&temp, &assets, display.port(), Some(&passwd));
    let client = connect_operator(&console);
    assert!(
        client.head().starts_with("HTTP/1.1 502"),
        "{}",
        client.head()
    );
    assert!(
        !client.head().contains("101"),
        "a failed auth never upgrades"
    );
    display.join();
}

#[test]
fn a_missing_auth_source_or_a_bad_protocol_is_502() {
    let temp = temp();
    let assets = display_assets(&temp);
    let unused = unused_loopback_port();
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        let mut display = display_config(&assets, unused);
        display.vnc_auth_socket = "/run/um-share/missing.sock".to_string();
        config.display = display;
    });
    let client = connect_operator(&console);
    assert!(
        client.head().starts_with("HTTP/1.1 502"),
        "{}",
        client.head()
    );

    let bad_version = FakeDisplay::start(|mut stream| {
        stream.write_all(b"RFB 004.001\n").expect("version");
        let _ = recv_exact(&mut stream, 12);
    });
    let console = start_rfb_console(&temp, &assets, bad_version.port(), None);
    let client = connect_operator(&console);
    assert!(
        client.head().starts_with("HTTP/1.1 502"),
        "{}",
        client.head()
    );
    bad_version.join();
}

#[test]
fn an_unsupported_security_type_is_502() {
    let temp = temp();
    let assets = display_assets(&temp);
    let display = FakeDisplay::start(|mut stream| {
        stream.write_all(b"RFB 003.008\n").expect("version");
        let _ = recv_exact(&mut stream, 12).expect("client version");
        stream.write_all(b"\x01\x03").expect("security types");
        let _ = recv_exact(&mut stream, 1);
    });
    let console = start_rfb_console(&temp, &assets, display.port(), None);
    let mut client = connect_operator(&console);
    assert!(
        client.head().starts_with("HTTP/1.1 502"),
        "{}",
        client.head()
    );
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&client.read_frame_body()).expect("error body"),
        json!({"error": "display_vnc_auth_unsupported"})
    );
    display.join();
}

fn unused_loopback_port() -> u16 {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").expect("bind unused");
    let port = listener.local_addr().expect("addr").port();
    assert!(!GOVERNED_PORTS.contains(&port));
    drop(listener);
    port
}

#[cfg(unix)]
#[test]
fn the_helper_socket_supplies_the_challenge_response() {
    use std::os::unix::net::UnixListener;
    let temp = temp();
    let socket_path = temp.path().join("vnc-auth.sock");
    let listener = UnixListener::bind(&socket_path).expect("auth socket");
    let helper = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("helper accept");
        assert_eq!(
            recv_exact(&mut stream, 16).expect("challenge"),
            (0u8..16).collect::<Vec<u8>>()
        );
        stream.write_all(&RESPONSE).expect("response");
    });
    let config = um_console::display_relay::DisplayConfig {
        enabled: true,
        vnc_host: "127.0.0.1".to_string(),
        vnc_port: 9,
        novnc_root: temp.path().to_path_buf(),
        launcher_file: temp.path().join("launcher.html"),
        vnc_auth_socket: socket_path.to_string_lossy().to_string(),
        ..DisplayConfig::default()
    };
    let relay = um_console::display_relay::DisplayRelay::new(config);
    let challenge: [u8; 16] = (0u8..16).collect::<Vec<u8>>().try_into().unwrap();
    assert_eq!(
        relay
            .auth_response_for_tests(&challenge)
            .expect("helper response"),
        RESPONSE
    );
    helper.join().expect("helper");
}
