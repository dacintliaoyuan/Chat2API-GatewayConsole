//! #86: local usage-tokens / usage-rates / stats read faces.
//!
//! The local role reads the private chat2api ledger and the frozen DSH day
//! snapshot. Missing ledgers keep their coverage honest (no fabricated rows or
//! zero totals), and the cloud role still refuses these faces.

mod support;

use chrono::{FixedOffset, TimeZone, Utc};
use serde_json::{json, Value};

use support::*;
use um_console::Role;

fn beijing() -> FixedOffset {
    FixedOffset::east_opt(8 * 3600).expect("+08:00")
}

fn today_stamp() -> (String, String) {
    let now = Utc::now().with_timezone(&beijing());
    let today = now.date_naive();
    let stamp = beijing()
        .from_local_datetime(&today.and_hms_opt(0, 0, 0).expect("midnight"))
        .single()
        .expect("local midnight")
        .format("%Y-%m-%dT%H:%M:%S+08:00")
        .to_string();
    (today.format("%Y-%m-%d").to_string(), stamp)
}

fn write_jsonl(path: &std::path::Path, rows: &[Value]) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).expect("create ledger dir");
    }
    std::fs::write(
        path,
        rows.iter()
            .map(Value::to_string)
            .collect::<Vec<_>>()
            .join("\n")
            + "\n",
    )
    .expect("write ledger");
}

fn local_console(temp: &um_test_harness::TempDataDir) -> Console {
    start_console_with(temp, stopped_runtime(), |config| {
        config.role = Role::Local;
        config.port = 8001;
        config.gateway_port = 8000;
        config.usage_log = temp.path().join("usage-chat2api.jsonl");
        config.dsh_snapshot = temp.path().join("dsh-ledger-20260902.json");
        config.service_log = temp.path().join("chat2api/chat2api.log");
        config.watch_dir = Some(temp.path().join("watch"));
        config.share_backend_dir = Some(temp.path().join("share-backend"));
    })
}

fn seed_usage(temp: &um_test_harness::TempDataDir) -> (String, String) {
    let (today, stamp) = today_stamp();
    let ledger = temp.path().join("usage-chat2api.jsonl");
    write_jsonl(
        &ledger,
        &[
            json!({
                "time": stamp,
                "model": "m1",
                "status": "ok",
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 10,
                "reasoning_tokens": 5,
                "duration_ms": 1000,
                "usage_source": "upstream",
            }),
            json!({
                "time": stamp,
                "model": "m1",
                "status": "ok",
                "input_tokens": 30,
                "duration_ms": 500,
                "usage_source": "upstream",
            }),
            json!({
                "time": stamp,
                "model": "m2",
                "status": "error",
                "input_tokens": 50,
                "output_tokens": 10,
                "duration_ms": 0,
                "usage_source": "upstream",
            }),
            json!({
                "time": "not-a-time",
                "model": "m3",
                "status": "ok",
                "input_tokens": 1,
                "output_tokens": 1,
            }),
        ],
    );
    std::fs::write(
        temp.path().join("dsh-ledger-20260902.json"),
        serde_json::to_vec(&json!({
            "days": {
                "2026-09-02": {
                    "calls": 3,
                    "input": 3000,
                    "output": 300,
                    "cacheRead": 100,
                    "reasoning": 20,
                    "byProviderModel": {
                        "openwebui:Test-A": {
                            "calls": 2,
                            "input": 2000,
                            "output": 200,
                            "cacheRead": 100,
                            "reasoning": 10,
                        },
                    },
                },
            },
        }))
        .expect("snapshot"),
    )
    .expect("write snapshot");
    (today, stamp)
}

#[test]
fn usage_tokens_matches_python_shape_and_keeps_missing_data_unknown() {
    let temp = temp();
    let (today, _stamp) = seed_usage(&temp);
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/usage-tokens?days=2"),
    )
    .expect("usage-tokens");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("cache-control"), Some("no-store"));
    let payload = response.json();
    assert_eq!(payload["timezone"], json!("Asia/Shanghai"));
    assert_eq!(payload["coverage"]["days"], json!(2));
    assert_eq!(payload["coverage"]["scanned"], json!(3));
    assert_eq!(payload["coverage"]["complete_rows"], json!(1));
    assert_eq!(payload["coverage"]["partial_rows"], json!(1));
    assert_eq!(
        payload["coverage"]["sources"]["chat2api"]["ledger_present"],
        json!(true)
    );
    assert_eq!(
        payload["coverage"]["sources"]["chat2api"]["events"],
        json!(4)
    );
    assert_eq!(
        payload["coverage"]["sources"]["chat2api"]["undated_records"],
        json!(1)
    );
    let rows = payload["days"].as_array().expect("days");
    let today_row = rows
        .iter()
        .find(|row| row["date"] == json!(today) && row["model"] == json!("m1"))
        .expect("today m1 row");
    assert_eq!(today_row["input_tokens"], json!(100));
    assert_eq!(today_row["output_tokens"], json!(20));
    assert_eq!(today_row["complete"], json!(false));
    assert_eq!(today_row["complete_requests"], json!(1));
    assert_eq!(today_row["partial_requests"], json!(1));
    assert_eq!(today_row["partial_input_tokens"], json!(30));
    assert_eq!(today_row["partial_output_tokens"], Value::Null);
    let missing = payload["missing"].as_array().expect("missing");
    let invalid = missing
        .iter()
        .find(|row| row["reason"] == json!("invalid_time"))
        .expect("invalid_time row");
    assert_eq!(invalid["date"], Value::Null);
    assert_eq!(invalid["input_tokens"], Value::Null);
    assert_eq!(invalid["output_tokens"], Value::Null);
    let partial = missing
        .iter()
        .find(|row| row["reason"] == json!("no_input"))
        .expect("no_input row");
    assert_eq!(partial["date"], json!(today));
    assert_eq!(partial["input_tokens"], json!(30));
    assert_eq!(partial["output_tokens"], Value::Null);
}

#[test]
fn usage_tokens_missing_ledger_keeps_null_coverage_instead_of_zero_rows() {
    let temp = temp();
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/usage-tokens?days=2"),
    )
    .expect("usage-tokens");
    assert_eq!(response.status, 200);
    let payload = response.json();
    assert_eq!(payload["days"], json!([]));
    assert_eq!(payload["missing"], json!([]));
    assert_eq!(payload["coverage"]["scanned"], json!(0));
    assert_eq!(payload["coverage"]["truncated"], json!(false));
    for source in ["chat2api", "dsh"] {
        assert_eq!(
            payload["coverage"]["sources"][source]["ledger_present"],
            json!(false),
            "{source}"
        );
        assert_eq!(
            payload["coverage"]["sources"][source]["events"],
            json!(0),
            "{source}"
        );
        assert_eq!(
            payload["coverage"]["sources"][source]["note"],
            json!("账本文件缺席：本来源不出行，不冒充零用量"),
            "{source}"
        );
    }
}

#[test]
fn usage_rates_matches_python_shape_and_counts_uncomputable_records_as_skipped() {
    let temp = temp();
    let (today, _stamp) = seed_usage(&temp);
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/usage-rates?hours=24"),
    )
    .expect("usage-rates");
    assert_eq!(response.status, 200);
    assert_eq!(response.header("cache-control"), Some("no-store"));
    let payload = response.json();
    assert_eq!(payload["timezone"], json!("Asia/Shanghai"));
    assert_eq!(payload["coverage"]["hours"], json!(24));
    assert_eq!(payload["coverage"]["rows"], json!(1));
    assert_eq!(payload["coverage"]["truncated"], json!(false));
    assert_eq!(payload["skipped"]["missing_output_tokens"], json!(1));
    assert_eq!(payload["skipped"]["missing_duration"], json!(0));
    assert_eq!(payload["skipped"]["invalid_duration"], json!(1));
    assert_eq!(payload["skipped"]["invalid_time"], json!(1));
    assert_eq!(payload["recorded_requests"]["chat2api"], json!(3));
    assert_eq!(payload["recorded_requests"]["dsh"], json!(0));
    let hours = payload["hours"].as_array().expect("hours");
    assert_eq!(hours.len(), 1);
    let row = &hours[0];
    assert_eq!(row["hour"], json!(format!("{today}T00:00:00+08:00")));
    assert_eq!(row["source"], json!("chat2api"));
    assert_eq!(row["model"], json!("m1"));
    assert_eq!(row["output_tokens"], json!(20));
    assert_eq!(row["duration_ms"], json!(1000));
    assert_eq!(row["requests"], json!(1));
    assert_eq!(row["rate"], json!(20.0));
}

#[test]
fn usage_rates_missing_ledger_keeps_empty_hours_and_null_recorded_counts() {
    let temp = temp();
    let console = local_console(&temp);
    let response = client_request(
        console.addr,
        &ClientRequest::get("/api/console/usage-rates?hours=24"),
    )
    .expect("usage-rates");
    assert_eq!(response.status, 200);
    let payload = response.json();
    assert_eq!(payload["hours"], json!([]));
    assert_eq!(
        payload["skipped"],
        json!({
            "missing_output_tokens": 0,
            "missing_duration": 0,
            "invalid_duration": 0,
            "invalid_time": 0,
        })
    );
    assert_eq!(payload["recorded_requests"]["chat2api"], Value::Null);
    assert_eq!(payload["recorded_requests"]["dsh"], Value::Null);
    for source in ["chat2api", "dsh"] {
        assert_eq!(
            payload["coverage"]["sources"][source]["ledger_present"],
            json!(false),
            "{source}"
        );
    }
}
