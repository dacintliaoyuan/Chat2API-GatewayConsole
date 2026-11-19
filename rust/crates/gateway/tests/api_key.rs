//! #114: the operator-saved Open WebUI API key is preferred for upstream
//! requests, and a rejected key falls back to the JWT bearer instead of
//! triggering a login round or wedging the gateway.

mod support;

use std::sync::Arc;

use serde_json::{json, Value};

use support::{
    client_request, start_chat_gateway, ChatAttempt, ChatStub, ClientRequest, StubUpstream,
    TempDir, UnavailableProvider, SHARE_SECRET,
};
use um_gateway::config::GatewayConfig;
use um_gateway::{GatewayServer, ServerHandle};

fn write_token(temp: &TempDir, url: &str, token: &str, api_key: Option<&str>) {
    let document = json!({
        "url": url,
        "token": token,
        "api_key": api_key,
        "saved_at": "2026-11-19 20:35:03",
    });
    temp.write("token.json", &document.to_string());
}

fn start_api_key_gateway(temp: &TempDir, url: &str) -> ServerHandle {
    let config = GatewayConfig {
        upstream_base_url: url.to_string(),
        upstream_token: "config-token".to_string(),
        token_file: Some(temp.path().join("token.json")),
        auth_state_file: Some(temp.path().join("auth-state.json")),
        ..GatewayConfig::default()
    };
    GatewayServer::start_with_reauth(config, Some(Arc::new(UnavailableProvider)))
        .expect("api-key gateway")
}

fn models_seen(stub: &StubUpstream) -> Vec<String> {
    stub.seen()
        .into_iter()
        .filter(|request| request.path == "/api/models")
        .filter_map(|request| request.authorization)
        .collect()
}

#[test]
fn the_saved_api_key_is_preferred_over_the_jwt_bearer() {
    let temp = TempDir::new("um-rust-api-key-preferred-");
    let stub = StubUpstream::start();
    write_token(&temp, &stub.url(), "jwt-token", Some("fixture-api-key"));
    let server = start_api_key_gateway(&temp, &stub.url());

    let response =
        client_request(server.addr(), &ClientRequest::get("/api/models")).expect("models request");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        models_seen(&stub),
        vec!["Bearer fixture-api-key".to_string()],
        "必须优先使用已保存的上游 API key"
    );
}

#[test]
fn the_startup_auth_state_records_the_saved_api_key() {
    let temp = TempDir::new("um-rust-api-key-startup-state-");
    let stub = StubUpstream::start();
    write_token(&temp, &stub.url(), "jwt-token", Some("fixture-api-key"));
    // The real login executor owns the startup write; a scripted provider skips
    // it, so this test starts the production login service instead.
    let config = GatewayConfig {
        upstream_base_url: stub.url(),
        upstream_token: "config-token".to_string(),
        token_file: Some(temp.path().join("token.json")),
        auth_state_file: Some(temp.path().join("auth-state.json")),
        ..GatewayConfig::default()
    };
    let _server = GatewayServer::start(config).expect("startup login gateway");

    let path = temp.path().join("auth-state.json");
    let text = std::fs::read_to_string(&path).expect("startup auth state");
    let state: Value = serde_json::from_str(&text).expect("startup auth state json");
    assert_eq!(state["api_key_present"], json!(true));
    assert_eq!(state["api_key_rejected"], json!(false));
    assert_eq!(state["saved_at"], json!("2026-11-19 20:35:03"));
    assert!(!text.contains("fixture-api-key"));
    assert!(!text.contains("jwt-token"));
}

#[test]
fn a_rejected_api_key_falls_back_to_the_jwt_bearer() {
    let temp = TempDir::new("um-rust-api-key-fallback-");
    let stub = StubUpstream::start();
    write_token(&temp, &stub.url(), "jwt-token", Some("rejected-token"));
    let server = start_api_key_gateway(&temp, &stub.url());

    let response =
        client_request(server.addr(), &ClientRequest::get("/api/models")).expect("models request");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        models_seen(&stub),
        vec![
            "Bearer rejected-token".to_string(),
            "Bearer jwt-token".to_string()
        ],
        "被上游拒绝的 API key 必须回退到 JWT bearer"
    );
}

#[test]
fn an_api_key_only_credential_serves_readiness_without_a_jwt() {
    let temp = TempDir::new("um-rust-api-key-only-");
    let stub = StubUpstream::start();
    write_token(&temp, &stub.url(), "", Some("fixture-api-key"));
    let server = start_api_key_gateway(&temp, &stub.url());

    let response =
        client_request(server.addr(), &ClientRequest::get("/ready")).expect("ready request");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    assert_eq!(
        models_seen(&stub),
        vec!["Bearer fixture-api-key".to_string()],
        "没有 JWT 时凭据探测也必须使用 API key"
    );
}

fn success_body() -> Value {
    json!({
        "id": "chatcmpl-fixture",
        "object": "chat.completion",
        "model": "fixture-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "OK"},
            "finish_reason": "stop",
        }],
    })
}

fn chat_body() -> Value {
    json!({
        "model": "fixture-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": false,
        "max_tokens": 16,
    })
}

#[test]
fn a_rejected_api_key_falls_back_to_the_jwt_bearer_for_business_requests() {
    let temp = TempDir::new("um-rust-api-key-chat-fallback-");
    let stub = ChatStub::start();
    write_token(&temp, &stub.url(), "jwt-token", Some("rejected-token"));
    stub.push(ChatAttempt::Json {
        status: 401,
        body: json!({"detail": "401 Unauthorized"}),
    });
    stub.push(ChatAttempt::Json {
        status: 200,
        body: success_body(),
    });
    let gateway = start_chat_gateway(&stub, &temp, true, |config| {
        config.token_file = Some(temp.path().join("token.json"));
    });

    let response = client_request(
        gateway.addr,
        &ClientRequest::post("/v1/chat/completions", &chat_body().to_string())
            .bearer(SHARE_SECRET)
            .header("Content-Type", "application/json"),
    )
    .expect("chat request");
    assert_eq!(
        response.status,
        200,
        "{}",
        String::from_utf8_lossy(&response.body)
    );
    let chat: Vec<String> = stub
        .seen()
        .into_iter()
        .filter(|request| request.path == "/api/chat/completions")
        .filter_map(|request| request.authorization)
        .collect();
    assert_eq!(
        chat,
        vec![
            "Bearer rejected-token".to_string(),
            "Bearer jwt-token".to_string()
        ],
        "业务请求遇到无效 API key 必须回退到 JWT bearer"
    );
}
