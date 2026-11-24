//! OpenAI-compatible embeddings passthrough (`POST /v1/embeddings`).
//!
//! Ports `chat2api.Handler._compat_proxy("/api/v1/embeddings")`: the accepted
//! body is forwarded unchanged to the upstream compatibility endpoint, the
//! upstream status/headers/body are relayed unchanged. The desktop instance
//! alone adds embeddings to its private request ledger.

use std::net::TcpStream;
use std::time::Instant;

use serde_json::{Map, Value};

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::ChatResult;
use crate::error::ErrorProtocol;
use crate::event_log::{AttemptStats, FinalFailure};
use crate::http::{Request, Response};
use crate::ledger::UsageRecord;
use crate::reauth::{self, AuthWaitBudget, StatusAction};
use crate::retry::{self, LastAttempt, RetryLimits, RetryOutcome, RetryState};
use crate::server::AppState;
use crate::upstream::{post_compat, PostError};

/// Serve one embeddings request.
pub fn handle_embeddings(
    state: &AppState,
    request: &Request,
    stream: &mut TcpStream,
) -> ChatResult {
    let dispatch = crate::server::ModelDispatch::new(state, request, stream);
    handle_embeddings_with_dispatch(state, request, &dispatch)
}

pub(crate) fn handle_embeddings_with_dispatch(
    state: &AppState,
    request: &Request,
    dispatch: &crate::server::ModelDispatch<'_>,
) -> ChatResult {
    let mut stats = AttemptStats::default();
    let started_at = Instant::now();
    let body = read_json_body(
        request,
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        },
    );
    let model = if state.config.desktop_instance {
        body.as_ref().ok().map(|body| {
            body.as_ref()
                .and_then(|body| body.get("model"))
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string()
        })
    } else {
        None
    };
    let mut usage = None;
    let result = match body {
        Ok(body) => {
            let deadline = Instant::now() + state.config.request_total;
            handle_embeddings_inner(state, body.unwrap_or_default(), &mut stats, &mut usage, deadline, &request.request_id, dispatch)
        }
        Err(error) => ChatResult::Response(error.response()),
    };
    let final_failure = result.final_failure_status();
    if let Some(model) = model {
        state.ledger.record(UsageRecord {
            model: &model,
            stream: false,
            started_at,
            usage: usage.as_ref(),
            input_estimate: 0,
            output_estimate: 0,
            status: final_failure.as_deref().unwrap_or("ok"),
            protocol: Some("embeddings"),
            key_id: None,
            request_id: Some(&request.request_id),
            attribution: "absent",
            first_token_ms: None,
        });
    }
    if let Some(final_status) = final_failure {
        state.event_log.final_failure(FinalFailure {
            request_id: &request.request_id,
            protocol: "embeddings",
            final_status,
            stats,
            started_at,
        });
    }
    result
}

fn handle_embeddings_inner(
    state: &AppState,
    body: Map<String, Value>,
    stats: &mut AttemptStats,
    usage: &mut Option<Value>,
    deadline: Instant,
    request_id: &str,
    dispatch: &crate::server::ModelDispatch<'_>,
) -> ChatResult {
    let raw = serde_json::to_vec(&serde_json::Value::Object(body)).unwrap_or_default();
    let mut retry_state = RetryState::default();
    let mut last = LastAttempt::default();
    let mut auth_wait_budget = AuthWaitBudget::new(state.config.auth_wait_budget());
    let limits = RetryLimits::from_config(&state.config);
    loop {
        if Instant::now() >= deadline {
            return ChatResult::Response(last.deadline_error().response());
        }
        if retry_state.physical_attempts >= state.config.max_physical_attempts {
            return ChatResult::Response(reauth::final_error(&last, &retry_state).response());
        }
        retry_state.record_attempt();
        stats.record_attempt();
        let attempt_started = Instant::now();
        let attempt = post_compat(
            dispatch,
            "/api/v1/embeddings",
            &raw,
            false,
            deadline,
            &[],
        );
        let attempt_elapsed = dispatch.attempt_elapsed(attempt_started);
        let (mut response, credential) = match attempt {
            Ok(response) => response,
            Err(PostError::Admission(result)) => {
                stats.tries -= 1;
                stats.physical_attempts -= 1;
                return result;
            }
            Err(PostError::Transport(failure)) => {
                state.proxy.observe_business_failure(None);
                last.record_transport(failure);
                if let Some(plan) = retry_state.plan(
                    RetryOutcome::Transport,
                    &limits,
                    deadline,
                    attempt_elapsed,
                ) {
                    if retry::sleep_with_deadline(plan.delay, deadline) {
                        continue;
                    }
                    return ChatResult::Response(last.deadline_error().response());
                }
                state
                    .proxy
                    .record_business_transport_failure(failure);
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        let status = response.status;
        if status != 200 {
            match reauth::handle_non_success(
                state,
                &mut retry_state,
                &mut last,
                response,
                attempt_elapsed,
                deadline,
                &mut auth_wait_budget,
                &credential,
            ) {
                StatusAction::Retry => continue,
                StatusAction::Final { error, .. } => {
                    return ChatResult::Response(error.response());
                }
            }
        }
        let raw = match response.body.read_all() {
            Ok(raw) => raw,
            Err(failure) => {
                last.record_transport(failure);
                state
                    .proxy
                    .record_business_transport_failure(failure);
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        if let Ok(mut payload) = serde_json::from_slice::<Value>(&raw) {
            if payload["data"].as_array().is_some_and(|data| !data.is_empty()) {
                state
                    .proxy
                    .record_business_success(request_id, "embeddings_completion", Some(200));
            }
            if state.config.desktop_instance {
                if let Some(reported) = payload.get_mut("usage").and_then(Value::as_object_mut) {
                    let input_reported = ["prompt_tokens", "input_tokens"]
                        .iter()
                        .any(|key| reported.get(*key).is_some_and(Value::is_u64));
                    if input_reported && !reported.contains_key("output_tokens") {
                        // An embedding response reports only input token usage.
                        reported.entry("completion_tokens").or_insert(Value::from(0));
                    }
                    *usage = Some(Value::Object(std::mem::take(reported)));
                }
            }
        }
        let mut translated = Response::new(status, raw);
        for name in ["content-type", "content-length", "cache-control"] {
            if let Some(value) = response.headers.get(name) {
                translated = translated.with_header(name, value);
            }
        }
        return ChatResult::Response(translated);
    }
}
