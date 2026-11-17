//! #82: local `/api/summary` is the production `usagesrv.py` local summary.
//!
//! It aggregates the private chat2api ledger (`data/usage-chat2api.jsonl`) and
//! the frozen DSH ledger day snapshot; a missing ledger is the legal empty
//! aggregate Python reports, never a fabricated zero row for an absent source.

mod support;

use serde_json::json;
use um_console::local_summary::{build_local_summary, LocalSummaryCache, LocalSummaryPaths};

use support::*;

fn paths(temp: &um_test_harness::TempDataDir) -> LocalSummaryPaths {
    LocalSummaryPaths {
        usage_log: temp.path().join("usage-chat2api.jsonl"),
        dsh_snapshot: temp.path().join("dsh-ledger-20260902.json"),
    }
}

#[test]
fn local_summary_matches_the_python_bucket_shape_and_totals() {
    let temp = temp();
    let paths = paths(&temp);
    std::fs::write(
        &paths.usage_log,
        [
            json!({
                "time": "2026-11-17T10:00:00+08:00",
                "model": "m1",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_input_tokens": 10,
                "reasoning_tokens": 5,
                "cache_report_status": "reported",
            }),
            json!({
                "time": "2026-11-17T11:00:00+08:00",
                "model": "m2",
                "input_tokens": 20,
                "output_tokens": 10,
                "cache_report_status": "unreported",
            }),
            json!({
                "time": "2026-11-16T10:00:00+08:00",
                "model": "m1",
                "input_tokens": 1,
                "output_tokens": 1,
            }),
        ]
        .iter()
        .map(|row| row.to_string())
        .collect::<Vec<_>>()
        .join("\n"),
    )
    .expect("write usage ledger");
    std::fs::write(
        &paths.dsh_snapshot,
        json!({
            "days": {
                "2026-09-02": {
                    "calls": 3,
                    "input": 30,
                    "output": 10,
                    "cacheRead": 5,
                    "reasoning": 2,
                    "byProviderModel": {
                        "deepseek:chat": {
                            "calls": 3,
                            "input": 30,
                            "output": 10,
                            "cacheRead": 5,
                            "reasoning": 2,
                        },
                    },
                },
                "2026-09-03": {"calls": 99, "input": 99, "output": 99},
            },
        })
        .to_string(),
    )
    .expect("write dsh snapshot");

    let summary = build_local_summary(&paths);
    assert_eq!(summary["sources"]["chat2apiLog"], json!("usage-chat2api.jsonl"));
    assert_eq!(summary["sources"]["dshLedger"], json!("dsh-ledger-20260902.json"));
    assert_eq!(summary["dsh"]["url"], json!("http://127.0.0.1:3080"));
    assert_eq!(
        summary["dsh"]["note"],
        json!("用量为 2026-09-02 账本日快照的原始用量；快照生成方法见 data/dsh-ledger-20260902.json 内 _说明")
    );

    let chat2api = &summary["services"]["chat2api"];
    assert_eq!(chat2api["kind"], json!("estimate"));
    assert_eq!(chat2api["requests"], json!(3));
    assert_eq!(
        chat2api["tokens"],
        json!({"input": 121, "output": 61, "cache": 10, "reasoning": 5})
    );
    assert_eq!(chat2api["cacheReportedRequests"], json!(1));
    assert_eq!(chat2api["cacheUnreportedRequests"], json!(1));
    assert_eq!(chat2api["cacheUnknownRequests"], json!(1));
    assert_eq!(chat2api["unknownUsageRequests"], json!(0));
    assert_eq!(
        chat2api["models"],
        json!([
            {"model": "m1", "requests": 2, "input": 101, "output": 51, "cache": 10,
             "reasoning": 5, "unknown_usage": 0, "cache_reported": 1,
             "cache_unreported": 0, "cache_unknown": 1},
            {"model": "m2", "requests": 1, "input": 20, "output": 10, "cache": 0,
             "reasoning": 0, "unknown_usage": 0, "cache_reported": 0,
             "cache_unreported": 1, "cache_unknown": 0},
        ])
    );
    assert_eq!(
        chat2api["days"],
        json!([
            {"day": "2026-11-16", "requests": 1, "input": 1, "output": 1, "cache": 0, "reasoning": 0},
            {"day": "2026-11-17", "requests": 2, "input": 120, "output": 60, "cache": 10, "reasoning": 5},
        ])
    );

    let dsh = &summary["services"]["dsh"];
    assert_eq!(dsh["kind"], json!("dsh-ledger"));
    assert_eq!(dsh["requests"], json!(3));
    assert_eq!(
        dsh["tokens"],
        json!({"input": 30, "output": 10, "cache": 5, "reasoning": 2})
    );
    assert_eq!(
        dsh["models"],
        json!([{"model": "deepseek:chat", "requests": 3, "input": 30, "output": 10,
                 "cache": 5, "reasoning": 2}])
    );
    assert_eq!(
        dsh["days"],
        json!([{"day": "2026-09-02", "requests": 3, "input": 30, "output": 10,
                 "cache": 5, "reasoning": 2}])
    );
}

#[test]
fn local_summary_keeps_missing_ledgers_as_pythons_empty_aggregate() {
    let temp = temp();
    let paths = paths(&temp);
    let summary = build_local_summary(&paths);
    assert_eq!(summary["services"]["chat2api"]["requests"], json!(0));
    assert_eq!(summary["services"]["chat2api"]["tokens"]["input"], json!(0));
    assert_eq!(summary["services"]["dsh"]["requests"], json!(0));
    assert_eq!(summary["services"]["dsh"]["models"], json!([]));
    assert_eq!(summary["services"]["dsh"]["days"], json!([]));
}

#[test]
fn local_summary_route_serves_the_cached_local_payload() {
    let temp = temp();
    let paths = paths(&temp);
    std::fs::write(
        &paths.usage_log,
        json!({
            "time": "2026-11-17T10:00:00+08:00",
            "model": "m1",
            "input_tokens": 7,
            "output_tokens": 3,
        })
        .to_string(),
    )
    .expect("write usage ledger");
    let console = start_console_with(&temp, stopped_runtime(), |config| {
        config.role = um_console::Role::Local;
        config.port = 8001;
        config.usage_log = paths.usage_log.clone();
        config.dsh_snapshot = paths.dsh_snapshot.clone();
    });
    let response = client_request(console.addr, &ClientRequest::get("/api/summary")).expect("summary");
    assert_eq!(response.status, 200);
    let payload = response.json();
    assert_eq!(payload["services"]["chat2api"]["requests"], json!(1));
    assert_eq!(payload["services"]["chat2api"]["tokens"]["input"], json!(7));
    assert!(payload["_generated_epoch"].is_number());
    assert_eq!(payload["_cached_age_seconds"], json!(0.0));
}

#[test]
fn local_summary_reads_at_most_the_last_200000_ledger_records() {
    let temp = temp();
    let paths = paths(&temp);
    let mut lines = Vec::with_capacity(200_001);
    lines.push(
        json!({
            "time": "2026-11-17T00:00:00+08:00",
            "model": "dropped",
            "input_tokens": 999,
        })
        .to_string(),
    );
    for _ in 0..200_000 {
        lines.push(
            json!({
                "time": "2026-11-17T00:00:00+08:00",
                "model": "kept",
                "input_tokens": 1,
            })
            .to_string(),
        );
    }
    std::fs::write(&paths.usage_log, lines.join("\n")).expect("write usage ledger");

    let summary = build_local_summary(&paths);
    let chat2api = &summary["services"]["chat2api"];
    assert_eq!(chat2api["requests"], json!(200_000));
    assert_eq!(chat2api["tokens"]["input"], json!(200_000));
    assert_eq!(chat2api["models"].as_array().expect("models").len(), 1);
    assert_eq!(chat2api["models"][0]["model"], json!("kept"));
    assert_eq!(chat2api["models"][0]["requests"], json!(200_000));
}

#[test]
fn local_summary_reports_corrupt_token_values_as_unavailable() {
    let temp = temp();
    let paths = paths(&temp);
    std::fs::write(
        &paths.usage_log,
        json!({
            "time": "2026-11-17T10:00:00+08:00",
            "model": "m1",
            "input_tokens": "not-a-number",
        })
        .to_string(),
    )
    .expect("write usage ledger");

    assert_eq!(
        build_local_summary(&paths),
        json!({"error": "summary unavailable"})
    );
    let cache = um_console::local_summary::LocalSummaryCache::new(paths);
    assert_eq!(cache.payload(), json!({"error": "summary unavailable"}));
}

#[test]
fn local_summary_treats_non_integer_token_values_as_corruption() {
    let temp = temp();
    let paths = paths(&temp);
    std::fs::write(
        &paths.usage_log,
        json!({
            "time": "2026-11-17T10:00:00+08:00",
            "model": "m1",
            "input_tokens": 1.5,
        })
        .to_string(),
    )
    .expect("write usage ledger");

    assert_eq!(
        build_local_summary(&paths),
        json!({"error": "summary unavailable"})
    );
}

#[test]
fn local_summary_reports_corrupt_dsh_snapshot_shapes_as_unavailable() {
    let temp = temp();
    let paths = paths(&temp);
    std::fs::write(
        &paths.dsh_snapshot,
        json!({"days": "not-an-object"}).to_string(),
    )
    .expect("write dsh snapshot");
    assert_eq!(
        build_local_summary(&paths),
        json!({"error": "summary unavailable"})
    );

    std::fs::write(
        &paths.dsh_snapshot,
        json!({
            "days": {
                "2026-09-02": {"byProviderModel": "not-an-object"},
            },
        })
        .to_string(),
    )
    .expect("write dsh snapshot");
    assert_eq!(
        build_local_summary(&paths),
        json!({"error": "summary unavailable"})
    );
}

fn write_single_usage(path: &std::path::Path, model: &str, input_tokens: i64) {
    std::fs::write(
        path,
        json!({
            "time": "2026-11-17T10:00:00+08:00",
            "model": model,
            "input_tokens": input_tokens,
        })
        .to_string(),
    )
    .expect("write usage ledger");
}

#[test]
fn local_summary_cache_serves_stale_data_while_refreshing_in_the_background() {
    let temp = temp();
    let paths = paths(&temp);
    write_single_usage(&paths.usage_log, "before", 1);
    let cache = LocalSummaryCache::with_cache_seconds(paths.clone(), 0.0);

    let first = cache.payload();
    assert_eq!(
        first["services"]["chat2api"]["models"][0]["model"],
        json!("before")
    );

    write_single_usage(&paths.usage_log, "after", 2);
    let stale = cache.payload();
    assert_eq!(
        stale["services"]["chat2api"]["models"][0]["model"],
        json!("before")
    );
    assert!(stale["_cached_age_seconds"].as_f64().unwrap_or_default() >= 0.0);

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        let payload = cache.payload();
        if payload["services"]["chat2api"]["models"][0]["model"] == json!("after") {
            assert_eq!(payload["services"]["chat2api"]["tokens"]["input"], json!(2));
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "background refresh never produced the new summary: {payload}"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
}

#[test]
fn local_summary_cache_keeps_stale_data_and_marks_a_refresh_failure() {
    let temp = temp();
    let paths = paths(&temp);
    write_single_usage(&paths.usage_log, "before", 1);
    let cache = LocalSummaryCache::with_cache_seconds(paths.clone(), 0.0);

    let first = cache.payload();
    assert_eq!(
        first["services"]["chat2api"]["models"][0]["model"],
        json!("before")
    );

    std::fs::write(
        &paths.usage_log,
        json!({
            "time": "2026-11-17T10:00:00+08:00",
            "model": "corrupt",
            "input_tokens": "not-a-number",
        })
        .to_string(),
    )
    .expect("write corrupt ledger");
    let _ = cache.payload();

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        let payload = cache.payload();
        if payload["_refresh_error"] == json!("summary unavailable") {
            assert_eq!(payload["_stale"], json!(true));
            assert_eq!(
                payload["services"]["chat2api"]["models"][0]["model"],
                json!("before")
            );
            assert_eq!(payload["services"]["chat2api"]["requests"], json!(1));
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "refresh failure was never marked: {payload}"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
}
