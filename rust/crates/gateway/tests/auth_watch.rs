//! 上游核验暂时失败不能启动重新登录；真实拒绝仍须恢复认证。
mod support;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use support::{StubBehaviour, StubUpstream, STUB_TOKEN};
use um_gateway::config::GatewayConfig;
use um_gateway::reauth::{ReauthProviderResult, ReauthenticationProvider};
use um_gateway::GatewayServer;

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
    assert_eq!(
        watch_login_count(
            StubBehaviour {
                models_status: 401,
                ..StubBehaviour::default()
            },
            Duration::from_secs(1),
            "upstream_rejected"
        ),
        1,
        "明确拒绝仍须启动认证恢复"
    );
}

fn watch_login_count(behaviour: StubBehaviour, timeout: Duration, expected_reason: &str) -> usize {
    let stub = StubUpstream::start();
    stub.set(behaviour);
    let provider = Arc::new(CountingLogin::default());
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: STUB_TOKEN.to_string(),
        upstream_timeout: timeout,
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
