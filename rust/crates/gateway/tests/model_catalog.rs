//! #79: the local model catalog appends virtual effort variants.
//!
//! The default catalog appends `<base>-Fast/-Low/-Medium/-High` for every
//! configured base that is present upstream. `X-Chat2Api-Models: raw-fresh` keeps
//! exactly the upstream catalog and marks the response, exactly like Python.

mod support;

use serde_json::json;

use support::*;

#[test]
fn the_default_catalog_appends_virtual_effort_models() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.effort_models = vec!["fixture-model-a".to_string()];
    });

    let response =
        client_request(gateway.addr, &ClientRequest::get("/v1/models")).expect("models");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("x-chat2api-raw-models"), None);
    let body = response.json();
    let ids: Vec<&str> = body["data"]
        .as_array()
        .expect("catalog data")
        .iter()
        .filter_map(|model| model["id"].as_str())
        .collect();
    assert_eq!(
        ids,
        vec![
            "fixture-model-a",
            "fixture-model-b",
            "fixture-model-a-Fast",
            "fixture-model-a-Low",
            "fixture-model-a-Medium",
            "fixture-model-a-High",
        ]
    );
    let variant = body["data"]
        .as_array()
        .expect("catalog data")
        .iter()
        .find(|model| model["id"] == json!("fixture-model-a-High"))
        .cloned()
        .expect("High variant");
    assert_eq!(variant["name"], json!("fixture-model-a-High"));
}

#[test]
fn raw_fresh_keeps_the_upstream_catalog_and_marks_the_response() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.effort_models = vec!["fixture-model-a".to_string()];
    });

    let response = client_request(
        gateway.addr,
        &ClientRequest::get("/v1/models").header("X-Chat2Api-Models", "raw-fresh"),
    )
    .expect("raw fresh models");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("x-chat2api-raw-models"), Some("1"));
    let body = response.json();
    let ids: Vec<&str> = body["data"]
        .as_array()
        .expect("catalog data")
        .iter()
        .filter_map(|model| model["id"].as_str())
        .collect();
    assert_eq!(ids, vec!["fixture-model-a", "fixture-model-b"]);
}

#[test]
fn an_absent_base_model_never_exposes_variants() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.effort_models = vec!["retired-base".to_string()];
    });

    let response =
        client_request(gateway.addr, &ClientRequest::get("/v1/models")).expect("models");
    let body = response.json();
    let ids: Vec<&str> = body["data"]
        .as_array()
        .expect("catalog data")
        .iter()
        .filter_map(|model| model["id"].as_str())
        .collect();
    assert_eq!(ids, vec!["fixture-model-a", "fixture-model-b"]);
}
