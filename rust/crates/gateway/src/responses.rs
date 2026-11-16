//! OpenAI Responses surface (`POST /v1/responses`).
//!
//! Ports `chat2api.Handler._responses` and the pure translations in
//! `shared/responses_protocol.py`: request admission, the Responses-to-chat
//! translation, the chat/Messages response translations and the stream event
//! translation. The image-bearing tool-result branch uses the Anthropic Messages
//! upstream exactly like Python.

use std::collections::BTreeMap;
use std::net::TcpStream;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::body::{read_json_body, BodyPolicy};
use crate::chat::{line_text, ChatResult, ClientWatcher};
use crate::error::GatewayError;
use crate::gate::Admission;
use crate::http::{write_chunk, write_response_head, Request, Response};
use crate::ledger::{message_tokens, UsageRecord};
use crate::nonstream::{AggregateFailure, ChatCompletionAggregator};
use crate::server::AppState;
use crate::upstream::{post_compat, NextItem, StreamReader, UpstreamFailure, UpstreamResponse};

/// The frozen Responses request field whitelist.
pub const RESPONSES_FIELDS: [&str; 17] = [
    "model",
    "input",
    "instructions",
    "stream",
    "max_output_tokens",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "store",
    "prompt_cache_key",
    "prompt_cache_retention",
    "prompt_cache_options",
    "service_tier",
    "reasoning",
    "include",
];

/// Python `valid_responses_request`: shape-only admission of the frozen subset.
pub fn valid_responses_request(body: &Map<String, Value>) -> bool {
    if body
        .keys()
        .any(|key| !RESPONSES_FIELDS.contains(&key.as_str()))
    {
        return false;
    }
    if !body
        .get("model")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.is_empty())
    {
        return false;
    }
    let source = body.get("input");
    match source {
        Some(Value::String(value)) if !value.is_empty() => {}
        Some(Value::Array(items)) if !items.is_empty() => {}
        _ => return false,
    }
    if !matches!(body.get("stream"), None | Some(Value::Bool(_))) {
        return false;
    }
    if body
        .get("store")
        .is_some_and(|value| value != &Value::Bool(false))
    {
        return false;
    }
    if body
        .get("max_output_tokens")
        .is_some_and(|value| !positive_integer(value))
    {
        return false;
    }
    for name in ["temperature", "top_p"] {
        if body
            .get(name)
            .is_some_and(|value| !value.is_number() || value.is_boolean())
        {
            return false;
        }
    }
    if body
        .get("instructions")
        .is_some_and(|value| !matches!(value, Value::Null | Value::String(_)))
    {
        return false;
    }
    if body
        .get("parallel_tool_calls")
        .is_some_and(|value| !matches!(value, Value::Null | Value::Bool(_)))
    {
        return false;
    }
    if let Some(tools) = body.get("tools") {
        let Some(tools) = tools.as_array() else {
            return false;
        };
        for tool in tools {
            let Some(tool) = tool.as_object() else {
                return false;
            };
            let Some(kind) = tool.get("type").and_then(Value::as_str) else {
                return false;
            };
            if kind.is_empty() {
                return false;
            }
            if kind == "function"
                && !tool
                    .get("name")
                    .and_then(Value::as_str)
                    .is_some_and(|name| !name.is_empty())
            {
                return false;
            }
        }
    }
    if body
        .get("reasoning")
        .is_some_and(|value| !matches!(value, Value::Null | Value::Object(_)))
    {
        return false;
    }
    if body.get("include").is_some_and(|value| {
        !matches!(value, Value::Null | Value::Array(_))
            || value
                .as_array()
                .is_some_and(|items| items.iter().any(|item| !item.is_string()))
    }) {
        return false;
    }
    !responses_to_chat_messages(body).is_empty()
}

fn positive_integer(value: &Value) -> bool {
    value.as_u64().is_some_and(|number| number >= 1)
}

/// Python `_responses_text`: flatten Responses text parts into one string.
fn responses_text(content: Option<&Value>) -> String {
    match content {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(parts)) => {
            let mut texts = Vec::new();
            for part in parts {
                match part {
                    Value::String(text) => texts.push(text.clone()),
                    Value::Object(part) => {
                        if matches!(
                            part.get("type").and_then(Value::as_str),
                            Some("input_text" | "output_text" | "text")
                        ) {
                            if let Some(text) = part.get("text").and_then(Value::as_str) {
                                texts.push(text.to_string());
                            }
                        }
                    }
                    _ => {}
                }
            }
            texts.join("\n")
        }
        _ => String::new(),
    }
}

/// Python `_responses_chat_content`: Responses content -> chat content.
fn responses_chat_content(content: Option<&Value>) -> Value {
    let Some(content) = content else {
        return Value::String(String::new());
    };
    if let Value::String(text) = content {
        return Value::String(text.clone());
    }
    let Value::Array(parts) = content else {
        return Value::String(String::new());
    };
    let mut translated = Vec::new();
    for part in parts {
        match part {
            Value::String(text) => translated.push(json!({"type": "text", "text": text})),
            Value::Object(part) => match part.get("type").and_then(Value::as_str) {
                Some("input_text" | "output_text" | "text") => {
                    if let Some(text) = part.get("text").and_then(Value::as_str) {
                        translated.push(json!({"type": "text", "text": text}));
                    }
                }
                Some("input_image") => {
                    let image_url = part.get("image_url");
                    let url = match image_url {
                        Some(Value::String(url)) => Some(url.clone()),
                        Some(Value::Object(image)) => {
                            image.get("url").and_then(Value::as_str).map(str::to_string)
                        }
                        _ => None,
                    };
                    if let Some(url) = url {
                        translated.push(json!({
                            "type": "image_url",
                            "image_url": {"url": url},
                        }));
                    }
                }
                _ => {}
            },
            _ => {}
        }
    }
    if !translated.is_empty()
        && translated
            .iter()
            .all(|part| part.get("type").and_then(Value::as_str) == Some("text"))
    {
        return Value::String(
            translated
                .iter()
                .filter_map(|part| part.get("text").and_then(Value::as_str))
                .collect::<Vec<_>>()
                .join("\n"),
        );
    }
    Value::Array(translated)
}

/// Python `responses_to_chat_messages`.
pub fn responses_to_chat_messages(data: &Map<String, Value>) -> Vec<Value> {
    let mut messages = Vec::new();
    if let Some(instructions) = data.get("instructions").and_then(Value::as_str) {
        if !instructions.is_empty() {
            messages.push(json!({"role": "system", "content": instructions}));
        }
    }
    let source = data.get("input");
    if let Some(Value::String(text)) = source {
        messages.push(json!({"role": "user", "content": text}));
        return messages;
    }
    let Some(Value::Array(items)) = source else {
        return messages;
    };
    for item in items {
        match item {
            Value::String(text) => messages.push(json!({"role": "user", "content": text})),
            Value::Object(item) => {
                let kind = item.get("type").and_then(Value::as_str);
                if kind == Some("message") || (kind.is_none() && item.contains_key("role")) {
                    let mut role = item.get("role").and_then(Value::as_str);
                    if matches!(role, Some("system" | "developer")) {
                        role = Some("system");
                    }
                    let Some(role) =
                        role.filter(|role| matches!(*role, "user" | "assistant" | "system"))
                    else {
                        continue;
                    };
                    messages.push(json!({
                        "role": role,
                        "content": responses_chat_content(item.get("content")),
                    }));
                } else if kind == Some("function_call") {
                    let (Some(call_id), Some(name)) = (
                        item.get("call_id").and_then(Value::as_str),
                        item.get("name").and_then(Value::as_str),
                    ) else {
                        continue;
                    };
                    let arguments = arguments_or_empty(item.get("arguments"));
                    let call = json!({
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    });
                    let mut appended = false;
                    if let Some(Value::Object(last)) = messages.last_mut() {
                        if last.get("role").and_then(Value::as_str) == Some("assistant") {
                            if let Some(Value::Array(existing)) = last.get_mut("tool_calls") {
                                existing.push(call.clone());
                                appended = true;
                            } else if last.get("content").map_or(true, Value::is_null) {
                                last.insert("tool_calls".to_string(), json!([call]));
                                appended = true;
                            }
                        }
                    }
                    if !appended {
                        messages.push(json!({
                            "role": "assistant",
                            "content": Value::Null,
                            "tool_calls": [call],
                        }));
                    }
                } else if kind == Some("function_call_output") {
                    let Some(call_id) = item.get("call_id").and_then(Value::as_str) else {
                        continue;
                    };
                    messages.push(json!({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": responses_text(item.get("output")),
                    }));
                }
            }
            _ => {}
        }
    }
    messages
}

/// Python `responses_to_chat_tools`.
fn responses_to_chat_tools(tools: Option<&Value>) -> Vec<Value> {
    let mut translated = Vec::new();
    let Some(Value::Array(tools)) = tools else {
        return translated;
    };
    for tool in tools {
        let Some(tool) = tool.as_object() else {
            continue;
        };
        if tool.get("type").and_then(Value::as_str) != Some("function") {
            continue;
        }
        let Some(name) = tool
            .get("name")
            .and_then(Value::as_str)
            .filter(|name| !name.is_empty())
        else {
            continue;
        };
        let mut function = Map::new();
        function.insert("name".to_string(), json!(name));
        if let Some(description) = tool.get("description").and_then(Value::as_str) {
            function.insert("description".to_string(), json!(description));
        }
        if let Some(parameters) = tool.get("parameters").and_then(Value::as_object) {
            function.insert("parameters".to_string(), Value::Object(parameters.clone()));
        }
        if let Some(strict) = tool.get("strict").and_then(Value::as_bool) {
            function.insert("strict".to_string(), json!(strict));
        }
        translated.push(json!({"type": "function", "function": function}));
    }
    translated
}

/// Python `responses_to_chat_tool_choice`.
fn responses_to_chat_tool_choice(value: Option<&Value>) -> Option<Value> {
    match value {
        Some(Value::String(value)) => Some(Value::String(value.clone())),
        Some(Value::Object(value)) => match value.get("type").and_then(Value::as_str) {
            Some(kind @ ("auto" | "none" | "required")) => Some(json!(kind)),
            Some("function") => value
                .get("name")
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
                .map(|name| json!({"type": "function", "function": {"name": name}})),
            _ => None,
        },
        _ => None,
    }
}

/// Python `responses_to_chat_request`.
pub fn responses_to_chat_request(data: &Map<String, Value>) -> Map<String, Value> {
    let mut body = Map::new();
    body.insert(
        "model".to_string(),
        data.get("model").cloned().unwrap_or(Value::Null),
    );
    body.insert(
        "messages".to_string(),
        Value::Array(responses_to_chat_messages(data)),
    );
    if data.get("stream") == Some(&Value::Bool(true)) {
        body.insert("stream".to_string(), Value::Bool(true));
        body.insert("stream_options".to_string(), json!({"include_usage": true}));
    }
    if let Some(max_output) = data.get("max_output_tokens") {
        if positive_integer(max_output) {
            body.insert("max_completion_tokens".to_string(), max_output.clone());
        }
    }
    for name in ["temperature", "top_p"] {
        if let Some(value) = data.get(name) {
            if value.is_number() && !value.is_boolean() {
                body.insert(name.to_string(), value.clone());
            }
        }
    }
    let tools = responses_to_chat_tools(data.get("tools"));
    if !tools.is_empty() {
        body.insert("tools".to_string(), Value::Array(tools));
    }
    if let Some(tool_choice) = responses_to_chat_tool_choice(data.get("tool_choice")) {
        body.insert("tool_choice".to_string(), tool_choice);
    }
    if let Some(parallel) = data.get("parallel_tool_calls").and_then(Value::as_bool) {
        body.insert("parallel_tool_calls".to_string(), json!(parallel));
    }
    if let Some(reasoning) = data.get("reasoning").and_then(Value::as_object) {
        if let Some(effort) = reasoning
            .get("effort")
            .and_then(Value::as_str)
            .filter(|effort| !effort.is_empty())
        {
            body.insert("reasoning_effort".to_string(), json!(effort));
        }
    }
    body
}

/// Python `_chat_usage`.
fn chat_usage(usage: Option<&Value>) -> Value {
    let usage = usage.and_then(Value::as_object);
    let number = |name: &str| {
        usage
            .and_then(|usage| usage.get(name))
            .and_then(Value::as_u64)
            .unwrap_or(0)
    };
    let mut translated = json!({
        "input_tokens": number("prompt_tokens"),
        "output_tokens": number("completion_tokens"),
        "total_tokens": number("total_tokens"),
    });
    if let Some(details) = usage
        .and_then(|usage| usage.get("prompt_tokens_details"))
        .and_then(Value::as_object)
    {
        translated["input_tokens_details"] = json!({
            "cached_tokens": details
                .get("cached_tokens")
                .and_then(Value::as_u64)
                .unwrap_or(0),
        });
    }
    if let Some(details) = usage
        .and_then(|usage| usage.get("completion_tokens_details"))
        .and_then(Value::as_object)
    {
        translated["output_tokens_details"] = json!({
            "reasoning_tokens": details
                .get("reasoning_tokens")
                .and_then(Value::as_u64)
                .unwrap_or(0),
        });
    }
    translated
}

/// Python `chat_to_responses_response`; `Err` is its `ValueError`.
/// One failed protocol translation: Python's `ValueError`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TranslationError;

pub fn chat_to_responses_response(
    payload: &Value,
    model: &str,
    created_at: i64,
    response_id: &str,
) -> Result<Value, TranslationError> {
    let Some(payload) = payload.as_object() else {
        return Err(TranslationError);
    };
    if payload
        .get("error")
        .is_some_and(|error| matches!(error, Value::String(_) | Value::Object(_)) && truthy(error))
    {
        return Err(TranslationError);
    }
    let choice = payload
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let message = choice.get("message").cloned().unwrap_or_default();
    let Some(message) = message.as_object() else {
        return Err(TranslationError);
    };
    let mut output = Vec::new();
    let text = responses_text(message.get("content"));
    if !text.is_empty() {
        output.push(json!({
            "type": "message",
            "id": format!("msg_{}", random_hex(12)),
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }));
    }
    if let Some(Value::Array(calls)) = message.get("tool_calls") {
        for call in calls {
            let Some(call) = call.as_object() else {
                continue;
            };
            let Some(function) = call.get("function").and_then(Value::as_object) else {
                continue;
            };
            let Some(name) = function
                .get("name")
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
            else {
                continue;
            };
            let arguments = arguments_or_empty(function.get("arguments"));
            let call_id = call
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
                .map(str::to_string)
                .unwrap_or_else(|| format!("call_{}", random_hex(12)));
            output.push(json!({
                "type": "function_call",
                "id": format!("fc_{}", random_hex(12)),
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "status": "completed",
            }));
        }
    }
    let finish_reason = choice.get("finish_reason").and_then(Value::as_str);
    let (status, incomplete) = match finish_reason {
        Some("length") => ("incomplete", Some(json!({"reason": "max_output_tokens"}))),
        Some("content_filter") => ("incomplete", Some(json!({"reason": "content_filter"}))),
        _ => ("completed", None),
    };
    let mut translated = json!({
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "usage": chat_usage(payload.get("usage")),
    });
    if let Some(incomplete) = incomplete {
        translated["incomplete_details"] = incomplete;
    }
    Ok(translated)
}

/// Python `responses_requires_messages`: an image-bearing tool result needs
/// the Anthropic Messages protocol.
pub fn responses_requires_messages(data: &Map<String, Value>) -> bool {
    let Some(Value::Array(items)) = data.get("input") else {
        return false;
    };
    for item in items {
        let Some(item) = item.as_object() else {
            continue;
        };
        if item.get("type").and_then(Value::as_str) != Some("function_call_output") {
            continue;
        }
        for part in responses_output_parts(item.get("output")) {
            if part.get("type").and_then(Value::as_str) == Some("input_image") {
                return true;
            }
        }
    }
    false
}

fn responses_output_parts(output: Option<&Value>) -> Vec<Value> {
    match output {
        Some(Value::Array(items)) => items.clone(),
        Some(Value::Object(output)) => match output.get("content") {
            Some(Value::Array(content)) => content.clone(),
            _ => vec![Value::Object(output.clone())],
        },
        Some(output) => vec![output.clone()],
        None => vec![Value::Null],
    }
}

fn responses_image_block(image_url: Option<&Value>) -> Option<Value> {
    let image_url = match image_url {
        Some(Value::Object(image)) => image.get("url"),
        image_url => image_url,
    };
    let image_url = image_url
        .and_then(Value::as_str)
        .filter(|url| !url.is_empty())?;
    if let Some(rest) = image_url.strip_prefix("data:") {
        let (header, data) = rest.split_once(',')?;
        let (media_type, encoding) = header.split_once(';')?;
        if !media_type.starts_with("image/") || encoding != "base64" || data.is_empty() {
            return None;
        }
        return Some(json!({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }));
    }
    if image_url.starts_with("http://") || image_url.starts_with("https://") {
        return Some(json!({
            "type": "image",
            "source": {"type": "url", "url": image_url},
        }));
    }
    None
}

fn responses_text_blocks(content: Option<&Value>) -> Vec<Value> {
    let Some(content) = content else {
        return Vec::new();
    };
    if let Value::String(text) = content {
        return vec![json!({"type": "text", "text": text})];
    }
    let Some(parts) = content.as_array() else {
        return Vec::new();
    };
    let mut blocks = Vec::new();
    for part in parts {
        match part {
            Value::String(text) => blocks.push(json!({"type": "text", "text": text})),
            Value::Object(part) => match part.get("type").and_then(Value::as_str) {
                Some("input_text" | "output_text" | "text") => {
                    if let Some(text) = part.get("text").and_then(Value::as_str) {
                        blocks.push(json!({"type": "text", "text": text}));
                    }
                }
                Some("input_image") => {
                    if let Some(block) = responses_image_block(part.get("image_url")) {
                        blocks.push(block);
                    }
                }
                _ => {}
            },
            _ => {}
        }
    }
    blocks
}

fn responses_tool_result_blocks(output: Option<&Value>) -> Vec<Value> {
    let mut blocks = Vec::new();
    for part in responses_output_parts(output) {
        match part {
            Value::String(text) => blocks.push(json!({"type": "text", "text": text})),
            Value::Object(part) => match part.get("type").and_then(Value::as_str) {
                Some("input_text" | "output_text" | "text") => {
                    if let Some(text) = part.get("text").and_then(Value::as_str) {
                        blocks.push(json!({"type": "text", "text": text}));
                    }
                }
                Some("input_image") => {
                    if let Some(block) = responses_image_block(part.get("image_url")) {
                        blocks.push(block);
                    }
                }
                _ => {}
            },
            _ => {}
        }
    }
    blocks
}

fn anthropic_tool_input(arguments: Option<&Value>) -> Value {
    match arguments {
        Some(Value::Object(arguments)) => Value::Object(arguments.clone()),
        Some(Value::String(arguments)) => serde_json::from_str::<Value>(arguments)
            .ok()
            .filter(Value::is_object)
            .unwrap_or_else(|| json!({})),
        _ => json!({}),
    }
}

/// Python `responses_to_messages_tool_choice`.
fn responses_to_messages_tool_choice(value: Option<&Value>) -> Option<Value> {
    match value {
        Some(Value::String(value)) => match value.as_str() {
            "auto" => Some(json!({"type": "auto"})),
            "none" => Some(json!({"type": "none"})),
            "required" => Some(json!({"type": "any"})),
            _ => None,
        },
        Some(Value::Object(value)) => match value.get("type").and_then(Value::as_str) {
            Some("function") => value
                .get("name")
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
                .map(|name| json!({"type": "tool", "name": name})),
            Some(kind @ ("auto" | "none" | "any")) => Some(json!({"type": kind})),
            _ => None,
        },
        _ => None,
    }
}

/// Python `responses_to_messages_request`.
pub fn responses_to_messages_request(data: &Map<String, Value>) -> Map<String, Value> {
    let mut system_parts = Vec::new();
    let mut messages: Vec<Value> = Vec::new();

    fn append_message(messages: &mut Vec<Value>, role: &str, blocks: Vec<Value>) {
        if blocks.is_empty() {
            return;
        }
        if let Some(Value::Object(last)) = messages.last_mut() {
            if last.get("role").and_then(Value::as_str) == Some(role) {
                let has_tool_result = role == "user"
                    && last
                        .get("content")
                        .and_then(Value::as_array)
                        .is_some_and(|content| {
                            content.iter().any(|block| {
                                block.get("type").and_then(Value::as_str) == Some("tool_result")
                            })
                        });
                if has_tool_result {
                    messages.push(json!({"role": role, "content": blocks}));
                } else if let Some(Value::Array(content)) = last.get_mut("content") {
                    content.extend(blocks);
                }
                return;
            }
        }
        messages.push(json!({"role": role, "content": blocks}));
    }

    if let Some(instructions) = data.get("instructions").and_then(Value::as_str) {
        if !instructions.is_empty() {
            system_parts.push(instructions.to_string());
        }
    }
    if let Some(Value::Array(items)) = data.get("input") {
        for item in items {
            match item {
                Value::String(text) => append_message(
                    &mut messages,
                    "user",
                    vec![json!({"type": "text", "text": text})],
                ),
                Value::Object(item) => {
                    let kind = item.get("type").and_then(Value::as_str);
                    if kind == Some("message") || (kind.is_none() && item.contains_key("role")) {
                        match item.get("role").and_then(Value::as_str) {
                            Some("system" | "developer") => {
                                let text = responses_text(item.get("content"));
                                if !text.is_empty() {
                                    system_parts.push(text);
                                }
                            }
                            Some(role @ ("user" | "assistant")) => {
                                append_message(
                                    &mut messages,
                                    role,
                                    responses_text_blocks(item.get("content")),
                                );
                            }
                            _ => {}
                        }
                    } else if kind == Some("function_call") {
                        let (Some(call_id), Some(name)) = (
                            item.get("call_id").and_then(Value::as_str),
                            item.get("name").and_then(Value::as_str),
                        ) else {
                            continue;
                        };
                        append_message(
                            &mut messages,
                            "assistant",
                            vec![json!({
                                "type": "tool_use",
                                "id": call_id,
                                "name": name,
                                "input": anthropic_tool_input(item.get("arguments")),
                            })],
                        );
                    } else if kind == Some("function_call_output") {
                        let Some(call_id) = item.get("call_id").and_then(Value::as_str) else {
                            continue;
                        };
                        let mut blocks = responses_tool_result_blocks(item.get("output"));
                        if blocks.is_empty() {
                            blocks.push(json!({"type": "text", "text": ""}));
                        }
                        append_message(
                            &mut messages,
                            "user",
                            vec![json!({
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "content": blocks,
                            })],
                        );
                    }
                }
                _ => {}
            }
        }
    }
    let mut body = Map::new();
    body.insert(
        "model".to_string(),
        data.get("model").cloned().unwrap_or(Value::Null),
    );
    body.insert("messages".to_string(), Value::Array(messages));
    body.insert(
        "max_tokens".to_string(),
        data.get("max_output_tokens")
            .filter(|value| positive_integer(value))
            .cloned()
            .unwrap_or(json!(32768)),
    );
    if !system_parts.is_empty() {
        body.insert("system".to_string(), json!(system_parts.join("\n\n")));
    }
    if data.get("stream") == Some(&Value::Bool(true)) {
        body.insert("stream".to_string(), Value::Bool(true));
    }
    for name in ["temperature", "top_p"] {
        if let Some(value) = data.get(name) {
            if value.is_number() && !value.is_boolean() {
                body.insert(name.to_string(), value.clone());
            }
        }
    }
    let mut tools = Vec::new();
    if let Some(Value::Array(definitions)) = data.get("tools") {
        for tool in definitions {
            let Some(tool) = tool.as_object() else {
                continue;
            };
            if tool.get("type").and_then(Value::as_str) != Some("function") {
                continue;
            }
            let Some(name) = tool
                .get("name")
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
            else {
                continue;
            };
            let mut definition = Map::new();
            definition.insert("name".to_string(), json!(name));
            if let Some(description) = tool.get("description").and_then(Value::as_str) {
                definition.insert("description".to_string(), json!(description));
            }
            if let Some(parameters) = tool.get("parameters").and_then(Value::as_object) {
                definition.insert(
                    "input_schema".to_string(),
                    Value::Object(parameters.clone()),
                );
            }
            tools.push(Value::Object(definition));
        }
    }
    if !tools.is_empty() {
        body.insert("tools".to_string(), Value::Array(tools));
    }
    if let Some(tool_choice) = responses_to_messages_tool_choice(data.get("tool_choice")) {
        body.insert("tool_choice".to_string(), tool_choice);
    }
    body
}

/// Python `_messages_usage`.
fn messages_usage(usage: Option<&Value>) -> Value {
    let usage = usage.and_then(Value::as_object);
    let number = |name: &str| {
        usage
            .and_then(|usage| usage.get(name))
            .and_then(Value::as_u64)
            .unwrap_or(0)
    };
    let input_tokens = number("input_tokens");
    let cache_read = number("cache_read_input_tokens");
    let cache_write = number("cache_creation_input_tokens");
    let output_tokens = number("output_tokens");
    let total_input = input_tokens + cache_read + cache_write;
    let mut translated = json!({
        "input_tokens": total_input,
        "output_tokens": output_tokens,
        "total_tokens": total_input + output_tokens,
    });
    if cache_read > 0 {
        translated["input_tokens_details"] = json!({"cached_tokens": cache_read});
    }
    translated
}

/// Python `messages_to_responses_response`.
pub fn messages_to_responses_response(
    payload: &Value,
    model: &str,
    created_at: i64,
    response_id: &str,
) -> Result<Value, TranslationError> {
    let Some(payload) = payload.as_object() else {
        return Err(TranslationError);
    };
    if payload
        .get("error")
        .is_some_and(|error| matches!(error, Value::String(_) | Value::Object(_)) && truthy(error))
    {
        return Err(TranslationError);
    }
    let mut output = Vec::new();
    let mut texts = Vec::new();
    if let Some(Value::Array(content)) = payload.get("content") {
        for block in content {
            let Some(block) = block.as_object() else {
                continue;
            };
            match block.get("type").and_then(Value::as_str) {
                Some("text") => {
                    if let Some(text) = block
                        .get("text")
                        .and_then(Value::as_str)
                        .filter(|text| !text.is_empty())
                    {
                        texts.push(text.to_string());
                    }
                }
                Some("tool_use") => {
                    let Some(name) = block
                        .get("name")
                        .and_then(Value::as_str)
                        .filter(|name| !name.is_empty())
                    else {
                        continue;
                    };
                    let arguments = arguments_or_empty(block.get("input"));
                    let call_id = block
                        .get("id")
                        .and_then(Value::as_str)
                        .filter(|id| !id.is_empty())
                        .map(str::to_string)
                        .unwrap_or_else(|| format!("call_{}", random_hex(12)));
                    output.push(json!({
                        "type": "function_call",
                        "id": format!("fc_{}", random_hex(12)),
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                        "status": "completed",
                    }));
                }
                _ => {}
            }
        }
    }
    if !texts.is_empty() {
        output.insert(
            0,
            json!({
                "type": "message",
                "id": format!("msg_{}", random_hex(12)),
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": texts.join("\n"), "annotations": []}],
            }),
        );
    }
    let stop_reason = payload.get("stop_reason").and_then(Value::as_str);
    let (status, incomplete) = match stop_reason {
        Some("max_tokens") => ("incomplete", Some(json!({"reason": "max_output_tokens"}))),
        Some("refusal" | "sensitive") => ("incomplete", Some(json!({"reason": "content_filter"}))),
        _ => ("completed", None),
    };
    let mut translated = json!({
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "usage": messages_usage(payload.get("usage")),
    });
    if let Some(incomplete) = incomplete {
        translated["incomplete_details"] = incomplete;
    }
    Ok(translated)
}

/// Python `json.dumps(value or {})` for a tool-call argument value.
fn arguments_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(arguments)) => arguments.clone(),
        Some(Value::Null) | None => "{}".to_string(),
        Some(value) => python_json_dumps(value),
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

/// Python `secrets.token_hex(n)`.
pub(crate) fn random_hex(bytes: usize) -> String {
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
    let mut output = String::new();
    for byte in digest.iter().take(bytes) {
        output.push_str(&format!("{byte:02x}"));
    }
    output
}

struct AdmissionGuard<'a> {
    state: &'a AppState,
    admission: Option<Admission>,
}

impl Drop for AdmissionGuard<'_> {
    fn drop(&mut self) {
        if let (Some(gate), Some(admission)) = (self.state.gate.as_deref(), self.admission.take()) {
            gate.unregister_inflight(&admission.key_id, &admission.ctx);
            gate.release(&admission.key_id);
        }
    }
}

struct ResponsesContext<'a> {
    state: &'a AppState,
    model: &'a str,
    started_at: Instant,
    deadline: Instant,
    client_stream: bool,
    input_estimate: u64,
    key_id: Option<&'a str>,
    request_id: Option<&'a str>,
    attribution: &'a str,
    origin: Option<String>,
}

/// Serve one Responses request.
pub fn handle_responses(
    state: &AppState,
    request: &Request,
    stream: &mut TcpStream,
    admission: Option<Admission>,
) -> ChatResult {
    let key_id = admission.as_ref().map(|value| value.key_id.clone());
    let request_id = key_id.as_ref().map(|_| random_hex(16));
    let attribution = if key_id.is_some() {
        "verified"
    } else {
        "absent"
    };
    let _guard = AdmissionGuard { state, admission };
    let body = match read_json_body(
        request,
        BodyPolicy::Share {
            max_request_bytes: state.config.share_max_request_bytes,
        },
    ) {
        Ok(Some(body)) => body,
        Ok(None) => {
            return ChatResult::Response(GatewayError::unsupported_responses_request().response())
        }
        Err(error) => return ChatResult::Response(error.response()),
    };
    if !valid_responses_request(&body) {
        return ChatResult::Response(GatewayError::unsupported_responses_request().response());
    }
    if responses_requires_messages(&body) {
        return handle_via_messages(
            state,
            &body,
            stream,
            key_id.as_deref(),
            request_id.as_deref(),
            attribution,
            request
                .headers
                .get("Origin")
                .filter(|origin| state.config.allowed_origins.contains(*origin))
                .map(str::to_string),
        );
    }
    let chat_body = responses_to_chat_request(&body);
    let client_stream = chat_body.get("stream") == Some(&Value::Bool(true));
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let upstream_stream = client_stream
        || crate::server::nonstream_aggregate_allowed(
            state,
            "responses",
            &model,
            key_id.as_deref(),
        );
    let mut chat_body = chat_body;
    if upstream_stream && !client_stream {
        chat_body.insert("stream".to_string(), Value::Bool(true));
        chat_body
            .entry("stream_options".to_string())
            .or_insert_with(|| json!({"include_usage": true}));
    }
    let started_at = Instant::now();
    let deadline = started_at + state.config.request_total;
    let origin = request
        .headers
        .get("Origin")
        .filter(|origin| state.config.allowed_origins.contains(*origin))
        .map(str::to_string);
    let context = ResponsesContext {
        state,
        model: &model,
        started_at,
        deadline,
        client_stream,
        input_estimate: message_tokens(chat_body.get("messages")),
        key_id: key_id.as_deref(),
        request_id: request_id.as_deref(),
        attribution,
        origin,
    };
    if client_stream {
        forward_stream(&context, stream, chat_body)
    } else if upstream_stream {
        forward_aggregate(&context, stream, chat_body, false)
    } else {
        forward_json(&context, chat_body, false)
    }
}

fn handle_via_messages(
    state: &AppState,
    body: &Map<String, Value>,
    stream: &mut TcpStream,
    key_id: Option<&str>,
    request_id: Option<&str>,
    attribution: &str,
    origin: Option<String>,
) -> ChatResult {
    let messages_body = responses_to_messages_request(body);
    let client_stream = messages_body.get("stream") == Some(&Value::Bool(true));
    let model = body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let upstream_stream = client_stream
        || crate::server::nonstream_aggregate_allowed(state, "responses", &model, key_id);
    let mut messages_body = messages_body;
    if upstream_stream && !client_stream {
        messages_body.insert("stream".to_string(), Value::Bool(true));
    }
    let started_at = Instant::now();
    let deadline = started_at + state.config.request_total;
    let context = ResponsesContext {
        state,
        model: &model,
        started_at,
        deadline,
        client_stream,
        input_estimate: message_tokens(messages_body.get("messages")),
        key_id,
        request_id,
        attribution,
        origin,
    };
    if client_stream {
        forward_messages_stream(&context, stream, messages_body)
    } else if upstream_stream {
        forward_aggregate(&context, stream, messages_body, true)
    } else {
        forward_json(&context, messages_body, true)
    }
}

fn record_usage(
    context: &ResponsesContext<'_>,
    status: &str,
    usage: Option<&Value>,
    output_estimate: u64,
) {
    context.state.ledger.record(UsageRecord {
        model: context.model,
        stream: context.client_stream,
        started_at: context.started_at,
        usage,
        input_estimate: context.input_estimate,
        output_estimate,
        status,
        protocol: Some("responses"),
        key_id: context.key_id,
        request_id: context.request_id,
        attribution: context.attribution,
    });
}

fn record_translated_usage(
    context: &ResponsesContext<'_>,
    translated: &Value,
    usage: Option<&Value>,
    status: &str,
) {
    let output_chars = output_text_chars(translated);
    let output_estimate = if status == "ok" && output_chars > 0 {
        (output_chars / 4).max(1)
    } else {
        output_chars / 4
    };
    record_usage(context, status, usage, output_estimate);
}

fn output_text_chars(translated: &Value) -> u64 {
    let mut chars = 0u64;
    if let Some(output) = translated.get("output").and_then(Value::as_array) {
        for item in output {
            if let Some(content) = item.get("content").and_then(Value::as_array) {
                for part in content {
                    if let Some(text) = part.get("text").and_then(Value::as_str) {
                        chars += text.chars().count() as u64;
                    }
                }
            }
        }
    }
    chars
}

fn forward_json(
    context: &ResponsesContext<'_>,
    request_body: Map<String, Value>,
    via_messages: bool,
) -> ChatResult {
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    let upstream_path = if via_messages {
        "/api/v1/messages"
    } else {
        "/api/chat/completions"
    };
    let extra_headers = if via_messages {
        vec![("Anthropic-Version".to_string(), "2023-06-01".to_string())]
    } else {
        Vec::new()
    };
    loop {
        if Instant::now() >= context.deadline {
            record_usage(context, "request_deadline_exceeded", None, 0);
            return ChatResult::Response(deadline_response());
        }
        if physical_attempts >= context.state.config.max_physical_attempts {
            record_usage(context, "upstream_attempt_budget_exhausted", None, 0);
            return ChatResult::Response(physical_budget_response());
        }
        physical_attempts += 1;
        let attempt_started = Instant::now();
        let mut response = match post_compat(
            &context.state.config,
            upstream_path,
            &serde_json::to_vec(&Value::Object(request_body.clone())).unwrap_or_default(),
            false,
            context.deadline,
            &extra_headers,
        ) {
            Ok(response) => response,
            Err(failure) => {
                let elapsed = attempt_started.elapsed();
                let slow = elapsed >= context.state.config.slow_retry_threshold;
                if retry_available(context, fast_retries, slow_retries, physical_attempts, slow) {
                    fast_retries += 1;
                    if slow {
                        slow_retries += 1;
                    }
                    if sleep_with_deadline(
                        retry_delay(context, fast_retries, slow),
                        context.deadline,
                    ) {
                        continue;
                    }
                }
                record_usage(context, failure.name(), None, 0);
                return ChatResult::Response(upstream_failure_error(failure).response());
            }
        };
        if response.status != 200 {
            let status = response.status;
            let elapsed = attempt_started.elapsed();
            let slow = elapsed >= context.state.config.slow_retry_threshold;
            if retryable_status(status)
                && retry_available(context, fast_retries, slow_retries, physical_attempts, slow)
            {
                fast_retries += 1;
                if slow {
                    slow_retries += 1;
                }
                let delay = retry_delay(context, fast_retries, slow);
                drop(response);
                if !sleep_with_deadline(delay, context.deadline) {
                    record_usage(context, "request_deadline_exceeded", None, 0);
                    return ChatResult::Response(deadline_response_with_last_status(status));
                }
                continue;
            }
            record_usage(context, &upstream_status_name(status), None, 0);
            return ChatResult::Response(upstream_status_error(status, response));
        }
        let raw = match response.body.read_all() {
            Ok(raw) => raw,
            Err(failure) => {
                record_usage(context, failure.name(), None, 0);
                return ChatResult::Response(upstream_failure_error(failure).response());
            }
        };
        let payload: Value = match serde_json::from_slice(&raw) {
            Ok(payload) => payload,
            Err(_) => {
                record_usage(context, "unexpected_upstream_response", None, 0);
                return ChatResult::Response(
                    GatewayError::new(502, "unexpected_upstream_response", "chat2api_error")
                        .response(),
                );
            }
        };
        let translated = if via_messages {
            messages_to_responses_response(
                &payload,
                context.model,
                unix_seconds(),
                &format!("resp_{}", random_hex(12)),
            )
        } else {
            chat_to_responses_response(
                &payload,
                context.model,
                unix_seconds(),
                &format!("resp_{}", random_hex(12)),
            )
        };
        let Ok(translated) = translated else {
            record_usage(context, "unexpected_upstream_response", None, 0);
            return ChatResult::Response(
                GatewayError::new(502, "unexpected_upstream_response", "chat2api_error").response(),
            );
        };
        let ledger_usage = if via_messages {
            translated.get("usage")
        } else {
            payload.get("usage")
        };
        record_translated_usage(context, &translated, ledger_usage, "ok");
        return ChatResult::Response(Response::json(200, &translated));
    }
}

fn forward_aggregate(
    context: &ResponsesContext<'_>,
    stream: &mut TcpStream,
    request_body: Map<String, Value>,
    via_messages: bool,
) -> ChatResult {
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    let upstream_path = if via_messages {
        "/api/v1/messages"
    } else {
        "/api/chat/completions"
    };
    let extra_headers = if via_messages {
        vec![("Anthropic-Version".to_string(), "2023-06-01".to_string())]
    } else {
        Vec::new()
    };
    loop {
        if Instant::now() >= context.deadline {
            record_usage(context, "request_deadline_exceeded", None, 0);
            return ChatResult::Response(deadline_response());
        }
        if physical_attempts >= context.state.config.max_physical_attempts {
            record_usage(context, "upstream_attempt_budget_exhausted", None, 0);
            return ChatResult::Response(physical_budget_response());
        }
        physical_attempts += 1;
        let attempt_started = Instant::now();
        let response = match post_compat(
            &context.state.config,
            upstream_path,
            &serde_json::to_vec(&Value::Object(request_body.clone())).unwrap_or_default(),
            true,
            context.deadline,
            &extra_headers,
        ) {
            Ok(response) => response,
            Err(failure) => {
                let elapsed = attempt_started.elapsed();
                let slow = elapsed >= context.state.config.slow_retry_threshold;
                if retry_available(context, fast_retries, slow_retries, physical_attempts, slow) {
                    fast_retries += 1;
                    if slow {
                        slow_retries += 1;
                    }
                    if sleep_with_deadline(
                        retry_delay(context, fast_retries, slow),
                        context.deadline,
                    ) {
                        continue;
                    }
                }
                record_usage(context, failure.name(), None, 0);
                return ChatResult::Response(upstream_failure_error(failure).response());
            }
        };
        if response.status != 200 {
            let status = response.status;
            let elapsed = attempt_started.elapsed();
            let slow = elapsed >= context.state.config.slow_retry_threshold;
            if retryable_status(status)
                && retry_available(context, fast_retries, slow_retries, physical_attempts, slow)
            {
                fast_retries += 1;
                if slow {
                    slow_retries += 1;
                }
                let delay = retry_delay(context, fast_retries, slow);
                drop(response);
                if !sleep_with_deadline(delay, context.deadline) {
                    record_usage(context, "request_deadline_exceeded", None, 0);
                    return ChatResult::Response(deadline_response_with_last_status(status));
                }
                continue;
            }
            record_usage(context, &upstream_status_name(status), None, 0);
            return ChatResult::Response(upstream_status_error(status, response));
        }
        let mut aggregator = ChatCompletionAggregator::new(Some(context.model));
        let watcher = ClientWatcher::start(stream);
        let reader = StreamReader::start(response.body);
        let mut data = None;
        let mut status = "ok".to_string();
        let mut exception = None;
        loop {
            if watcher.as_ref().is_some_and(ClientWatcher::disconnected) {
                status = "client_disconnected".to_string();
                break;
            }
            match reader.next_item(Duration::from_millis(50)) {
                NextItem::Timeout => continue,
                NextItem::End => {
                    match aggregator.finish() {
                        Ok(value) => {
                            data = Some(value);
                        }
                        Err(failure) => {
                            status = aggregate_status(&failure).to_string();
                            if let AggregateFailure::ExplicitError(message) = failure {
                                exception = Some(message);
                            }
                        }
                    }
                    break;
                }
                NextItem::Error(error) => {
                    status = if error.timed_out {
                        "upstream_timeout".to_string()
                    } else {
                        "upstream_read_error".to_string()
                    };
                    exception = Some(error.name.to_string());
                    break;
                }
                NextItem::Line(line) => {
                    if let Err(failure) = aggregator.feed_line(&line_text(&line)) {
                        status = aggregate_status(&failure).to_string();
                        if let AggregateFailure::ExplicitError(message) = failure {
                            exception = Some(message);
                        }
                        break;
                    }
                }
            }
        }
        reader.cancel();
        drop(reader);
        if let Some(data) = data {
            let translated = if via_messages {
                messages_to_responses_response(
                    &data,
                    context.model,
                    unix_seconds(),
                    &format!("resp_{}", random_hex(12)),
                )
            } else {
                chat_to_responses_response(
                    &data,
                    context.model,
                    unix_seconds(),
                    &format!("resp_{}", random_hex(12)),
                )
            };
            let Ok(translated) = translated else {
                record_usage(
                    context,
                    "unexpected_upstream_response",
                    aggregator.usage(),
                    aggregator.output_estimate(),
                );
                return ChatResult::Response(
                    GatewayError::new(502, "unexpected_upstream_response", "chat2api_error")
                        .response(),
                );
            };
            let ledger_usage = if via_messages {
                translated.get("usage")
            } else {
                data.get("usage")
            };
            record_translated_usage(context, &translated, ledger_usage, "ok");
            return ChatResult::Response(Response::json(200, &translated));
        }
        if status == "client_disconnected" {
            record_usage(
                context,
                "client_disconnected",
                aggregator.usage(),
                aggregator.output_estimate(),
            );
            return ChatResult::Written { close: true };
        }
        record_usage(
            context,
            &format!("aggregate_{status}"),
            aggregator.usage(),
            aggregator.output_estimate(),
        );
        return ChatResult::Response(aggregate_failure_response(&status, exception.as_deref()));
    }
}

fn aggregate_status(failure: &AggregateFailure) -> &'static str {
    match failure {
        AggregateFailure::BadFrame => "upstream_bad_frame",
        AggregateFailure::ExplicitError(_) => "upstream_explicit_error",
        AggregateFailure::MissingTerminal => "upstream_missing_terminal",
    }
}

fn aggregate_failure_response(status: &str, exception: Option<&str>) -> Response {
    match status {
        "upstream_timeout" => GatewayError::upstream_timeout()
            .with_detail(json!({"reason": "upstream_timeout"}))
            .response(),
        "upstream_explicit_error" => {
            GatewayError::new(502, "Upstream request failed", "upstream_explicit_error")
                .with_detail(json!({
                    "reason": "upstream_explicit_error",
                    "upstream_message": exception,
                }))
                .response()
        }
        "upstream_bad_frame" => GatewayError::new(
            502,
            "Upstream returned an invalid SSE frame",
            "upstream_stream_error",
        )
        .with_detail(json!({"reason": "upstream_bad_frame"}))
        .response(),
        "upstream_missing_terminal" => GatewayError::new(
            502,
            "Upstream stream ended without a complete terminal",
            "upstream_stream_error",
        )
        .with_detail(json!({"reason": "upstream_missing_terminal"}))
        .response(),
        _ => GatewayError::new(502, "Upstream stream failed", "upstream_stream_error")
            .with_detail(json!({
                "reason": "upstream_read_error",
                "exception": exception,
            }))
            .response(),
    }
}

fn forward_stream(
    context: &ResponsesContext<'_>,
    stream: &mut TcpStream,
    request_body: Map<String, Value>,
) -> ChatResult {
    forward_stream_with(
        context,
        stream,
        request_body,
        "/api/chat/completions",
        Vec::new(),
        false,
    )
}

fn forward_messages_stream(
    context: &ResponsesContext<'_>,
    stream: &mut TcpStream,
    request_body: Map<String, Value>,
) -> ChatResult {
    forward_stream_with(
        context,
        stream,
        request_body,
        "/api/v1/messages",
        vec![("Anthropic-Version".to_string(), "2023-06-01".to_string())],
        true,
    )
}

fn forward_stream_with(
    context: &ResponsesContext<'_>,
    stream: &mut TcpStream,
    request_body: Map<String, Value>,
    upstream_path: &str,
    extra_headers: Vec<(String, String)>,
    via_messages: bool,
) -> ChatResult {
    let mut fast_retries = 0u32;
    let mut slow_retries = 0u32;
    let mut physical_attempts = 0u32;
    loop {
        if Instant::now() >= context.deadline {
            record_usage(context, "request_deadline_exceeded", None, 0);
            return ChatResult::Response(deadline_response());
        }
        if physical_attempts >= context.state.config.max_physical_attempts {
            record_usage(context, "upstream_attempt_budget_exhausted", None, 0);
            return ChatResult::Response(physical_budget_response());
        }
        physical_attempts += 1;
        let attempt_started = Instant::now();
        let response = match post_compat(
            &context.state.config,
            upstream_path,
            &serde_json::to_vec(&Value::Object(request_body.clone())).unwrap_or_default(),
            true,
            context.deadline,
            &extra_headers,
        ) {
            Ok(response) => response,
            Err(failure) => {
                let elapsed = attempt_started.elapsed();
                let slow = elapsed >= context.state.config.slow_retry_threshold;
                if retry_available(context, fast_retries, slow_retries, physical_attempts, slow) {
                    fast_retries += 1;
                    if slow {
                        slow_retries += 1;
                    }
                    if sleep_with_deadline(
                        retry_delay(context, fast_retries, slow),
                        context.deadline,
                    ) {
                        continue;
                    }
                }
                record_usage(context, failure.name(), None, 0);
                return ChatResult::Response(upstream_failure_error(failure).response());
            }
        };
        if response.status != 200 {
            let status = response.status;
            let elapsed = attempt_started.elapsed();
            let slow = elapsed >= context.state.config.slow_retry_threshold;
            if retryable_status(status)
                && retry_available(context, fast_retries, slow_retries, physical_attempts, slow)
            {
                fast_retries += 1;
                if slow {
                    slow_retries += 1;
                }
                let delay = retry_delay(context, fast_retries, slow);
                drop(response);
                if !sleep_with_deadline(delay, context.deadline) {
                    record_usage(context, "request_deadline_exceeded", None, 0);
                    return ChatResult::Response(deadline_response_with_last_status(status));
                }
                continue;
            }
            record_usage(context, &upstream_status_name(status), None, 0);
            return ChatResult::Response(upstream_status_error(status, response));
        }
        let mut headers = vec![
            ("Content-Type".to_string(), "text/event-stream".to_string()),
            ("Cache-Control".to_string(), "no-store".to_string()),
            ("Connection".to_string(), "close".to_string()),
        ];
        if let Some(origin) = &context.origin {
            headers.push(("Access-Control-Allow-Origin".to_string(), origin.clone()));
        }
        if write_response_head(stream, 200, &headers).is_err() {
            return ChatResult::Written { close: true };
        }
        let created_at = unix_seconds();
        let response_id = format!("resp_{}", random_hex(12));
        let created = json!({
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": created_at,
                "status": "in_progress",
                "model": context.model,
                "output": [],
            },
        });
        if write_sse_event(stream, "response.created", &created).is_err() {
            record_usage(context, "client_disconnected", None, 0);
            return ChatResult::Written { close: true };
        }
        let mut translator = ResponsesStreamTranslator::new(
            context.model.to_string(),
            response_id.clone(),
            created_at,
            via_messages,
        );
        let watcher = ClientWatcher::start(stream);
        let reader = StreamReader::start(response.body);
        let mut output_chars = 0u64;
        let mut status = "ok".to_string();
        let mut usage = Value::Object(Map::new());
        let mut terminal = false;
        let mut client_disconnected = false;
        loop {
            if watcher.as_ref().is_some_and(ClientWatcher::disconnected) {
                client_disconnected = true;
                break;
            }
            match reader.next_item(Duration::from_millis(50)) {
                NextItem::Timeout => continue,
                NextItem::End => {
                    for (name, payload) in translator.finish() {
                        update_stream_status(
                            &name,
                            &payload,
                            &mut status,
                            &mut usage,
                            &mut output_chars,
                            &mut terminal,
                        );
                        if write_sse_event(stream, &name, &payload).is_err() {
                            client_disconnected = true;
                            break;
                        }
                    }
                    break;
                }
                NextItem::Error(error) => {
                    let failed = json!({
                        "type": "response.failed",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created_at,
                            "status": "failed",
                            "model": context.model,
                            "output": [],
                            "error": {
                                "code": "upstream_stream_error",
                                "message": "upstream stream failed",
                            },
                        },
                    });
                    status = "upstream_stream_error".to_string();
                    let _ = write_sse_event(stream, "response.failed", &failed);
                    let _ = error;
                    break;
                }
                NextItem::Line(line) => {
                    for (name, payload) in translator.feed_line(&line_text(&line)) {
                        update_stream_status(
                            &name,
                            &payload,
                            &mut status,
                            &mut usage,
                            &mut output_chars,
                            &mut terminal,
                        );
                        if write_sse_event(stream, &name, &payload).is_err() {
                            client_disconnected = true;
                            break;
                        }
                    }
                    if client_disconnected || terminal {
                        break;
                    }
                }
            }
        }
        reader.cancel();
        drop(reader);
        if client_disconnected {
            record_stream_usage(context, &status, &usage, output_chars);
            return ChatResult::Written { close: true };
        }
        record_stream_usage(context, &status, &usage, output_chars);
        return ChatResult::Written { close: true };
    }
}

fn record_stream_usage(
    context: &ResponsesContext<'_>,
    status: &str,
    usage: &Value,
    output_chars: u64,
) {
    let output_estimate = if status == "ok" {
        (output_chars / 4).max(1)
    } else {
        output_chars / 4
    };
    record_usage(
        context,
        status,
        Some(usage).filter(|usage| usage.as_object().is_some_and(|usage| !usage.is_empty())),
        output_estimate,
    );
}

fn update_stream_status(
    name: &str,
    payload: &Value,
    status: &mut String,
    usage: &mut Value,
    output_chars: &mut u64,
    terminal: &mut bool,
) {
    match name {
        "response.output_text.delta" | "response.function_call_arguments.delta" => {
            if let Some(delta) = payload.get("delta").and_then(Value::as_str) {
                *output_chars += delta.chars().count() as u64;
            }
        }
        "response.completed" | "response.incomplete" => {
            if let Some(response) = payload.get("response") {
                if let Some(stream_usage) = response.get("usage") {
                    *usage = stream_usage.clone();
                }
            }
            *status = if name == "response.completed" {
                "ok".to_string()
            } else {
                "incomplete".to_string()
            };
            *terminal = true;
        }
        "response.failed" => {
            let code = payload
                .get("response")
                .and_then(|response| response.get("error"))
                .and_then(|error| error.get("code"))
                .and_then(Value::as_str);
            *status = code.unwrap_or("upstream_error").to_string();
            *terminal = true;
        }
        _ => {}
    }
}

fn write_sse_event(stream: &mut TcpStream, name: &str, payload: &Value) -> std::io::Result<()> {
    let mut event = format!("event: {name}\ndata: ");
    event.push_str(&serde_json::to_string(payload).unwrap_or_default());
    event.push_str("\n\n");
    write_chunk(stream, event.as_bytes())
}

fn retry_available(
    context: &ResponsesContext<'_>,
    fast_retries: u32,
    slow_retries: u32,
    physical_attempts: u32,
    slow: bool,
) -> bool {
    fast_retries < context.state.config.max_fast_retries
        && (!slow || slow_retries < context.state.config.max_slow_retries)
        && physical_attempts < context.state.config.max_physical_attempts
        && Instant::now() < context.deadline
}

fn retry_delay(context: &ResponsesContext<'_>, retry_number: u32, slow: bool) -> Duration {
    if slow {
        return context.state.config.retry_delay_base * 2;
    }
    let exponent = retry_number.saturating_sub(1).min(4);
    (context.state.config.retry_delay_base * (1u32 << exponent)).min(Duration::from_secs(2))
}

fn sleep_with_deadline(delay: Duration, deadline: Instant) -> bool {
    if delay.is_zero() {
        return Instant::now() < deadline;
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return false;
    }
    thread::sleep(delay.min(remaining));
    Instant::now() < deadline
}

fn retryable_status(status: u16) -> bool {
    matches!(status, 408 | 429 | 500 | 502 | 503 | 504)
}

fn deadline_response() -> Response {
    GatewayError::request_deadline_exceeded(None).response()
}

fn deadline_response_with_last_status(status: u16) -> Response {
    GatewayError::request_deadline_exceeded(Some(status)).response()
}

fn upstream_failure_error(failure: UpstreamFailure) -> GatewayError {
    match failure {
        UpstreamFailure::Timeout => GatewayError::upstream_timeout_with_reason(failure.name()),
        UpstreamFailure::ConnectionError | UpstreamFailure::Protocol => {
            GatewayError::upstream_transport_failure(failure.name())
        }
    }
}

fn physical_budget_response() -> Response {
    GatewayError::new(
        502,
        "Upstream request could not start another attempt",
        "upstream_attempt_budget_exhausted",
    )
    .response()
}

fn upstream_status_name(status: u16) -> String {
    if status == 401 {
        "reauthentication_required".to_string()
    } else {
        format!("HTTP{status}")
    }
}

fn upstream_status_error(status: u16, mut response: UpstreamResponse) -> Response {
    let retry_after = response.headers.get("retry-after").cloned();
    if status == 401 {
        let mut error = GatewayError::reauthentication_required(Some("upstream_rejected"));
        if let Some(retry_after) = retry_after.filter(|value| !value.is_empty()) {
            error = error.with_retry_after(retry_after);
        }
        return error.response();
    }
    let text = response
        .body
        .read_prefix(2048)
        .ok()
        .map(|raw| String::from_utf8_lossy(&raw).to_string())
        .unwrap_or_default();
    GatewayError::upstream_status_error(status, Some(&text), retry_after.as_deref()).response()
}

fn unix_seconds() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}

// The stream translator is added by acceptance 2.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StreamMode {
    Chat,
    Messages,
}

struct ToolSlot {
    id: String,
    call_id: String,
    name: String,
    arguments: String,
    sent: usize,
    added: bool,
    output_index: Option<usize>,
}

enum BlockSlot {
    Text {
        id: String,
        output_index: usize,
        text: String,
        done: bool,
    },
    Tool {
        id: String,
        call_id: String,
        name: String,
        arguments: String,
        output_index: usize,
        done: bool,
    },
}

/// Translates one upstream SSE stream into Responses events.
pub struct ResponsesStreamTranslator {
    model: String,
    response_id: String,
    created_at: i64,
    mode: StreamMode,
    message_id: String,
    text: String,
    message_added: bool,
    message_index: Option<usize>,
    next_index: usize,
    tool_calls: BTreeMap<i64, ToolSlot>,
    blocks: BTreeMap<i64, Option<BlockSlot>>,
    output_items: BTreeMap<usize, Value>,
    usage: Value,
    done_seen: bool,
    finish_reason: Option<String>,
    stop_reason: Option<String>,
    saw_message_stop: bool,
    event_name: Option<String>,
    terminal: bool,
}

impl ResponsesStreamTranslator {
    pub fn new(model: String, response_id: String, created_at: i64, via_messages: bool) -> Self {
        Self {
            model,
            response_id,
            created_at,
            mode: if via_messages {
                StreamMode::Messages
            } else {
                StreamMode::Chat
            },
            message_id: format!("msg_{}", random_hex(12)),
            text: String::new(),
            message_added: false,
            message_index: None,
            next_index: 0,
            tool_calls: BTreeMap::new(),
            blocks: BTreeMap::new(),
            output_items: BTreeMap::new(),
            usage: Value::Object(Map::new()),
            done_seen: false,
            finish_reason: None,
            stop_reason: None,
            saw_message_stop: false,
            event_name: None,
            terminal: false,
        }
    }

    /// Feed one SSE line (without its trailing newline).
    pub fn feed_line(&mut self, line: &str) -> Vec<(String, Value)> {
        if self.terminal {
            return Vec::new();
        }
        let line = line.trim();
        if line.is_empty() {
            return Vec::new();
        }
        match self.mode {
            StreamMode::Chat => self.feed_chat_line(line),
            StreamMode::Messages => self.feed_messages_line(line),
        }
    }

    /// Finish the stream at EOF.
    pub fn finish(&mut self) -> Vec<(String, Value)> {
        if self.terminal {
            return Vec::new();
        }
        match self.mode {
            StreamMode::Chat => self.finish_chat(),
            StreamMode::Messages => self.finish_messages(),
        }
    }

    fn failed(&mut self, code: &str, message: &str) -> Vec<(String, Value)> {
        self.terminal = true;
        vec![(
            "response.failed".to_string(),
            json!({
                "type": "response.failed",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "created_at": self.created_at,
                    "status": "failed",
                    "model": self.model,
                    "output": [],
                    "error": {"code": code, "message": message},
                },
            }),
        )]
    }

    fn feed_chat_line(&mut self, line: &str) -> Vec<(String, Value)> {
        let Some(value) = line.strip_prefix("data:") else {
            return Vec::new();
        };
        let value = value.trim();
        if value == "[DONE]" {
            self.done_seen = true;
            return self.finish_chat();
        }
        let Ok(chunk) = serde_json::from_str::<Value>(value) else {
            return Vec::new();
        };
        let Some(chunk) = chunk.as_object() else {
            return Vec::new();
        };
        if chunk.get("error").is_some_and(truthy) {
            return self.failed("upstream_error", "upstream stream failed");
        }
        if let Some(usage) = chunk.get("usage").and_then(Value::as_object) {
            self.usage = Value::Object(usage.clone());
        }
        let mut events = Vec::new();
        if let Some(choices) = chunk.get("choices").and_then(Value::as_array) {
            for choice in choices {
                let Some(choice) = choice.as_object() else {
                    continue;
                };
                if let Some(finish) = choice.get("finish_reason").and_then(Value::as_str) {
                    self.finish_reason = Some(finish.to_string());
                }
                let Some(delta) = choice.get("delta").and_then(Value::as_object) else {
                    continue;
                };
                let piece = responses_text(delta.get("content"));
                if !piece.is_empty() {
                    if !self.message_added {
                        self.message_added = true;
                        self.message_index = Some(self.next_index);
                        self.next_index += 1;
                        events.push((
                            "response.output_item.added".to_string(),
                            json!({
                                "type": "response.output_item.added",
                                "output_index": self.message_index,
                                "item": {
                                    "type": "message",
                                    "id": self.message_id,
                                    "status": "in_progress",
                                    "role": "assistant",
                                    "content": [],
                                },
                            }),
                        ));
                    }
                    self.text.push_str(&piece);
                    events.push((
                        "response.output_text.delta".to_string(),
                        json!({
                            "type": "response.output_text.delta",
                            "item_id": self.message_id,
                            "output_index": self.message_index,
                            "content_index": 0,
                            "delta": piece,
                        }),
                    ));
                }
                let Some(tool_calls) = delta.get("tool_calls").and_then(Value::as_array) else {
                    continue;
                };
                for call in tool_calls {
                    let Some(call) = call.as_object() else {
                        continue;
                    };
                    let index = call
                        .get("index")
                        .and_then(Value::as_i64)
                        .filter(|index| *index >= 0)
                        .unwrap_or(0);
                    let slot = self.tool_calls.entry(index).or_insert_with(|| ToolSlot {
                        id: format!("fc_{}", random_hex(12)),
                        call_id: String::new(),
                        name: String::new(),
                        arguments: String::new(),
                        sent: 0,
                        added: false,
                        output_index: None,
                    });
                    if let Some(call_id) = call.get("id").and_then(Value::as_str) {
                        if !call_id.is_empty() {
                            slot.call_id = call_id.to_string();
                        }
                    }
                    let Some(function) = call.get("function").and_then(Value::as_object) else {
                        continue;
                    };
                    if let Some(name) = function
                        .get("name")
                        .and_then(Value::as_str)
                        .filter(|name| !name.is_empty())
                    {
                        slot.name = name.to_string();
                    }
                    let Some(arguments) = function.get("arguments").and_then(Value::as_str) else {
                        continue;
                    };
                    if arguments.is_empty() {
                        continue;
                    }
                    slot.arguments.push_str(arguments);
                    if slot.name.is_empty() {
                        continue;
                    }
                    if !slot.added {
                        slot.added = true;
                        slot.output_index = Some(self.next_index);
                        self.next_index += 1;
                        events.push((
                            "response.output_item.added".to_string(),
                            json!({
                                "type": "response.output_item.added",
                                "output_index": slot.output_index,
                                "item": {
                                    "type": "function_call",
                                    "id": slot.id,
                                    "call_id": if slot.call_id.is_empty() {
                                        slot.id.clone()
                                    } else {
                                        slot.call_id.clone()
                                    },
                                    "name": slot.name,
                                    "arguments": "",
                                    "status": "in_progress",
                                },
                            }),
                        ));
                    }
                    let pending = slot.arguments[slot.sent..].to_string();
                    slot.sent = slot.arguments.len();
                    if !pending.is_empty() {
                        events.push((
                            "response.function_call_arguments.delta".to_string(),
                            json!({
                                "type": "response.function_call_arguments.delta",
                                "item_id": slot.id,
                                "output_index": slot.output_index,
                                "delta": pending,
                            }),
                        ));
                    }
                }
            }
        }
        events
    }

    fn finish_chat(&mut self) -> Vec<(String, Value)> {
        let mut events = Vec::new();
        if !self.done_seen {
            events.extend(self.failed(
                "upstream_eof_without_done",
                "upstream stream ended without [DONE]",
            ));
            return events;
        }
        if self.message_added {
            let Some(index) = self.message_index else {
                return events;
            };
            let item = json!({
                "type": "message",
                "id": self.message_id,
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self.text,
                              "annotations": []}],
            });
            self.output_items.insert(index, item.clone());
            events.push((
                "response.output_item.done".to_string(),
                json!({
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": item,
                }),
            ));
        }
        let indices: Vec<i64> = self.tool_calls.keys().copied().collect();
        for index in indices {
            let slot = self.tool_calls.get_mut(&index).expect("tool slot");
            if slot.name.is_empty() {
                continue;
            }
            if !slot.added {
                slot.added = true;
                slot.output_index = Some(self.next_index);
                self.next_index += 1;
                events.push((
                    "response.output_item.added".to_string(),
                    json!({
                        "type": "response.output_item.added",
                        "output_index": slot.output_index,
                        "item": {
                            "type": "function_call",
                            "id": slot.id,
                            "call_id": if slot.call_id.is_empty() {
                                slot.id.clone()
                            } else {
                                slot.call_id.clone()
                            },
                            "name": slot.name,
                            "arguments": "",
                            "status": "in_progress",
                        },
                    }),
                ));
            }
            let item = json!({
                "type": "function_call",
                "id": slot.id,
                "call_id": if slot.call_id.is_empty() {
                    slot.id.clone()
                } else {
                    slot.call_id.clone()
                },
                "name": slot.name,
                "arguments": slot.arguments,
                "status": "completed",
            });
            if let Some(output_index) = slot.output_index {
                self.output_items.insert(output_index, item.clone());
                events.push((
                    "response.output_item.done".to_string(),
                    json!({
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    }),
                ));
            }
        }
        let (status, incomplete) = match self.finish_reason.as_deref() {
            Some("length") => ("incomplete", Some(json!({"reason": "max_output_tokens"}))),
            Some("content_filter") => ("incomplete", Some(json!({"reason": "content_filter"}))),
            _ => ("completed", None),
        };
        let ordered: Vec<Value> = self.output_items.values().cloned().collect();
        let name = if incomplete.is_some() {
            "response.incomplete"
        } else {
            "response.completed"
        };
        let mut response = json!({
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": ordered,
            "usage": chat_usage(Some(&self.usage)),
        });
        if let Some(incomplete) = incomplete {
            response["incomplete_details"] = incomplete;
        }
        self.terminal = true;
        events.push((
            name.to_string(),
            json!({"type": name, "response": response}),
        ));
        events
    }

    fn feed_messages_line(&mut self, line: &str) -> Vec<(String, Value)> {
        if let Some(name) = line.strip_prefix("event:") {
            self.event_name = Some(name.trim().to_string());
            return Vec::new();
        }
        let Some(value) = line.strip_prefix("data:") else {
            return Vec::new();
        };
        let value = value.trim();
        if value == "[DONE]" {
            return Vec::new();
        }
        let Ok(event) = serde_json::from_str::<Value>(value) else {
            return Vec::new();
        };
        let Some(event) = event.as_object() else {
            return Vec::new();
        };
        let kind = event
            .get("type")
            .and_then(Value::as_str)
            .or(self.event_name.as_deref())
            .map(str::to_string);
        self.event_name = None;
        let Some(kind) = kind else {
            return Vec::new();
        };
        match kind.as_str() {
            "message_start" => {
                if let Some(message) = event.get("message").and_then(Value::as_object) {
                    self.merge_messages_usage(message.get("usage"));
                }
                Vec::new()
            }
            "content_block_start" => {
                let Some(index) = event.get("index").and_then(Value::as_i64) else {
                    return Vec::new();
                };
                let Some(block) = event.get("content_block").and_then(Value::as_object) else {
                    return Vec::new();
                };
                let mut events = Vec::new();
                let slot = match block.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        let item = json!({
                            "type": "message",
                            "id": format!("msg_{}", random_hex(12)),
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        });
                        let output_index = self.next_index;
                        self.next_index += 1;
                        events.push((
                            "response.output_item.added".to_string(),
                            json!({
                                "type": "response.output_item.added",
                                "output_index": output_index,
                                "item": item,
                            }),
                        ));
                        BlockSlot::Text {
                            id: item["id"].as_str().unwrap_or_default().to_string(),
                            output_index,
                            text: String::new(),
                            done: false,
                        }
                    }
                    Some("tool_use") => {
                        let Some(name) = block
                            .get("name")
                            .and_then(Value::as_str)
                            .filter(|name| !name.is_empty())
                        else {
                            self.blocks.insert(index, None);
                            return events;
                        };
                        let call_id = block
                            .get("id")
                            .and_then(Value::as_str)
                            .filter(|id| !id.is_empty())
                            .map(str::to_string)
                            .unwrap_or_else(|| format!("call_{}", random_hex(12)));
                        let id = format!("fc_{}", random_hex(12));
                        let output_index = self.next_index;
                        self.next_index += 1;
                        events.push((
                            "response.output_item.added".to_string(),
                            json!({
                                "type": "response.output_item.added",
                                "output_index": output_index,
                                "item": {
                                    "type": "function_call",
                                    "id": id,
                                    "call_id": call_id,
                                    "name": name,
                                    "arguments": "",
                                    "status": "in_progress",
                                },
                            }),
                        ));
                        BlockSlot::Tool {
                            id,
                            call_id,
                            name: name.to_string(),
                            arguments: String::new(),
                            output_index,
                            done: false,
                        }
                    }
                    _ => {
                        self.blocks.insert(index, None);
                        return events;
                    }
                };
                self.blocks.insert(index, Some(slot));
                let Some(Some(slot)) = self.blocks.get_mut(&index) else {
                    return events;
                };
                match slot {
                    BlockSlot::Text {
                        id,
                        output_index,
                        text,
                        ..
                    } => {
                        if let Some(piece) = block
                            .get("text")
                            .and_then(Value::as_str)
                            .filter(|piece| !piece.is_empty())
                        {
                            text.push_str(piece);
                            events.push((
                                "response.output_text.delta".to_string(),
                                json!({
                                    "type": "response.output_text.delta",
                                    "item_id": id,
                                    "output_index": output_index,
                                    "content_index": 0,
                                    "delta": piece,
                                }),
                            ));
                        }
                    }
                    BlockSlot::Tool {
                        id,
                        output_index,
                        arguments,
                        ..
                    } => {
                        if let Some(input) = block.get("input").and_then(Value::as_object) {
                            if !input.is_empty() {
                                let piece = python_json_dumps(&Value::Object(input.clone()));
                                arguments.push_str(&piece);
                                events.push((
                                    "response.function_call_arguments.delta".to_string(),
                                    json!({
                                        "type": "response.function_call_arguments.delta",
                                        "item_id": id,
                                        "output_index": output_index,
                                        "delta": piece,
                                    }),
                                ));
                            }
                        }
                    }
                }
                events
            }
            "content_block_delta" => {
                let Some(index) = event.get("index").and_then(Value::as_i64) else {
                    return Vec::new();
                };
                let Some(Some(slot)) = self.blocks.get_mut(&index) else {
                    return Vec::new();
                };
                let Some(delta) = event.get("delta").and_then(Value::as_object) else {
                    return Vec::new();
                };
                match (delta.get("type").and_then(Value::as_str), slot) {
                    (
                        Some("text_delta"),
                        BlockSlot::Text {
                            id,
                            output_index,
                            text,
                            ..
                        },
                    ) => {
                        let Some(piece) = delta
                            .get("text")
                            .and_then(Value::as_str)
                            .filter(|piece| !piece.is_empty())
                        else {
                            return Vec::new();
                        };
                        text.push_str(piece);
                        vec![(
                            "response.output_text.delta".to_string(),
                            json!({
                                "type": "response.output_text.delta",
                                "item_id": id,
                                "output_index": output_index,
                                "content_index": 0,
                                "delta": piece,
                            }),
                        )]
                    }
                    (
                        Some("input_json_delta"),
                        BlockSlot::Tool {
                            id,
                            output_index,
                            arguments,
                            ..
                        },
                    ) => {
                        let Some(piece) = delta
                            .get("partial_json")
                            .and_then(Value::as_str)
                            .filter(|piece| !piece.is_empty())
                        else {
                            return Vec::new();
                        };
                        arguments.push_str(piece);
                        vec![(
                            "response.function_call_arguments.delta".to_string(),
                            json!({
                                "type": "response.function_call_arguments.delta",
                                "item_id": id,
                                "output_index": output_index,
                                "delta": piece,
                            }),
                        )]
                    }
                    _ => Vec::new(),
                }
            }
            "content_block_stop" => {
                let Some(index) = event.get("index").and_then(Value::as_i64) else {
                    return Vec::new();
                };
                self.done_event(index).into_iter().collect()
            }
            "message_delta" => {
                if let Some(stop_reason) = event
                    .get("delta")
                    .and_then(|delta| delta.get("stop_reason"))
                    .and_then(Value::as_str)
                {
                    self.stop_reason = Some(stop_reason.to_string());
                }
                self.merge_messages_usage(event.get("usage"));
                Vec::new()
            }
            "message_stop" => {
                self.saw_message_stop = true;
                self.finish_messages()
            }
            "error" => self.failed("upstream_error", "upstream stream failed"),
            _ => Vec::new(),
        }
    }

    fn merge_messages_usage(&mut self, update: Option<&Value>) {
        let Some(update) = update.and_then(Value::as_object) else {
            return;
        };
        let Some(usage) = self.usage.as_object_mut() else {
            return;
        };
        for (key, value) in update {
            if let Value::Object(value) = value {
                let mut current = usage
                    .get(key)
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                for (nested_key, nested_value) in value {
                    current.insert(nested_key.clone(), nested_value.clone());
                }
                usage.insert(key.clone(), Value::Object(current));
            } else {
                usage.insert(key.clone(), value.clone());
            }
        }
    }

    fn done_event(&mut self, index: i64) -> Option<(String, Value)> {
        let slot = self.blocks.get_mut(&index)?.as_mut()?;
        match slot {
            BlockSlot::Text {
                id,
                output_index,
                text,
                done,
            } => {
                if *done {
                    return None;
                }
                *done = true;
                let item = json!({
                    "type": "message",
                    "id": id,
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text,
                                  "annotations": []}],
                });
                self.output_items.insert(*output_index, item.clone());
                Some((
                    "response.output_item.done".to_string(),
                    json!({
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    }),
                ))
            }
            BlockSlot::Tool {
                id,
                call_id,
                name,
                arguments,
                output_index,
                done,
            } => {
                if *done {
                    return None;
                }
                *done = true;
                let item = json!({
                    "type": "function_call",
                    "id": id,
                    "call_id": call_id,
                    "name": name,
                    "arguments": if arguments.is_empty() { "{}" } else { arguments },
                    "status": "completed",
                });
                self.output_items.insert(*output_index, item.clone());
                Some((
                    "response.output_item.done".to_string(),
                    json!({
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    }),
                ))
            }
        }
    }

    fn finish_messages(&mut self) -> Vec<(String, Value)> {
        if !self.saw_message_stop {
            return self.failed(
                "upstream_eof_without_message_stop",
                "upstream stream ended without message_stop",
            );
        }
        let mut events = Vec::new();
        let indices: Vec<i64> = self.blocks.keys().copied().collect();
        for index in indices {
            if let Some(event) = self.done_event(index) {
                events.push(event);
            }
        }
        let (status, incomplete) = match self.stop_reason.as_deref() {
            Some("max_tokens") => ("incomplete", Some(json!({"reason": "max_output_tokens"}))),
            Some("refusal" | "sensitive") => {
                ("incomplete", Some(json!({"reason": "content_filter"})))
            }
            _ => ("completed", None),
        };
        let ordered: Vec<Value> = self.output_items.values().cloned().collect();
        let name = if incomplete.is_some() {
            "response.incomplete"
        } else {
            "response.completed"
        };
        let mut response = json!({
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": ordered,
            "usage": messages_usage(Some(&self.usage)),
        });
        if let Some(incomplete) = incomplete {
            response["incomplete_details"] = incomplete;
        }
        self.terminal = true;
        events.push((
            name.to_string(),
            json!({"type": name, "response": response}),
        ));
        events
    }
}

/// Python `json.dumps(value)` for one SSE tool-input object: default
/// separators and insertion order, so the emitted piece matches Python.
fn python_json_dumps(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let items = map
                .iter()
                .map(|(key, value)| {
                    format!(
                        "{}: {}",
                        serde_json::to_string(key).unwrap_or_default(),
                        python_json_dumps(value)
                    )
                })
                .collect::<Vec<_>>()
                .join(", ");
            format!("{{{items}}}")
        }
        Value::Array(items) => {
            let items = items
                .iter()
                .map(python_json_dumps)
                .collect::<Vec<_>>()
                .join(", ");
            format!("[{items}]")
        }
        value => serde_json::to_string(value).unwrap_or_default(),
    }
}
