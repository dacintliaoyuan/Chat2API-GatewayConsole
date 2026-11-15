//! AC1: the POSIX endpoint policy that mirrors `share/broker_client.py`.
//!
//! These tests run on every platform: the policy is a pure function over the
//! facts a caller observed (existence, file type, mode bits, liveness). The real
//! AF_UNIX adapter that produces those facts lives behind `cfg(unix)` and is
//! exercised in `tests/unix_socket.rs`.

use std::path::Path;

use um_broker::socket_policy::{
    plan_bind_endpoint, plan_connect_endpoint, plan_private_directory, validate_unix_socket_path,
    DirectoryFacts, DirectoryPlan, EndpointFacts, EndpointLiveness, EndpointPlan,
    SocketPolicyError, UNIX_SOCKET_DIRECTORY_MODE, UNIX_SOCKET_FILE_MODE,
    UNIX_SOCKET_MAX_PATH_BYTES,
};

fn dir(exists: bool, is_directory: bool, mode: u32) -> DirectoryFacts {
    DirectoryFacts {
        path: Path::new("/run/um-share").to_path_buf(),
        exists,
        is_directory,
        mode,
    }
}

fn endpoint(exists: bool, is_socket: bool, mode: u32) -> EndpointFacts {
    EndpointFacts {
        path: Path::new("/run/um-share/broker.sock").to_path_buf(),
        exists,
        is_socket,
        mode,
    }
}

#[test]
fn path_must_be_an_absolute_posix_path() {
    assert_eq!(
        validate_unix_socket_path(Path::new("/run/um-share/broker.sock")).unwrap(),
        Path::new("/run/um-share/broker.sock")
    );
    assert!(matches!(
        validate_unix_socket_path(Path::new("relative.sock")),
        Err(SocketPolicyError::InvalidPath(_))
    ));
    assert!(matches!(
        validate_unix_socket_path(Path::new("")),
        Err(SocketPolicyError::InvalidPath(_))
    ));
}

#[test]
fn path_must_fit_the_sun_path_bound() {
    let longest = format!("/tmp/{}", "x".repeat(UNIX_SOCKET_MAX_PATH_BYTES - 5));
    assert_eq!(longest.len(), UNIX_SOCKET_MAX_PATH_BYTES);
    assert!(validate_unix_socket_path(Path::new(&longest)).is_ok());
    let too_long = format!("/tmp/{}", "x".repeat(UNIX_SOCKET_MAX_PATH_BYTES - 4));
    assert_eq!(too_long.len(), UNIX_SOCKET_MAX_PATH_BYTES + 1);
    assert!(matches!(
        validate_unix_socket_path(Path::new(&too_long)),
        Err(SocketPolicyError::InvalidPath(_))
    ));
}

#[test]
fn named_pipe_paths_and_nul_bytes_are_refused() {
    assert!(matches!(
        validate_unix_socket_path(Path::new(r"\\.\pipe\um-share-broker")),
        Err(SocketPolicyError::InvalidPath(_))
    ));
    assert!(matches!(
        validate_unix_socket_path(Path::new("/run/um-share/bro\0ker.sock")),
        Err(SocketPolicyError::InvalidPath(_))
    ));
}

#[test]
fn a_missing_directory_is_created_with_0700() {
    assert_eq!(
        plan_private_directory(&dir(false, false, 0)).unwrap(),
        DirectoryPlan::Create
    );
}

#[test]
fn an_existing_private_directory_is_ready() {
    assert_eq!(
        plan_private_directory(&dir(true, true, UNIX_SOCKET_DIRECTORY_MODE)).unwrap(),
        DirectoryPlan::Ready
    );
    assert_eq!(
        plan_private_directory(&dir(true, true, 0o600)).unwrap(),
        DirectoryPlan::Ready
    );
}

#[test]
fn a_wider_directory_is_an_access_refusal() {
    for mode in [0o755, 0o701, 0o770, 0o777, 0o750] {
        let error = plan_private_directory(&dir(true, true, mode)).unwrap_err();
        assert_eq!(
            error.result_code().as_str(),
            "access_denied",
            "mode {mode:o}"
        );
        assert!(error.to_string().contains("0700"), "{error}");
    }
}

#[test]
fn a_parent_that_is_not_a_directory_is_refused() {
    let error = plan_private_directory(&dir(true, false, 0o700)).unwrap_err();
    assert_eq!(error.result_code().as_str(), "not_installed");
    assert!(error.to_string().contains("not a directory"), "{error}");
}

#[test]
fn a_private_socket_is_accepted_for_connect() {
    assert_eq!(
        plan_connect_endpoint(&endpoint(true, true, UNIX_SOCKET_FILE_MODE)).unwrap(),
        EndpointPlan::Connect
    );
}

#[test]
fn a_wider_socket_file_is_an_access_refusal() {
    for mode in [0o644, 0o666, 0o610, 0o700] {
        let error = plan_connect_endpoint(&endpoint(true, true, mode)).unwrap_err();
        assert_eq!(
            error.result_code().as_str(),
            "access_denied",
            "mode {mode:o}"
        );
        assert!(error.to_string().contains("0600"), "{error}");
    }
}

#[test]
fn a_missing_or_non_socket_endpoint_is_not_installed() {
    assert_eq!(
        plan_connect_endpoint(&endpoint(false, false, 0))
            .unwrap_err()
            .result_code()
            .as_str(),
        "not_installed"
    );
    assert_eq!(
        plan_connect_endpoint(&endpoint(true, false, 0o600))
            .unwrap_err()
            .result_code()
            .as_str(),
        "not_installed"
    );
}

#[test]
fn a_missing_endpoint_is_bound() {
    assert_eq!(
        plan_bind_endpoint(&endpoint(false, false, 0), None).unwrap(),
        EndpointPlan::Bind
    );
}

#[test]
fn a_stale_socket_is_reclaimed_and_a_live_one_is_refused() {
    assert_eq!(
        plan_bind_endpoint(
            &endpoint(true, true, UNIX_SOCKET_FILE_MODE),
            Some(EndpointLiveness::Stale)
        )
        .unwrap(),
        EndpointPlan::Reclaim
    );
    let live = plan_bind_endpoint(
        &endpoint(true, true, UNIX_SOCKET_FILE_MODE),
        Some(EndpointLiveness::Live),
    )
    .unwrap_err();
    assert!(live.to_string().contains("already served"), "{live}");
    let busy = plan_bind_endpoint(
        &endpoint(true, true, UNIX_SOCKET_FILE_MODE),
        Some(EndpointLiveness::Busy),
    )
    .unwrap_err();
    assert!(busy.to_string().contains("busy"), "{busy}");
}

#[test]
fn a_regular_file_at_the_endpoint_is_never_unlinked() {
    let error = plan_bind_endpoint(&endpoint(true, false, 0o600), None).unwrap_err();
    assert!(error.to_string().contains("not a socket"), "{error}");
}
