mod common;

use std::path::Path;

use chrono::DateTime;
use serde_json::{json, Value};
use um_desktop::usage::{
    keep_previous_on_failure, keep_previous_page_on_failure, read_request_logs, read_requests,
    read_usage, request_detail, Coverage, Range,
};

const REQUEST: &str = "f314e7d8c7526367baec95ac155b6406";
const OTHER_REQUEST: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

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

fn ledger_row(
    at: &str,
    status: &str,
    model: &str,
    stream: bool,
    protocol: &str,
    input: u64,
    output: u64,
    request_id: &str,
) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": model,
        "stream": stream,
        "status": status,
        "input_tokens": input,
        "output_tokens": output,
        "cached_input_tokens": 0,
        "cache_report_status": "reported",
        "reasoning_tokens": 0,
        "total_tokens": input + output,
        "estimated": false,
        "duration_ms": 900,
        "usage_source": "upstream",
        "protocol": protocol,
        "key_id": null,
        "request_id": request_id,
        "attribution": "absent",
    })
}

fn failed_row(at: &str, model: &str, request_id: &str) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": model,
        "stream": false,
        "status": "upstream_unavailable",
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_report_status": "unreported",
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "estimated": false,
        "duration_ms": 900,
        "usage_source": "none",
        "protocol": "chat",
        "key_id": null,
        "request_id": request_id,
        "attribution": "absent",
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
    assert_eq!(today.buckets[0].tokens, "4,300,000,009");
    assert_eq!(today.buckets[0].requests, "1");
    assert_eq!(today.buckets[0].token_count, Some(4_300_000_009));
    assert_eq!(today.buckets[0].request_count, Some(1));
    assert_eq!(today.token_total, "4,300,000,009");
    assert_eq!(today.request_total, "1");
    let week = read_usage(&ledger, Range::Week, now);
    assert_eq!(week.buckets.len(), 7);
    assert_eq!(week.buckets.last().unwrap().label, "2026-11-20");
    assert_eq!(week.token_total, "4,300,000,513");
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

#[test]
fn success_failure_stream_and_embeddings_rows_are_counted_from_the_ledger() {
    let tmp = common::TempDir::new("usage-shapes");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            ledger_row(
                "2026-11-20T00:01:00+08:00",
                "ok",
                "um-chat",
                false,
                "chat",
                100,
                20,
                REQUEST,
            ),
            failed_row("2026-11-20T00:02:00+08:00", "um-chat", OTHER_REQUEST),
            ledger_row(
                "2026-11-20T00:03:00+08:00",
                "ok",
                "um-chat",
                true,
                "chat",
                40,
                10,
                "22222222222222222222222222222222",
            ),
            ledger_row(
                "2026-11-20T00:04:00+08:00",
                "ok",
                "text-embedding-3-small",
                false,
                "embeddings",
                7,
                0,
                "33333333333333333333333333333333",
            ),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    let today = read_usage(&ledger, Range::Today, now);
    assert_eq!(today.coverage, Coverage::Complete);
    assert_eq!(today.buckets[0].label, "00:00");
    assert_eq!(today.buckets[0].tokens, "≥ 177");
    assert_eq!(today.buckets[0].requests, "4");
    assert_eq!(today.token_total, "≥ 177");
    assert_eq!(today.request_total, "4");
    assert_eq!(today.buckets[0].token_count, Some(177));
    assert_eq!(today.buckets[0].request_count, Some(4));

    let page = read_requests(&ledger, Range::Today, 0, "", None, now);
    assert_eq!(page.rows.len(), 4);
    assert_eq!(
        page.rows.iter().filter(|row| row.result == "成功").count(),
        3
    );
    assert_eq!(
        page.rows.iter().filter(|row| row.result == "失败").count(),
        1
    );
    assert!(page
        .rows
        .iter()
        .any(|row| row.model == "text-embedding-3-small"));
}

#[test]
fn range_switches_keep_ledger_bucket_counts_labels_and_totals() {
    let tmp = common::TempDir::new("usage-ranges");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            ledger_row(
                "2026-11-20T00:01:00+08:00",
                "ok",
                "um-chat",
                false,
                "chat",
                10,
                2,
                REQUEST,
            ),
            ledger_row(
                "2026-11-19T23:59:00+08:00",
                "ok",
                "um-chat",
                false,
                "chat",
                20,
                4,
                OTHER_REQUEST,
            ),
            ledger_row(
                "2026-11-15T12:00:00+08:00",
                "ok",
                "um-chat",
                false,
                "chat",
                30,
                6,
                "22222222222222222222222222222222",
            ),
            ledger_row(
                "2026-11-10T12:00:00+08:00",
                "ok",
                "um-chat",
                false,
                "chat",
                40,
                8,
                "33333333333333333333333333333333",
            ),
            ledger_row(
                "2026-10-20T12:00:00+08:00",
                "ok",
                "um-chat",
                false,
                "chat",
                50,
                10,
                "44444444444444444444444444444444",
            ),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    let today = read_usage(&ledger, Range::Today, now);
    assert_eq!(today.buckets.len(), 24);
    assert_eq!(today.buckets[0].label, "00:00");
    assert_eq!(today.token_total, "12");
    assert_eq!(today.request_total, "1");

    let day = read_usage(&ledger, Range::Day, now);
    assert_eq!(day.buckets.len(), 24);
    assert_eq!(day.buckets.first().unwrap().label, "19:00");
    assert_eq!(day.buckets.last().unwrap().label, "18:00");
    assert_eq!(day.token_total, "36");
    assert_eq!(day.request_total, "2");

    let week = read_usage(&ledger, Range::Week, now);
    assert_eq!(week.buckets.len(), 7);
    assert_eq!(week.buckets.first().unwrap().label, "2026-11-14");
    assert_eq!(week.buckets.last().unwrap().label, "2026-11-20");
    assert_eq!(week.buckets.last().unwrap().tokens, today.token_total);
    assert_eq!(week.buckets.last().unwrap().requests, today.request_total);
    assert_eq!(week.token_total, "72");
    assert_eq!(week.request_total, "3");

    let month = read_usage(&ledger, Range::Month, now);
    assert_eq!(month.buckets.len(), 30);
    assert_eq!(month.buckets.first().unwrap().label, "2026-10-22");
    assert_eq!(month.buckets.last().unwrap().label, "2026-11-20");
    assert_eq!(month.buckets.last().unwrap().tokens, today.token_total);
    assert_eq!(month.token_total, "120");
    assert_eq!(month.request_total, "4");

    let all = read_usage(&ledger, Range::All, now);
    assert_eq!(all.buckets.len(), 32);
    assert_eq!(all.buckets.first().unwrap().label, "2026-10-20");
    assert_eq!(all.buckets.last().unwrap().label, "2026-11-20");
    assert_eq!(all.token_total, "180");
    assert_eq!(all.request_total, "5");
}

#[test]
fn range_indices_match_the_segmented_control_order() {
    assert_eq!(Range::from_index(0), Range::Today);
    assert_eq!(Range::from_index(1), Range::Day);
    assert_eq!(Range::from_index(2), Range::Week);
    assert_eq!(Range::from_index(3), Range::Month);
    assert_eq!(Range::from_index(4), Range::All);
    assert_eq!(Range::from_index(5), Range::Today);
}

#[test]
fn empty_missing_partial_and_unreadable_each_keep_their_own_reason() {
    let tmp = common::TempDir::new("usage-reasons");
    let ledger = tmp.path().join("usage.jsonl");
    let now = time("2026-11-20T19:00:00+08:00");
    let missing = read_usage(&ledger, Range::Today, now);
    assert_eq!(missing.coverage, Coverage::Empty);
    assert_eq!(missing.token_total, "—");
    assert_eq!(missing.request_total, "—");
    assert!(missing.message.contains("尚未生成"));

    std::fs::write(&ledger, "").unwrap();
    let empty = read_usage(&ledger, Range::Today, now);
    assert_eq!(empty.coverage, Coverage::Empty);
    assert_eq!(empty.token_total, "0");
    assert_eq!(empty.request_total, "0");
    assert!(empty.message.contains("没有已记录"));

    let ok = row("2026-11-20T00:02:00+08:00", "ok", 12, 3);
    std::fs::write(&ledger, format!("{ok}\n{{bad JSON\n")).unwrap();
    let partial = read_usage(&ledger, Range::Today, now);
    assert_eq!(partial.coverage, Coverage::Partial);
    assert_eq!(partial.token_total, "≥ 15");
    assert!(partial.message.contains("坏行"));

    let unreadable = read_usage(tmp.path(), Range::Today, now);
    assert_eq!(unreadable.coverage, Coverage::Unavailable);
    assert_eq!(unreadable.token_total, "—");
    assert_eq!(unreadable.request_total, "—");
    assert!(unreadable.message.contains("读取失败"));

    let messages = vec![
        missing.message,
        empty.message,
        partial.message,
        unreadable.message,
    ];
    let mut unique = messages.clone();
    unique.sort();
    unique.dedup();
    assert_eq!(
        unique.len(),
        messages.len(),
        "each state needs its own reason"
    );
}

#[test]
fn diagnostics_filter_by_time_result_and_keyword() {
    let tmp = common::TempDir::new("usage-filters");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            ledger_row(
                "2026-11-20T00:01:00+08:00",
                "ok",
                "alpha",
                false,
                "chat",
                10,
                2,
                REQUEST,
            ),
            failed_row("2026-11-20T00:02:00+08:00", "beta", OTHER_REQUEST),
            ledger_row(
                "2026-11-15T12:00:00+08:00",
                "ok",
                "alpha",
                false,
                "chat",
                20,
                4,
                "22222222222222222222222222222222",
            ),
            failed_row(
                "2026-11-10T12:00:00+08:00",
                "beta",
                "33333333333333333333333333333333",
            ),
            ledger_row(
                "2026-10-20T12:00:00+08:00",
                "ok",
                "alpha",
                false,
                "chat",
                50,
                10,
                "44444444444444444444444444444444",
            ),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    assert_eq!(
        read_requests(&ledger, Range::Today, 0, "", None, now)
            .rows
            .len(),
        2
    );
    assert_eq!(
        read_requests(&ledger, Range::Day, 0, "", None, now)
            .rows
            .len(),
        2
    );
    assert_eq!(
        read_requests(&ledger, Range::Week, 0, "", None, now)
            .rows
            .len(),
        3
    );
    assert_eq!(
        read_requests(&ledger, Range::Month, 0, "", None, now)
            .rows
            .len(),
        4
    );
    assert_eq!(
        read_requests(&ledger, Range::All, 0, "", None, now)
            .rows
            .len(),
        5
    );

    let success = read_requests(&ledger, Range::Month, 1, "", None, now);
    assert_eq!(success.rows.len(), 2);
    assert!(success.rows.iter().all(|row| row.result == "成功"));
    let failed = read_requests(&ledger, Range::Month, 2, "", None, now);
    assert_eq!(failed.rows.len(), 2);
    assert!(failed.rows.iter().all(|row| row.result == "失败"));
    assert!(failed
        .rows
        .iter()
        .any(|row| row.detail.contains("上游不可用")));

    let by_model = read_requests(&ledger, Range::Month, 0, "ALPHA", None, now);
    assert_eq!(by_model.rows.len(), 2);
    let by_request_id = read_requests(&ledger, Range::Month, 0, &OTHER_REQUEST[..8], None, now);
    assert_eq!(by_request_id.rows.len(), 1);
    assert_eq!(
        by_request_id.rows[0].request_id.as_deref(),
        Some(OTHER_REQUEST)
    );
    let no_match = read_requests(&ledger, Range::Month, 0, "gamma", None, now);
    assert!(no_match.rows.is_empty());
    assert_eq!(no_match.coverage, Coverage::Empty);
    assert!(no_match.message.contains("没有符合条件"));
}

#[test]
fn refresh_failure_keeps_the_previous_usage_and_request_page() {
    let tmp = common::TempDir::new("usage-refresh-failure");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(&ledger, &[row("2026-11-20T00:01:00+08:00", "ok", 8, 2)]);
    let now = time("2026-11-20T19:00:00+08:00");
    let previous = read_usage(&ledger, Range::Today, now);
    assert_eq!(previous.coverage, Coverage::Complete);
    assert!(!previous.buckets.is_empty());

    // A directory is unreadable as a ledger, so the refresh fails instead of
    // becoming a legal empty result.
    let failed = read_usage(tmp.path(), Range::Today, now);
    assert_eq!(failed.coverage, Coverage::Unavailable);
    let kept = keep_previous_on_failure(&previous, failed);
    assert_eq!(kept.buckets, previous.buckets);
    assert_eq!(kept.token_total, previous.token_total);
    assert_eq!(kept.request_total, previous.request_total);
    assert!(kept.message.starts_with("刷新失败："), "{}", kept.message);

    let previous_page = read_requests(&ledger, Range::Today, 0, "", None, now);
    assert_eq!(previous_page.rows.len(), 1);
    let failed_page = read_requests(tmp.path(), Range::Today, 0, "", None, now);
    assert_eq!(failed_page.coverage, Coverage::Unavailable);
    let kept_page = keep_previous_page_on_failure(&previous_page, failed_page);
    assert_eq!(kept_page.rows, previous_page.rows);
    assert_eq!(kept_page.earlier, previous_page.earlier);
    assert!(
        kept_page.message.starts_with("刷新失败："),
        "{}",
        kept_page.message
    );
}

#[test]
fn request_detail_shows_ledger_fields_and_only_sanitized_events_for_this_request() {
    let tmp = common::TempDir::new("usage-detail");
    let ledger = tmp.path().join("usage.jsonl");
    let log = tmp.path().join("gateway.log");
    write_rows(&ledger, &[row("2026-11-20T00:01:00+08:00", "ok", 8, 2)]);
    write_rows(
        &log,
        &[
            json!({
                "time": "2026-11-20T00:01:00+08:00",
                "request_id": REQUEST,
                "event": "request_summary",
                "protocol": "chat",
                "final": "ok",
                "tries": 1,
                "authorization": "secret-must-not-escape",
            }),
            json!({
                "time": "2026-11-20T00:01:01+08:00",
                "request_id": REQUEST,
                "event": "upstream_retry",
                "error": "password=hunter2 api_key=sk-secret authorization=Bearer top-secret",
            }),
            json!({
                "time": "2026-11-20T00:01:02+08:00",
                "request_id": OTHER_REQUEST,
                "event": "request_summary",
                "protocol": "chat",
                "final": "upstream_unavailable",
                "tries": 2,
            }),
        ],
    );
    let now = time("2026-11-20T19:00:00+08:00");
    let page = read_requests(&ledger, Range::Today, 0, "", None, now);
    let request = page.rows.first().expect("one ledger row");
    assert_eq!(request.request_id.as_deref(), Some(REQUEST));

    let pending = request_detail(request, None);
    assert!(pending.contains("正在读取关联运行日志"));

    let logs = read_request_logs(&log, request.request_id.as_deref(), now);
    assert_eq!(logs.lines.len(), 2);
    let detail = request_detail(request, Some(&logs));
    assert!(detail.contains("fixture-model"));
    assert!(detail.contains(REQUEST));
    assert!(detail.contains("1200 ms"));
    assert!(detail.contains("输入  8 Token"));
    assert!(detail.contains("输出  2 Token"));
    assert!(detail.contains("运行日志"));
    assert!(detail.contains("request_summary"));
    assert!(!detail.contains("secret-must-not-escape"));
    assert!(!detail.contains("hunter2"));
    assert!(!detail.contains("sk-secret"));
    assert!(!detail.contains("top-secret"));

    let no_id = um_desktop::usage::RequestRow {
        request_id: None,
        ..request.clone()
    };
    let without_id = read_request_logs(&log, None, now);
    assert!(without_id.message.contains("没有可关联的请求 ID"));
    let without_id_detail = request_detail(&no_id, Some(&without_id));
    assert!(without_id_detail.contains("请求 ID  —"));
    assert!(without_id_detail.contains("没有可关联的请求 ID"));

    let unreadable = read_request_logs(&tmp.path().join("missing.log"), Some(REQUEST), now);
    assert!(unreadable.message.contains("暂不可读"));
    let unreadable_detail = request_detail(request, Some(&unreadable));
    assert!(unreadable_detail.contains("暂不可读"));
}
