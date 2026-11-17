//! #82: local `/api/console/um/*` read faces.
//!
//! `today`/`trend`/`requests` read the private chat2api ledger; `logs` and
//! `request-logs` read the bounded chat2api run log; `maintenance` reads the
//! ORIGINAL console operation records. Missing/unavailable data keeps its own
//! reading kind and is never folded into a real zero.

mod support;

use chrono::{DateTime, Duration, SecondsFormat};
use serde_json::{json, Value};
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
        .join("\n")
        + "\n",
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
fn logs_coverage_reports_the_actual_tail_window() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    let prefix = "2026-11-17T10:00:00+08:00 INFO ";
    let mut text = String::new();
    while text.len() < 300 * 1024 {
        text.push_str(prefix);
        text.push_str(&"x".repeat(200));
        text.push('\n');
    }
    std::fs::write(&service, &text).expect("write service log");
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
    let size = std::fs::metadata(&service).expect("metadata").len();
    let coverage = &payload["coverage"];
    assert_eq!(coverage["scanned_bytes"], json!(262144));
    let expected_start = {
        let bytes = std::fs::read(&service).expect("read service log");
        let raw_start = (size - 262144) as usize;
        if bytes[raw_start - 1] == b'\n' {
            raw_start as u64
        } else {
            raw_start as u64
                + bytes[raw_start..]
                    .iter()
                    .position(|byte| *byte == b'\n')
                    .expect("line boundary") as u64
                + 1
        }
    };
    assert_eq!(coverage["window_start_offset"], json!(expected_start));
    assert_eq!(coverage["truncated"], json!(true));
}

#[test]
fn logs_scope_reports_the_requested_window_label() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    std::fs::write(
        &service,
        "2026-11-17T10:00:00+08:00 INFO hello\n",
    )
    .expect("write service log");
    for (window, label) in [
        ("today", "今天"),
        ("24h", "近 24 小时"),
        ("7d", "近 7 天"),
        ("30d", "近 30 天"),
    ] {
        let payload = um_logs(
            &service,
            None,
            window,
            &[],
            "service",
            None,
            80,
            now(),
        );
        let scope = payload["scope"].as_str().expect("scope");
        assert!(
            scope.contains(&format!("北京时间{label}")),
            "{window}: {scope}"
        );
    }
}

#[cfg(windows)]
#[test]
fn unreadable_ledger_metadata_is_unavailable_not_missing() {
    let temp = temp();
    let invalid = temp.path().join("bad*name.jsonl");
    let payload = um_today(&invalid, now());
    assert_eq!(payload["coverage"]["status"], json!("unavailable"));
    assert_eq!(payload["coverage"]["ledger_present"], json!(true));
    assert_eq!(
        payload["coverage"]["errors"][0]["type"],
        json!("read_error")
    );
}

#[cfg(windows)]
#[test]
fn unreadable_log_path_is_an_io_error_not_missing() {
    let temp = temp();
    let invalid = temp.path().join("bad*name.log");
    let payload = um_logs(
        &invalid,
        None,
        "today",
        &[],
        "service",
        None,
        80,
        now(),
    );
    assert_eq!(payload["status"], json!("error"));
    assert_eq!(payload["error"], json!("io_error:OSError"));
}

#[cfg(windows)]
#[test]
fn a_directory_log_path_is_denied_not_missing() {
    let temp = temp();
    let directory = temp.path().join("logs");
    std::fs::create_dir_all(&directory).expect("create dir");
    let payload = um_logs(
        &directory,
        None,
        "today",
        &[],
        "service",
        None,
        80,
        now(),
    );
    assert_eq!(payload["status"], json!("denied"));
    assert_eq!(payload["error"], json!("permission_denied"));
}

#[test]
fn request_logs_do_not_guess_a_request_id_from_free_text() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    let id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    std::fs::write(
        &service,
        format!("2026-11-17T10:00:00+08:00 INFO payload {id} tail\n"),
    )
    .expect("write service log");
    let payload = um_request_logs(&service, None, id, 100, now()).expect("request logs");
    assert_eq!(payload["lines"], json!([]));
    assert_eq!(payload["coverage"]["rows_read"], json!(0));
}

#[test]
fn requests_ignore_unreliable_request_ids_for_attachment_and_display() {
    let temp = temp();
    let ledger = temp.path().join("usage-chat2api.jsonl");
    let invalid = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    let rows = [
        json!({
            "time": "2026-11-17T10:00:00+08:00",
            "model": "m1",
            "input_tokens": 10,
            "status": "ok",
            "request_id": invalid,
        }),
        json!({
            "time": "2026-11-17T10:00:01+08:00",
            "model": "m1",
            "input_tokens": 1,
            "status": "upstream_transient_retried",
            "request_id": invalid,
        }),
    ];
    std::fs::write(
        &ledger,
        rows.iter()
            .map(|row| row.to_string())
            .collect::<Vec<_>>()
            .join("\n"),
    )
    .expect("write ledger");
    let payload = um_requests(
        &ledger,
        "today",
        None,
        "all",
        "",
        None,
        50,
        now(),
    )
    .expect("requests");
    let rows = payload["page"]["rows"].as_array().expect("rows");
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0]["requestId"], Value::Null);
    assert_eq!(rows[0]["retries"], Value::Null);
    assert_eq!(payload["coverage"]["unattached_retries"], json!(1));
}

#[test]
fn logs_page_through_every_bounded_reader_window() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    let base = DateTime::parse_from_rfc3339("2026-11-17T10:00:00+08:00")
        .expect("timestamp");
    let mut text = String::new();
    for index in 0..1400 {
        let at = base + Duration::seconds(index);
        text.push_str(&format!(
            "{} INFO line-{:04} {}\n",
            at.to_rfc3339_opts(SecondsFormat::Secs, false),
            index,
            "x".repeat(200)
        ));
    }
    std::fs::write(&service, &text).expect("write service log");
    let mut cursor = None;
    let mut seen = Vec::new();
    for _ in 0..100 {
        let payload = um_logs(
            &service,
            None,
            "today",
            &[],
            "service",
            cursor.as_deref(),
            100,
            now(),
        );
        assert_eq!(payload["status"], json!("ok"));
        for line in payload["lines"].as_array().expect("lines") {
            seen.push(line["text"].as_str().expect("text").to_string());
        }
        match payload["earlier"].as_str() {
            Some(earlier) => cursor = Some(earlier.to_string()),
            None => break,
        }
    }
    assert_eq!(seen.len(), 1400);
    assert!(seen.iter().any(|text| text.starts_with("line-0000")), "{:?}", seen.last());
    assert!(seen.iter().any(|text| text.starts_with("line-1399")), "{:?}", seen.first());
}

#[test]
fn logs_reject_a_malformed_cursor_instead_of_returning_the_first_page() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    std::fs::write(
        &service,
        "2026-11-17T10:00:00+08:00 INFO hello",
    )
    .expect("write service log");
    let payload = um_logs(
        &service,
        None,
        "today",
        &[],
        "service",
        Some("not-a-cursor"),
        80,
        now(),
    );
    assert_eq!(payload["status"], json!("error"));
    assert_eq!(payload["error"], json!("invalid_cursor"));
    assert_eq!(payload["lines"], json!([]));
    assert_eq!(payload["earlier"], Value::Null);
}

#[test]
fn logs_redact_the_same_secret_carriers_as_python() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    let secrets = [
        "cookie-secret",
        "admin-control-secret",
        "api-key-secret",
        "bearer-secret",
        "basic-secret",
        "password-secret",
        "api-key-value",
        "json-token-secret",
        "json-sign-secret",
        "json-code-secret",
        "sk-FAKEFAKE12345678",
        "query-secret",
    ];
    let lines = [
        "2026-11-17T10:00:00+08:00 INFO cookie: session=cookie-secret; theme=dark",
        "2026-11-17T10:00:01+08:00 INFO x-admin-control: admin-control-secret",
        "2026-11-17T10:00:02+08:00 INFO x-api-key: api-key-secret",
        "2026-11-17T10:00:03+08:00 INFO authorization: Bearer bearer-secret",
        "2026-11-17T10:00:04+08:00 INFO authorization: Basic basic-secret",
        "2026-11-17T10:00:05+08:00 INFO password=password-secret",
        "2026-11-17T10:00:06+08:00 INFO api_key=api-key-value",
        "2026-11-17T10:00:07+08:00 INFO {\"token\": \"json-token-secret\"}",
        "2026-11-17T10:00:08+08:00 INFO {\"sign\": \"json-sign-secret\"}",
        "2026-11-17T10:00:09+08:00 INFO {\"code\": \"json-code-secret\"}",
        "2026-11-17T10:00:10+08:00 INFO key sk-FAKEFAKE12345678",
        "2026-11-17T10:00:11+08:00 INFO GET https://x.test/?a=b&token=query-secret",
    ];
    std::fs::write(&service, format!("{}\n", lines.join("\n")))
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
    let rows = payload["lines"].as_array().expect("lines");
    assert_eq!(rows.len(), lines.len());
    let joined = rows
        .iter()
        .map(|row| row["text"].as_str().unwrap_or_default())
        .collect::<Vec<_>>()
        .join("\n");
    for secret in secrets {
        assert!(
            !joined.contains(secret),
            "secret leaked: {secret}\n{joined}"
        );
    }
    assert!(joined.contains("<redacted>"), "{joined}");
    assert!(joined.contains("<redacted-key>"), "{joined}");
}

#[test]
fn logs_parse_launcher_and_go_timestamps_and_keep_the_tag() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    std::fs::write(
        &service,
        [
            "[launcher] 2026/11/16 周三 12:00:00.65 launcher message",
            "2026/11/16 12:00:01 go message",
            "2026-11-16T12:00:02+08:00 INFO watch message",
        ]
        .join("\n")
        + "\n",
    )
    .expect("write service log");
    let payload = um_logs(
        &service,
        None,
        "7d",
        &[],
        "service",
        None,
        80,
        now(),
    );
    assert_eq!(payload["status"], json!("ok"));
    let rows = payload["lines"].as_array().expect("lines");
    assert_eq!(rows.len(), 3);
    assert!(rows.iter().all(|row| row["at"].is_i64()), "{rows:?}");
    let texts: Vec<&str> = rows
        .iter()
        .filter_map(|row| row["text"].as_str())
        .collect();
    assert!(
        texts.contains(&"[launcher] launcher message"),
        "{texts:?}"
    );
    assert!(texts.contains(&"go message"), "{texts:?}");
    assert!(texts.contains(&"watch message"), "{texts:?}");
}

#[test]
fn request_logs_match_only_a_reliable_request_id() {
    let temp = temp();
    let service = temp.path().join("chat2api.log");
    let id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    std::fs::write(
        &service,
        format!("2026-11-17 10:00:00,123 INFO request={id} summary\n"),
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

    let bad_cursor = client_request(
        console.addr,
        &ClientRequest::get("/api/console/um/logs?cursor=not-a-cursor"),
    )
    .expect("bad cursor");
    assert_eq!(bad_cursor.status, 400);
    assert_eq!(bad_cursor.json()["error"], json!("invalid_um_query"));
    assert_eq!(bad_cursor.json()["detail"], json!("invalid_cursor"));

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
