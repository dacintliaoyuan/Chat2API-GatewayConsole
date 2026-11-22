//! #146: the desktop instance's admin read faces.
//!
//! Every route is loopback + control-token protected and reads only the gateway
//! process itself: the upstream model catalog, the in-process context probes, the
//! login-round history the gateway already writes, and the gateway's own service
//! log. The App keeps one loopback port; no second listener is assembled.

mod support;

use std::time::Duration;

use serde_json::{json, Value};

use support::*;

fn admin_get(gateway: &Gateway, target: &str) -> ClientResponse {
    client_request(
        gateway.addr,
        &ClientRequest::get(target).header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("admin GET")
}

fn admin_post(gateway: &Gateway, target: &str, body: &str) -> ClientResponse {
    client_request(
        gateway.addr,
        &ClientRequest::post(target, body)
            .json_body(body)
            .header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("admin POST")
}

fn admin_delete(gateway: &Gateway, target: &str) -> ClientResponse {
    client_request(
        gateway.addr,
        &ClientRequest::delete(target).header("X-Admin-Control", CONTROL_TOKEN),
    )
    .expect("admin DELETE")
}

#[test]
fn the_admin_model_catalog_reports_names_context_efforts_and_last_test() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        models_body: Some(json!({
            "object": "list",
            "data": [
                {
                    "id": "fixture-model-a",
                    "name": "Fixture A",
                    "max_model_len": 128000,
                    "efforts": ["low", "high"],
                    "default_effort": "high",
                },
                {"id": "fixture-model-b", "name": "Fixture B"},
            ],
        })),
        ..StubBehaviour::default()
    });
    let gateway = start_local_gateway(&stub, &temp);

    let response = admin_get(&gateway, "/admin/models");
    assert_eq!(response.status, 200, "{:?}", response.body);
    let body = response.json();
    assert_eq!(body["source"], json!("um"));
    assert!(body["checked_epoch"].is_number());
    let models = body["models"].as_array().expect("models");
    assert_eq!(models.len(), 2);
    assert_eq!(models[0]["id"], json!("fixture-model-a"));
    assert_eq!(models[0]["name"], json!("Fixture A"));
    assert_eq!(models[0]["context"], json!(128000));
    assert_eq!(models[0]["context_known"], json!(true));
    assert_eq!(models[0]["efforts"], json!(["low", "high"]));
    assert_eq!(models[0]["default_effort"], json!("high"));
    assert_eq!(models[0]["last_test"], Value::Null);
    assert_eq!(models[1]["id"], json!("fixture-model-b"));
    assert_eq!(models[1]["context"], Value::Null);
    assert_eq!(models[1]["context_known"], json!(false));
}

#[test]
fn the_admin_probe_surface_queues_probes_and_records_real_evidence() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let initial = admin_get(&gateway, "/admin/model-context/probes");
    assert_eq!(initial.status, 200, "{:?}", initial.body);
    let body = initial.json();
    assert_eq!(body["records"], json!([]));
    assert_eq!(body["queue"], json!([]));
    assert_eq!(body["running"], json!(false));
    assert_eq!(body["steps"], json!([4000, 16000, 64000, 256000]));

    let started = admin_post(
        &gateway,
        "/admin/model-context/probes",
        r#"{"models":["fixture-model-a"]}"#,
    );
    assert_eq!(started.status, 202, "{:?}", started.body);
    let body = started.json();
    assert_eq!(body["status"], json!("started"));
    assert_eq!(body["queued"], json!(1));

    wait_until(Duration::from_secs(10), || {
        let snapshot = admin_get(&gateway, "/admin/model-context/probes").json();
        snapshot["running"] == json!(false) && snapshot["records"][0]["status"] == json!("verified")
    });
    let snapshot = admin_get(&gateway, "/admin/model-context/probes").json();
    let record = &snapshot["records"][0];
    assert_eq!(record["model_id"], json!("fixture-model-a"));
    assert_eq!(record["status"], json!("verified"));
    assert_eq!(record["observed_floor"], json!(256000));
    assert_eq!(record["observed_ceiling"], Value::Null);
    assert_eq!(record["attempts"], json!(4));
    let history = record["history"].as_array().expect("history");
    assert_eq!(history.len(), 1, "一次探测批次记录一条历史");
    assert_eq!(history[0]["observed_floor"], json!(256000));

    let catalog = admin_get(&gateway, "/admin/models").json();
    let model = catalog["models"]
        .as_array()
        .expect("models")
        .iter()
        .find(|model| model["id"] == json!("fixture-model-a"))
        .cloned()
        .expect("fixture-model-a");
    assert_eq!(model["last_test"]["status"], json!("verified"));
    assert_eq!(model["last_test"]["observed_floor"], json!(256000));
    assert_eq!(snapshot["queue"], json!([]));

    let probes: Vec<SeenRequest> = stub
        .seen()
        .into_iter()
        .filter(|request| request.path == "/api/chat/completions")
        .collect();
    assert_eq!(probes.len(), 4, "四个步进各发一次真实请求");
    assert_eq!(
        probes[0].authorization.as_deref(),
        Some("Bearer fixture-upstream-token")
    );
    let body: Value = serde_json::from_slice(&probes[0].body).expect("probe body");
    assert_eq!(body["model"], json!("fixture-model-a"));
    assert_eq!(body["max_tokens"], json!(64));
    assert_eq!(body["stream"], json!(false));
}

#[test]
fn the_admin_probe_history_survives_a_gateway_restart() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let usage = temp.path().join("usage.jsonl");
    let first = start_local_gateway_with(&stub, &temp, |config| {
        config.usage_log = Some(usage.clone());
    });
    let started = admin_post(
        &first,
        "/admin/model-context/probes",
        r#"{"models":["fixture-model-a"]}"#,
    );
    assert_eq!(started.status, 202, "{:?}", started.body);
    wait_until(Duration::from_secs(10), || {
        let snapshot = admin_get(&first, "/admin/model-context/probes").json();
        snapshot["running"] == json!(false) && snapshot["records"][0]["status"] == json!("verified")
    });
    drop(first);

    let second = start_local_gateway_with(&stub, &temp, |config| {
        config.usage_log = Some(usage);
    });
    let snapshot = admin_get(&second, "/admin/model-context/probes").json();
    assert_eq!(snapshot["records"][0]["status"], json!("verified"));
    assert_eq!(snapshot["records"][0]["observed_floor"], json!(256000));
    assert_eq!(snapshot["state_error"], Value::Null);
    let catalog = admin_get(&second, "/admin/models").json();
    let model = catalog["models"]
        .as_array()
        .expect("models")
        .iter()
        .find(|model| model["id"] == json!("fixture-model-a"))
        .cloned()
        .expect("fixture-model-a");
    assert_eq!(model["last_test"]["status"], json!("verified"));
}

#[test]
fn deleting_the_admin_probe_queue_stops_subsequent_requests() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        probe_delay: Duration::from_millis(150),
        ..StubBehaviour::default()
    });
    let gateway = start_local_gateway(&stub, &temp);

    let started = admin_post(
        &gateway,
        "/admin/model-context/probes",
        r#"{"models":["fixture-model-a","fixture-model-b"]}"#,
    );
    assert_eq!(started.status, 202, "{:?}", started.body);
    wait_until(Duration::from_secs(5), || {
        stub.seen()
            .iter()
            .any(|request| request.path == "/api/chat/completions")
    });

    let stopped = admin_delete(&gateway, "/admin/model-context/probes");
    assert_eq!(stopped.status, 200, "{:?}", stopped.body);
    assert_eq!(stopped.json()["stopped"], json!(true));

    wait_until(Duration::from_secs(10), || {
        let snapshot = admin_get(&gateway, "/admin/model-context/probes").json();
        snapshot["running"] == json!(false)
    });
    let snapshot = admin_get(&gateway, "/admin/model-context/probes").json();
    assert_eq!(snapshot["stop_requested"], json!(true));
    let probes = stub
        .seen()
        .into_iter()
        .filter(|request| request.path == "/api/chat/completions")
        .count();
    assert!(
        probes < 8,
        "停止后不得继续发起后续探测请求，实际 {probes} 次"
    );
    let records = snapshot["records"].as_array().expect("records");
    assert!(
        records
            .iter()
            .any(|record| record["status"] == json!("cancelled")),
        "未执行的模型必须标为 cancelled：{snapshot}"
    );
}

#[test]
fn the_admin_service_surface_reports_this_gateway_and_start_is_idempotent() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let response = admin_get(&gateway, "/admin/um/services");
    assert_eq!(response.status, 200, "{:?}", response.body);
    let body = response.json();
    let services = body["services"].as_array().expect("services");
    assert_eq!(services.len(), 1);
    let service = &services[0];
    assert_eq!(service["key"], json!("chat2api"));
    assert_eq!(service["port"], json!(gateway.addr.port()));
    assert_eq!(service["state"], json!("Running"));
    assert_eq!(service["healthy"], json!(true));
    assert_eq!(service["processIds"], json!([std::process::id()]));
    assert!(body["checkedAt"].is_number());

    let started = admin_post(&gateway, "/admin/um/services/start", "{}");
    assert_eq!(started.status, 200, "{:?}", started.body);
    let body = started.json();
    assert_eq!(body["status"], json!("already_running"));
    assert_eq!(body["started"], json!(false));
    assert_eq!(body["service"]["port"], json!(gateway.addr.port()));
    assert_eq!(body["service"]["processIds"], json!([std::process::id()]));
}

#[test]
fn the_admin_login_rounds_read_and_filter_the_gateway_history() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let started_epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_secs_f64()
        .floor();
    let ended_epoch = started_epoch + 60.0;
    let history = temp.write(
        "login-history.jsonl",
        &format!(
            "{}\n",
            json!({
                "schema": 1,
                "run_id": "run-1",
                "round": 1,
                "state": "completed",
                "trigger_source": "auth_watch",
                "driver": "native",
                "visible": false,
                "started_epoch": started_epoch,
                "ended_epoch": ended_epoch,
                "submit_count": 1,
                "outcome": "restored",
                "reason": null,
                "detail": null,
                "upstream_status": 200,
                "evidence_verdict": "valid",
                "rejected_before_expiry_status": null,
            })
        ),
    );
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.login_history_file = Some(history.clone());
    });

    let response = admin_get(&gateway, "/admin/um/login-rounds");
    assert_eq!(response.status, 200, "{:?}", response.body);
    let body = response.json();
    assert_eq!(body["status"], json!("ok"));
    assert_eq!(body["count"], json!(1), "{body}");
    assert_eq!(body["truncated"], json!(false));
    let rounds = body["rounds"].as_array().expect("rounds");
    assert_eq!(rounds.len(), 1);
    let round = &rounds[0];
    assert_eq!(round["key"], json!("run-1:1"));
    assert_eq!(round["runId"], json!("run-1"));
    assert_eq!(round["round"], json!(1));
    assert_eq!(round["state"], json!("completed"));
    assert_eq!(round["triggerSource"], json!("auth_watch"));
    assert_eq!(round["driver"], json!("native"));
    assert_eq!(round["startedAt"], json!((started_epoch * 1000.0) as i64));
    assert_eq!(round["endedAt"], json!((ended_epoch * 1000.0) as i64));
    assert_eq!(round["submitCount"], json!(1));
    assert_eq!(round["outcome"], json!("restored"));
    assert_eq!(round["upstreamStatus"], json!(200));
    assert_eq!(round["evidenceVerdict"], json!("valid"));

    let by_round = admin_get(&gateway, "/admin/um/login-rounds?round=2");
    assert_eq!(by_round.json()["count"], json!(0));
    let by_reason = admin_get(&gateway, "/admin/um/login-rounds?q=restored");
    assert_eq!(by_reason.json()["count"], json!(1));
}

#[test]
fn an_absent_admin_login_history_is_empty_and_an_unconfigured_path_is_unavailable() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);
    let unconfigured = admin_get(&gateway, "/admin/um/login-rounds");
    assert_eq!(unconfigured.status, 200);
    let body = unconfigured.json();
    assert_eq!(body["status"], json!("unconfigured"));
    assert_eq!(body["error"], json!("login_history_unconfigured"));
    assert_eq!(body["rounds"], json!([]));

    let missing = temp.path().join("missing-history.jsonl");
    let configured = start_local_gateway_with(&stub, &temp, |config| {
        config.login_history_file = Some(missing);
    });
    let response = admin_get(&configured, "/admin/um/login-rounds");
    assert_eq!(response.status, 200);
    let body = response.json();
    assert_eq!(body["status"], json!("ok"));
    assert_eq!(body["error"], Value::Null);
    assert_eq!(body["rounds"], json!([]));
    assert_eq!(body["count"], json!(0));
}

#[test]
fn the_admin_log_history_filters_time_levels_source_and_pages_earlier() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let log = temp.write(
        "gateway.log",
        &format!(
            "{}\n{}\n{}\n{}\n",
            json!({"time": 1000, "level": "error", "service": "chat2api", "event": "request_summary", "request_id": "req-1"}),
            json!({"time": 2000, "level": "warn", "service": "chat2api", "event": "listener_failure"}),
            json!({"time": 3000, "service": "chat2api", "event": "request_summary", "request_id": "req-2"}),
            json!({"time": 4000, "level": "debug", "service": "chat2api", "event": "other"}),
        ),
    );
    let gateway = start_local_gateway_with(&stub, &temp, |config| {
        config.service_log = Some(log.clone());
    });

    let window = admin_get(&gateway, "/admin/um/logs?since=1500&until=3500");
    assert_eq!(window.status, 200, "{:?}", window.body);
    let body = window.json();
    assert_eq!(body["status"], json!("ok"));
    assert_eq!(body["sources"], json!(["chat2api"]));
    let lines = body["lines"].as_array().expect("lines");
    assert_eq!(lines.len(), 2, "{body}");
    assert_eq!(lines[0]["at"], json!(3000));
    assert_eq!(lines[0]["level"], json!("error"));
    assert_eq!(lines[0]["requestId"], json!("req-2"));
    assert_eq!(lines[1]["at"], json!(2000));
    assert_eq!(lines[1]["level"], json!("warn"));

    let levels = admin_get(&gateway, "/admin/um/logs?level=error&level=warn").json();
    assert_eq!(levels["lines"].as_array().expect("level lines").len(), 3);

    let page = admin_get(&gateway, "/admin/um/logs?limit=1").json();
    let page_lines = page["lines"].as_array().expect("page lines");
    assert_eq!(page_lines.len(), 1);
    assert_eq!(page_lines[0]["at"], json!(4000));
    let earlier = page["earlier"]
        .as_str()
        .expect("more history has an earlier cursor")
        .to_string();
    let older = admin_get(
        &gateway,
        &format!("/admin/um/logs?limit=1&cursor={earlier}"),
    )
    .json();
    let older_lines = older["lines"].as_array().expect("older lines");
    assert_eq!(older_lines.len(), 1);
    assert_eq!(older_lines[0]["at"], json!(3000));

    let source = admin_get(&gateway, "/admin/um/logs?source=chat2api").json();
    assert_eq!(source["lines"].as_array().expect("source lines").len(), 4);
    let all = admin_get(&gateway, "/admin/um/logs?source=all").json();
    assert_eq!(all["lines"].as_array().expect("all lines").len(), 4);

    let invalid_time = admin_get(&gateway, "/admin/um/logs?since=nope");
    assert_eq!(invalid_time.status, 400);
    assert_eq!(
        error_body(&invalid_time)["error"]["code"],
        json!("invalid_request")
    );
    let invalid_source = admin_get(&gateway, "/admin/um/logs?source=console");
    assert_eq!(invalid_source.status, 400);
    assert_eq!(
        error_body(&invalid_source)["error"]["code"],
        json!("invalid_request")
    );
}

#[test]
fn the_admin_maintenance_read_is_explicitly_unavailable() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let response = admin_get(&gateway, "/admin/um/maintenance");
    assert_eq!(response.status, 200, "{:?}", response.body);
    let body = response.json();
    assert_eq!(body["status"], json!("unavailable"));
    assert_eq!(body["error"], json!("maintenance_source_unavailable"));
    assert_eq!(body["rows"], json!([]));
    assert!(body["reason"]
        .as_str()
        .is_some_and(|reason| reason.contains("8001")));
    assert_eq!(body["coverage"]["status"], json!("unavailable"));
    assert_eq!(
        body["coverage"]["error"],
        json!("maintenance_source_unavailable")
    );
}

#[test]
fn the_admin_connection_reports_the_openai_compatible_base_url_and_hints() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    let response = admin_get(&gateway, "/admin/connection");
    assert_eq!(response.status, 200, "{:?}", response.body);
    let body = response.json();
    assert_eq!(body["ok"], json!(true));
    assert_eq!(body["service"], json!("chat2api"));
    assert_eq!(body["port"], json!(gateway.addr.port()));
    assert_eq!(
        body["base_url"],
        json!(format!("http://127.0.0.1:{}/v1", gateway.addr.port()))
    );
    assert_eq!(body["protocol"], json!("openai-compatible"));
    assert_eq!(body["interface_type"], json!("openai-compatible"));
    assert_eq!(body["protocol_path"], json!("/v1"));
    assert_eq!(body["health_path"], json!("/health"));
    assert_eq!(body["runtime_state"], json!("running"));
    assert_eq!(body["runtime_healthy"], json!(true));
    assert_eq!(body["auth"], json!("loopback_no_key"));
    assert_eq!(body["key_state"], json!("not_required"));
    assert!(body["checked_epoch"].is_number());
    let hints = body["hints"].as_array().expect("hints");
    assert!(!hints.is_empty(), "接入信息必须带 hints：{body}");
}

#[test]
fn the_admin_reads_distinguish_missing_tokens_from_foreign_origins() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    let gateway = start_local_gateway(&stub, &temp);

    for request in [
        ClientRequest::get("/admin/models"),
        ClientRequest::get("/admin/model-context/probes"),
        ClientRequest::get("/admin/um/services"),
        ClientRequest::get("/admin/um/login-rounds"),
        ClientRequest::get("/admin/um/logs"),
        ClientRequest::get("/admin/um/maintenance"),
        ClientRequest::get("/admin/connection"),
        ClientRequest::post("/admin/model-context/probes", "{}").json_body("{}"),
        ClientRequest::delete("/admin/model-context/probes"),
        ClientRequest::post("/admin/um/services/start", "{}").json_body("{}"),
    ] {
        let response =
            client_request(gateway.addr, &request).expect("unauthenticated admin request");
        assert_eq!(response.status, 403, "{:?}", response.body);
        assert_eq!(
            error_body(&response)["error"]["code"],
            json!("control_denied_token"),
            "{:?}",
            response.body
        );
    }

    let wrong_token = client_request(
        gateway.addr,
        &ClientRequest::get("/admin/models").header("X-Admin-Control", "wrong-token"),
    )
    .expect("wrong token request");
    assert_eq!(wrong_token.status, 403);
    assert_eq!(
        error_body(&wrong_token)["error"]["code"],
        json!("control_denied_token")
    );

    let foreign_origin = client_request(
        gateway.addr,
        &ClientRequest::get("/admin/models")
            .header("X-Admin-Control", CONTROL_TOKEN)
            .header("Origin", DENIED_ORIGIN),
    )
    .expect("foreign origin request");
    assert_eq!(foreign_origin.status, 403);
    assert_eq!(
        error_body(&foreign_origin)["error"]["code"],
        json!("control_denied_endpoint")
    );

    let wrong_method = admin_delete(&gateway, "/admin/models");
    assert_eq!(wrong_method.status, 405);
    assert_eq!(
        error_body(&wrong_method)["error"]["code"],
        json!("method_not_allowed")
    );
}

#[test]
fn the_admin_model_catalog_fresh_read_is_strict_and_never_serves_the_cache() {
    let temp = TempDir::new(unique_temp_prefix());
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        models_body: Some(json!({
            "object": "list",
            "data": [{"id": "fixture-model-a", "name": "Fixture A"}],
        })),
        ..StubBehaviour::default()
    });
    let gateway = start_local_gateway(&stub, &temp);

    let first = admin_get(&gateway, "/admin/models");
    assert_eq!(first.status, 200, "{:?}", first.body);
    assert_eq!(first.json()["fresh"], json!(false));

    stub.set(StubBehaviour {
        models_status: 503,
        models_body: None,
        ..StubBehaviour::default()
    });
    let cached = admin_get(&gateway, "/admin/models");
    assert_eq!(cached.status, 200, "{:?}", cached.body);
    let body = cached.json();
    assert_eq!(body["fresh"], json!(false));
    assert_eq!(body["models"].as_array().expect("cached models").len(), 1);

    let fresh = admin_get(&gateway, "/admin/models?fresh=1");
    assert_eq!(fresh.status, 502, "{:?}", fresh.body);
    assert_eq!(
        error_body(&fresh)["error"]["code"],
        json!("models_unavailable")
    );

    let invalid = admin_get(&gateway, "/admin/models?fresh=yes");
    assert_eq!(invalid.status, 400, "{:?}", invalid.body);
    assert_eq!(
        error_body(&invalid)["error"]["code"],
        json!("invalid_request")
    );

    stub.set(StubBehaviour {
        models_body: Some(json!({
            "object": "list",
            "data": [
                {"id": "fixture-model-a", "name": "Fixture A"},
                {"id": "fixture-model-b", "name": "Fixture B"},
            ],
        })),
        ..StubBehaviour::default()
    });
    let refreshed = admin_get(&gateway, "/admin/models?fresh=1");
    assert_eq!(refreshed.status, 200, "{:?}", refreshed.body);
    let body = refreshed.json();
    assert_eq!(body["fresh"], json!(true));
    assert_eq!(body["models"].as_array().expect("fresh models").len(), 2);
}
