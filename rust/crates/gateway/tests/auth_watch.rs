//! 上游核验暂时失败不能启动重新登录；真实拒绝仍须恢复认证。
mod support;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::json;

use support::{client_request, ClientRequest, StubBehaviour, StubUpstream, STUB_TOKEN};
use um_gateway::config::GatewayConfig;
use um_gateway::reauth::{ReauthProviderResult, ReauthenticationProvider};
use um_gateway::{GatewayServer, ServerHandle};

#[derive(Default)]
struct CountingLogin(AtomicUsize);

impl ReauthenticationProvider for CountingLogin {
    fn reauthenticate(&self, _deadline: Instant) -> ReauthProviderResult {
        self.0.fetch_add(1, Ordering::SeqCst);
        ReauthProviderResult::Restored
    }
}

#[test]
fn an_unknown_watch_probe_does_not_start_login() {
    assert_eq!(
        watch_login_count(
            StubBehaviour {
                models_status: 503,
                ..StubBehaviour::default()
            },
            Duration::from_secs(1),
            "upstream_5xx"
        ),
        0,
        "核验未知不能被当作凭据失效并启动浏览器登录"
    );
}

#[test]
fn a_slow_watch_probe_does_not_start_login() {
    assert_eq!(
        watch_login_count(
            StubBehaviour {
                // 生产核验现在有独立的 20 秒预算；响应必须更慢才会真实超时。
                delay: Duration::from_secs(21),
                ..StubBehaviour::default()
            },
            Duration::from_millis(50),
            "timeout"
        ),
        0,
        "核验超时只能重新核验，不能重开登录"
    );
}

#[test]
fn a_rejected_watch_probe_still_starts_login() {
    assert!(
        watch_login_count(
            StubBehaviour {
                models_status: 401,
                ..StubBehaviour::default()
            },
            Duration::from_secs(1),
            "upstream_rejected"
        ) >= 1,
        "明确拒绝仍须启动认证恢复"
    );
}

#[test]
fn the_watch_reports_real_rounds_and_the_next_due_interval() {
    let stub = StubUpstream::start();
    let provider = Arc::new(CountingLogin::default());
    let base = Duration::from_secs(1);
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        auth_watch_interval: Some(base),
        ..GatewayConfig::default()
    };
    let mut server = GatewayServer::start_with_reauth(config, Some(provider)).expect("gateway");
    let watch = wait_for_watch(&server, 1);
    assert_eq!(watch["thread_alive"], json!(true));
    assert!(
        watch["rounds"].as_u64().expect("rounds") >= 1,
        "首轮结束后必须记录真实轮数: {watch}"
    );
    assert_eq!(watch["last_round_outcome"], json!("valid"));
    let due = watch["next_round_due"].as_f64().expect("next due");
    let epoch = watch["last_round_epoch"].as_f64().expect("last epoch");
    assert!(
        (due - epoch - base.as_secs_f64()).abs() < 1e-6,
        "next_round_due 必须是上一轮 epoch 加门控下一轮的间隔: {watch}"
    );

    let health = client_request(server.addr(), &ClientRequest::get("/health")).expect("health");
    assert_eq!(health.json()["watch"]["thread_alive"], json!(true));
    assert!(
        health.json()["watch"]["rounds"]
            .as_u64()
            .expect("health rounds")
            >= 1,
        "health 必须带真实 watch 状态: {}",
        health.json()["watch"]
    );
    assert_eq!(health.json()["watch"]["last_round_outcome"], json!("valid"));

    server.shutdown();
    assert_eq!(
        server.state().watch.snapshot()["thread_alive"],
        json!(false),
        "巡检线程退出后必须如实报告 thread_alive=false"
    );
}

#[test]
fn a_non_valid_watch_round_backs_off_the_next_due_interval() {
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        models_status: 503,
        ..StubBehaviour::default()
    });
    let provider = Arc::new(CountingLogin::default());
    let base = Duration::from_millis(100);
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        auth_watch_interval: Some(base),
        ..GatewayConfig::default()
    };
    let mut server = GatewayServer::start_with_reauth(config, Some(provider)).expect("gateway");
    let watch = wait_for_watch(&server, 2);
    assert_eq!(watch["last_round_outcome"], json!("unknown"));
    let due = watch["next_round_due"].as_f64().expect("next due");
    let epoch = watch["last_round_epoch"].as_f64().expect("last epoch");
    assert!(
        (due - epoch - (base * 4).as_secs_f64()).abs() < 1e-6,
        "非 valid 轮次必须按 2 倍退避发布门控间隔: {watch}"
    );
    server.shutdown();
}

#[test]
fn browser_disabled_still_backs_off_the_watch_interval() {
    let stub = StubUpstream::start();
    stub.set(StubBehaviour {
        models_status: 503,
        ..StubBehaviour::default()
    });
    let provider = Arc::new(CountingLogin::default());
    let base = Duration::from_millis(100);
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        browser_disabled: true,
        auth_watch_interval: Some(base),
        ..GatewayConfig::default()
    };
    let mut server =
        GatewayServer::start_with_reauth(config, Some(provider.clone())).expect("gateway");
    let watch = wait_for_watch(&server, 2);
    let due = watch["next_round_due"].as_f64().expect("next due");
    let epoch = watch["last_round_epoch"].as_f64().expect("last epoch");
    assert!(
        (due - epoch - (base * 4).as_secs_f64()).abs() < 1e-6,
        "browser_disabled 也必须按非 valid 轮次退避: {watch}"
    );
    assert_eq!(
        provider.0.load(Ordering::SeqCst),
        0,
        "browser_disabled 巡检不得启动登录"
    );
    server.shutdown();
}

fn wait_for_watch(server: &ServerHandle, rounds: u64) -> serde_json::Value {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let watch = server.state().watch.snapshot();
        if watch["rounds"].as_u64().unwrap_or(0) >= rounds {
            return watch;
        }
        assert!(
            Instant::now() < deadline,
            "timed out waiting for {rounds} watch round(s): {watch}"
        );
        thread::sleep(Duration::from_millis(20));
    }
}

fn watch_login_count(behaviour: StubBehaviour, timeout: Duration, expected_reason: &str) -> usize {
    let stub = StubUpstream::start();
    stub.set(behaviour);
    let provider = Arc::new(CountingLogin::default());
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        upstream_timeout: timeout,
        auth_watch_interval: Some(Duration::from_secs(1)),
        ..GatewayConfig::default()
    };
    let mut server =
        GatewayServer::start_with_reauth(config, Some(provider.clone())).expect("gateway");
    thread::sleep(Duration::from_millis(2400));
    server.shutdown();
    assert_eq!(
        server.state().proxy.health_snapshot()["upstream_reachability"]["reason"],
        expected_reason,
        "必须实际走到目标核验失败分类"
    );
    assert!(
        stub.seen()
            .iter()
            .any(|request| request.path == "/api/models"),
        "必须实际执行巡检，避免未运行也通过"
    );
    provider.0.load(Ordering::SeqCst)
}
