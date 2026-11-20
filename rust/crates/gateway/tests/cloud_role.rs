//! #79: the cloud role keeps the general `/api/*` surface unassembled.
//!
//! The cloud role is the default and is also derived from 8002/8003; only 8000
//! derives the local role that assembles the passthrough.

mod support;

use serde_json::json;

use um_gateway::config::Role;

use support::*;

fn local_get(target: &str) -> ClientRequest {
    ClientRequest::get(target).header("X-Admin-Control", CONTROL_TOKEN)
}

fn local_post(target: &str, body: &str) -> ClientRequest {
    ClientRequest::post(target, body)
        .header("X-Admin-Control", CONTROL_TOKEN)
        .json_body(body)
}

#[test]
fn cloud_role_keeps_the_local_api_surface_unassembled() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_gateway(&stub, &temp, false);

    for request in [
        local_get("/api/echo"),
        local_post("/api/echo", r#"{"ping":true}"#),
    ] {
        let response = client_request(gateway.addr, &request).expect("cloud local API surface");
        assert_eq!(response.status, 501);
        assert_eq!(
            body_field(&response.json(), "code"),
            &json!("not_implemented")
        );
        assert_eq!(
            body_field(&response.json(), "detail"),
            &json!("local_api_surface_s1a")
        );
    }

    assert!(
        !stub
            .seen()
            .iter()
            .any(|request| request.path == "/api/echo"),
        "the cloud role must not forward the local API surface"
    );
}

#[test]
fn the_role_is_derived_from_the_production_port() {
    assert_eq!(Role::for_port(7998), Role::Local);
    assert_eq!(Role::for_port(7999), Role::Local);
    assert_eq!(Role::for_port(8000), Role::Local);
    assert_eq!(Role::for_port(8002), Role::Cloud);
    assert_eq!(Role::for_port(8003), Role::Cloud);
    assert_eq!(Role::for_port(0), Role::Cloud);
    assert_eq!(Role::parse("local"), Some(Role::Local));
    assert_eq!(Role::parse("cloud"), Some(Role::Cloud));
    assert_eq!(Role::parse("production"), None);
}

#[test]
fn the_role_switch_fails_closed_on_an_unknown_value() {
    let help = std::process::Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"))
        .arg("--help")
        .output()
        .expect("run help");
    assert!(help.status.success());
    let stdout = String::from_utf8_lossy(&help.stdout);
    assert!(
        stdout.contains("--role local|cloud"),
        "help must document --role: {stdout}"
    );

    let output = std::process::Command::new(env!("CARGO_BIN_EXE_um-gateway-candidate"))
        .arg("--role")
        .arg("production")
        .arg("--upstream-base-url")
        .arg("http://127.0.0.1:9")
        .output()
        .expect("run invalid role");
    assert!(!output.status.success(), "an unknown role must fail closed");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("--role must be local or cloud"),
        "stderr: {stderr}"
    );
}
