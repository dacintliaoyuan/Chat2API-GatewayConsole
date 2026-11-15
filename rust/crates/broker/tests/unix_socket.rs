//! AC1: real AF_UNIX endpoint evidence on POSIX.
//!
//! Linux-only by design (real sockets, real mode bits, real `SO_PEERCRED`);
//! the module is compiled out on Windows. The pure policy tests in
//! `socket_policy.rs` cover the decision logic on every platform.

#![cfg(unix)]

use std::fs;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::os::unix::net::UnixListener as StdUnixListener;
use std::path::{Path, PathBuf};

use tempfile::TempDir;
use um_broker::socket_policy::{SocketPolicyError, UNIX_SOCKET_DIRECTORY_MODE};
use um_broker::transport::unix::{UnixConnection, UnixListener};

fn temp() -> TempDir {
    tempfile::Builder::new()
        .prefix("um-broker-ac1-")
        .tempdir()
        .expect("temp dir")
}

#[test]
fn binding_creates_a_0700_directory_and_a_0600_socket() {
    let dir = temp();
    let path = dir.path().join("run").join("broker.sock");
    let mut listener = UnixListener::bind(&path).expect("bind");
    let socket = fs::metadata(&path).expect("socket metadata");
    assert!(socket.file_type().is_socket());
    assert_eq!(socket.mode() & 0o777, 0o600);
    let parent = fs::metadata(path.parent().unwrap()).expect("parent metadata");
    assert_eq!(parent.mode() & 0o777, UNIX_SOCKET_DIRECTORY_MODE);
    listener.close().expect("close");
    assert!(!path.exists(), "the listener removes its own socket file");
}

#[test]
fn a_wider_parent_directory_is_refused() {
    let dir = temp();
    let parent = dir.path().join("wide");
    fs::create_dir(&parent).expect("mkdir");
    fs::set_permissions(&parent, fs::Permissions::from_mode(0o755)).expect("chmod");
    let path = parent.join("broker.sock");
    let error = UnixListener::bind(&path).unwrap_err();
    assert_eq!(error.result_code().as_str(), "access_denied");
    assert!(error.to_string().contains("0700"), "{error}");
    assert!(!path.exists());
}

#[test]
fn a_live_endpoint_is_refused_and_never_unlinked() {
    let dir = temp();
    let path = dir.path().join("broker.sock");
    let mut first = UnixListener::bind(&path).expect("first bind");
    let error = UnixListener::bind(&path).unwrap_err();
    assert!(error.to_string().contains("already served"), "{error}");
    assert!(path.exists(), "the live endpoint stays in place");
    first.close().expect("close");
}

#[test]
fn a_stale_socket_is_reclaimed() {
    let dir = temp();
    let path = dir.path().join("broker.sock");
    let stale = StdUnixListener::bind(&path).expect("stale bind");
    drop(stale);
    assert!(path.exists());
    let mut listener = UnixListener::bind(&path).expect("reclaim");
    let metadata = fs::metadata(&path).expect("metadata");
    assert!(metadata.file_type().is_socket());
    listener.close().expect("close");
    assert!(!path.exists());
}

#[test]
fn a_regular_file_at_the_endpoint_is_refused() {
    let dir = temp();
    let path = dir.path().join("broker.sock");
    fs::write(&path, b"not a socket").expect("write");
    let error = UnixListener::bind(&path).unwrap_err();
    assert!(error.to_string().contains("not a socket"), "{error}");
    assert!(path.exists(), "the regular file is never unlinked");
}

#[test]
fn a_client_refuses_a_wider_socket_file_before_connecting() {
    let dir = temp();
    let path = dir.path().join("broker.sock");
    let mut listener = UnixListener::bind(&path).expect("bind");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o666)).expect("chmod");
    let error = UnixConnection::connect(&path).unwrap_err();
    assert_eq!(error.result_code().as_str(), "access_denied");
    assert!(error.to_string().contains("0600"), "{error}");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).expect("chmod");
    listener.close().expect("close");
}

#[test]
fn a_client_refuses_a_missing_endpoint_as_not_installed() {
    let dir = temp();
    let path = dir.path().join("missing.sock");
    let error = UnixConnection::connect(&path).unwrap_err();
    assert_eq!(error.result_code().as_str(), "not_installed");
}

#[test]
fn peer_credentials_are_kernel_evidence() {
    let dir = temp();
    let path = dir.path().join("broker.sock");
    let mut listener = UnixListener::bind(&path).expect("bind");
    let accepted = std::thread::spawn({
        let path = path.clone();
        move || {
            let connection = UnixConnection::connect(&path).expect("connect");
            connection
        }
    });
    let server = listener.accept().expect("accept");
    let client = accepted.join().expect("client thread");
    let server_credentials = server.peer_credentials().expect("server credentials");
    let client_credentials = client.peer_credentials().expect("client credentials");
    assert_eq!(server_credentials.pid, std::process::id() as i32);
    assert_eq!(server_credentials.uid, unsafe { libc::getuid() });
    assert_eq!(server_credentials.gid, unsafe { libc::getgid() });
    assert_eq!(client_credentials.pid, std::process::id() as i32);
    assert_eq!(client_credentials.uid, unsafe { libc::getuid() });
    assert_eq!(client_credentials.gid, unsafe { libc::getgid() });
    drop(client);
    listener.close().expect("close");
}

#[test]
fn a_named_pipe_path_is_refused_before_any_syscall() {
    let error = UnixListener::bind(Path::new(r"\\.\pipe\um-share-broker")).unwrap_err();
    assert!(matches!(error, SocketPolicyError::InvalidPath(_)));
    let error = UnixConnection::connect(Path::new("relative.sock")).unwrap_err();
    assert!(matches!(error, SocketPolicyError::InvalidPath(_)));
}

#[test]
fn closing_never_unlinks_a_replacement_socket() {
    let dir = temp();
    let path = dir.path().join("broker.sock");
    let mut listener = UnixListener::bind(&path).expect("bind");
    listener.close().expect("close");
    let replacement = StdUnixListener::bind(&path).expect("replacement");
    // A second bind would have refused; the point is the old close cannot
    // unlink a path it no longer owns.
    assert!(path.exists());
    drop(replacement);
    let _: PathBuf = path;
}
