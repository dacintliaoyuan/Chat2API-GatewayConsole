//! Non-stream Chat completion aggregation.
//!
//! Ports the decoder in `shared/nonstream_aggregate.py` for the fields the
//! Responses translation consumes: per-choice content, reasoning channels, tool
//! calls, the legacy single-call channel, refusal, annotations, audio, logprobs,
//! finish reason and the merged usage tail. Only a stream that observed BOTH the
//! real `[DONE]` sentinel and a non-empty `finish_reason` yields a body; a
//! partial completion is never returned as a success.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

/// Why an aggregate failed; the caller must not return a success body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AggregateFailure {
    BadFrame,
    ExplicitError(String),
    MissingTerminal,
}

#[derive(Debug, Default, Clone)]
struct ToolCallState {
    id: String,
    kind: String,
    name: String,
    arguments: Vec<String>,
}

#[derive(Debug, Default)]
struct ChoiceState {
    content: Vec<String>,
    reasoning: BTreeMap<String, Vec<String>>,
    tool_calls: BTreeMap<i64, ToolCallState>,
    function_call: Option<ToolCallState>,
    refusal: Vec<String>,
    annotations: Vec<Value>,
    audio: Option<Value>,
    logprobs: Option<Value>,
    finish_reason: Option<String>,
}

/// The non-stream Chat completion aggregator.
#[derive(Debug)]
pub struct ChatCompletionAggregator {
    default_model: Option<String>,
    choices: BTreeMap<i64, ChoiceState>,
    id: Option<String>,
    created: Option<i64>,
    model: Option<String>,
    system_fingerprint: Option<String>,
    service_tier: Option<String>,
    usage: Option<Value>,
    finish_reason_seen: bool,
    done: bool,
}

impl ChatCompletionAggregator {
    pub fn new(default_model: Option<&str>) -> Self {
        Self {
            default_model: default_model.map(str::to_string),
            choices: BTreeMap::new(),
            id: None,
            created: None,
            model: None,
            system_fingerprint: None,
            service_tier: None,
            usage: None,
            finish_reason_seen: false,
            done: false,
        }
    }

    /// Feed one SSE line (without its newline).
    pub fn feed_line(&mut self, line: &str) -> Result<(), AggregateFailure> {
        if self.done {
            return Ok(());
        }
        let line = line.trim_end_matches('\r');
        if line.is_empty() || line.starts_with(':') {
            return Ok(());
        }
        let Some(value) = line.strip_prefix("data:") else {
            return Ok(());
        };
        let value = value.strip_prefix(' ').unwrap_or(value);
        if value.trim() == "[DONE]" {
            self.done = true;
            return Ok(());
        }
        let chunk: Value = serde_json::from_str(value).map_err(|_| AggregateFailure::BadFrame)?;
        let chunk = chunk.as_object().ok_or(AggregateFailure::BadFrame)?;
        if let Some(reason) = explicit_error_reason(chunk) {
            return Err(AggregateFailure::ExplicitError(reason));
        }
        self.observe_chunk(chunk)
    }

    /// Close the stream at EOF and return the one completion body.
    pub fn finish(&mut self) -> Result<Value, AggregateFailure> {
        if !(self.done && self.finish_reason_seen) {
            return Err(AggregateFailure::MissingTerminal);
        }
        Ok(self.result())
    }

    pub fn usage(&self) -> Option<&Value> {
        self.usage.as_ref()
    }

    pub fn output_estimate(&self) -> u64 {
        let mut chars = 0u64;
        for state in self.choices.values() {
            chars += state
                .content
                .iter()
                .map(|piece| piece.chars().count() as u64)
                .sum::<u64>();
            chars += state
                .reasoning
                .values()
                .flatten()
                .map(|piece| piece.chars().count() as u64)
                .sum::<u64>();
        }
        chars / 4
    }

    fn observe_chunk(&mut self, chunk: &Map<String, Value>) -> Result<(), AggregateFailure> {
        if let Some(id) = chunk
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
        {
            if self.id.is_none() {
                self.id = Some(id.to_string());
            }
        }
        if let Some(created) = chunk.get("created").and_then(Value::as_i64) {
            if self.created.is_none() {
                self.created = Some(created);
            }
        }
        if let Some(model) = chunk
            .get("model")
            .and_then(Value::as_str)
            .filter(|model| !model.is_empty())
        {
            if self.model.is_none() {
                self.model = Some(model.to_string());
            }
        }
        for (field, target) in [
            ("system_fingerprint", &mut self.system_fingerprint),
            ("service_tier", &mut self.service_tier),
        ] {
            if let Some(value) = chunk
                .get(field)
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
            {
                if target.is_none() {
                    *target = Some(value.to_string());
                }
            }
        }
        let empty = Vec::new();
        let choices = match chunk.get("choices") {
            None | Some(Value::Null) => &empty,
            Some(Value::Array(choices)) => choices,
            Some(_) => return Err(AggregateFailure::BadFrame),
        };
        for choice in choices {
            let choice = choice.as_object().ok_or(AggregateFailure::BadFrame)?;
            let index = match choice.get("index") {
                None | Some(Value::Null) => {
                    if choices.len() > 1 {
                        return Err(AggregateFailure::BadFrame);
                    }
                    0
                }
                Some(Value::Number(number)) => number
                    .as_i64()
                    .filter(|index| *index >= 0)
                    .ok_or(AggregateFailure::BadFrame)?,
                Some(_) => return Err(AggregateFailure::BadFrame),
            };
            let state = self.choices.entry(index).or_default();
            if let Some(delta) = choice.get("delta") {
                let delta = delta.as_object().ok_or(AggregateFailure::BadFrame)?;
                observe_delta(state, delta)?;
            }
            if let Some(logprobs) = choice.get("logprobs") {
                state.logprobs = Some(merge_aux_dict(state.logprobs.take(), logprobs)?);
            }
            if let Some(refusal) = choice.get("refusal") {
                match refusal {
                    Value::String(piece) if !piece.is_empty() => state.refusal.push(piece.clone()),
                    Value::Null => {}
                    _ => return Err(AggregateFailure::BadFrame),
                }
            }
            if let Some(annotations) = choice.get("annotations") {
                state.annotations =
                    append_annotations(std::mem::take(&mut state.annotations), Some(annotations))?;
            }
            if let Some(audio) = choice.get("audio") {
                state.audio = merge_audio(state.audio.take(), Some(audio))?;
            }
            if let Some(finish_reason) = choice.get("finish_reason") {
                if !truthy(finish_reason) {
                    continue;
                }
                let finish_reason = finish_reason.as_str().ok_or(AggregateFailure::BadFrame)?;
                state.finish_reason = Some(finish_reason.to_string());
                self.finish_reason_seen = true;
            }
        }
        if let Some(usage) = chunk.get("usage") {
            if !usage.is_object() {
                return Err(AggregateFailure::BadFrame);
            }
            self.usage = merge_usage(self.usage.take(), Some(usage))?;
        }
        Ok(())
    }

    fn result(&self) -> Value {
        let mut choices = Vec::new();
        for (index, state) in &self.choices {
            let mut message = Map::new();
            message.insert("role".to_string(), json!("assistant"));
            message.insert("content".to_string(), json!(state.content.join("")));
            for key in ["reasoning", "reasoning_content"] {
                if let Some(pieces) = state.reasoning.get(key) {
                    if !pieces.is_empty() {
                        message.insert(key.to_string(), json!(pieces.join("")));
                    }
                }
            }
            if !state.tool_calls.is_empty() {
                let tool_calls = state
                    .tool_calls
                    .values()
                    .map(|call| {
                        let mut function = Map::new();
                        function.insert("arguments".to_string(), json!(call.arguments.join("")));
                        if !call.name.is_empty() {
                            function.insert("name".to_string(), json!(call.name));
                        }
                        let mut item = Map::new();
                        item.insert("function".to_string(), Value::Object(function));
                        if !call.id.is_empty() {
                            item.insert("id".to_string(), json!(call.id));
                        }
                        if !call.kind.is_empty() {
                            item.insert("type".to_string(), json!(call.kind));
                        }
                        Value::Object(item)
                    })
                    .collect();
                message.insert("tool_calls".to_string(), Value::Array(tool_calls));
            } else if let Some(call) = &state.function_call {
                let mut function = Map::new();
                function.insert("arguments".to_string(), json!(call.arguments.join("")));
                if !call.name.is_empty() {
                    function.insert("name".to_string(), json!(call.name));
                }
                message.insert(
                    "tool_calls".to_string(),
                    json!([{"type": "function", "function": function}]),
                );
            }
            if !state.refusal.is_empty() {
                message.insert("refusal".to_string(), json!(state.refusal.join("")));
            }
            if !state.annotations.is_empty() {
                message.insert(
                    "annotations".to_string(),
                    Value::Array(state.annotations.clone()),
                );
            }
            if let Some(audio) = &state.audio {
                message.insert("audio".to_string(), audio.clone());
            }
            let mut choice = Map::new();
            choice.insert("index".to_string(), json!(index));
            choice.insert("message".to_string(), Value::Object(message));
            choice.insert(
                "finish_reason".to_string(),
                state
                    .finish_reason
                    .as_ref()
                    .map(|reason| json!(reason))
                    .unwrap_or(Value::Null),
            );
            if let Some(logprobs) = &state.logprobs {
                choice.insert("logprobs".to_string(), logprobs.clone());
            }
            choices.push(Value::Object(choice));
        }
        let mut result = Map::new();
        result.insert(
            "id".to_string(),
            json!(self.id.as_deref().unwrap_or_default()),
        );
        result.insert("object".to_string(), json!("chat.completion"));
        result.insert(
            "model".to_string(),
            json!(self
                .model
                .as_deref()
                .or(self.default_model.as_deref())
                .unwrap_or_default()),
        );
        result.insert("choices".to_string(), Value::Array(choices));
        if let Some(created) = self.created {
            result.insert("created".to_string(), json!(created));
        }
        if let Some(fingerprint) = &self.system_fingerprint {
            result.insert("system_fingerprint".to_string(), json!(fingerprint));
        }
        if let Some(service_tier) = &self.service_tier {
            result.insert("service_tier".to_string(), json!(service_tier));
        }
        if let Some(usage) = &self.usage {
            result.insert("usage".to_string(), usage.clone());
        }
        Value::Object(result)
    }
}

fn observe_delta(
    state: &mut ChoiceState,
    delta: &Map<String, Value>,
) -> Result<(), AggregateFailure> {
    if let Some(content) = delta.get("content") {
        match content {
            Value::String(piece) if !piece.is_empty() => state.content.push(piece.clone()),
            Value::String(_) | Value::Null => {}
            _ => return Err(AggregateFailure::BadFrame),
        }
    }
    for key in ["reasoning", "reasoning_content"] {
        if let Some(piece) = delta.get(key) {
            match piece {
                Value::String(piece) if !piece.is_empty() => state
                    .reasoning
                    .entry(key.to_string())
                    .or_default()
                    .push(piece.clone()),
                Value::String(_) | Value::Null => {}
                _ => return Err(AggregateFailure::BadFrame),
            }
        }
    }
    if let Some(refusal) = delta.get("refusal") {
        match refusal {
            Value::String(piece) if !piece.is_empty() => state.refusal.push(piece.clone()),
            Value::String(_) | Value::Null => {}
            _ => return Err(AggregateFailure::BadFrame),
        }
    }
    if let Some(annotations) = delta.get("annotations") {
        state.annotations =
            append_annotations(std::mem::take(&mut state.annotations), Some(annotations))?;
    }
    if let Some(audio) = delta.get("audio") {
        state.audio = merge_audio(state.audio.take(), Some(audio))?;
    }
    if let Some(logprobs) = delta.get("logprobs") {
        state.logprobs = Some(merge_aux_dict(state.logprobs.take(), logprobs)?);
    }
    if let Some(function_call) = delta.get("function_call") {
        let function_call = function_call
            .as_object()
            .ok_or(AggregateFailure::BadFrame)?;
        let name = match function_call.get("name") {
            Some(Value::String(name)) => Some(name.clone()),
            None | Some(Value::Null) => None,
            _ => return Err(AggregateFailure::BadFrame),
        };
        let arguments = match function_call.get("arguments") {
            Some(Value::String(arguments)) => Some(arguments.clone()),
            None | Some(Value::Null) => None,
            _ => return Err(AggregateFailure::BadFrame),
        };
        if name.is_some() || arguments.is_some() {
            let legacy = state
                .function_call
                .get_or_insert_with(ToolCallState::default);
            if let Some(name) = name.filter(|name| !name.is_empty()) {
                if legacy.name.is_empty() {
                    legacy.name = name;
                }
            }
            if let Some(arguments) = arguments.filter(|arguments| !arguments.is_empty()) {
                legacy.arguments.push(arguments);
            }
        }
    }
    let Some(tool_calls) = delta.get("tool_calls") else {
        return Ok(());
    };
    let tool_calls = tool_calls.as_array().ok_or(AggregateFailure::BadFrame)?;
    for tool_call in tool_calls {
        let tool_call = tool_call.as_object().ok_or(AggregateFailure::BadFrame)?;
        let index = match tool_call.get("index") {
            None | Some(Value::Null) => {
                if tool_calls.len() > 1 {
                    return Err(AggregateFailure::BadFrame);
                }
                0
            }
            Some(Value::Number(number)) => number
                .as_i64()
                .filter(|index| *index >= 0)
                .ok_or(AggregateFailure::BadFrame)?,
            Some(_) => return Err(AggregateFailure::BadFrame),
        };
        let call_id = match tool_call.get("id") {
            Some(Value::String(id)) => Some(id.clone()),
            None | Some(Value::Null) => None,
            _ => return Err(AggregateFailure::BadFrame),
        };
        let kind = match tool_call.get("type") {
            Some(Value::String(kind)) => Some(kind.clone()),
            None | Some(Value::Null) => None,
            _ => return Err(AggregateFailure::BadFrame),
        };
        let function = match tool_call.get("function") {
            None | Some(Value::Null) => None,
            Some(Value::Object(function)) => Some(function),
            Some(_) => return Err(AggregateFailure::BadFrame),
        };
        let name = match function.and_then(|function| function.get("name")) {
            Some(Value::String(name)) => Some(name.clone()),
            None | Some(Value::Null) => None,
            _ => return Err(AggregateFailure::BadFrame),
        };
        let arguments = match function.and_then(|function| function.get("arguments")) {
            Some(Value::String(arguments)) => Some(arguments.clone()),
            None | Some(Value::Null) => None,
            _ => return Err(AggregateFailure::BadFrame),
        };
        if !(call_id.is_some() || kind.is_some() || name.is_some() || arguments.is_some()) {
            continue;
        }
        let call = state.tool_calls.entry(index).or_default();
        if let Some(call_id) = call_id.filter(|id| !id.is_empty()) {
            if call.id.is_empty() {
                call.id = call_id;
            }
        }
        if let Some(kind) = kind.filter(|kind| !kind.is_empty()) {
            if call.kind.is_empty() {
                call.kind = kind;
            }
        }
        if let Some(name) = name.filter(|name| !name.is_empty()) {
            if call.name.is_empty() {
                call.name = name;
            }
        }
        if let Some(arguments) = arguments.filter(|arguments| !arguments.is_empty()) {
            call.arguments.push(arguments);
        }
    }
    Ok(())
}

fn reported_count(value: &Value) -> Option<u64> {
    value.as_u64()
}

fn merge_usage(
    base: Option<Value>,
    update: Option<&Value>,
) -> Result<Option<Value>, AggregateFailure> {
    let Some(update) = update.and_then(Value::as_object) else {
        return Ok(base);
    };
    if update.is_empty() {
        return Ok(base);
    }
    let mut merged = base
        .and_then(|base| base.as_object().cloned())
        .unwrap_or_default();
    for (key, value) in update {
        if let Value::Object(value) = value {
            let nested = merged.get(key).and_then(Value::as_object).cloned();
            if let Some(nested) = merge_usage(
                nested.map(Value::Object),
                Some(&Value::Object(value.clone())),
            )? {
                merged.insert(key.clone(), nested);
            }
            continue;
        }
        if let Some(count) = reported_count(value) {
            merged.insert(key.clone(), json!(count));
        }
    }
    Ok(if merged.is_empty() {
        None
    } else {
        Some(Value::Object(merged))
    })
}

fn merge_aux_dict(base: Option<Value>, update: &Value) -> Result<Value, AggregateFailure> {
    let update = update.as_object().ok_or(AggregateFailure::BadFrame)?;
    let mut merged = base
        .and_then(|base| base.as_object().cloned())
        .unwrap_or_default();
    for (key, value) in update {
        if value.is_null() {
            continue;
        }
        if let Value::Object(value) = value {
            let nested = merge_aux_dict(merged.get(key).cloned(), &Value::Object(value.clone()))?;
            merged.insert(key.clone(), nested);
        } else if let Value::Array(items) = value {
            if items.is_empty() {
                continue;
            }
            match merged.get(key) {
                None | Some(Value::Null) => {
                    merged.insert(key.clone(), Value::Array(items.clone()));
                }
                Some(Value::Array(existing)) => {
                    let mut combined = existing.clone();
                    combined.extend(items.iter().cloned());
                    merged.insert(key.clone(), Value::Array(combined));
                }
                Some(_) => return Err(AggregateFailure::BadFrame),
            }
        } else {
            merged.insert(key.clone(), value.clone());
        }
    }
    Ok(if merged.is_empty() {
        Value::Null
    } else {
        Value::Object(merged)
    })
}

fn append_annotations(
    base: Vec<Value>,
    update: Option<&Value>,
) -> Result<Vec<Value>, AggregateFailure> {
    let Some(update) = update else {
        return Ok(base);
    };
    let update = update.as_array().ok_or(AggregateFailure::BadFrame)?;
    if update.is_empty() {
        return Ok(base);
    }
    let mut merged = base;
    merged.extend(update.iter().cloned());
    Ok(merged)
}

fn merge_audio(
    base: Option<Value>,
    update: Option<&Value>,
) -> Result<Option<Value>, AggregateFailure> {
    let Some(update) = update else {
        return Ok(base);
    };
    let update = update.as_object().ok_or(AggregateFailure::BadFrame)?;
    let mut merged = base
        .and_then(|base| base.as_object().cloned())
        .unwrap_or_default();
    for (key, value) in update {
        if value.is_null() {
            continue;
        }
        match key.as_str() {
            "data" | "transcript" => {
                let piece = value.as_str().ok_or(AggregateFailure::BadFrame)?;
                if !piece.is_empty() {
                    let current = merged.get(key).and_then(Value::as_str).unwrap_or_default();
                    merged.insert(key.clone(), json!(format!("{current}{piece}")));
                }
            }
            "id" => {
                let id = value.as_str().ok_or(AggregateFailure::BadFrame)?;
                if !id.is_empty() && !merged.contains_key("id") {
                    merged.insert("id".to_string(), json!(id));
                }
            }
            "expires_at" => {
                if value.as_i64().is_none() {
                    return Err(AggregateFailure::BadFrame);
                }
                merged.insert(key.clone(), value.clone());
            }
            _ => {
                merged.insert(key.clone(), value.clone());
            }
        }
    }
    Ok(if merged.is_empty() {
        None
    } else {
        Some(Value::Object(merged))
    })
}

fn explicit_error_reason(chunk: &Map<String, Value>) -> Option<String> {
    let error = chunk.get("error")?;
    if !truthy(error) {
        return None;
    }
    let message = match error {
        Value::Object(error) => error.get("message"),
        error => Some(error),
    };
    match message {
        Some(Value::String(text)) if !text.trim().is_empty() => {
            let text = text.replace(['\r', '\n'], " ");
            Some(
                text.split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")
                    .chars()
                    .take(200)
                    .collect(),
            )
        }
        _ => Some("upstream reported an explicit stream error".to_string()),
    }
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
