//! In a separate test process so allocator fault injection cannot affect other suites.

mod support;

use std::alloc::{GlobalAlloc, Layout, System};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use um_gateway::ingress::{IngressConfig, IngressServer};

use support::{client_request, read_client_response, ClientRequest, StreamingClient, TempDir};

const BODY_LEN: usize = 1_349_611;
// The padded request below needs a 6,198-byte forwarding head reservation.
// The padding keeps this allocation distinct from the parser's buffers.
const FORWARD_HEAD_ALLOCATION: usize = 6_198;
const TIMEOUT: Duration = Duration::from_secs(5);
const ALLOW: u8 = 0;
const DENY_EXTRA_COPIES: u8 = 1;
const DENY_FORWARD_HEAD: u8 = 2;
static ALLOCATION_MODE: AtomicU8 = AtomicU8::new(ALLOW);

struct FaultAllocator;

fn deny(size: usize) -> bool {
    match ALLOCATION_MODE.load(Ordering::Relaxed) {
        DENY_EXTRA_COPIES => size == BODY_LEN || size == BODY_LEN + 256,
        DENY_FORWARD_HEAD => size == FORWARD_HEAD_ALLOCATION,
        _ => false,
    }
}

#[global_allocator]
static ALLOCATOR: FaultAllocator = FaultAllocator;

// Delegate all successful allocations to System with their original layouts.
unsafe impl GlobalAlloc for FaultAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if deny(layout.size()) {
            return std::ptr::null_mut();
        }
        unsafe { System.alloc(layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        if deny(size) {
            return std::ptr::null_mut();
        }
        unsafe { System.realloc(ptr, layout, size) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) }
    }
}

fn read_worker_request(listener: &TcpListener, expected: &str, length: usize) -> TcpStream {
    let (stream, _) = listener.accept().expect("worker connection");
    stream.set_read_timeout(Some(TIMEOUT)).unwrap();
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    assert_eq!(line, expected);
    let mut declared = None;
    loop {
        line.clear();
        assert_ne!(reader.read_line(&mut line).unwrap(), 0);
        if line == "\r\n" {
            break;
        }
        if let Some((name, value)) = line.split_once(':') {
            if name.eq_ignore_ascii_case("Content-Length") {
                assert!(declared.is_none());
                declared = Some(value.trim().parse::<usize>().unwrap());
            }
        }
    }
    assert_eq!(declared, Some(length));
    assert_eq!(
        io::copy(&mut reader.by_ref().take(length as u64), &mut io::sink()).unwrap(),
        length as u64
    );
    reader.into_inner()
}

#[test]
fn memory_pressure_preserves_the_ingress_and_an_in_flight_stream() {
    let temp = TempDir::new("ingress-memory-pressure");
    let worker = TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let worker_port = worker.local_addr().unwrap().port();
    let worker_port_file = temp.write(
        "worker.port",
        &format!("{worker_port}\n{}\n", std::process::id()),
    );
    let desired_state = temp.write("desired.json", r#"{"state":"running","boot_id":"fixture"}"#);
    let ingress = IngressServer::start(IngressConfig {
        worker_port_file,
        desired_state_file: Some(desired_state),
        boot_id: Some("fixture".to_owned()),
        ..IngressConfig::default()
    })
    .unwrap();
    let addr = ingress.addr();
    let (finish_stream, wait_to_finish) = mpsc::channel();
    let worker_thread = thread::spawn(move || {
        let mut large = read_worker_request(&worker, "POST /v1/large HTTP/1.1\r\n", BODY_LEN);
        large
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            .unwrap();

        let mut stream = read_worker_request(&worker, "GET /v1/stream HTTP/1.1\r\n", 0);
        stream.write_all(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/event-stream\r\n\r\nD\r\ndata: first\n\n\r\n"
        ).unwrap();
        wait_to_finish.recv_timeout(TIMEOUT).unwrap();
        stream
            .write_all(b"E\r\ndata: second\n\n\r\n0\r\n\r\n")
            .unwrap();

        // The ingress connected while preparing the refused request, but must
        // close the worker socket without sending any part of the request.
        let (mut empty, _) = worker.accept().unwrap();
        empty.set_read_timeout(Some(TIMEOUT)).unwrap();
        assert_eq!(empty.read(&mut [0]).unwrap(), 0);
        let mut probe = read_worker_request(&worker, "GET /health HTTP/1.1\r\n", 0);
        probe
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            .unwrap();
    });

    let mut large_request = ClientRequest::post("/v1/large", "");
    large_request.body = Some(vec![b'x'; BODY_LEN]);
    ALLOCATION_MODE.store(DENY_EXTRA_COPIES, Ordering::Relaxed);
    let large_response = client_request(addr, &large_request);
    ALLOCATION_MODE.store(ALLOW, Ordering::Relaxed);
    assert_eq!(large_response.unwrap().body, b"ok");

    let mut stream = StreamingClient::send(addr, &ClientRequest::get("/v1/stream")).unwrap();
    let first = stream.read_until(b"data: first\n\n", TIMEOUT).unwrap();
    assert!(first.windows(13).any(|bytes| bytes == b"data: first\n\n"));

    let mut refused = TcpStream::connect(addr).unwrap();
    refused.set_read_timeout(Some(TIMEOUT)).unwrap();
    ALLOCATION_MODE.store(DENY_FORWARD_HEAD, Ordering::Relaxed);
    refused.write_all(
        b"POST /v1/refused HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\nX-Reserve-Probe: ",
    ).unwrap();
    refused.write_all(&[b'x'; 6000]).unwrap();
    refused.write_all(b"\r\n\r\n").unwrap();
    let response = read_client_response(&mut refused);
    ALLOCATION_MODE.store(ALLOW, Ordering::Relaxed);
    let response = response.unwrap();
    assert_eq!(response.status, 503);
    assert_eq!(response.json()["error"]["code"], "gateway_memory_pressure");

    finish_stream.send(()).unwrap();
    let last = stream.read_until(b"0\r\n\r\n", TIMEOUT).unwrap();
    assert!(last.ends_with(b"E\r\ndata: second\n\n\r\n0\r\n\r\n"));
    assert_eq!(
        client_request(addr, &ClientRequest::get("/health"))
            .unwrap()
            .status,
        200
    );
    worker_thread.join().unwrap();
}
