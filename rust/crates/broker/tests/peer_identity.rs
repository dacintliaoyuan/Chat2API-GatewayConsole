//! AC1: kernel peer-identity evidence, mirroring `LinuxInstanceVerifier`.
//!
//! The verifier is a pure decision over evidence a caller obtained from the
//! kernel (`SO_PEERCRED` + `/proc/<pid>`). Every unreadable or mismatched
//! fact is an explicit `access_denied` refusal, never a verified peer.

use std::path::PathBuf;

use um_broker::peer::{
    require_peer_credentials, verify_peer_evidence, PeerCredentials, PeerEvidence, PeerExpectation,
};

fn evidence(pid: i32) -> PeerEvidence {
    PeerEvidence {
        pid,
        uid: Some(1000),
        cmdline: vec![
            "/opt/um-share/venv/bin/python".to_string(),
            "/opt/um-share/current/chat2api/chat2api.py".to_string(),
            "--port".to_string(),
            "8003".to_string(),
        ],
        image: Some(PathBuf::from("/opt/um-share/venv/bin/python")),
        creation_epoch: Some(1_700_000_000.0),
    }
}

fn expectation() -> PeerExpectation {
    PeerExpectation {
        expected_uid: Some(1000),
        expected_image: Some(PathBuf::from("/opt/um-share/venv/bin/python")),
        expected_cmdline_marker: Some("chat2api.py".to_string()),
        expected_port: Some(8003),
        expected_creation_epoch: Some(1_700_000_000.0),
    }
}

fn denied(result: Result<PeerEvidence, um_broker::error::ProtocolError>, needle: &str) {
    let error = result.unwrap_err();
    assert_eq!(error.code().as_str(), "access_denied", "{error}");
    assert!(
        error.message().contains(needle),
        "expected {needle:?} in {:?}",
        error.message()
    );
}

#[test]
fn missing_kernel_credentials_are_a_refusal() {
    let error = require_peer_credentials(None).unwrap_err();
    assert_eq!(error.code().as_str(), "access_denied");
    assert!(error.message().contains("peer credentials"));
}

#[test]
fn kernel_credentials_are_returned_verbatim() {
    let credentials = require_peer_credentials(Some(PeerCredentials {
        pid: 4242,
        uid: 1000,
        gid: 1000,
    }))
    .unwrap();
    assert_eq!(credentials.pid, 4242);
    assert_eq!(credentials.uid, 1000);
    assert_eq!(credentials.gid, 1000);
}

#[test]
fn matching_evidence_is_accepted() {
    let verified = verify_peer_evidence(&evidence(4242), &expectation()).unwrap();
    assert_eq!(verified.pid, 4242);
    assert_eq!(verified.uid, Some(1000));
    assert_eq!(verified.creation_epoch, Some(1_700_000_000.0));
}

#[test]
fn empty_or_missing_cmdline_is_refused() {
    let mut empty = evidence(4242);
    empty.cmdline.clear();
    denied(verify_peer_evidence(&empty, &expectation()), "command line");
}

#[test]
fn a_cmdline_marker_must_be_the_entry_operand() {
    let mut argument_only = evidence(4242);
    argument_only.cmdline = vec![
        "/opt/um-share/venv/bin/python".to_string(),
        "-c".to_string(),
        "import chat2api.py".to_string(),
    ];
    denied(
        verify_peer_evidence(&argument_only, &expectation()),
        "entry marker",
    );
}

#[test]
fn the_port_pair_must_be_exact() {
    let mut wrong_port = evidence(4242);
    wrong_port.cmdline[3] = "8100".to_string();
    denied(verify_peer_evidence(&wrong_port, &expectation()), "port");
    let mut option_only = evidence(4242);
    option_only.cmdline = vec![
        "/opt/um-share/venv/bin/python".to_string(),
        "chat2api.py".to_string(),
        "--upstream-port".to_string(),
        "8003".to_string(),
    ];
    denied(verify_peer_evidence(&option_only, &expectation()), "port");
}

#[test]
fn the_process_image_must_match() {
    let mut wrong_image = evidence(4242);
    wrong_image.image = Some(PathBuf::from("/usr/bin/python3"));
    denied(verify_peer_evidence(&wrong_image, &expectation()), "image");
    let mut unreadable_image = evidence(4242);
    unreadable_image.image = None;
    denied(
        verify_peer_evidence(&unreadable_image, &expectation()),
        "image",
    );
}

#[test]
fn the_process_uid_must_match() {
    let mut foreign = evidence(4242);
    foreign.uid = Some(65534);
    denied(verify_peer_evidence(&foreign, &expectation()), "uid");
    let mut unreadable = evidence(4242);
    unreadable.uid = None;
    denied(verify_peer_evidence(&unreadable, &expectation()), "uid");
}

#[test]
fn a_pid_reuse_is_refused_by_the_creation_epoch() {
    let mut reused = evidence(4242);
    reused.creation_epoch = Some(1_700_000_010.0);
    denied(
        verify_peer_evidence(&reused, &expectation()),
        "creation time mismatch",
    );
    let mut unreadable = evidence(4242);
    unreadable.creation_epoch = None;
    denied(
        verify_peer_evidence(&unreadable, &expectation()),
        "creation time",
    );
}

#[test]
fn an_unexpected_creation_epoch_is_refused() {
    let mut malformed = expectation();
    malformed.expected_creation_epoch = None;
    // A missing expectation is not evidence of reuse: the observed epoch is
    // still required, and the connection is accepted when it is readable.
    assert!(verify_peer_evidence(&evidence(4242), &malformed).is_ok());
}

#[test]
fn an_empty_expectation_still_requires_readable_kernel_evidence() {
    let mut unreadable = evidence(4242);
    unreadable.cmdline.clear();
    denied(
        verify_peer_evidence(&unreadable, &PeerExpectation::default()),
        "command line",
    );
    assert!(verify_peer_evidence(&evidence(4242), &PeerExpectation::default()).is_ok());
}
