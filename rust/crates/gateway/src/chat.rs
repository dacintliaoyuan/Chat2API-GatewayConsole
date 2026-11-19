//! Chat Completions forwarding (`POST /v1/chat/completions` and the two local
//! compatibility aliases).
//!
//! This is the S1b slice: non-stream JSON relay, SSE byte passthrough, the
//! pre-commit staging/commit gate, pre-commit 502 vs post-commit in-band error,
//! the first-byte/silence/total budgets, hidden retries before any SSE event,
//! client-disconnect cancellation and the share ledger row. The Python behavior
//! being ported is `chat2api.Handler._chat` and `_UpstreamStream`.

use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::body::{read_json_body, BodyPolicy};
use crate::error::{ErrorProtocol, GatewayError};
use crate::event_log::{AttemptStats, FinalFailure};
use crate::gate::{Admission, ShareGate};
use crate::http::{write_chunk, write_response_head, Request, Response};
use crate::ledger::{estimate_tokens, merge_usage_snapshots, message_tokens, UsageRecord};
use crate::reauth::{self, AuthWaitBudget, StatusAction};
use crate::retry::{self, LastAttempt, RetryLimits, RetryOutcome, RetryState};
use crate::server::{valid_chat_request, AppState};
use crate::upstream::{NextItem, StreamReader, UpstreamBody};

/// SSE content type used after the 200 commit.
pub const SSE_CONTENT_TYPE: &str = "text/event-stream; charset=utf-8";
/// Maximum held-back prelude bytes before the 200 commit.
pub const PRELUDE_MAX_BYTES: usize = 64 * 1024;
/// Python `STREAM_ERROR_MESSAGES` values.
pub const UPSTREAM_EOF_WITHOUT_DONE: &str =
    "Upstream closed the stream before the completion finished (no [DONE] received).";
pub const UPSTREAM_READ_ERROR: &str = "Upstream connection failed while streaming.";
pub const UPSTREAM_FIRST_BYTE_TIMEOUT: &str =
    "Upstream sent no stream data within the first-byte budget.";
pub const UPSTREAM_MISSING_FINISH_REASON: &str =
    "Upstream finished the stream ([DONE]) without a finish_reason; the completion is incomplete.";
pub const UPSTREAM_PRELUDE_OVERFLOW: &str =
    "Upstream sent too much non-event data before the first event; the held-back prelude exceeded its byte bound.";
pub const UPSTREAM_STREAM_ERROR: &str =
    "Upstream reported an explicit stream error; the completion is incomplete.";
pub const REQUEST_DEADLINE_EXCEEDED: &str = "Upstream request could not start another attempt";

/// The outcome of one Chat route.
pub enum ChatResult {
    /// A normal response, written by the connection loop.
    Response(Response),
    /// The route wrote the response itself; `close` ends the connection and
    /// `failed` marks an in-band error as a final failure for the event log.
    Written { close: bool, failed: bool },
}

impl ChatResult {
    /// The final failure label used by the runtime event log.
    pub fn final_failure_status(&self) -> Option<String> {
        match self {
            Self::Response(response) if response.status >= 400 => {
                Some(format!("HTTP{}", response.status))
            }
            Self::Written { failed: true, .. } => Some("stream_error".to_string()),
            _ => None,
        }
    }
}

struct AdmissionGuard<'a> {
    gate: Option<&'a ShareGate>,
    admission: Option<Admission>,
}

impl Drop for AdmissionGuard<'_> {
    fn drop(&mut self) {
        if let (Some(gate), Some(admission)) = (self.gate, self.admission.take()) {
            gate.unregister_inflight(&admission.key_id, &admission.ctx);
            gate.release(&admission.key_id);
        }
    }
}

/// Serve one Chat request.
pub fn handle_chat(
    state: &AppState,
    request: &Request,
    stream: &mut TcpStream,
    admission: Option<Admission>,
) -> ChatResult {
    let key_id = admission.as_ref().map(|value| value.key_id.clone());
    let request_id = key_id.as_ref().map(|_| random_request_id());
    let attribution = if key_id.is_some() {
        "verified"
    } else {
        "absent"
    };
    let _guard = AdmissionGuard {
        gate: state.gate.as_deref(),
        admission,
    };
    let body = match read_json_body(
        request,
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        },
    ) {
        Ok(Some(body)) => body,
        Ok(None) => Map::new(),
        Err(error) => return ChatResult::Response(error.response()),
    };
    if state.gate.is_some() && !valid_chat_request(&body) {
        return ChatResult::Response(GatewayError::unsupported_chat_request().response());
    }
    if !body
        .get("messages")
        .and_then(Value::as_array)
        .is_some_and(|messages| !messages.is_empty())
    {
        return ChatResult::Response(
            GatewayError::new(
                400,
                "Request body is missing the messages field",
                "chat2api_error",
            )
            .response(),
        );
    }

    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let client_stream = body.get("stream").is_some_and(truthy);
    let started_at = Instant::now();
    let deadline = started_at + state.config.request_total;
    let mut upstream_body = body.clone();
    if client_stream {
        upstream_body
            .entry("stream_options".to_string())
            .or_insert_with(|| json!({"include_usage": true}));
        upstream_body.insert("stream".to_string(), Value::Bool(true));
    }
    let origin = request
        .headers
        .get("Origin")
        .filter(|origin| state.config.allowed_origins.contains(*origin))
        .map(str::to_string);
    let context = ChatContext {
        state,
        model: &model,
        started_at,
        deadline,
        input_estimate: message_tokens(upstream_body.get("messages")),
        key_id: key_id.as_deref(),
        request_id: request_id.as_deref(),
        http_request_id: &request.request_id,
        attribution,
        upstream_body,
        origin,
    };
    if client_stream {
        forward_stream(&context, stream)
    } else {
        forward_json(&context)
    }
}

struct ChatContext<'a> {
    state: &'a AppState,
    model: &'a str,
    started_at: Instant,
    deadline: Instant,
    input_estimate: u64,
    key_id: Option<&'a str>,
    request_id: Option<&'a str>,
    http_request_id: &'a str,
    attribution: &'a str,
    upstream_body: Map<String, Value>,
    origin: Option<String>,
}

fn forward_json(context: &ChatContext<'_>) -> ChatResult {
    let mut stats = AttemptStats::default();
    let result = forward_json_inner(context, &mut stats);
    log_final_failure(context, &stats, &result);
    result
}

fn forward_json_inner(context: &ChatContext<'_>, stats: &mut AttemptStats) -> ChatResult {
    let mut body = context.upstream_body.clone();
    let mut compat_retry_used = false;
    let mut retry_state = RetryState::default();
    let mut last = LastAttempt::default();
    let mut auth_wait_budget = AuthWaitBudget::new(context.state.config.auth_wait_budget());
    let limits = RetryLimits::from_config(&context.state.config);
    loop {
        if Instant::now() >= context.deadline {
            return ChatResult::Response(last.deadline_error().response());
        }
        if retry_state.physical_attempts >= context.state.config.max_physical_attempts {
            return ChatResult::Response(reauth::final_error(&last, &retry_state).response());
        }
        retry_state.record_attempt();
        stats.record_attempt();
        let attempt_started = Instant::now();
        let credential = context.state.proxy.upstream_credential();
        let mut response = match crate::upstream::post_chat(
            &context.state.config,
            &credential.value,
            &serde_json::to_vec(&Value::Object(body.clone())).unwrap_or_default(),
            false,
            context.deadline,
        ) {
            Ok(response) => response,
            Err(failure) => {
                last.record_transport(failure);
                if let Some(plan) = retry_state.plan(
                    RetryOutcome::Transport,
                    &limits,
                    context.deadline,
                    attempt_started.elapsed(),
                ) {
                    if !retry::sleep_with_deadline(plan.delay, context.deadline) {
                        return ChatResult::Response(last.deadline_error().response());
                    }
                    continue;
                }
                context
                    .state
                    .proxy
                    .record_business_transport_failure(failure);
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        let status = response.status;
        if status == 400 && body.contains_key("stream_options") && !compat_retry_used {
            body.remove("stream_options");
            compat_retry_used = true;
            drop(response);
            continue;
        }
        if status != 200 {
            match reauth::handle_non_success(
                context.state,
                &mut retry_state,
                &mut last,
                response,
                attempt_started.elapsed(),
                context.deadline,
                &mut auth_wait_budget,
                credential.api_key,
            ) {
                StatusAction::Retry => continue,
                StatusAction::Final { error, .. } => {
                    return ChatResult::Response(error.response());
                }
            }
        }
        let raw = match response.body.read_all() {
            Ok(raw) => raw,
            Err(_) => {
                return ChatResult::Response(
                    GatewayError::new(502, "Upstream returned invalid JSON", "JSONDecodeError")
                        .response(),
                )
            }
        };
        let data: Value = match serde_json::from_slice(&raw) {
            Ok(data) => data,
            Err(_) => {
                return ChatResult::Response(
                    GatewayError::new(502, "Upstream returned invalid JSON", "JSONDecodeError")
                        .response(),
                )
            }
        };
        let Value::Object(data) = data else {
            return ChatResult::Response(
                GatewayError::new(
                    502,
                    "Upstream returned an invalid JSON shape",
                    json_type_name(&data),
                )
                .response(),
            );
        };
        if let Some(reason) = explicit_error_reason(&Value::Object(data.clone())) {
            return ChatResult::Response(
                GatewayError::new(502, "Upstream request failed", "upstream_explicit_error")
                    .with_detail(json!({
                        "reason": "upstream_explicit_error",
                        "upstream_message": reason,
                    }))
                    .response(),
            );
        }
        let out_est = data
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|choices| choices.first())
            .and_then(|choice| choice.get("message"))
            .and_then(|message| message.get("content"))
            .and_then(Value::as_str)
            .map(estimate_tokens)
            .unwrap_or(0);
        context.state.ledger.record(UsageRecord {
            model: context.model,
            stream: false,
            started_at: context.started_at,
            usage: data.get("usage"),
            input_estimate: context.input_estimate,
            output_estimate: out_est,
            status: "ok",
            protocol: Some("chat"),
            key_id: context.key_id,
            request_id: context.request_id,
            attribution: context.attribution,
        });
        if data
            .get("choices")
            .and_then(Value::as_array)
            .is_some_and(|choices| !choices.is_empty())
        {
            context
                .state
                .proxy
                .record_business_success("chat_completion", Some(200));
        }
        let mut response = Response::new(200, raw);
        response = response.with_header("Content-Type", "application/json; charset=utf-8");
        return ChatResult::Response(response);
    }
}

fn log_final_failure(context: &ChatContext<'_>, stats: &AttemptStats, result: &ChatResult) {
    let Some(final_status) = result.final_failure_status() else {
        return;
    };
    context.state.event_log.final_failure(FinalFailure {
        request_id: context.http_request_id,
        protocol: "chat",
        final_status,
        stats: *stats,
        started_at: context.started_at,
    });
}

fn forward_stream(context: &ChatContext<'_>, stream: &mut TcpStream) -> ChatResult {
    let mut stats = AttemptStats::default();
    let result = forward_stream_inner(context, stream, &mut stats);
    log_final_failure(context, &stats, &result);
    result
}

fn forward_stream_inner(
    context: &ChatContext<'_>,
    stream: &mut TcpStream,
    stats: &mut AttemptStats,
) -> ChatResult {
    let mut retry_state = RetryState::default();
    let mut last = LastAttempt::default();
    let mut auth_wait_budget = AuthWaitBudget::new(context.state.config.auth_wait_budget());
    let limits = RetryLimits::from_config(&context.state.config);
    let mut compat_retry_used = false;
    let mut upstream_body = context.upstream_body.clone();
    loop {
        if Instant::now() >= context.deadline {
            return ChatResult::Response(last.deadline_error().response());
        }
        if retry_state.physical_attempts >= context.state.config.max_physical_attempts {
            return ChatResult::Response(reauth::final_error(&last, &retry_state).response());
        }
        retry_state.record_attempt();
        stats.record_attempt();
        let attempt_started = Instant::now();
        let credential = context.state.proxy.upstream_credential();
        let response = match crate::upstream::post_chat(
            &context.state.config,
            &credential.value,
            &serde_json::to_vec(&Value::Object(upstream_body.clone())).unwrap_or_default(),
            true,
            context.deadline,
        ) {
            Ok(response) => response,
            Err(failure) => {
                last.record_transport(failure);
                if let Some(plan) = retry_state.plan(
                    RetryOutcome::Transport,
                    &limits,
                    context.deadline,
                    attempt_started.elapsed(),
                ) {
                    if !retry::sleep_with_deadline(plan.delay, context.deadline) {
                        return ChatResult::Response(last.deadline_error().response());
                    }
                    continue;
                }
                context
                    .state
                    .proxy
                    .record_business_transport_failure(failure);
                return ChatResult::Response(last.response(ErrorProtocol::OpenAi));
            }
        };
        if response.status == 400
            && upstream_body.contains_key("stream_options")
            && !compat_retry_used
        {
            compat_retry_used = true;
            upstream_body.remove("stream_options");
            drop(response);
            continue;
        }
        if response.status != 200 {
            match reauth::handle_non_success(
                context.state,
                &mut retry_state,
                &mut last,
                response,
                attempt_started.elapsed(),
                context.deadline,
                &mut auth_wait_budget,
                credential.api_key,
            ) {
                StatusAction::Retry => continue,
                StatusAction::Final { error, .. } => {
                    return ChatResult::Response(error.response());
                }
            }
        }
        let outcome = consume_stream(context, stream, response.body);
        if outcome.status == StreamStatus::ClientDisconnected {
            record_stream_usage(context, &outcome);
            return ChatResult::Written {
                close: true,
                failed: false,
            };
        }
        if !outcome.committed
            && matches!(
                outcome.status,
                StreamStatus::UpstreamEofWithoutDone
                    | StreamStatus::UpstreamReadError
                    | StreamStatus::UpstreamFirstByteTimeout
            )
        {
            if let Some(plan) = retry_state.plan(
                RetryOutcome::Stream,
                &limits,
                context.deadline,
                outcome.elapsed,
            ) {
                if outcome.reasoning_chars > 0 {
                    context.state.ledger.record(UsageRecord {
                        model: context.model,
                        stream: true,
                        started_at: context.started_at,
                        usage: outcome.usage.as_ref(),
                        input_estimate: context.input_estimate,
                        output_estimate: outcome.reasoning_chars / 4,
                        status: "upstream_transient_retried",
                        protocol: Some("chat"),
                        key_id: context.key_id,
                        request_id: context.request_id,
                        attribution: context.attribution,
                    });
                }
                if !retry::sleep_with_deadline(plan.delay, context.deadline) {
                    return ChatResult::Response(last.deadline_error().response());
                }
                continue;
            }
        }
        if outcome.committed {
            if !outcome.status.is_success() {
                write_stream_error(stream, outcome.status, &outcome, context.http_request_id);
            }
        } else if !outcome.status.is_success() {
            record_stream_usage(context, &outcome);
            return ChatResult::Response(stream_error_response(&outcome, context.http_request_id));
        }
        record_stream_usage(context, &outcome);
        return ChatResult::Written {
            close: true,
            failed: outcome.committed && !outcome.status.is_success(),
        };
    }
}

struct SseState {
    committed: bool,
    staged: Vec<u8>,
    event_raw: Vec<u8>,
    event_lines: Vec<String>,
    saw_finish_reason: bool,
    done_seen: bool,
    usage: Option<Value>,
    completion_chars: u64,
    reasoning_chars: u64,
    data_lines: u64,
    status: StreamStatus,
    exception: Option<&'static str>,
    explicit_reason: Option<String>,
}

impl SseState {
    fn new() -> Self {
        Self {
            committed: false,
            staged: Vec::new(),
            event_raw: Vec::new(),
            event_lines: Vec::new(),
            saw_finish_reason: false,
            done_seen: false,
            usage: None,
            completion_chars: 0,
            reasoning_chars: 0,
            data_lines: 0,
            status: StreamStatus::Ok,
            exception: None,
            explicit_reason: None,
        }
    }
}

struct StreamOutcome {
    committed: bool,
    status: StreamStatus,
    usage: Option<Value>,
    completion_chars: u64,
    reasoning_chars: u64,
    data_lines: u64,
    exception: Option<&'static str>,
    explicit_reason: Option<String>,
    elapsed: Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StreamStatus {
    Ok,
    UpstreamEofWithoutDone,
    UpstreamReadError,
    UpstreamFirstByteTimeout,
    UpstreamPreludeOverflow,
    UpstreamStreamError,
    UpstreamMissingFinishReason,
    RequestDeadlineExceeded,
    ClientDisconnected,
}

impl StreamStatus {
    fn reason(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::UpstreamEofWithoutDone => "upstream_eof_without_done",
            Self::UpstreamReadError => "upstream_read_error",
            Self::UpstreamFirstByteTimeout => "upstream_first_byte_timeout",
            Self::UpstreamPreludeOverflow => "upstream_prelude_overflow",
            Self::UpstreamStreamError => "upstream_stream_error",
            Self::UpstreamMissingFinishReason => "upstream_missing_finish_reason",
            Self::RequestDeadlineExceeded => "request_deadline_exceeded",
            Self::ClientDisconnected => "client_disconnected",
        }
    }

    fn message(self) -> &'static str {
        match self {
            Self::UpstreamEofWithoutDone => UPSTREAM_EOF_WITHOUT_DONE,
            Self::UpstreamReadError => UPSTREAM_READ_ERROR,
            Self::UpstreamFirstByteTimeout => UPSTREAM_FIRST_BYTE_TIMEOUT,
            Self::UpstreamMissingFinishReason => UPSTREAM_MISSING_FINISH_REASON,
            Self::UpstreamPreludeOverflow => UPSTREAM_PRELUDE_OVERFLOW,
            Self::UpstreamStreamError => UPSTREAM_STREAM_ERROR,
            Self::RequestDeadlineExceeded => REQUEST_DEADLINE_EXCEEDED,
            Self::Ok => "Upstream stream failed.",
            Self::ClientDisconnected => "Client disconnected.",
        }
    }

    fn is_success(self) -> bool {
        self == Self::Ok
    }
}

enum EventAction {
    Continue,
    Stop(StreamStatus),
}

fn consume_stream(
    context: &ChatContext<'_>,
    stream: &mut TcpStream,
    body: UpstreamBody,
) -> StreamOutcome {
    let watcher = ClientWatcher::start(stream);
    let reader = StreamReader::start(body);
    let mut state = SseState::new();
    let first_byte_deadline = Instant::now()
        + context
            .state
            .config
            .first_byte_timeout
            .min(context.deadline - Instant::now());
    loop {
        if watcher.as_ref().is_some_and(ClientWatcher::disconnected) {
            state.status = StreamStatus::ClientDisconnected;
            break;
        }
        if Instant::now() >= context.deadline {
            state.status = StreamStatus::RequestDeadlineExceeded;
            break;
        }
        if !state.committed && Instant::now() >= first_byte_deadline {
            state.status = StreamStatus::UpstreamFirstByteTimeout;
            break;
        }
        match reader.next_item(Duration::from_millis(50)) {
            NextItem::Timeout => continue,
            NextItem::End => {
                if !state.event_raw.is_empty() {
                    if let EventAction::Stop(status) =
                        process_event(stream, context, &mut state, false)
                    {
                        state.status = status;
                    }
                }
                if state.status.is_success() && !state.done_seen {
                    state.status = StreamStatus::UpstreamEofWithoutDone;
                }
                break;
            }
            NextItem::Error(error) => {
                state.status = if error.timed_out && !state.committed {
                    StreamStatus::UpstreamFirstByteTimeout
                } else if error.timed_out && Instant::now() >= context.deadline {
                    StreamStatus::RequestDeadlineExceeded
                } else {
                    StreamStatus::UpstreamReadError
                };
                state.exception = Some(error.name);
                break;
            }
            NextItem::Line(line) => {
                state.event_raw.extend_from_slice(&line);
                let text = line_text(&line);
                if text.is_empty() {
                    let action = process_event(stream, context, &mut state, true);
                    state.event_raw.clear();
                    state.event_lines.clear();
                    if let EventAction::Stop(status) = action {
                        state.status = status;
                        break;
                    }
                } else {
                    state.event_lines.push(text);
                }
            }
        }
    }
    reader.cancel();
    drop(reader);
    StreamOutcome {
        committed: state.committed,
        status: state.status,
        usage: state.usage,
        completion_chars: state.completion_chars,
        reasoning_chars: state.reasoning_chars,
        data_lines: state.data_lines,
        exception: state.exception,
        explicit_reason: state.explicit_reason,
        elapsed: context.started_at.elapsed(),
    }
}

fn process_event(
    stream: &mut TcpStream,
    context: &ChatContext<'_>,
    state: &mut SseState,
    had_separator: bool,
) -> EventAction {
    let data_lines: Vec<&String> = state
        .event_lines
        .iter()
        .filter(|line| line.starts_with("data:"))
        .collect();
    let payload = data_lines
        .iter()
        .map(|line| data_value(line))
        .collect::<Vec<_>>()
        .join("\n");
    let chunk = if !payload.is_empty() && payload != "[DONE]" {
        serde_json::from_str::<Value>(&payload).ok()
    } else {
        None
    };
    if let Some(chunk) = chunk.as_ref() {
        if let Some(reason) = explicit_error_reason(chunk) {
            state.data_lines += data_lines.len() as u64;
            state.explicit_reason = Some(reason);
            return EventAction::Stop(StreamStatus::UpstreamStreamError);
        }
    }
    let mut substantive = false;
    let mut saw_finish = false;
    if let Some(Value::Object(chunk)) = chunk.as_ref() {
        if let Some(usage) = chunk.get("usage") {
            state.usage = merge_usage_snapshots(state.usage.as_ref(), usage);
        }
        if let Some(choices) = chunk.get("choices").and_then(Value::as_array) {
            for choice in choices {
                if choice.get("finish_reason").is_some_and(truthy) {
                    saw_finish = true;
                }
                let Some(delta) = choice.get("delta").and_then(Value::as_object) else {
                    continue;
                };
                if delta
                    .get("content")
                    .and_then(Value::as_str)
                    .is_some_and(|text| !text.is_empty())
                    || delta.get("tool_calls").is_some_and(truthy)
                    || delta.get("function_call").is_some_and(truthy)
                {
                    substantive = true;
                }
                if let Some(text) = delta.get("content").and_then(Value::as_str) {
                    state.completion_chars += text.chars().count() as u64;
                }
                let reasoning = delta
                    .get("reasoning_content")
                    .and_then(Value::as_str)
                    .or_else(|| delta.get("reasoning").and_then(Value::as_str));
                if let Some(text) = reasoning {
                    state.reasoning_chars += text.chars().count() as u64;
                    // Python 的 preflight 在首段思考内容到达时提交响应。
                    // 思考属于业务输出，不能继续计入 64 KiB 无事件前导缓冲。
                    substantive |= !text.is_empty();
                }
            }
        }
        if saw_finish {
            substantive = true;
        }
    } else if !data_lines.is_empty() && !payload.is_empty() && payload != "[DONE]" {
        substantive = true;
    }
    state.saw_finish_reason |= saw_finish;
    if !data_lines.is_empty() {
        state.data_lines += data_lines.len() as u64;
    }
    let raw = std::mem::take(&mut state.event_raw);
    let _ = had_separator;
    if payload == "[DONE]" {
        if !state.saw_finish_reason {
            return EventAction::Stop(StreamStatus::UpstreamMissingFinishReason);
        }
        state.done_seen = true;
        if state.committed {
            let _ = write_chunk(stream, &raw);
        }
        return EventAction::Stop(StreamStatus::Ok);
    }
    if substantive {
        if !state.committed && !commit(stream, context, state) {
            return EventAction::Stop(StreamStatus::ClientDisconnected);
        }
        let _ = write_chunk(stream, &raw);
        return EventAction::Continue;
    }
    if state.committed {
        let _ = write_chunk(stream, &raw);
    } else {
        state.staged.extend_from_slice(&raw);
        if state.staged.len() > PRELUDE_MAX_BYTES {
            return EventAction::Stop(StreamStatus::UpstreamPreludeOverflow);
        }
    }
    EventAction::Continue
}

fn commit(stream: &mut TcpStream, context: &ChatContext<'_>, state: &mut SseState) -> bool {
    state.committed = true;
    let mut headers = vec![
        ("Content-Type".to_string(), SSE_CONTENT_TYPE.to_string()),
        ("Cache-Control".to_string(), "no-cache".to_string()),
        (
            "X-Request-Id".to_string(),
            context.http_request_id.to_string(),
        ),
        ("Connection".to_string(), "close".to_string()),
    ];
    if let Some(origin) = &context.origin {
        headers.push(("Access-Control-Allow-Origin".to_string(), origin.clone()));
    }
    if write_response_head(stream, 200, &headers).is_err() {
        return false;
    }
    let staged = std::mem::take(&mut state.staged);
    write_chunk(stream, &staged).is_ok()
}

fn record_stream_usage(context: &ChatContext<'_>, outcome: &StreamOutcome) {
    if outcome.status.is_success() {
        context
            .state
            .proxy
            .record_business_success("chat_stream", Some(200));
    } else if outcome.status != StreamStatus::ClientDisconnected {
        let class = match outcome.status {
            StreamStatus::UpstreamFirstByteTimeout
            | StreamStatus::RequestDeadlineExceeded => {
                crate::upstream_fault::FaultClass::Timeout
            }
            StreamStatus::UpstreamReadError => {
                crate::upstream_fault::FaultClass::NetworkError
            }
            _ => crate::upstream_fault::FaultClass::StreamError,
        };
        context.state.proxy.record_business_failure(
            class,
            None,
            outcome.exception.map(str::to_string),
        );
    }
    let output_chars = outcome.completion_chars + outcome.reasoning_chars;
    let out_est = if outcome.status.is_success() {
        (output_chars / 4).max(1)
    } else {
        output_chars / 4
    };
    context.state.ledger.record(UsageRecord {
        model: context.model,
        stream: true,
        started_at: context.started_at,
        usage: outcome.usage.as_ref(),
        input_estimate: context.input_estimate,
        output_estimate: out_est,
        status: outcome.status.reason(),
        protocol: Some("chat"),
        key_id: context.key_id,
        request_id: context.request_id,
        attribution: context.attribution,
    });
}

fn stream_error_response(outcome: &StreamOutcome, request_id: &str) -> Response {
    let error = match outcome.status {
        StreamStatus::UpstreamFirstByteTimeout => GatewayError::upstream_timeout(),
        StreamStatus::RequestDeadlineExceeded => GatewayError::request_deadline_exceeded(None),
        _ => GatewayError::new(502, outcome.status.message(), "upstream_stream_error"),
    };
    error
        .with_detail(stream_error_detail(outcome, request_id))
        .response()
}

fn write_stream_error(
    stream: &mut TcpStream,
    status: StreamStatus,
    outcome: &StreamOutcome,
    request_id: &str,
) {
    let payload = json!({"error": GatewayError::new(502, status.message(), status.reason())
        .with_detail(stream_error_detail(outcome, request_id))
        .object()});
    let raw = serde_json::to_vec(&payload).unwrap_or_default();
    let mut event = Vec::new();
    event.extend_from_slice(b"data: ");
    event.extend_from_slice(&raw);
    event.extend_from_slice(b"\n\n");
    let _ = write_chunk(stream, &event);
}

fn stream_error_detail(outcome: &StreamOutcome, request_id: &str) -> Value {
    let mut detail = json!({
        "reason": outcome.status.reason(),
        "events": outcome.data_lines,
        "duration_s": (outcome.elapsed.as_secs_f64() * 10.0).round() / 10.0,
        "request_id": request_id,
    });
    if let Some(exception) = outcome.exception {
        detail["exception"] = json!(exception);
    }
    if let Some(reason) = &outcome.explicit_reason {
        detail["upstream_message"] = json!(reason);
    }
    detail
}

pub(crate) fn line_text(line: &[u8]) -> String {
    let text = String::from_utf8_lossy(line);
    text.trim_end_matches('\n')
        .trim_end_matches('\r')
        .to_string()
}

fn data_value(line: &str) -> &str {
    let value = line.strip_prefix("data:").unwrap_or(line);
    value.strip_prefix(' ').unwrap_or(value)
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_some_and(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

pub(crate) fn explicit_error_reason(chunk: &Value) -> Option<String> {
    let error = chunk.get("error")?;
    if !truthy(error) {
        return None;
    }
    let message = match error {
        Value::Object(map) => map.get("message"),
        other => Some(other),
    };
    match message {
        Some(Value::String(text)) if !text.trim().is_empty() => Some(sanitize_reason(text)),
        _ => Some("upstream reported an explicit stream error".to_string()),
    }
}

fn sanitize_reason(text: &str) -> String {
    let text = text.replace(['\r', '\n'], " ");
    let collapsed = text.split_whitespace().collect::<Vec<_>>().join(" ");
    collapsed.chars().take(200).collect()
}

fn json_type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

fn random_request_id() -> String {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let mut hasher = Sha256::new();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    hasher.update(nanos.to_le_bytes());
    hasher.update(COUNTER.fetch_add(1, Ordering::SeqCst).to_le_bytes());
    hasher.update(std::process::id().to_le_bytes());
    let digest = hasher.finalize();
    digest[..8]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

pub(crate) struct ClientWatcher {
    flag: Arc<AtomicBool>,
    done: Arc<AtomicBool>,
}

impl ClientWatcher {
    pub(crate) fn start(stream: &TcpStream) -> Option<Self> {
        let clone = stream.try_clone().ok()?;
        let _ = clone.set_read_timeout(Some(Duration::from_millis(100)));
        let flag = Arc::new(AtomicBool::new(false));
        let done = Arc::new(AtomicBool::new(false));
        let worker_flag = Arc::clone(&flag);
        let worker_done = Arc::clone(&done);
        thread::spawn(move || {
            let mut byte = [0u8; 1];
            while !worker_done.load(Ordering::SeqCst) {
                match clone.peek(&mut byte) {
                    Ok(0) => {
                        worker_flag.store(true, Ordering::SeqCst);
                        break;
                    }
                    Ok(_) => thread::sleep(Duration::from_millis(20)),
                    Err(error)
                        if error.kind() == std::io::ErrorKind::WouldBlock
                            || error.kind() == std::io::ErrorKind::TimedOut => {}
                    Err(_) => {
                        worker_flag.store(true, Ordering::SeqCst);
                        break;
                    }
                }
            }
        });
        Some(Self { flag, done })
    }

    pub(crate) fn disconnected(&self) -> bool {
        self.flag.load(Ordering::SeqCst)
    }
}

impl Drop for ClientWatcher {
    fn drop(&mut self) {
        self.done.store(true, Ordering::SeqCst);
    }
}
