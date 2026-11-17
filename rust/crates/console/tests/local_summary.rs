//! #82: local `/api/summary` is the production `usagesrv.py` local summary.
//!
//! It aggregates the private chat2api ledger (`data/usage-chat2api.jsonl`) and
//! the frozen DSH ledger day snapshot; a missing ledger is the legal empty
//! aggregate Python reports, never a fabricated zero row for an absent source.

mod support;

use serde_json::json;
use um_console::local_summary::{build_local_summary, LocalSummaryPaths};

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
