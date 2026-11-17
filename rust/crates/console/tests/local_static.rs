//! #83: the local 8001 console hosts `web/dist` and verifies its build identity.
//!
//! The expected behavior is `usagesrv.py`: `GET /` and `GET /index.html` serve
//! the dist entry page only when `candidate-build.json` names the local role and
//! a production line with a source identity. Every dist file is served with the
//! Python MIME map and `Cache-Control: no-store`; the cloud role keeps its
//! existing unknown-path answer.

mod support;

use std::fs;
use std::path::Path;

use serde_json::{json, Value};
use um_console::Role;

use support::*;

const STUB_INDEX: &[u8] = b"<html><body>console-dist-stub</body></html>";

fn write_index(dist: &Path) {
    fs::create_dir_all(dist).expect("create dist");
    fs::write(dist.join("index.html"), STUB_INDEX).expect("write index");
}

fn write_identity(dist: &Path, identity: Value) {
    fs::create_dir_all(dist).expect("create dist");
    fs::write(dist.join("candidate-build.json"), identity.to_string())
        .expect("write build identity");
}

fn local_identity() -> Value {
    json!({
        "line": "production",
        "role": "local",
        "sourceHash": "a".repeat(64),
    })
}

fn local_console(temp: &um_test_harness::TempDataDir, dist: &Path) -> Console {
    start_console_with(temp, stopped_runtime(), |config| {
        config.role = Role::Local;
        config.port = 8001;
        config.console_dist_dir = dist.to_path_buf();
    })
}

#[test]
fn local_root_serves_the_dist_index() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_index(&dist);
    write_identity(&dist, local_identity());
    let console = local_console(&temp, &dist);

    for target in ["/", "/index.html"] {
        let response = client_request(console.addr, &ClientRequest::get(target)).expect(target);
        assert_eq!(response.status, 200, "{target}");
        assert_eq!(response.body, STUB_INDEX, "{target}");
        assert_eq!(
            response.header("content-type"),
            Some("text/html; charset=utf-8"),
            "{target}"
        );
        assert_eq!(
            response.header("cache-control"),
            Some("no-store"),
            "{target}"
        );
    }
}

#[test]
fn cloud_role_keeps_the_unknown_path_answer() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_index(&dist);
    write_identity(&dist, local_identity());
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = Role::Cloud;
        config.console_dist_dir = dist.clone();
    });

    for target in ["/", "/index.html"] {
        let response = client_request(console.addr, &ClientRequest::get(target)).expect(target);
        assert_eq!(response.status, 404, "{target}");
        assert_eq!(response.json()["error"], json!("unknown path"), "{target}");
    }
}
