//! Explicit real-Edge regression: an isolated persistent profile must keep
//! cookies across the production browser adapter's close/reopen boundary.
#![cfg(windows)]

mod support;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use um_gateway::login::{CredentialField, LaunchRequest, LoginBrowserFactory, SystemBrowserFactory};

#[test]
#[ignore = "requires installed Edge; only uses an isolated profile and loopback fixture"]
fn real_edge_persists_cookie_across_close_and_reopen() {
    let temp = support::TempDir::new("um-rust-browser-persistence-");
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let stopped = Arc::new(AtomicBool::new(false));
    let stop_server = stopped.clone();
    let server = thread::spawn(move || {
        while !stop_server.load(Ordering::SeqCst) {
            let (mut stream, _) = match listener.accept() {
                Ok(connection) => connection,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(10));
                    continue;
                }
                Err(error) => panic!("loopback fixture: {error}"),
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut raw = Vec::new();
            let mut buffer = [0u8; 1024];
            while raw.len() < 16384 {
                match stream.read(&mut buffer) {
                    Ok(0) | Err(_) => break,
                    Ok(length) => raw.extend_from_slice(&buffer[..length]),
                }
                if raw.windows(4).any(|part| part == b"\r\n\r\n") {
                    break;
                }
            }
            let request = String::from_utf8_lossy(&raw);
            let seed = request.starts_with("GET /seed ");
            let restored = request.lines().any(|line| {
                line.to_ascii_lowercase().starts_with("cookie:")
                    && line.contains("um_fixture=retained")
            });
            let script = if !seed && restored {
                "localStorage.setItem('token','cookie-restored')"
            } else {
                "localStorage.removeItem('token')"
            };
            let body = format!("<!doctype html><script>{script}</script>");
            let cookie = if seed {
                "Set-Cookie: um_fixture=retained; Max-Age=86400; Path=/; SameSite=Lax\r\n"
            } else {
                ""
            };
            let response = format!("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n{cookie}Content-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
            let _ = stream.write_all(response.as_bytes());
        }
    });

    let factory = SystemBrowserFactory;
    let request = LaunchRequest {
        executable: factory.resolve_browser().expect("installed Edge"),
        profile_dir: temp.path().join("isolated-profile"),
        headful: false,
        display: None,
        initial_url: "about:blank".into(),
    };
    let mut first = factory.launch(&request).expect("first browser");
    first.page().goto(&format!("{base}/seed")).unwrap();
    first.close().expect("first close");
    drop(first);

    let mut second = factory.launch(&request).expect("reopened browser");
    second.page().goto(&format!("{base}/check")).unwrap();
    let restored = second.page().read_token();
    second.close().expect("second close");
    drop(second);
    stopped.store(true, Ordering::SeqCst);
    server.join().unwrap();
    assert_eq!(
        restored.as_deref(),
        Some("cookie-restored"),
        "persistent cookie was lost across browser close"
    );
}

#[test]
#[ignore = "requires installed Edge; uses only fixture form values and an isolated profile"]
fn real_edge_replaces_credentials_and_submits_without_spurious_input_events() {
    let temp = support::TempDir::new("um-rust-browser-form-");
    let factory = SystemBrowserFactory;
    let mut browser = factory
        .launch(&LaunchRequest {
            executable: factory.resolve_browser().expect("installed Edge"),
            profile_dir: temp.path().join("isolated-profile"),
            headful: false,
            display: None,
            initial_url: "about:blank".into(),
        })
        .expect("isolated browser");
    let html = r#"
        <!doctype html><meta charset="utf-8">
        <form>
          <input id="account" value="old-fixture-account">
          <input id="password" type="password" value="old-fixture-password">
          <input id="empty">
          <input class="duplicate" value="first"><input class="duplicate" value="second">
          <input id="locked" readonly value="read-only">
          <button id="submit">Sign in</button>
        </form>
        <p id="accepted" hidden>accepted</p>
        <p id="invalid-input" hidden>unexpected empty or synthetic input</p>
        <script>
          const account = document.querySelector('#account');
          const password = document.querySelector('#password');
          let events = 0;
          let submits = 0;
          for (const field of [account, password]) {
            field.addEventListener('input', event => {
              events++;
              if (!event.isTrusted || !field.value) {
                document.querySelector('#invalid-input').hidden = false;
              }
            });
          }
          document.querySelector('form').addEventListener('submit', event => {
            event.preventDefault();
            submits++;
            const duplicates = Array.from(document.querySelectorAll('.duplicate'));
            document.querySelector('#accepted').hidden = !(
              events === 2 && submits === 1 &&
              account.value === 'fixture-account' &&
              password.value === 'fixture-password' &&
              duplicates.map(field => field.value).join(',') === 'first,second' &&
              document.querySelector('#locked').value === 'read-only'
            );
          });
        </script>"#;
    let encoded = html
        .bytes()
        .map(|byte| format!("%{byte:02X}"))
        .collect::<String>();
    let page = browser.page();
    page.goto(&format!("data:text/html,{encoded}")).unwrap();
    assert_eq!(page.credential_field(&["#account"]), CredentialField::Filled);
    assert_eq!(page.credential_field(&["#empty"]), CredentialField::Empty);
    assert_eq!(page.credential_field(&["#absent"]), CredentialField::Missing);
    assert_eq!(
        page.credential_field(&[".duplicate"]),
        CredentialField::Ambiguous
    );
    assert!(page.fill(&[".duplicate"], "changed").is_err());
    assert!(page.fill(&["#locked"], "changed").is_err());
    page.fill(&["#account"], "fixture-account").unwrap();
    page.fill(&["#password"], "fixture-password").unwrap();
    let clicked = page
        .one_visible(&["#submit"])
        .expect("single submit button");
    page.click(&clicked).unwrap();
    assert!(
        !page.any_visible(&["#invalid-input"]),
        "filling triggered unexpected input events"
    );
    assert!(
        page.any_visible(&["#accepted"]),
        "the form did not accept exactly one submission"
    );
    browser.close().expect("close isolated browser");
}
