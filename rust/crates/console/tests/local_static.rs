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
fn local_root_refuses_a_cloud_build_identity() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_index(&dist);
    write_identity(
        &dist,
        json!({
            "line": "production",
            "role": "cloud",
            "sourceHash": "a".repeat(64),
        }),
    );
    let console = local_console(&temp, &dist);

    let response = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(response.status, 500);
    let error = response.json()["error"]
        .as_str()
        .expect("identity error")
        .to_string();
    assert!(error.contains("cloud"), "{error}");
    assert!(error.contains("local"), "{error}");
    assert!(!response
        .body
        .windows(STUB_INDEX.len())
        .any(|window| window == STUB_INDEX));
}

#[test]
fn local_root_refuses_a_candidate_line_build() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_index(&dist);
    write_identity(
        &dist,
        json!({
            "line": "candidate-local",
            "role": "local",
            "sourceHash": "a".repeat(64),
        }),
    );
    let console = local_console(&temp, &dist);

    let response = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(response.status, 500);
    assert!(
        response.json()["error"]
            .as_str()
            .expect("identity error")
            .contains("line"),
        "{}",
        String::from_utf8_lossy(&response.body)
    );
}

#[test]
fn local_root_refuses_a_missing_or_malformed_build_identity() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_index(&dist);
    let console = local_console(&temp, &dist);

    let missing = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(missing.status, 500);
    assert!(
        missing.json()["error"]
            .as_str()
            .expect("identity error")
            .contains("candidate-build.json"),
        "{}",
        String::from_utf8_lossy(&missing.body)
    );

    fs::write(dist.join("candidate-build.json"), b"not json").expect("write malformed identity");
    let malformed = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(malformed.status, 500);
    assert!(
        malformed.json()["error"]
            .as_str()
            .expect("identity error")
            .contains("JSON"),
        "{}",
        String::from_utf8_lossy(&malformed.body)
    );
}

#[test]
fn local_root_refuses_a_missing_or_malformed_source_hash() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_index(&dist);
    let console = local_console(&temp, &dist);

    for source_hash in [json!(null), json!("not-a-sha256"), json!("A".repeat(64))] {
        write_identity(
            &dist,
            json!({
                "line": "production",
                "role": "local",
                "sourceHash": source_hash,
            }),
        );
        let response = client_request(console.addr, &ClientRequest::get("/")).expect("root");
        assert_eq!(response.status, 500, "{source_hash}");
        assert!(
            response.json()["error"]
                .as_str()
                .expect("identity error")
                .contains("sourceHash"),
            "{source_hash}: {}",
            String::from_utf8_lossy(&response.body)
        );
    }
}

#[test]
fn local_root_rechecks_the_build_identity_on_every_request() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_index(&dist);
    write_identity(&dist, local_identity());
    let console = local_console(&temp, &dist);

    let accepted = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(accepted.status, 200);

    write_identity(
        &dist,
        json!({
            "line": "production",
            "role": "cloud",
            "sourceHash": "a".repeat(64),
        }),
    );
    let refused = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(refused.status, 500);
}

#[test]
fn local_root_missing_index_is_an_explicit_error() {
    let temp = temp();
    let dist = temp.path().join("dist");
    write_identity(&dist, local_identity());
    let console = local_console(&temp, &dist);

    let response = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(response.status, 500);
    assert!(
        response.json()["error"]
            .as_str()
            .expect("missing index error")
            .contains("web/dist"),
        "{}",
        String::from_utf8_lossy(&response.body)
    );
}

#[test]
fn local_root_missing_dist_tree_is_an_explicit_error() {
    let temp = temp();
    let dist = temp.path().join("no-such-dist");
    let console = local_console(&temp, &dist);

    let response = client_request(console.addr, &ClientRequest::get("/")).expect("root");
    assert_eq!(response.status, 500);
    assert!(
        response.json()["error"]
            .as_str()
            .expect("missing dist error")
            .contains("web/dist"),
        "{}",
        String::from_utf8_lossy(&response.body)
    );
}

#[test]
fn local_static_assets_use_the_python_paths_and_mime_map() {
    let temp = temp();
    let dist = temp.path().join("dist");
    fs::create_dir_all(dist.join("assets")).expect("create dist assets");
    let cases = [
        ("asset.html", "text/html; charset=utf-8"),
        ("asset.js", "text/javascript; charset=utf-8"),
        ("asset.css", "text/css; charset=utf-8"),
        ("asset.json", "application/json; charset=utf-8"),
        (
            "asset.webmanifest",
            "application/manifest+json; charset=utf-8",
        ),
        ("asset.svg", "image/svg+xml"),
        ("asset.png", "image/png"),
        ("asset.ico", "image/x-icon"),
        ("asset.woff2", "font/woff2"),
        ("asset.woff", "font/woff"),
        ("asset.glb", "model/gltf-binary"),
        ("asset.mp3", "audio/mpeg"),
        ("asset.wav", "audio/wav"),
        ("asset.txt", "text/plain; charset=utf-8"),
        ("asset.md", "text/markdown; charset=utf-8"),
        ("asset.bin", "application/octet-stream"),
        ("asset-upper.JS", "text/javascript; charset=utf-8"),
    ];
    for (name, _) in cases {
        fs::write(dist.join("assets").join(name), format!("stub:{name}")).expect("write asset");
    }
    let console = local_console(&temp, &dist);

    for (name, content_type) in cases {
        let target = format!("/assets/{name}");
        let response = client_request(console.addr, &ClientRequest::get(&target)).expect(&target);
        assert_eq!(response.status, 200, "{target}");
        assert_eq!(
            response.body,
            format!("stub:{name}").into_bytes(),
            "{target}"
        );
        assert_eq!(
            response.header("content-type"),
            Some(content_type),
            "{target}"
        );
        assert_eq!(
            response.header("cache-control"),
            Some("no-store"),
            "{target}"
        );
    }

    let alias = client_request(
        console.addr,
        &ClientRequest::get("/gw/usagesrv/assets/asset.js"),
    )
    .expect("alias asset");
    assert_eq!(alias.status, 200);
    assert_eq!(alias.body, b"stub:asset.js");
}

#[test]
fn local_static_rejects_traversal_missing_and_directories() {
    let temp = temp();
    let dist = temp.path().join("dist");
    fs::create_dir_all(dist.join("assets")).expect("create dist assets");
    fs::write(temp.path().join("secret.txt"), b"outside-secret").expect("write secret");
    let console = local_console(&temp, &dist);

    for target in ["/assets/../../secret.txt", "/assets/nope.js", "/assets"] {
        let response = client_request(console.addr, &ClientRequest::get(target)).expect(target);
        assert_eq!(response.status, 404, "{target}");
        assert_eq!(response.json()["error"], json!("unknown path"), "{target}");
        assert!(
            !response
                .body
                .windows(b"outside-secret".len())
                .any(|window| window == b"outside-secret"),
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
    fs::create_dir_all(dist.join("assets")).expect("create dist assets");
    fs::write(dist.join("assets/app.js"), b"cloud-stub").expect("write asset");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = Role::Cloud;
        config.console_dist_dir = dist.clone();
    });

    for target in ["/", "/index.html", "/assets/app.js"] {
        let response = client_request(console.addr, &ClientRequest::get(target)).expect(target);
        assert_eq!(response.status, 404, "{target}");
        assert_eq!(response.json()["error"], json!("unknown path"), "{target}");
    }
}
