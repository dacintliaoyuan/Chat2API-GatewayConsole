use std::path::PathBuf;

use um_contract::ContractFixture;
use um_test_harness::{
    HarnessConfig, HttpRequest, HttpResponse, TempDataDir, TestClient, TestServer,
};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

#[test]
fn candidate_binds_an_ephemeral_port_outside_the_production_set() {
    let fixture = ContractFixture::load_default().expect("generated fixture loads");
    let server = TestServer::start_with(
        HarnessConfig {
            bind_host: fixture.harness.bind_host.clone(),
            bind_port: fixture.harness.bind_port,
            forbidden_ports: fixture.harness.production_ports.clone(),
        },
        |_| HttpResponse::text(200, "candidate"),
    )
    .expect("candidate starts");

    assert_eq!(server.host(), fixture.harness.bind_host);
    assert!(!fixture.harness.production_ports.contains(&server.port()));
    let response =
        TestClient::request(server.addr(), &HttpRequest::get("/health")).expect("request");
    assert_eq!(response.body, b"candidate");
}

#[test]
fn temp_data_dir_is_outside_every_production_root() {
    let fixture = ContractFixture::load_default().expect("generated fixture loads");
    let temp = TempDataDir::new(&fixture.harness.temp_dir_prefix).expect("temp dir");
    let repo_root = repo_root();

    for root in &fixture.harness.production_data_roots {
        let root_path = PathBuf::from(root);
        if root_path.is_absolute() {
            assert!(
                !temp.path().starts_with(&root_path),
                "temp dir must not live under {root}"
            );
        } else {
            assert!(
                !temp.path().starts_with(repo_root.join(&root_path)),
                "temp dir must not live under {root}"
            );
        }
    }

    temp.close().expect("cleanup");
}

#[test]
fn a_failed_candidate_start_leaves_no_temp_state() {
    let fixture = ContractFixture::load_default().expect("generated fixture loads");
    let production_port = *fixture
        .harness
        .production_ports
        .first()
        .expect("fixture lists production ports");
    let config = HarnessConfig {
        bind_host: fixture.harness.bind_host.clone(),
        bind_port: production_port,
        forbidden_ports: fixture.harness.production_ports.clone(),
    };
    assert!(TestServer::start_with(config, |_| HttpResponse::text(200, "never")).is_err());

    let temp = TempDataDir::new(&fixture.harness.temp_dir_prefix).expect("temp dir");
    let path = temp.path().to_path_buf();
    temp.write("candidate.json", b"{}").expect("candidate file");
    drop(temp);
    assert!(!path.exists(), "failed candidate data must be deleted");
}
