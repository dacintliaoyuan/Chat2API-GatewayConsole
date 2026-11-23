//! Behaviour of the settings page's UM login-round table (#159).
//!
//! The table reads the app instance's gateway `/admin/um/login-rounds`; a missing
//! or unreadable history keeps its reason instead of becoming an empty table, and an
//! unknown closed-set value reads 「未知」 instead of being folded into success or
//! failure.

use serde_json::json;
use slint::{ComponentHandle, LogicalSize, Model};
use um_desktop::gateway::ControlToken;
use um_desktop::login_rounds::{
    apply_login_rounds, open_round, parse_login_rounds, query_url, read, LoginRoundsApi,
    LoginRoundsQuery, LoginRoundsView,
};
use um_desktop::overview::Tone;
use um_desktop::ui::*;

fn payload(rounds: serde_json::Value) -> serde_json::Value {
    json!({
        "status": "ok",
        "error": null,
        "rounds": rounds,
        "count": 1,
        "truncated": false,
        "scope": "本机实例 UM 登录轮次历史（最近 7 天 / 4 MiB）· 只读，不触发探测或登录 · 本页 1 条",
        "coverage": {
            "status": "ok",
            "error": null,
            "source": "login-history.jsonl",
            "bounded": true,
            "note": "读取失败/损坏不是合法空列表",
        },
    })
}

fn round() -> serde_json::Value {
    json!({
        "key": "run-1:3",
        "runId": "run-1",
        "round": 3,
        "state": "completed",
        "triggerSource": "auth_watch",
        "driver": "native",
        "visible": false,
        "startedAt": 1_765_000_000_000i64,
        "endedAt": 1_765_000_060_000i64,
        "submitCount": 2,
        "outcome": "restored",
        "reason": "duo_approved",
        "detail": "openwebui_login_button",
        "upstreamStatus": 200,
        "evidenceVerdict": "valid",
        "rejectedBeforeExpiryStatus": 401,
    })
}

fn round_with(
    round: u64,
    reason: Option<&str>,
    outcome: Option<&str>,
    trigger: Option<&str>,
    rejected: Option<u16>,
) -> serde_json::Value {
    json!({
        "key": format!("run-{round}:{round}"),
        "runId": format!("run-{round}"),
        "round": round,
        "state": "completed",
        "triggerSource": trigger,
        "driver": "native",
        "visible": false,
        "startedAt": 1_765_000_000_000i64,
        "endedAt": 1_765_000_060_000i64,
        "submitCount": 1,
        "outcome": outcome,
        "reason": reason,
        "detail": null,
        "upstreamStatus": 200,
        "evidenceVerdict": "valid",
        "rejectedBeforeExpiryStatus": rejected,
    })
}

fn rows(view: LoginRoundsView) -> Vec<String> {
    let LoginRoundsView::Loaded(page) = view else {
        panic!("a successful read must be loaded, got {view:?}");
    };
    page.rows.into_iter().map(|row| row.round).collect()
}

struct FakeApi {
    payload: Result<serde_json::Value, String>,
    urls: std::sync::Mutex<Vec<String>>,
    tokens: std::sync::Mutex<Vec<String>>,
}

impl FakeApi {
    fn returning(payload: serde_json::Value) -> Self {
        Self {
            payload: Ok(payload),
            urls: std::sync::Mutex::new(Vec::new()),
            tokens: std::sync::Mutex::new(Vec::new()),
        }
    }
}

impl LoginRoundsApi for FakeApi {
    fn get_json(&self, url: &str, token: &str) -> Result<serde_json::Value, String> {
        self.urls.lock().unwrap().push(url.to_string());
        self.tokens.lock().unwrap().push(token.to_string());
        self.payload.clone()
    }
}

#[test]
fn an_empty_history_is_an_empty_state_not_a_table() {
    let view = parse_login_rounds(&payload(json!([])), &LoginRoundsQuery::default())
        .expect("empty history");
    let LoginRoundsView::Loaded(page) = &view else {
        panic!("a legal empty history must be loaded, got {view:?}");
    };
    assert!(page.rows.is_empty());
    assert!(!page.table_visible);
    assert_eq!(page.status, "暂无登录轮次");
    assert_eq!(page.empty_text, "暂无登录轮次");
    assert_eq!(page.tone, Tone::Neutral);

    let filtered = parse_login_rounds(
        &payload(json!([])),
        &LoginRoundsQuery {
            round: "9".to_string(),
            q: String::new(),
        },
    )
    .expect("filtered empty history");
    let LoginRoundsView::Loaded(page) = &filtered else {
        panic!("a filtered empty history must be loaded, got {filtered:?}");
    };
    assert!(!page.table_visible);
    assert_eq!(page.empty_text, "没有符合条件的登录轮次");
    assert_ne!(page.empty_text, "暂无登录轮次");
}

#[test]
fn an_unconfigured_or_unreadable_history_keeps_its_reason() {
    let unconfigured = parse_login_rounds(
        &json!({
            "status": "unconfigured",
            "error": "login_history_unconfigured",
            "rounds": [],
            "count": 0,
            "truncated": false,
            "scope": "本机实例 UM 登录轮次历史",
            "coverage": {"status": "unconfigured", "error": "login_history_unconfigured"},
        }),
        &LoginRoundsQuery::default(),
    )
    .expect("unconfigured history");
    let LoginRoundsView::Unavailable { reason } = &unconfigured else {
        panic!("an unconfigured history must not be empty, got {unconfigured:?}");
    };
    assert!(reason.contains("未配置"), "{reason}");
    assert!(unconfigured.rows().is_empty());
    assert!(!unconfigured.table_visible());

    let unreadable = parse_login_rounds(
        &json!({
            "status": "unavailable",
            "error": "login_history_unreadable: permission denied",
            "rounds": [],
            "count": 0,
            "truncated": false,
            "scope": "本机实例 UM 登录轮次历史",
            "coverage": {"status": "unavailable", "error": "login_history_unreadable"},
        }),
        &LoginRoundsQuery::default(),
    )
    .expect("unreadable history");
    let LoginRoundsView::Unavailable { reason } = &unreadable else {
        panic!("an unreadable history must not be empty, got {unreadable:?}");
    };
    assert!(reason.contains("读取失败"), "{reason}");
    assert!(reason.contains("permission denied"), "{reason}");
    assert!(!unreadable.table_visible());
    assert!(unreadable.rows().is_empty());

    let api = FakeApi::returning(json!({}));
    let api = FakeApi {
        payload: Err(
            "http://127.0.0.1:7999/admin/um/login-rounds: 无法连接本机网关（连接被拒绝）"
                .to_string(),
        ),
        ..api
    };
    let token = ControlToken::parse(&"c3".repeat(32)).unwrap();
    let view = read(
        &api,
        "http://127.0.0.1:7999",
        &token,
        &LoginRoundsQuery::default(),
    );
    let LoginRoundsView::Failed(reason) = &view else {
        panic!("a failed read must stay failed, got {view:?}");
    };
    assert!(reason.contains("连接被拒绝"), "{reason}");
    assert!(!view.table_visible());
}

#[test]
fn an_unknown_status_is_a_failed_read_not_an_empty_table() {
    let error = parse_login_rounds(
        &json!({"status": "future_status", "rounds": []}),
        &LoginRoundsQuery::default(),
    )
    .expect_err("unknown status");
    assert!(error.contains("future_status"), "{error}");
}

#[test]
fn unknown_closed_set_values_read_unknown_without_becoming_success_or_failure() {
    let payload = payload(json!([{
        "key": "run-9:9",
        "runId": "run-9",
        "round": 9,
        "state": "future_state",
        "triggerSource": "future_trigger",
        "driver": "native",
        "visible": null,
        "startedAt": 1_765_000_000_000i64,
        "endedAt": null,
        "submitCount": 0,
        "outcome": "future_outcome",
        "reason": null,
        "detail": null,
        "upstreamStatus": null,
        "evidenceVerdict": "future_evidence",
        "rejectedBeforeExpiryStatus": null,
    }]));
    let view = parse_login_rounds(&payload, &LoginRoundsQuery::default()).expect("unknown values");
    let LoginRoundsView::Loaded(page) = view else {
        panic!("unknown closed-set values must still load, got {view:?}");
    };
    let row = &page.rows[0];
    assert_eq!(row.state, "未知");
    assert_eq!(row.state_tone, Tone::Neutral);
    assert_eq!(row.trigger, "未知");
    assert_eq!(row.outcome, "未知");
    assert_eq!(row.mode, "未知");
    assert_eq!(row.upstream, "无 HTTP 状态 · 未知");
    for value in [
        &row.state,
        &row.trigger,
        &row.outcome,
        &row.mode,
        &row.upstream,
    ] {
        assert_ne!(value, "已完成", "{value}");
        assert_ne!(value, "已恢复", "{value}");
        assert_ne!(value, "有效", "{value}");
        assert_ne!(value, "后台", "{value}");
    }
    assert!(row.detail.contains("future_state"), "{}", row.detail);
    assert!(row.detail.contains("future_trigger"), "{}", row.detail);
    assert!(row.detail.contains("future_outcome"), "{}", row.detail);
    assert!(row.detail.contains("future_evidence"), "{}", row.detail);
}

#[test]
fn a_login_round_shows_every_field() {
    let view = parse_login_rounds(&payload(json!([round()])), &LoginRoundsQuery::default())
        .expect("login rounds");
    let LoginRoundsView::Loaded(page) = view else {
        panic!("a successful read must be loaded, got {view:?}");
    };

    assert_eq!(page.rows.len(), 1);
    let row = &page.rows[0];
    assert_eq!(row.at, "2025-12-06 13:46:40");
    assert_eq!(row.round, "3");
    assert_eq!(row.state, "已完成");
    assert_eq!(row.state_tone, Tone::Good);
    assert_eq!(row.mode, "后台");
    assert_eq!(row.trigger, "后台巡检");
    assert_eq!(row.submit, "2");
    assert_eq!(row.outcome, "已恢复");
    assert!(row.reason.contains("duo_approved"), "{}", row.reason);
    assert!(row.reason.contains("旧凭据尚未到期"), "{}", row.reason);
    assert!(row.reason.contains("401"), "{}", row.reason);
    assert_eq!(row.upstream, "HTTP 200 · 有效");
    assert!(row.detail.contains("run-1"), "{}", row.detail);
    assert!(row.detail.contains("native"), "{}", row.detail);
    assert!(row.detail.contains("2025-12-06 13:47:40"), "{}", row.detail);

    assert!(page.status.contains("本页 1 轮"), "{}", page.status);
    assert_eq!(page.tone, Tone::Good);
    assert!(page.table_visible);
    assert!(page.empty_text.is_empty());
    assert!(!page.truncated);
    assert_eq!(
        page.scope,
        "本机实例 UM 登录轮次历史（最近 7 天 / 4 MiB）· 只读，不触发探测或登录 · 本页 1 条"
    );
}

#[test]
fn round_and_reason_filters_select_the_matching_rows() {
    let payload = payload(json!([
        round_with(
            2,
            Some("duo_approved"),
            Some("restored"),
            Some("business_request"),
            None
        ),
        round_with(
            3,
            Some("upstream_rejected"),
            Some("rejected"),
            Some("explicit"),
            None
        ),
        round_with(4, None, Some("restored"), Some("auth_watch"), Some(401)),
    ]));

    let by_round = parse_login_rounds(
        &payload,
        &LoginRoundsQuery {
            round: "3".to_string(),
            q: String::new(),
        },
    )
    .expect("round filter");
    assert_eq!(rows(by_round), vec!["3"]);

    let by_outcome = parse_login_rounds(
        &payload,
        &LoginRoundsQuery {
            round: String::new(),
            q: "rejected".to_string(),
        },
    )
    .expect("outcome filter");
    assert_eq!(rows(by_outcome), vec!["3"]);

    let by_trigger = parse_login_rounds(
        &payload,
        &LoginRoundsQuery {
            round: String::new(),
            q: "auth_watch".to_string(),
        },
    )
    .expect("trigger filter");
    assert_eq!(rows(by_trigger), vec!["4"]);

    let by_rejected_evidence = parse_login_rounds(
        &payload,
        &LoginRoundsQuery {
            round: String::new(),
            q: "旧凭据".to_string(),
        },
    )
    .expect("rejected-before-expiry filter");
    assert_eq!(rows(by_rejected_evidence), vec!["4"]);
}

#[test]
fn the_read_requests_the_gateway_login_rounds_route_with_the_filters() {
    let api = FakeApi::returning(payload(json!([round_with(
        3,
        Some("上游 拒绝"),
        Some("rejected"),
        Some("explicit"),
        None
    )])));
    let token = ControlToken::parse(&"c1".repeat(32)).unwrap();
    let query = LoginRoundsQuery {
        round: "3".to_string(),
        q: "上游 拒绝".to_string(),
    };
    let view = read(&api, "http://127.0.0.1:7999/", &token, &query);

    assert_eq!(
        api.urls.lock().unwrap().as_slice(),
        ["http://127.0.0.1:7999/admin/um/login-rounds?round=3&q=%E4%B8%8A%E6%B8%B8%20%E6%8B%92%E7%BB%9D&limit=200"]
    );
    assert_eq!(api.tokens.lock().unwrap().as_slice(), [token.expose()]);
    assert_eq!(rows(view), vec!["3"]);
}

#[test]
fn a_non_numeric_round_filter_is_reported_before_the_read() {
    let api = FakeApi::returning(payload(json!([])));
    let token = ControlToken::parse(&"c2".repeat(32)).unwrap();
    let view = read(
        &api,
        "http://127.0.0.1:7999",
        &token,
        &LoginRoundsQuery {
            round: "abc".to_string(),
            q: String::new(),
        },
    );

    assert!(
        api.urls.lock().unwrap().is_empty(),
        "the gateway must not be called"
    );
    assert!(matches!(view, LoginRoundsView::Failed(_)));
    assert!(query_url(
        "http://127.0.0.1:7999",
        &LoginRoundsQuery {
            round: "abc".to_string(),
            q: String::new(),
        },
    )
    .unwrap_err()
    .contains("轮次必须是数字"));
}

thread_local! {
    static INIT: std::sync::Once = std::sync::Once::new();
}

fn window() -> MainWindow {
    INIT.with(|init| {
        init.call_once(|| i_slint_backend_testing::init_no_event_loop());
    });
    let window = MainWindow::new().expect("create MainWindow");
    slintplot::register!(window);
    window.set_page(2);
    window.window().set_size(LogicalSize::new(1060.0, 720.0));
    window.show().expect("show MainWindow");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    window
}

#[test]
fn the_settings_table_shows_rows_in_a_bounded_list() {
    let rounds: Vec<serde_json::Value> = (1..=200)
        .map(|round| {
            json!({
                "key": format!("run-1:{round}"),
                "runId": "run-1",
                "round": round,
                "state": "completed",
                "triggerSource": "auth_watch",
                "driver": "native",
                "visible": false,
                "startedAt": 1_765_000_000_000i64,
                "endedAt": 1_765_000_060_000i64,
                "submitCount": 1,
                "outcome": "restored",
                "reason": "duo_approved",
                "detail": null,
                "upstreamStatus": 200,
                "evidenceVerdict": "valid",
                "rejectedBeforeExpiryStatus": null,
            })
        })
        .collect();
    let view = parse_login_rounds(&payload(json!(rounds)), &LoginRoundsQuery::default())
        .expect("login rounds");
    let window = window();
    apply_login_rounds(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(window.get_login_rounds_table_visible());
    assert_eq!(window.get_login_rounds().row_count(), 200);
    assert!(window.get_login_rounds_empty_text().is_empty());
    assert!(window.get_login_rounds_status().contains("本页 200 轮"));
    let row = window
        .get_login_rounds()
        .row_data(0)
        .expect("the first login round");
    assert_eq!(row.round, "1");
    assert_eq!(row.state, "已完成");
    assert_eq!(row.state_tone, 0);
    assert_eq!(row.mode, "后台");
    assert_eq!(row.trigger, "后台巡检");
    assert_eq!(row.submit, "1");
    assert_eq!(row.outcome, "已恢复");
    assert_eq!(row.upstream, "HTTP 200 · 有效");
    assert!(
        window.get_login_rounds_list_height() > 0.0,
        "the table needs a viewport"
    );
    assert!(
        window.get_login_rounds_list_height() < 200.0 * window.get_login_rounds_row_height(),
        "a long history must not render every row"
    );
}

#[test]
fn an_empty_or_failed_read_is_an_empty_state_not_a_table() {
    let window = window();
    let empty = parse_login_rounds(&payload(json!([])), &LoginRoundsQuery::default())
        .expect("empty history");
    apply_login_rounds(&window, &empty);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(!window.get_login_rounds_table_visible());
    assert_eq!(window.get_login_rounds().row_count(), 0);
    assert_eq!(window.get_login_rounds_empty_text(), "暂无登录轮次");
    assert_eq!(window.get_login_rounds_tone(), 3);

    let failed = LoginRoundsView::failed("无法连接本机网关（连接被拒绝）");
    apply_login_rounds(&window, &failed);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    assert!(!window.get_login_rounds_table_visible());
    assert_eq!(window.get_login_rounds().row_count(), 0);
    assert!(
        window.get_login_rounds_empty_text().contains("连接被拒绝"),
        "{}",
        window.get_login_rounds_empty_text()
    );
    assert!(
        window.get_login_rounds_status().contains("读取失败"),
        "{}",
        window.get_login_rounds_status()
    );
    assert_eq!(window.get_login_rounds_tone(), 2);
}

#[test]
fn activating_a_round_opens_and_closes_its_detail() {
    let window = window();
    let view = parse_login_rounds(&payload(json!([round()])), &LoginRoundsQuery::default())
        .expect("login rounds");
    apply_login_rounds(&window, &view);
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);

    open_round(&window, Some(&view), "run-1:3");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert_eq!(window.get_login_rounds_selected(), "run-1:3");
    assert!(
        window.get_login_rounds_detail().contains("run-1"),
        "{}",
        window.get_login_rounds_detail()
    );
    assert!(
        window.get_login_rounds_detail().contains("native"),
        "{}",
        window.get_login_rounds_detail()
    );

    open_round(&window, Some(&view), "run-1:3");
    i_slint_backend_testing::mock_elapsed_time(std::time::Duration::ZERO);
    assert!(window.get_login_rounds_selected().is_empty());
    assert!(window.get_login_rounds_detail().is_empty());
}

#[test]
fn the_filter_action_reads_the_two_fields() {
    let window = window();
    let captured = std::rc::Rc::new(std::cell::RefCell::new(None::<(String, String)>));
    let sink = captured.clone();
    window.on_filter_login_rounds(move |round, query| {
        *sink.borrow_mut() = Some((round.to_string(), query.to_string()));
    });
    window.set_login_rounds_round_filter("3".into());
    window.set_login_rounds_reason_filter("restored".into());
    window.invoke_filter_login_rounds(
        window.get_login_rounds_round_filter(),
        window.get_login_rounds_reason_filter(),
    );
    assert_eq!(
        captured.borrow().clone(),
        Some(("3".to_string(), "restored".to_string()))
    );
}

#[test]
fn a_query_longer_than_the_gateway_bound_is_truncated_like_the_gateway() {
    let long_reason = "a".repeat(120);
    let payload = payload(json!([round_with(
        1,
        Some(long_reason.as_str()),
        Some("restored"),
        Some("explicit"),
        None
    )]));
    let query = LoginRoundsQuery {
        round: String::new(),
        q: format!("{long_reason}b"),
    };
    let view = parse_login_rounds(&payload, &query).expect("truncated query");
    assert_eq!(rows(view), vec!["1"]);

    let url = query_url("http://127.0.0.1:7999", &query).expect("query url");
    assert!(
        url.contains(&format!("q={}&limit=200", "a".repeat(120))),
        "{url}"
    );
    assert!(!url.contains(&format!("q={}b", "a".repeat(120))), "{url}");
}
