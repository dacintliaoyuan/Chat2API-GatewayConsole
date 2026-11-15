#![allow(dead_code)]

//! Shared fake display surface for the S7 tests.
//!
//! Every helper binds `127.0.0.1:0` (or uses a caller-supplied loopback
//! ephemeral port); a fake x11vnc/RFB server is never the real display on
//! 8097 and the tests never connect to it.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};
use std::time::Duration;

use um_console::display_relay::DisplayConfig;
use um_test_harness::TempDataDir;

/// Ports a fake display server must never bind or connect to.
pub const GOVERNED_PORTS: [u16; 7] = [5173, 8000, 8001, 8002, 8003, 8097, 8099];

pub struct DisplayAssets {
    pub novnc_root: PathBuf,
    pub launcher: PathBuf,
}

/// Create the launcher file, both launcher assets and one noVNC asset.
pub fn display_assets(temp: &TempDataDir) -> DisplayAssets {
    let novnc_root = temp.path().join("novnc");
    std::fs::create_dir_all(novnc_root.join("core")).expect("create novnc root");
    std::fs::write(
        novnc_root.join("core").join("rfb.js"),
        b"export default class RFB {}\n",
    )
    .expect("write rfb asset");
    std::fs::write(novnc_root.join("package.json"), b"{}\n").expect("write package");
    let launcher = temp.path().join("novnc-launcher.html");
    std::fs::write(
        &launcher,
        b"<!doctype html><title>display</title><script type=\"module\" src=\"/display/launcher.js\"></script>",
    )
    .expect("write launcher");
    std::fs::write(
        temp.path().join("novnc-launcher.js"),
        b"const launcher = 1;\n",
    )
    .expect("write launcher script");
    std::fs::write(
        temp.path().join("novnc-launcher-state.mjs"),
        b"export function displayLauncherState() {}\n",
    )
    .expect("write launcher state");
    DisplayAssets {
        novnc_root,
        launcher,
    }
}

/// The enabled display config pointing at the given (fake) VNC port.
pub fn display_config(assets: &DisplayAssets, vnc_port: u16) -> DisplayConfig {
    DisplayConfig {
        enabled: true,
        vnc_host: "127.0.0.1".to_string(),
        vnc_port,
        novnc_root: assets.novnc_root.clone(),
        launcher_file: assets.launcher.clone(),
        max_connections: 2,
        idle_timeout: Duration::from_secs(900),
        max_session_seconds: Duration::from_secs(14400),
        handshake_timeout: Duration::from_secs(3),
        relay_join_timeout: Duration::from_secs(2),
        ..DisplayConfig::default()
    }
}

/// One fake x11vnc/RFB listener on an ephemeral loopback port.
///
/// The listener accepts connections until `join`/drop sets the stop flag, so a
/// test that refuses every handshake never blocks on accept and a test with two
/// viewers gets one fake upstream connection each.
pub struct FakeDisplay {
    port: u16,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

impl FakeDisplay {
    pub fn start<F>(script: F) -> Self
    where
        F: Fn(TcpStream) + Send + Sync + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind fake display");
        listener
            .set_nonblocking(true)
            .expect("fake display nonblocking");
        let port = listener.local_addr().expect("fake display addr").port();
        assert!(
            !GOVERNED_PORTS.contains(&port),
            "fake display bound a governed port"
        );
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = Arc::clone(&stop);
        let script = Arc::new(script);
        let handle = thread::spawn(move || {
            let mut handlers = Vec::new();
            while !worker_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let script = Arc::clone(&script);
                        handlers.push(thread::spawn(move || {
                            let _ = stream.set_nonblocking(false);
                            script(stream);
                        }));
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => break,
                }
            }
            for handler in handlers {
                let _ = handler.join();
            }
        });
        Self {
            port,
            stop,
            handle: Some(handle),
        }
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn join(mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

impl Drop for FakeDisplay {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(handle) = self.handle.take() {
            let _ = handle.join();
        }
    }
}

pub fn recv_exact(stream: &mut TcpStream, size: usize) -> std::io::Result<Vec<u8>> {
    let mut data = Vec::with_capacity(size);
    let mut buffer = [0u8; 4096];
    while data.len() < size {
        let remaining = size - data.len();
        let read = stream.read(&mut buffer[..remaining])?;
        if read == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::UnexpectedEof,
                format!("peer closed while reading {size} bytes"),
            ));
        }
        data.extend_from_slice(&buffer[..read]);
    }
    Ok(data)
}

/// One masked client WebSocket frame.
pub fn client_frame(opcode: u8, payload: &[u8]) -> Vec<u8> {
    let mask = [0x11u8, 0x22, 0x33, 0x44];
    let mut frame = vec![0x80 | opcode];
    let length = payload.len();
    if length < 126 {
        frame.push(0x80 | length as u8);
    } else if length <= 0xFFFF {
        frame.push(0x80 | 126);
        frame.extend_from_slice(&(length as u16).to_be_bytes());
    } else {
        frame.push(0x80 | 127);
        frame.extend_from_slice(&(length as u64).to_be_bytes());
    }
    frame.extend_from_slice(&mask);
    for (index, byte) in payload.iter().enumerate() {
        frame.push(byte ^ mask[index % 4]);
    }
    frame
}

/// A raw WebSocket client for the display relay tests.
pub struct WsClient {
    pub stream: TcpStream,
    head: String,
}

impl WsClient {
    pub fn connect(
        addr: SocketAddr,
        target: &str,
        headers: &[(&str, &str)],
    ) -> std::io::Result<Self> {
        let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(5))?;
        stream.set_read_timeout(Some(Duration::from_secs(10)))?;
        stream.set_write_timeout(Some(Duration::from_secs(10)))?;
        let mut values: Vec<(String, String)> = vec![
            ("Host".to_string(), addr.to_string()),
            ("Upgrade".to_string(), "websocket".to_string()),
            ("Connection".to_string(), "Upgrade".to_string()),
            ("Sec-WebSocket-Version".to_string(), "13".to_string()),
            (
                "Sec-WebSocket-Key".to_string(),
                "MDEyMzQ1Njc4OWFiY2RlZg==".to_string(),
            ),
            ("Sec-WebSocket-Protocol".to_string(), "binary".to_string()),
        ];
        for (name, value) in headers {
            match values
                .iter_mut()
                .find(|(existing, _)| existing.eq_ignore_ascii_case(name))
            {
                Some((_, existing)) => *existing = value.to_string(),
                None => values.push((name.to_string(), value.to_string())),
            }
        }
        let mut lines = vec![format!("GET {target} HTTP/1.1")];
        for (name, value) in values {
            lines.push(format!("{name}: {value}"));
        }
        lines.push(String::new());
        lines.push(String::new());
        stream.write_all(lines.join("\r\n").as_bytes())?;
        stream.flush()?;
        let mut head = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            let read = stream.read(&mut byte)?;
            if read == 0 {
                break;
            }
            head.push(byte[0]);
            if head.ends_with(b"\r\n\r\n") {
                break;
            }
        }
        Ok(Self {
            stream,
            head: String::from_utf8_lossy(&head).to_string(),
        })
    }

    pub fn head(&self) -> &str {
        &self.head
    }

    pub fn send(&mut self, opcode: u8, payload: &[u8]) {
        self.stream
            .write_all(&client_frame(opcode, payload))
            .expect("write client frame");
        self.stream.flush().expect("flush client frame");
    }

    pub fn read_frame(&mut self) -> std::io::Result<(u8, Vec<u8>)> {
        let first = recv_exact(&mut self.stream, 1)?[0];
        let second = recv_exact(&mut self.stream, 1)?[0];
        assert_eq!(second & 0x80, 0, "server frame must not be masked");
        let mut length = u64::from(second & 0x7F);
        if length == 126 {
            length = u64::from(u16::from_be_bytes(
                recv_exact(&mut self.stream, 2)?.try_into().expect("length"),
            ));
        } else if length == 127 {
            length =
                u64::from_be_bytes(recv_exact(&mut self.stream, 8)?.try_into().expect("length"));
        }
        let payload = recv_exact(&mut self.stream, length as usize)?;
        Ok((first & 0x0F, payload))
    }
}

impl Drop for WsClient {
    fn drop(&mut self) {
        let _ = self.stream.shutdown(std::net::Shutdown::Both);
    }
}
