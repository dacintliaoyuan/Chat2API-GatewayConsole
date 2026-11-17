//! #82: local `/api/console/um/*` read faces.
//!
//! `today`/`trend`/`requests` read the private chat2api ledger; `logs` and
//! `request-logs` read the bounded chat2api run log; `maintenance` reads the
//! ORIGINAL console operation records. Missing/unavailable data keeps its own
//! reading kind and is never folded into a real zero.

mod support;

use chrono::DateTime;
use serde_json::json;
use um_console::local_um::{
    um_logs, um_maintenance, um_request_logs, um_requests, um_today, um_trend,
};
use um_console::operations::OperationsReader;

use support::*;

fn beijing_ms(text: &str) -> f64 {
    DateTime::parse_from_rfc3339(text)
        .expect("timestamp")
        .timestamp_millis() as f64
}

fn now() -> f64 {
    beijing_ms("2026-11-17T12:00:00+08:00")
}

fn ledger(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("usage-chat2api.jsonl");
    let rows = [
        json!({
            "time": "2026-11-17T10:00:00+08:00",
            "model": "m1",
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_input_tokens": 10,
            "cache_report_status": "reported",
            "reasoning_tokens": 5,
            "duration_ms": 1000,
            "request_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "usage_source": "upstream",
            "status": "ok",
        }),
        json!({
            "time": "2026-11-17T10:00:05+08:00",
            "model": "m1",
            "input_tokens": 5,
            "output_tokens": 0,
            "status": "upstream_transient_retried",
            "request_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "usage_source": "upstream",
        }),
        json!({
            "time": "2026-11-17T11:00:00+08:00",
            "model": "m2",
            "input_tokens": 30,
            "status": "upstream_unavailable",
            "duration_ms": 2000,
            "request_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        }),
        json!({
            "time": "2026-11-16T10:00:00+08:00",
            "model": "m1",
            "input_tokens": 7,
            "output_tokens": 3,
            "status": "ok",
        }),
        json!({
            "model": "m3",
            "input_tokens": 1,
        }),
    ];
    std::fs::write(
        &path,
        rows.iter()
            .map(|row| row.to_string())
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .expect("write ledger");
    path
}

#[test]
fn today_reads_logical_requests_and_keeps_retry_consumption() {
    let temp = temp();
    let ledger = ledger(&temp);
    let payload = um_today(&ledger, now());
    let today = &payload["today"];
    assert_eq!(today["day"], json!("2026-11-17"));
    assert_eq!(
        today["tokens"]["input"],
        json!({"kind": "exact", "value": 135})
    );
    assert_eq!(
        today["tokens"]["output"],
        json!({"kind": "atLeast", "value": 20,
                 "note": "旧记录缺少 output Token 字段"})
    );
    assert_eq!(
        today["tokens"]["total"],
        json!({"kind": "atLeast", "value": 155,
                 "note": "旧记录缺少 output Token 字段"})
    );
    assert_eq!(
        today["cacheRead"]["tokens"],
        json!({"kind": "atLeast", "value": 10,
                 "note": "部分记录未报告缓存，合计为下界"})
    );
    assert_eq!(today["cacheRead"]["report"], json!("partial"));
    assert_eq!(
        today["requests"]["recorded"],
        json!({"kind": "exact", "value": 2})
    );
    assert_eq!(
        today["requests"]["failed"],
        json!({"kind": "exact", "value": 1})
    );
    assert_eq!(today["results"]["success"], json!(1));
    assert_eq!(today["results"]["failed"], json!(1));
    assert_eq!(today["results"]["unknown"], json!(0));
    assert_eq!(
        today["speed"]["value"],
        json!({"kind": "estimate", "value": 20.0,
                 "note": "按 1 个可计时请求的整次平均；1 个请求缺输出/耗时，0 个输出为估算"})
    );
    assert_eq!(payload["coverage"]["status"], json!("ok"));
    assert_eq!(payload["coverage"]["events"], json!(5));
    assert_eq!(payload["coverage"]["scanned"], json!(3));
    assert_eq!(payload["coverage"]["undated_records"], json!(1));
    assert_eq!(payload["coverage"]["logical_records"], json!(2));
    assert_eq!(payload["coverage"]["retry_records"], json!(1));
    assert_eq!(payload["timezone"], json!("Asia/Shanghai"));
}

#[test]
fn missing_ledger_keeps_missing_readings_instead_of_zero() {
    let temp = temp();
    let missing = temp.path().join("absent.jsonl");
    let payload = um_today(&missing, now());
    assert_eq!(payload["coverage"]["status"], json!("missing"));
    assert_eq!(
        payload["today"]["tokens"]["total"]["kind"],
        json!("missing")
    );
    assert_eq!(
        payload["today"]["requests"]["recorded"]["kind"],
        json!("missing")
    );
    assert_eq!(payload["today"]["speed"]["samples"], json!(null));
}

#[test]
fn trend_aligns_daily_buckets_and_ranks_models() {
    let temp = temp();
    let ledger = ledger(&temp);
    let payload = um_trend(&ledger, "7d", now()).expect("trend");
    let trend = &payload["trend"];
    assert_eq!(trend["range"], json!("7d"));
    assert_eq!(trend["days"].as_array().map(Vec::len), Some(7));
    assert_eq!(trend["all"]["daily"].as_array().map(Vec::len), Some(7));
    assert_eq!(
        trend["all"]["total"]["tokens"],
        json!({"kind": "atLeast", "value": 165,
                 "note": "旧记录缺少 output Token 字段"})
    );
    assert_eq!(trend["models"][0]["model"], json!("m1"));
    assert_eq!(trend["models"][1]["model"], json!("m2"));
    assert_eq!(payload["coverage"]["status"], json!("ok"));

    let error = um_trend(&ledger, "90d", now()).expect_err("bad range");
    assert!(error.0.contains("invalid trend range"), "{error}");
}

#[test]
fn requests_page_newest_first_and_attach_retries() {
    let temp = temp();
    let ledger = ledger(&temp);
    let payload = um_requests(
        &ledger,
        "today",
        None,
        "all",
        "",
        None,
        1,
        now(),
    )
    .expect("requests");
    let rows = payload["page"]["rows"].as_array().expect("rows");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["model"], json!("m2"));
    assert_eq!(rows[0]["result"], json!("failed"));
    assert_eq!(rows[0]["httpStatus"], json!(null));
    assert!(payload["page"]["earlier"].is_string());
    assert_eq!(payload["models"], json!(["m1", "m2"]));
    assert_eq!(payload["coverage"]["logical_records"], json!(2));
    assert_eq!(payload["coverage"]["retry_records"], json!(1));

    let error = um_requests(
        &ledger,
        "today",
        None,
        "all",
        "",
        Some("not-a-cursor"),
        50,
        now(),
    )
    .expect_err("bad cursor");
    assert!(error.0.contains("invalid cursor"), "{error}");
}

#[test]
fn logs_read_the_bounded_service_window_and_redact_secrets() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    std::fs::write(
        &service,
        [
            "2026-11-17 10:00:00,123 INFO hello token=secret",
            "2026-11-17 11:00:00,456 ERROR boom",
            "2026-11-16 10:00:00,000 INFO yesterday",
        ]
        .join("\n"),
    )
    .expect("write service log");
    let payload = um_logs(
        &service,
        None,
        "today",
        &[],
        "service",
        None,
        80,
        now(),
    );
    assert_eq!(payload["status"], json!("ok"));
    let lines = payload["lines"].as_array().expect("lines");
    assert_eq!(lines.len(), 2);
    assert_eq!(lines[0]["level"], json!("error"));
    assert_eq!(lines[1]["text"], json!("hello token=<redacted>"));
    assert_eq!(lines[0]["source"], json!("chat2api"));
    assert_eq!(payload["coverage"]["status"], json!("ok"));

    let errors = um_logs(
        &service,
        None,
        "today",
        &["error".to_string()],
        "service",
        None,
        80,
        now(),
    );
    assert_eq!(errors["lines"].as_array().map(Vec::len), Some(1));

    let invalid = um_logs(
        &service,
        None,
        "today",
        &[],
        "nope",
        None,
        80,
        now(),
    );
    assert_eq!(invalid["status"], json!("error"));
    assert_eq!(invalid["error"], json!("unknown_log_source"));
}

#[test]
fn request_logs_match_only_a_reliable_request_id() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    let id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    std::fs::write(
        &service,
        format!("2026-11-17 10:00:00,123 INFO {{\"request_id\":\"{id}\",\"message\":\"x\"}}"),
    )
    .expect("write service log");
    let payload = um_request_logs(&service, None, id, 100, now()).expect("request logs");
    assert_eq!(payload["coverage"]["status"], json!("partial"));
    assert_eq!(
        payload["coverage"]["categories"]["service"]["status"],
        json!("ok")
    );
    assert_eq!(payload["lines"].as_array().map(Vec::len), Some(1));
    assert_eq!(payload["lines"][0]["requestId"], json!(id));

    let error = um_request_logs(&service, None, "bad-id", 100, now()).expect_err("bad id");
    assert!(error.0.contains("invalid_request_id"), "{error}");
}

fn seed_operations(temp: &um_test_harness::TempDataDir) -> std::path::PathBuf {
    let path = temp.path().join("console.sqlite3");
    let connection = rusqlite::Connection::open(&path).expect("open operations db");
    connection
        .execute_batch(
            "CREATE TABLE console_meta(key TEXT PRIMARY KEY, value TEXT);
             CREATE TABLE operations(
                id TEXT PRIMARY KEY, action TEXT, target TEXT, state TEXT,
                resource TEXT, generation INTEGER, created_at REAL,
                started_at REAL, finished_at REAL, cancel_requested INTEGER,
                steps_json TEXT, result_json TEXT, error_json TEXT,
                idempotency_key TEXT);
             INSERT INTO console_meta(key, value) VALUES('schema_version', '1');",
        )
        .expect("create operations schema");
    for (id, action, target, state, created, result) in [
        (
            "op-um",
            "account.um_status",
            "chat2api",
            "succeeded",
            1_700_000_000.0,
            Some("{\"status\":\"valid\"}"),
        ),
        (
            "op-share",
            "share.status",
            "share",
            "succeeded",
            1_700_000_100.0,
            None,
        ),
        (
            "op-service",
            "service.start",
            "other",
            "failed",
            1_700_000_200.0,
            None,
        ),
    ] {
        connection
            .execute(
                "INSERT INTO operations(id, action, target, state, resource, generation,
                    created_at, started_at, finished_at, cancel_requested, steps_json,
                    result_json, error_json, idempotency_key)
                 VALUES(?, ?, ?, ?, 'chat2api', 0, ?, ?, NULL, 0, '[]', ?, NULL, NULL)",
                rusqlite::params![id, action, target, state, created, created, result],
            )
            .expect("insert operation");
    }
    path
}

#[test]
fn maintenance_reads_only_um_operations_from_the_original_records() {
    let temp = temp();
    let path = seed_operations(&temp);
    let reader = OperationsReader::new(Some(path));
    let payload = um_maintenance(&reader, 50);
    let rows = payload["rows"].as_array().expect("rows");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["key"], json!("op-um"));
    assert_eq!(rows[0]["action"], json!("account.um_status"));
    assert_eq!(rows[0]["state"], json!("succeeded"));
    assert_eq!(rows[0]["at"], json!(1_700_000_000_000i64));
    assert_eq!(rows[0]["result"], json!("status=valid"));
    assert_eq!(payload["coverage"]["status"], json!("ok"));
    assert_eq!(payload["coverage"]["operations_read"], json!(3));
    assert_eq!(payload["coverage"]["rows_returned"], json!(1));

    let missing = um_maintenance(&OperationsReader::new(None), 50);
    assert_eq!(missing["coverage"]["status"], json!("ok"));
    assert_eq!(missing["rows"], json!([]));
}

#[test]
fn routes_serve_um_faces_and_keep_invalid_queries_distinct() {
    let temp = temp();
    let ledger = ledger(&temp);
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Local;
        config.port = 8001;
        config.usage_log = ledger.clone();
        config.dsh_snapshot = temp.path().join("absent-dsh.json");
        config.service_log = temp.path().join("chat2api.log");
    });
    let today = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/today"),
    )
    .expect("today");
    assert_eq!(today.status, 200);
    assert_eq!(today.json()["coverage"]["status"], json!("ok"));
    assert_eq!(
        today.json()["today"]["tokens"]["input"],
        json!({"kind": "exact", "value": 135})
    );

    let bad_range = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/trend?range=90d"),
    )
    .expect("bad range");
    assert_eq!(bad_range.status, 400);
    assert_eq!(bad_range.json()["error"], json!("invalid_um_query"));
    assert!(bad_range.json()["detail"]
        .as_str()
        .is_some_and(|detail| detail.contains("invalid trend range")));

    let bad_limit = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/requests?limit=abc"),
    )
    .expect("bad limit");
    assert_eq!(bad_limit.status, 400);
    assert_eq!(bad_limit.json()["error"], json!("invalid_um_query"));

    let bad_source = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/logs?source=nope"),
    )
    .expect("bad source");
    assert_eq!(bad_source.status, 400);
    assert_eq!(bad_source.json()["error"], json!("invalid_um_query"));
    assert_eq!(bad_source.json()["detail"], json!("unknown_log_source"));

    let bad_id = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/request-logs?request_id=bad"),
    )
    .expect("bad id");
    assert_eq!(bad_id.status, 400);
    assert_eq!(bad_id.json()["error"], json!("invalid_um_query"));

    let unknown = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/nope"),
    )
    .expect("unknown face");
    assert_eq!(unknown.status, 404);

    let write = client_request(
        console.addr,
        &ClientRequest::post("/api/console/um/today", b"{}".to_vec()),
    )
    .expect("write face");
    assert_eq!(write.status, 404);

    let missing = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/maintenance"),
    )
    .expect("maintenance");
    assert_eq!(missing.status, 200);
    assert_eq!(missing.json()["rows"], json!([]));
}
