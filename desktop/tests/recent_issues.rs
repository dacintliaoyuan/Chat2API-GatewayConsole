//! Slint-level behaviour for the overview recent-issues card (#151).
//!
//! The card reads the instance ledger's request results, the gateway's login-round
//! history, the UM service card and the model catalog/probe read faces. A read
//! failure keeps its reason and never becomes a zero; a source without an
//! operation-record origin says 不可用 with the reason. The request rows carry the
//! result filter the 查看证据 jump applies.

mod common;

use std::path::Path;

use chrono::DateTime;
use serde_json::{json, Value};

use um_desktop::models::{CatalogView, ModelRow, ProbeHistoryRow, ProbeRow, ProbeView};
use um_desktop::overview::Tone;
use um_desktop::recent_issues::{read_recent_issues, IssueQuery, LoginRounds, RecentIssues};
use um_desktop::um_service::ServiceView;

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

fn ledger_row(at: &str, status: &str, model: &str, request_id: &str) -> Value {
    json!({
        "time": at,
        "service": "chat2api",
        "model": model,
        "stream": false,
        "status": status,
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_input_tokens": 0,
        "cache_report_status": "reported",
        "reasoning_tokens": 0,
        "total_tokens": 12,
        "estimated": false,
        "duration_ms": 900,
        "usage_source": "upstream",
        "protocol": "chat",
        "key_id": null,
        "request_id": request_id,
        "attribution": "absent",
    })
}

fn login_ok(rounds: Value) -> LoginRounds {
    LoginRounds::Rounds(json!({"status": "ok", "rounds": rounds}))
}

fn running_service() -> ServiceView {
    ServiceView {
        state: "运行中".to_string(),
        detail: "端口 7999 · PID 4321".to_string(),
        tone: Tone::Good,
        checked_at: "核对于 10:00:00".to_string(),
        gateway_missing: false,
        gateway_detail: String::new(),
        can_start_gateway: false,
    }
}

fn clean_catalog() -> CatalogView {
    CatalogView {
        status: "UM 目录共 1 个模型".to_string(),
        tone: Tone::Neutral,
        stale: false,
        empty: false,
        models: vec![ModelRow {
            id: "fixture-model".to_string(),
            name: "Fixture".to_string(),
            context: "未知".to_string(),
            efforts: "未知".to_string(),
            last_test: "未测".to_string(),
            detail: String::new(),
        }],
    }
}

fn clean_probe() -> ProbeView {
    ProbeView {
        status: "尚未执行上下文探测".to_string(),
        tone: Tone::Neutral,
        running: false,
        stop_requested: false,
        progress: String::new(),
        budget: "请求预算：不可用".to_string(),
        rows: Vec::<ProbeRow>::new(),
        history: Vec::<ProbeHistoryRow>::new(),
    }
}

fn read(
    ledger: &Path,
    now: i64,
    login: &LoginRounds,
    service: Option<&ServiceView>,
    catalog: Option<&CatalogView>,
    probe: Option<&ProbeView>,
) -> RecentIssues {
    read_recent_issues(ledger, now, login, service, catalog, probe)
}

fn read_clean(ledger: &Path, now: i64) -> RecentIssues {
    let login = login_ok(json!([]));
    let service = running_service();
    let catalog = clean_catalog();
    let probe = clean_probe();
    read(
        ledger,
        now,
        &login,
        Some(&service),
        Some(&catalog),
        Some(&probe),
    )
}

#[test]
fn request_failures_truncations_and_client_disconnects_come_from_the_ledger() {
    let tmp = common::TempDir::new("recent-issues-ledger");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[
            ledger_row(
                "2026-11-23T09:00:00+08:00",
                "ok",
                "alpha",
                "11111111111111111111111111111111",
            ),
            ledger_row(
                "2026-11-23T09:01:00+08:00",
                "upstream_unavailable",
                "alpha",
                "22222222222222222222222222222222",
            ),
            ledger_row(
                "2026-11-23T09:02:00+08:00",
                "upstream_unavailable",
                "beta",
                "33333333333333333333333333333333",
            ),
            ledger_row(
                "2026-11-23T09:03:00+08:00",
                "upstream_missing_finish_reason",
                "beta",
                "44444444444444444444444444444444",
            ),
            ledger_row(
                "2026-11-23T09:04:00+08:00",
                "client_disconnected",
                "gamma",
                "55555555555555555555555555555555",
            ),
            ledger_row(
                "2026-11-23T09:05:00+08:00",
                "cancelled",
                "gamma",
                "66666666666666666666666666666666",
            ),
        ],
    );
    let now = time("2026-11-23T19:00:00+08:00");

    let issues = read_clean(&ledger, now);
    let titles: Vec<&str> = issues.rows.iter().map(|row| row.title.as_str()).collect();
    assert_eq!(titles, ["请求失败", "截断", "客户端断开"]);

    let failed = &issues.rows[0];
    assert_eq!(failed.count, "2 次");
    assert!(failed.detail.contains("今天账本"), "{:?}", failed.detail);
    assert_eq!(
        failed.evidence,
        Some(IssueQuery {
            result: um_desktop::config::RequestResult::Failed,
            window: um_desktop::config::TimeRange::Today,
        })
    );
    assert_eq!(failed.tone, Tone::Bad);
    assert!(failed.evidence_note.is_empty());

    let truncated = &issues.rows[1];
    assert_eq!(truncated.count, "1 次");
    assert!(truncated.detail.contains("截断"), "{:?}", truncated.detail);
    assert_eq!(
        truncated.evidence,
        Some(IssueQuery {
            result: um_desktop::config::RequestResult::Truncated,
            window: um_desktop::config::TimeRange::Today,
        })
    );

    let disconnected = &issues.rows[2];
    assert_eq!(disconnected.count, "1 次");
    assert!(
        disconnected.detail.contains("客户端"),
        "{:?}",
        disconnected.detail
    );
    assert_eq!(
        disconnected.evidence,
        Some(IssueQuery {
            result: um_desktop::config::RequestResult::ClientDisconnected,
            window: um_desktop::config::TimeRange::Today,
        })
    );
    assert_eq!(disconnected.tone, Tone::Warn);
    assert_eq!(issues.status, "近期问题 4 项");
    assert_eq!(issues.tone, Tone::Bad);
}

#[test]
fn a_clean_ledger_and_clean_read_faces_show_recently_no_issues() {
    let tmp = common::TempDir::new("recent-issues-clean");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[ledger_row(
            "2026-11-23T09:00:00+08:00",
            "ok",
            "alpha",
            "11111111111111111111111111111111",
        )],
    );
    let now = time("2026-11-23T19:00:00+08:00");

    let issues = read_clean(&ledger, now);
    assert!(issues.rows.is_empty(), "{:?}", issues.rows);
    assert_eq!(issues.status, "近期无问题");
    assert_eq!(issues.tone, Tone::Good);
}

#[test]
fn an_unreadable_ledger_keeps_its_reason_instead_of_zero() {
    let tmp = common::TempDir::new("recent-issues-ledger-failure");
    let ledger = tmp.path().join("usage.jsonl");
    // A directory cannot be read as a ledger: the read face is unavailable,
    // which is not the legal empty state.
    std::fs::create_dir(&ledger).unwrap();
    let now = time("2026-11-23T19:00:00+08:00");

    let issues = read_clean(&ledger, now);
    let failed = issues
        .rows
        .iter()
        .find(|row| row.title.contains("请求结果读取失败"))
        .expect("a failed request read is its own row");
    assert_eq!(failed.count, "不可用");
    assert!(
        failed.detail.contains("读取失败") || failed.detail.contains("账本"),
        "{:?}",
        failed.detail
    );
    assert_eq!(failed.evidence, None);
    assert!(
        failed.evidence_note.contains("证据不可用"),
        "{:?}",
        failed.evidence_note
    );
    assert!(issues.status.contains("读取不完整"), "{:?}", issues.status);
    assert!(
        issues.rows.iter().all(|row| row.count != "0 次"),
        "a failed read never becomes zero"
    );
}

#[test]
fn failed_and_manual_takeover_login_rounds_are_counted_with_the_latest_reason() {
    let tmp = common::TempDir::new("recent-issues-login");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[ledger_row(
            "2026-11-23T09:00:00+08:00",
            "ok",
            "alpha",
            "11111111111111111111111111111111",
        )],
    );
    let now = time("2026-11-23T19:00:00+08:00");
    let login = login_ok(json!([
        {
            "round": 1,
            "state": "completed",
            "outcome": "rejected",
            "reason": "invalid_credentials",
            "startedAt": 1_700_000_000_000i64,
        },
        {
            "round": 2,
            "state": "completed",
            "outcome": "manual_takeover",
            "reason": "umpass_mfa_required",
            "startedAt": 1_700_000_100_000i64,
        },
        {
            "round": 3,
            "state": "incomplete",
            "reason": "process_exit",
            "startedAt": 1_700_000_200_000i64,
        },
        {
            "round": 4,
            "state": "completed",
            "outcome": "restored",
            "startedAt": 1_700_000_300_000i64,
        },
        {
            "round": 5,
            "state": "completed",
            "outcome": "cancelled",
            "startedAt": 1_700_000_400_000i64,
        },
    ]));
    let service = running_service();
    let catalog = clean_catalog();
    let probe = clean_probe();

    let issues = read(
        &ledger,
        now,
        &login,
        Some(&service),
        Some(&catalog),
        Some(&probe),
    );
    let row = issues
        .rows
        .iter()
        .find(|row| row.title.contains("登录"))
        .expect("a login row");
    assert_eq!(row.count, "3 轮");
    assert!(row.detail.contains("失败 2 轮"), "{:?}", row.detail);
    assert!(row.detail.contains("人工接管 1 轮"), "{:?}", row.detail);
    assert!(row.detail.contains("轮次中断"), "{:?}", row.detail);
    assert!(row.detail.contains("process_exit"), "{:?}", row.detail);
    assert_eq!(row.evidence, None);
    assert!(
        row.evidence_note.contains("设置页"),
        "{:?}",
        row.evidence_note
    );
}

#[test]
fn a_login_round_read_failure_keeps_its_reason_instead_of_zero() {
    let tmp = common::TempDir::new("recent-issues-login-failure");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(
        &ledger,
        &[ledger_row(
            "2026-11-23T09:00:00+08:00",
            "ok",
            "alpha",
            "11111111111111111111111111111111",
        )],
    );
    let now = time("2026-11-23T19:00:00+08:00");
    let login = LoginRounds::Unavailable("登录历史不可读：permission denied".to_string());
    let service = running_service();
    let catalog = clean_catalog();
    let probe = clean_probe();

    let issues = read(
        &ledger,
        now,
        &login,
        Some(&service),
        Some(&catalog),
        Some(&probe),
    );
    let row = issues
        .rows
        .iter()
        .find(|row| row.title.contains("登录轮次读取失败"))
        .expect("a failed login read is its own row");
    assert_eq!(row.count, "不可用");
    assert!(row.detail.contains("permission denied"), "{:?}", row.detail);
    assert_eq!(row.evidence, None);
    assert!(issues.status.contains("读取不完整"), "{:?}", issues.status);
    assert!(
        issues
            .failures
            .iter()
            .any(|failure| failure.contains("登录")),
        "{:?}",
        issues.failures
    );
}

#[test]
fn a_login_round_payload_without_rounds_is_a_read_failure_not_zero() {
    let tmp = common::TempDir::new("recent-issues-login-shape");
    let ledger = tmp.path().join("usage.jsonl");
    write_rows(&ledger, &[]);
    let now = time("2026-11-23T19:00:00+08:00");
    let login = login_ok(json!(Value::Null));
    let service = running_service();
    let catalog = clean_catalog();
    let probe = clean_probe();

    let issues = read(
        &ledger,
        now,
        &login,
        Some(&service),
        Some(&catalog),
        Some(&probe),
    );
    let row = issues
        .rows
        .iter()
        .find(|row| row.title.contains("登录轮次读取失败"))
        .expect("a malformed login payload is not a zero");
    assert_eq!(row.count, "不可用");
    assert!(row.detail.contains("rounds"), "{:?}", row.detail);
}

fn stopped_service() -> ServiceView {
    ServiceView {
        state: "未运行".to_string(),
        detail: "端口 7999 · 本实例网关已停止".to_string(),
        tone: Tone::Bad,
        checked_at: "核对于 10:00:00".to_string(),
        gateway_missing: false,
        gateway_detail: String::new(),
        can_start_gateway: false,
    }
}

fn missing_gateway_service() -> ServiceView {
    ServiceView {
        state: "不可用".to_string(),
        detail: "网关未运行，先启动网关".to_string(),
        tone: Tone::Bad,
        checked_at: "核对时间未知".to_string(),
        gateway_missing: true,
        gateway_detail: "本实例网关尚未启动".to_string(),
        can_start_gateway: true,
    }
}

fn failed_service() -> ServiceView {
    ServiceView {
        state: "读取失败".to_string(),
        detail: "connection refused".to_string(),
        tone: Tone::Bad,
        checked_at: "核对时间未知".to_string(),
        gateway_missing: false,
        gateway_detail: String::new(),
        can_start_gateway: false,
    }
}

fn failed_catalog() -> CatalogView {
    CatalogView {
        status: "刷新失败：UM 返回了登录页；下表仍是上次取到的目录，已过期。".to_string(),
        tone: Tone::Bad,
        stale: true,
        empty: false,
        models: clean_catalog().models,
    }
}

fn failed_probe() -> ProbeView {
    ProbeView {
        status: "最近一次探测已结束 · 已验证 1 · 未通过 1 · 已取消 0".to_string(),
        tone: Tone::Warn,
        running: false,
        stop_requested: false,
        progress: "已完成 2".to_string(),
        budget: "请求预算：最多 20 次 · 每次最多 64 Token".to_string(),
        rows: vec![
            ProbeRow {
                model_id: "fixture-model".to_string(),
                name: "Fixture".to_string(),
                state: "已验证".to_string(),
                summary: "≥256,000 Token".to_string(),
                tone: Tone::Good,
                selected: false,
            },
            ProbeRow {
                model_id: "fixture-model-b".to_string(),
                name: "Fixture B".to_string(),
                state: "未通过".to_string(),
                summary: "上限 64,000 Token · 原因：ContextLimitRefused".to_string(),
                tone: Tone::Bad,
                selected: false,
            },
        ],
        history: Vec::<ProbeHistoryRow>::new(),
    }
}

fn unreadable_probe() -> ProbeView {
    ProbeView {
        status: "读取上下文探测失败：timeout".to_string(),
        tone: Tone::Bad,
        running: false,
        stop_requested: false,
        progress: String::new(),
        budget: "请求预算：不可用".to_string(),
        rows: Vec::<ProbeRow>::new(),
        history: Vec::<ProbeHistoryRow>::new(),
    }
}

fn read_with(
    ledger: &Path,
    now: i64,
    service: Option<&ServiceView>,
    catalog: Option<&CatalogView>,
    probe: Option<&ProbeView>,
) -> RecentIssues {
    let login = login_ok(json!([]));
    read(ledger, now, &login, service, catalog, probe)
}

fn clean_ledger(label: &str) -> common::TempDir {
    let tmp = common::TempDir::new(label);
    write_rows(
        &tmp.path().join("usage.jsonl"),
        &[ledger_row(
            "2026-11-23T09:00:00+08:00",
            "ok",
            "alpha",
            "11111111111111111111111111111111",
        )],
    );
    tmp
}

#[test]
fn a_stopped_or_missing_service_is_a_clear_fact_not_zero() {
    let tmp = clean_ledger("recent-issues-service");
    let ledger = tmp.path().join("usage.jsonl");
    let now = time("2026-11-23T19:00:00+08:00");
    let catalog = clean_catalog();
    let probe = clean_probe();

    let stopped = stopped_service();
    let issues = read_with(&ledger, now, Some(&stopped), Some(&catalog), Some(&probe));
    let row = issues
        .rows
        .iter()
        .find(|row| row.title.contains("服务"))
        .expect("a stopped service is an issue");
    assert_eq!(row.title, "UM 服务未运行");
    assert_eq!(row.count, "1 项");
    assert!(row.detail.contains("端口 7999"), "{:?}", row.detail);
    assert_eq!(row.evidence, None);
    assert!(
        row.evidence_note.contains("不在请求页"),
        "{:?}",
        row.evidence_note
    );

    let missing = missing_gateway_service();
    let issues = read_with(&ledger, now, Some(&missing), Some(&catalog), Some(&probe));
    let row = issues
        .rows
        .iter()
        .find(|row| row.title.contains("网关"))
        .expect("a missing gateway is an issue");
    assert_eq!(row.title, "本实例网关未运行");
    assert_eq!(row.count, "1 项");
    assert!(row.detail.contains("尚未启动"), "{:?}", row.detail);
}

#[test]
fn catalog_refresh_failure_and_probe_failures_are_counted() {
    let tmp = clean_ledger("recent-issues-catalog");
    let ledger = tmp.path().join("usage.jsonl");
    let now = time("2026-11-23T19:00:00+08:00");
    let service = running_service();
    let catalog = failed_catalog();
    let probe = failed_probe();

    let issues = read_with(&ledger, now, Some(&service), Some(&catalog), Some(&probe));
    let row = issues
        .rows
        .iter()
        .find(|row| row.title == "目录/测试")
        .expect("catalog and test issues share one row");
    assert_eq!(row.count, "2 项");
    assert!(row.detail.contains("目录刷新失败"), "{:?}", row.detail);
    assert!(
        row.detail.contains("上下文探测未通过 1 个模型"),
        "{:?}",
        row.detail
    );
    assert!(
        row.detail.contains("Fixture B") && row.detail.contains("ContextLimitRefused"),
        "{:?}",
        row.detail
    );
    assert_eq!(row.evidence, None);
    assert!(
        row.evidence_note.contains("设置页"),
        "{:?}",
        row.evidence_note
    );
}

#[test]
fn a_probe_read_failure_keeps_its_reason_instead_of_zero() {
    let tmp = clean_ledger("recent-issues-probe-failure");
    let ledger = tmp.path().join("usage.jsonl");
    let now = time("2026-11-23T19:00:00+08:00");
    let service = running_service();
    let catalog = clean_catalog();
    let probe = unreadable_probe();

    let issues = read_with(&ledger, now, Some(&service), Some(&catalog), Some(&probe));
    let row = issues
        .rows
        .iter()
        .find(|row| row.title == "目录/测试")
        .expect("a probe read failure is its own issue");
    assert_eq!(row.count, "不可用");
    assert!(row.detail.contains("timeout"), "{:?}", row.detail);
    assert_eq!(row.evidence, None);
}

#[test]
fn a_service_read_failure_keeps_its_reason_instead_of_zero() {
    let tmp = clean_ledger("recent-issues-service-failure");
    let ledger = tmp.path().join("usage.jsonl");
    let now = time("2026-11-23T19:00:00+08:00");
    let service = failed_service();
    let catalog = clean_catalog();
    let probe = clean_probe();

    let issues = read_with(&ledger, now, Some(&service), Some(&catalog), Some(&probe));
    let row = issues
        .rows
        .iter()
        .find(|row| row.title.contains("服务状态读取失败"))
        .expect("a service read failure is its own row");
    assert_eq!(row.count, "不可用");
    assert!(
        row.detail.contains("connection refused"),
        "{:?}",
        row.detail
    );
    assert_eq!(row.evidence, None);
}
#[test]
fn never_read_service_and_catalog_test_show_unavailable_without_faking_zero() {
    let tmp = clean_ledger("recent-issues-unread");
    let ledger = tmp.path().join("usage.jsonl");
    let now = time("2026-11-23T19:00:00+08:00");

    let issues = read_with(&ledger, now, None, None, None);
    let service = issues
        .rows
        .iter()
        .find(|row| row.title == "服务")
        .expect("a never-read service row");
    assert_eq!(service.count, "不可用");
    assert!(
        service.detail.contains("不可用（本机实例无操作记录来源）"),
        "{:?}",
        service.detail
    );
    assert_eq!(service.evidence, None);

    let catalog = issues
        .rows
        .iter()
        .find(|row| row.title == "目录/测试")
        .expect("a never-read catalog/test row");
    assert_eq!(catalog.count, "不可用");
    assert!(
        catalog.detail.contains("不可用（本机实例无操作记录来源）"),
        "{:?}",
        catalog.detail
    );
    assert_eq!(catalog.evidence, None);
    assert!(issues.status.contains("读取不完整"), "{:?}", issues.status);
}

use std::cell::Cell;
use std::rc::Rc;
use std::time::Duration;

use slint::{ComponentHandle, LogicalSize};
use um_desktop::recent_issues::{apply_recent_issues, IssueRow as DomainIssueRow};
use um_desktop::ui::*;

thread_local! {
    static UI_INIT: std::sync::Once = std::sync::Once::new();
}

fn issues_window() -> MainWindow {
    UI_INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(0);
    window.window().set_size(LogicalSize::new(1060.0, 900.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);
    window
}

fn request_issue() -> DomainIssueRow {
    DomainIssueRow {
        title: "请求失败".to_string(),
        count: "2 次".to_string(),
        detail: "今天账本记录了 2 次失败；重试不重复计请求".to_string(),
        tone: Tone::Bad,
        evidence: Some(IssueQuery {
            result: um_desktop::config::RequestResult::Failed,
            window: um_desktop::config::TimeRange::Today,
        }),
        evidence_note: String::new(),
    }
}

fn unavailable_issue() -> DomainIssueRow {
    DomainIssueRow {
        title: "服务".to_string(),
        count: "不可用".to_string(),
        detail: "不可用（本机实例无操作记录来源）：尚未读取 UM 服务状态".to_string(),
        tone: Tone::Bad,
        evidence: None,
        evidence_note: "证据不可用：尚未读取 UM 服务状态".to_string(),
    }
}

fn issues(rows: Vec<DomainIssueRow>) -> RecentIssues {
    RecentIssues {
        status: if rows.is_empty() {
            "近期无问题".to_string()
        } else {
            format!("近期问题 {} 项", rows.len())
        },
        tone: if rows.is_empty() {
            Tone::Good
        } else {
            Tone::Bad
        },
        rows,
        total: 2,
        failures: Vec::new(),
    }
}

#[test]
fn the_card_shows_rows_and_the_evidence_jump_applies_the_result_filter() {
    let window = issues_window();
    let issues = issues(vec![request_issue(), unavailable_issue()]);
    apply_recent_issues(&window, &issues);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert_eq!(window.get_recent_issue_count(), 2);
    assert_eq!(window.get_recent_issues_status_text(), "近期问题 2 项");
    assert_eq!(window.get_recent_issues_tone_value(), 2);
    assert_eq!(window.get_recent_issue_title_0(), "请求失败");
    assert_eq!(window.get_recent_issue_count_text_0(), "2 次");
    assert!(window.get_recent_issue_evidence_enabled_0());
    assert!(!window.get_recent_issue_evidence_enabled_1());
    assert!(
        window.get_recent_issue_evidence_note_1().contains("不可用"),
        "{:?}",
        window.get_recent_issue_evidence_note_1()
    );
    assert!(window.get_recent_issues_card_height() > 0.0);

    let queries = Rc::new(Cell::new(0));
    window.on_query_requests({
        let queries = queries.clone();
        move |_| queries.set(queries.get() + 1)
    });
    window.set_page(2);
    window.set_request_result(0);
    window.set_request_period(3);
    window.invoke_jump_to_issue(0);

    assert_eq!(window.get_page(), 1);
    assert_eq!(window.get_request_result(), 2);
    assert_eq!(window.get_request_period(), 0);
    assert_eq!(window.get_request_keyword(), "");
    assert_eq!(window.get_request_model_filter(), "");
    assert_eq!(window.get_request_model_index(), 0);
    assert_eq!(queries.get(), 1);
}

#[test]
fn an_empty_card_says_recently_no_issues() {
    let window = issues_window();
    apply_recent_issues(&window, &issues(Vec::new()));
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert_eq!(window.get_recent_issue_count(), 0);
    assert_eq!(window.get_recent_issues_status_text(), "近期无问题");
    assert_eq!(window.get_recent_issues_tone_value(), 0);
    assert!(window.get_recent_issues_card_height() > 0.0);
}

#[test]
fn a_read_failure_keeps_its_reason_and_disables_the_jump() {
    let window = issues_window();
    let row = DomainIssueRow {
        title: "请求结果读取失败".to_string(),
        count: "不可用".to_string(),
        detail: "读取失败：账本读取失败或结构损坏".to_string(),
        tone: Tone::Bad,
        evidence: None,
        evidence_note: "证据不可用：请求结果读取失败".to_string(),
    };
    let issues = RecentIssues {
        status: "近期问题读取失败：请求结果读取失败".to_string(),
        tone: Tone::Bad,
        rows: vec![row],
        total: 0,
        failures: vec!["请求结果读取失败".to_string()],
    };
    apply_recent_issues(&window, &issues);
    i_slint_backend_testing::mock_elapsed_time(Duration::ZERO);

    assert_eq!(window.get_recent_issue_count(), 1);
    assert_eq!(
        window.get_recent_issues_status_text(),
        "近期问题读取失败：请求结果读取失败"
    );
    assert_eq!(window.get_recent_issue_title_0(), "请求结果读取失败");
    assert!(
        window.get_recent_issue_detail_0().contains("读取失败"),
        "{:?}",
        window.get_recent_issue_detail_0()
    );
    assert!(!window.get_recent_issue_evidence_enabled_0());
    assert!(
        window
            .get_recent_issue_evidence_note_0()
            .contains("证据不可用"),
        "{:?}",
        window.get_recent_issue_evidence_note_0()
    );
}
