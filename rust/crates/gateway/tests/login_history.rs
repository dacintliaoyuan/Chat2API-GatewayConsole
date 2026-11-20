//! #115: the durable UM login-round history written by 8000.
//!
//! Every started round is one record; concurrent triggers and internal browser
//! retries stay under that round. The file is append-only JSONL with rotation,
//! seven-day retention and a 4 MiB total cap. A round left `running` by an exited
//! process is marked `incomplete` on the next start while its progress stays.

mod support;

use std::fs;
use std::path::Path;

use support::TempDir;

use um_gateway::login::history::{
    read_records, HistoryLimits, LoginHistory, MAX_TOTAL_BYTES, RETENTION_SECONDS,
};
use um_gateway::reauth::ReauthTrigger;

fn history_file(temp: &TempDir) -> std::path::PathBuf {
    temp.path().join("login-history.jsonl")
}

fn total_bytes(path: &Path) -> u64 {
    let mut total = 0;
    for name in [
        "login-history.jsonl",
        "login-history.jsonl.1",
        "login-history.jsonl.2",
        "login-history.jsonl.3",
    ] {
        if let Ok(metadata) = fs::metadata(path.with_file_name(name)) {
            total += metadata.len();
        }
    }
    total
}

#[test]
fn a_completed_round_records_trigger_visibility_submit_and_result() {
    let temp = TempDir::new("um-rust-login-history-round-");
    let path = history_file(&temp);
    let history = LoginHistory::open_at(Some(path.clone()), 1_000.0);
    history.begin_at(1, Some(ReauthTrigger::AuthWatch), false, 1_000.0);
    history.note_submission(1, 1);
    history.finish_at(1, "restored", None, Some(200), Some("valid"), 1_060.0);

    let records = read_records(&path).expect("read history");
    assert_eq!(records.len(), 1);
    let record = &records[0];
    assert!(!record.run_id.is_empty(), "运行标识必须落盘");
    assert_eq!(record.round, 1);
    assert_eq!(record.state, "completed");
    assert_eq!(record.trigger_source.as_deref(), Some("auth_watch"));
    assert!(!record.visible);
    assert_eq!(record.started_epoch, 1_000.0);
    assert_eq!(record.ended_epoch, Some(1_060.0));
    assert_eq!(record.submit_count, 1);
    assert_eq!(record.outcome.as_deref(), Some("restored"));
    assert_eq!(record.reason, None);
    assert_eq!(record.upstream_status, Some(200));
    assert_eq!(record.evidence_verdict.as_deref(), Some("valid"));
}

#[test]
fn an_abandoned_round_is_incomplete_and_keeps_its_progress() {
    let temp = TempDir::new("um-rust-login-history-incomplete-");
    let path = history_file(&temp);
    let history = LoginHistory::open_at(Some(path.clone()), 1_000.0);
    let old_run = history.run_id().to_string();
    history.begin_at(1, Some(ReauthTrigger::BusinessRequest), true, 1_000.0);
    history.note_submission(1, 1);
    drop(history);

    let reopened = LoginHistory::open_at(Some(path.clone()), 1_100.0);
    assert_ne!(reopened.run_id(), old_run, "新进程必须有独立运行标识");
    let records = read_records(&path).expect("read history");
    assert_eq!(records.len(), 1);
    let record = &records[0];
    assert_eq!(record.state, "incomplete");
    assert_eq!(record.reason.as_deref(), Some("process_exit"));
    assert_eq!(record.started_epoch, 1_000.0);
    assert_eq!(record.ended_epoch, None);
    assert_eq!(record.submit_count, 1, "未完成轮次必须保留已记录进度");
    assert_eq!(record.trigger_source.as_deref(), Some("business_request"));
    assert!(record.visible);

    reopened.begin_at(1, Some(ReauthTrigger::Explicit), false, 1_100.0);
    reopened.finish_at(1, "restored", None, Some(200), Some("valid"), 1_200.0);
    let records = read_records(&path).expect("read history");
    assert_eq!(records.len(), 2);
    let newest = &records[0];
    let older = &records[1];
    assert_eq!(newest.round, 1);
    assert_eq!(older.round, 1);
    assert_ne!(
        newest.run_id, older.run_id,
        "同一轮次号在不同进程里必须可区分"
    );
    assert_eq!(older.state, "incomplete");
    assert_eq!(newest.state, "completed");
}

#[test]
fn records_older_than_seven_days_are_evicted_from_the_oldest() {
    let temp = TempDir::new("um-rust-login-history-retention-");
    let path = history_file(&temp);
    let now = 100.0 * 86_400.0;
    let history = LoginHistory::open_at(Some(path.clone()), now);
    history.begin_at(
        1,
        Some(ReauthTrigger::Explicit),
        false,
        now - 8.0 * 86_400.0,
    );
    history.finish_at(
        1,
        "restored",
        None,
        Some(200),
        Some("valid"),
        now - 8.0 * 86_400.0 + 1.0,
    );
    history.begin_at(
        2,
        Some(ReauthTrigger::Explicit),
        false,
        now - 6.0 * 86_400.0,
    );
    history.finish_at(
        2,
        "restored",
        None,
        Some(200),
        Some("valid"),
        now - 6.0 * 86_400.0 + 1.0,
    );
    history.prune_at(now);

    let records = read_records(&path).expect("read history");
    assert_eq!(
        records.len(),
        1,
        "超过 {RETENTION_SECONDS} 秒的记录必须淘汰"
    );
    assert_eq!(records[0].round, 2);
}

#[test]
fn rotation_files_are_read_back_with_the_active_file() {
    let temp = TempDir::new("um-rust-login-history-rotation-");
    let path = history_file(&temp);
    let limits = HistoryLimits {
        rotate_bytes: 16_384,
        max_total_bytes: MAX_TOTAL_BYTES,
        retention_seconds: RETENTION_SECONDS,
    };
    let history = LoginHistory::open_with_limits(Some(path.clone()), limits, 1_000.0);
    let reason = "x".repeat(160);
    for round in 1..=50u64 {
        history.begin_at(
            round,
            Some(ReauthTrigger::Explicit),
            false,
            1_000.0 + round as f64,
        );
        history.finish_at(
            round,
            "restored",
            Some(&reason),
            Some(200),
            Some("valid"),
            1_000.0 + round as f64 + 0.5,
        );
    }

    assert!(
        path.with_file_name("login-history.jsonl.1").exists(),
        "超过单文件阈值必须轮转"
    );
    let records = read_records(&path).expect("read history");
    assert_eq!(records.len(), 50, "轮转文件必须与活动文件一起读回");
    assert_eq!(records.last().map(|record| record.round), Some(1));
    assert_eq!(records.first().map(|record| record.round), Some(50));
}

#[test]
fn the_total_size_cap_evicts_the_oldest_records() {
    let temp = TempDir::new("um-rust-login-history-size-");
    let path = history_file(&temp);
    let limits = HistoryLimits {
        rotate_bytes: 1_000,
        max_total_bytes: 4_000,
        retention_seconds: RETENTION_SECONDS,
    };
    let history = LoginHistory::open_with_limits(Some(path.clone()), limits, 1_000.0);
    let reason = "x".repeat(160);
    for round in 1..=100u64 {
        history.begin_at(
            round,
            Some(ReauthTrigger::Explicit),
            false,
            1_000.0 + round as f64,
        );
        history.finish_at(
            round,
            "restored",
            Some(&reason),
            Some(200),
            Some("valid"),
            1_000.0 + round as f64 + 0.5,
        );
    }

    assert!(
        total_bytes(&path) <= limits.max_total_bytes,
        "活动文件与轮转文件合计不得超过 {} 字节",
        limits.max_total_bytes
    );
    let records = read_records(&path).expect("read history");
    assert!(records.len() < 100, "触达容量上限时必须从最旧记录开始淘汰");
    assert_eq!(records.first().map(|record| record.round), Some(100));
    assert!(
        records.last().map(|record| record.round) > Some(1),
        "最旧记录必须已被淘汰"
    );
}

#[test]
fn a_write_failure_never_blocks_the_caller() {
    let temp = TempDir::new("um-rust-login-history-write-failure-");
    let blocked = temp.path().join("history-is-a-directory");
    fs::create_dir(&blocked).expect("create blocker");
    let history = LoginHistory::open_at(Some(blocked.clone()), 1_000.0);
    history.begin_at(1, Some(ReauthTrigger::Explicit), false, 1_000.0);
    history.note_submission(1, 1);
    history.finish_at(1, "restored", None, Some(200), Some("valid"), 1_060.0);
    assert!(read_records(&blocked).is_err(), "目录不是可读的历史文件");
}
