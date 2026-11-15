use std::path::PathBuf;

use um_test_harness::{
    HarnessConfig, HarnessError, HttpRequest, HttpResponse, TempDataDir, TestClient, TestServer,
};

#[test]
fn server_binds_ephemeral_loopback_port_and_serves_http() {
    let server = TestServer::start(|request| {
        assert_eq!(request.method, "GET");
        assert_eq!(request.path, "/health");
        HttpResponse::json(200, r#"{"status":"ok"}"#)
    })
    .expect("server starts");

    assert_eq!(server.host(), "127.0.0.1");
    assert_ne!(server.port(), 0);

    let response =
        TestClient::request(server.addr(), &HttpRequest::get("/health")).expect("request succeeds");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("content-type"), Some("application/json"));
    assert_eq!(response.body, br#"{"status":"ok"}"#);
}

#[test]
fn two_servers_get_distinct_ephemeral_ports() {
    let first = TestServer::start(|_| HttpResponse::text(200, "first")).expect("first server");
    let second = TestServer::start(|_| HttpResponse::text(200, "second")).expect("second server");

    assert_ne!(first.port(), 0);
    assert_ne!(second.port(), 0);
    assert_ne!(first.port(), second.port());
}

#[test]
fn temp_data_dir_is_isolated_and_removed_on_close() {
    let temp = TempDataDir::new("um-rust-contract-").expect("temp dir");
    let path = temp.path().to_path_buf();
    assert!(path.is_dir());
    assert!(path
        .file_name()
        .expect("directory name")
        .to_string_lossy()
        .starts_with("um-rust-contract-"));

    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    for forbidden in ["data", "profile", "tmp", "output"] {
        assert!(
            !path.starts_with(repo_root.join(forbidden)),
            "temp data dir must not live under {}",
            forbidden
        );
    }

    let written = temp
        .write("nested/state.json", br#"{"fixture":true}"#)
        .expect("write fixture file");
    assert_eq!(
        std::fs::read(&written).expect("read back"),
        br#"{"fixture":true}"#
    );
    assert_eq!(
        temp.read("nested/state.json").expect("read helper"),
        br#"{"fixture":true}"#
    );

    temp.close().expect("cleanup");
    assert!(!path.exists());
}

#[test]
fn production_port_is_refused_before_binding() {
    let config = HarnessConfig {
        bind_port: 8003,
        forbidden_ports: vec![8003],
        ..HarnessConfig::default()
    };
    let error = TestServer::start_with(config, |_| HttpResponse::text(200, "never"))
        .expect_err("production port must be refused");

    assert!(matches!(error, HarnessError::ProductionPort(8003)));
}
