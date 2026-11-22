mod common;

use std::path::Path;

use chrono::DateTime;
use serde_json::{json, Value};
use um_desktop::usage::{read_request_logs, read_requests, read_usage, Coverage, Range};

const REQUEST: &str = "f314e7d8c7526367baec95ac155b6406";

fn time(text: &str) -> i64 {
    DateTime::parse_from_rfc3339(text)
        .unwrap()
        .timestamp_millis()
}

fn write_rows(path: &Path, rows: &[Value]) {
    let lines = rows
        .iter()
        .map(|row| format!("{row}\n"))
        .collect::<String>();
    std::fs::write(path, lines).unwrap();
}

fn row(at: &str, status: &str, input: u64, output: u64) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": "fixture-model",
        "status": status,
        "input_tokens": input,
        "output_tokens": output,
        "usage_source": "upstream",
        "duration_ms": 1200,
        "request_id": REQUEST,
    })
}

#[test]
fn one_real_ledger_supplies_calendar_buckets_and_logical_request_counts() {
    let tmp = common::TempDir::new("usage");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            row("2026-11-19T23:59:59+08:00", "ok", 500, 4),
            row(
                "2026-11-20T00:01:00+08:00",
                "upstream_transient_retried",
                3,
                0,
            ),
            row("2026-11-20T00:02:00+08:00", "ok", 4_300_000_000, 6),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    let today = read_usage(&ledger, Range::Today, now);
    assert_eq!(today.coverage, Coverage::Complete);
    assert_eq!(today.buckets.len(), 24);
    assert_eq!(today.buckets[0].label, "00:00");
    assert_eq!(today.buckets[0].tokens, "4300000009");
    assert_eq!(today.buckets[0].requests, "1");
    assert!(today.buckets[0].token_fraction.unwrap() > 0.97);
    assert_eq!(today.request_ticks, ["4", "3", "2", "1", "0"]);
    assert_eq!(today.token_ticks, ["4.4B", "3.3B", "2.2B", "1.1B", "0"]);
    assert_eq!(today.token_total, "4300000009");
    assert_eq!(today.request_total, "1");
    let week = read_usage(&ledger, Range::Week, now);
    assert_eq!(week.buckets.len(), 7);
    assert_eq!(week.buckets.last().unwrap().label, "2026-11-20");
    assert_eq!(week.token_total, "4300000513");
    assert_eq!(week.request_total, "2");
    assert_eq!(read_usage(&ledger, Range::Month, now).buckets.len(), 30);
}

#[test]
fn missing_empty_partial_and_unreadable_ledgers_are_distinct() {
    let tmp = common::TempDir::new("usage-states");
    let ledger = tmp.path().join("usage.jsonl");
    let now = time("2026-11-20T19:00:00+08:00");
    let missing = read_usage(&ledger, Range::Today, now);
    assert_eq!(missing.coverage, Coverage::Empty);
    assert_eq!(missing.token_total, "—");
    std::fs::write(&ledger, "").unwrap();
    let empty = read_usage(&ledger, Range::Today, now);
    assert_eq!(empty.coverage, Coverage::Empty);
    assert_eq!(empty.token_total, "0");
    assert_ne!(empty.message, missing.message);
    let ok = row("2026-11-20T00:02:00+08:00", "ok", 12, 3);
    std::fs::write(&ledger, format!("{ok}\n{{bad JSON\n")).unwrap();
    let partial = read_usage(&ledger, Range::Today, now);
    assert_eq!(partial.coverage, Coverage::Partial);
    assert_eq!(partial.token_total, "≥ 15");
    assert_eq!(
        read_usage(tmp.path(), Range::Today, now).coverage,
        Coverage::Unavailable
    );
}

#[test]
fn diagnostics_search_page_and_link_only_matches_for_this_request() {
    let tmp = common::TempDir::new("usage-requests");
    let ledger = tmp.path().join("usage.jsonl");
    let log = tmp.path().join("gateway.log");
    let mut records = (0..61)
        .map(|minute| {
            row(
                &format!("2026-11-20T{:02}:{:02}:00+08:00", minute / 60, minute % 60),
                "ok",
                8,
                2,
            )
        })
        .collect::<Vec<_>>();
    records.push(row(
        "2026-11-20T00:00:01+08:00",
        "upstream_transient_retried",
        2,
        0,
    ));
    write_rows(&ledger, &records);
    write_rows(
        &log,
        &[
            json!({"time": "2026-11-20T00:01:00+08:00", "request_id": REQUEST, "event": "request_summary", "status": "ok", "authorization": "secret-must-not-escape"}),
            json!({"time": "2026-11-20T00:01:00+08:00", "request_id": "f1111111111111111111111111111111", "event": "other"}),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    let first = read_requests(&ledger, Range::Today, 1, "FIXTURE-MODEL", None, now);
    assert_eq!(first.rows.len(), 60);
    let next = read_requests(
        &ledger,
        Range::Today,
        1,
        "FIXTURE-MODEL",
        first.earlier.as_deref(),
        now,
    );
    assert_eq!(next.rows.len(), 1);
    assert!(next.earlier.is_none());
    let request = &next.rows[0];
    assert_eq!(request.at, "11-20 00:00:00");
    assert_eq!(request.request_id.as_deref(), Some(REQUEST));
    assert_eq!(request.duration, "1200 ms");
    assert!(request.detail.contains("物理重试 1 次"));
    let related = read_request_logs(&log, request.request_id.as_deref(), now);
    assert_eq!(related.lines.len(), 1);
    assert!(!related.lines[0].contains("secret-must-not-escape"));
    assert_eq!(
        read_requests(&ledger, Range::Today, 2, "", None, now).coverage,
        Coverage::Empty
    );
}
