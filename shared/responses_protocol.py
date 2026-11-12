# -*- coding: utf-8 -*-
"""Responses and Messages protocol translations shared by both gateways.

Pure request admission, request/response mapping and SSE event translation for the
OpenAI Responses and Anthropic Messages surfaces. Each call is independent: no
host, credential, broker or I/O state lives here.
"""

import json
import secrets
import time


MESSAGES_FIELDS = frozenset({
    "model", "messages", "max_tokens", "system", "stream",
    "temperature", "top_p", "top_k", "stop_sequences",
    "metadata", "tools", "tool_choice", "thinking"})
# OpenAI Responses is a THIRD request contract. chat2api now exposes
# /v1/responses (one translation onto its Chat Completions upstream), so the
# share validates this narrow subset and relays the ORIGINAL body to that route;
# it never keeps a second translation. Accepting the Harness defaults
# (store=false and its cache-routing hints) never asks for storage.
#
# Both field sets are frozensets: they decide request admission, so a mutable set
# would let any importer widen the accepted surface process-wide. A caller that
# needs a different surface passes ``allowed_fields`` to
# ``valid_responses_request`` instead of editing the module constant.
RESPONSES_FIELDS = frozenset({
    "model", "input", "instructions", "stream",
    "max_output_tokens", "temperature", "top_p", "tools",
    "tool_choice", "parallel_tool_calls", "store",
    "prompt_cache_key", "prompt_cache_retention",
    "prompt_cache_options", "service_tier",
    "reasoning", "include"})


def _responses_text(content):
    """Flatten Responses text parts to the text a chat message carries."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict) and part.get("type") in (
                "input_text", "output_text", "text"):
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def _responses_chat_content(content):
    """Responses message content -> chat content (text string or parts)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            text = part.get("text")
            if isinstance(text, str):
                parts.append({"type": "text", "text": text})
        elif kind == "input_image":
            image_url = part.get("image_url")
            if isinstance(image_url, str):
                parts.append({"type": "image_url",
                              "image_url": {"url": image_url}})
            elif isinstance(image_url, dict) and isinstance(
                    image_url.get("url"), str):
                parts.append({"type": "image_url",
                              "image_url": {"url": image_url["url"]}})
    if parts and all(part.get("type") == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts


def responses_to_chat_messages(data):
    """Translate a Responses input into the chat messages chat2api accepts.

    Function calls and their outputs become chat assistant tool_calls and tool
    messages; reasoning items carry no chat equivalent and are skipped.
    """
    messages = []
    instructions = data.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    source = data.get("input")
    if isinstance(source, str):
        messages.append({"role": "user", "content": source})
        return messages
    for item in source or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message" or (kind is None and "role" in item):
            role = item.get("role")
            if role in ("system", "developer"):
                role = "system"
            if role not in ("user", "assistant", "system"):
                continue
            messages.append({"role": role,
                             "content": _responses_chat_content(
                                 item.get("content"))})
        elif kind == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {})
            call = {"id": call_id, "type": "function",
                    "function": {"name": name, "arguments": arguments}}
            # Chat requires tool results to follow the assistant message that
            # declared their tool_calls. Responses carries each call as its own
            # item, so consecutive calls must share ONE assistant message or the
            # second call's result follows a message that never declared it.
            if messages and messages[-1].get("role") == "assistant":
                existing = messages[-1].get("tool_calls")
                if isinstance(existing, list):
                    existing.append(call)
                    continue
                if messages[-1].get("content") is None:
                    messages[-1]["tool_calls"] = [call]
                    continue
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": [call]})
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": _responses_text(item.get("output"))})
    return messages


def responses_to_chat_tools(tools):
    """Translate Responses function tools; built-in tools have no chat shape."""
    translated = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        function = {"name": name}
        if isinstance(tool.get("description"), str):
            function["description"] = tool["description"]
        if isinstance(tool.get("parameters"), dict):
            function["parameters"] = tool["parameters"]
        if isinstance(tool.get("strict"), bool):
            function["strict"] = tool["strict"]
        translated.append({"type": "function", "function": function})
    return translated


def responses_to_chat_tool_choice(value):
    """Translate Responses tool_choice into the chat request shape."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    if kind in ("auto", "none", "required"):
        return kind
    if kind == "function":
        name = value.get("name")
        if isinstance(name, str) and name:
            return {"type": "function", "function": {"name": name}}
    return None


def responses_to_chat_request(data):
    """Translate one Responses request onto the chat2api request contract."""
    body = {"model": data["model"],
            "messages": responses_to_chat_messages(data)}
    if data.get("stream") is True:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    max_output = data.get("max_output_tokens")
    if isinstance(max_output, int) and not isinstance(max_output, bool) \
            and max_output > 0:
        body["max_completion_tokens"] = max_output
    for name in ("temperature", "top_p"):
        value = data.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            body[name] = value
    tools = responses_to_chat_tools(data.get("tools"))
    if tools:
        body["tools"] = tools
    tool_choice = responses_to_chat_tool_choice(data.get("tool_choice"))
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if isinstance(data.get("parallel_tool_calls"), bool):
        body["parallel_tool_calls"] = data["parallel_tool_calls"]
    reasoning = data.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if isinstance(effort, str) and effort:
            body["reasoning_effort"] = effort
    return body


def valid_responses_request(data, allowed_fields=None):
    """Validate the narrow Responses subset the share translates.

    ``allowed_fields`` defaults to :data:`RESPONSES_FIELDS`. A caller may pass its
    own immutable set to validate against a different surface; the module constant
    is never modified.
    """
    if allowed_fields is None:
        allowed_fields = RESPONSES_FIELDS
    if not isinstance(data, dict) or set(data) - allowed_fields:
        return False
    if not isinstance(data.get("model"), str) or not data["model"]:
        return False
    source = data.get("input")
    if not isinstance(source, (str, list)) or not source:
        return False
    if not isinstance(data.get("stream", False), bool):
        return False
    if "store" in data and data["store"] is not False:
        return False
    max_output = data.get("max_output_tokens")
    if max_output is not None and (
            not isinstance(max_output, int) or isinstance(max_output, bool)
            or max_output <= 0):
        return False
    for name in ("temperature", "top_p"):
        value = data.get(name)
        if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)):
            return False
    if isinstance(data.get("instructions"), (int, float, bool, list, dict)):
        return False
    if isinstance(data.get("parallel_tool_calls"), (int, float, str, list, dict)):
        return False
    tools = data.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            return False
        for tool in tools:
            if not isinstance(tool, dict):
                return False
            if not isinstance(tool.get("type"), str) or not tool["type"]:
                return False
            if tool["type"] == "function" and (
                    not isinstance(tool.get("name"), str)
                    or not tool["name"]):
                return False
    reasoning = data.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        return False
    include = data.get("include")
    if include is not None and (
            not isinstance(include, list)
            or any(not isinstance(item, str) for item in include)):
        return False
    # An input whose items all translate to nothing would reach the chat
    # upstream as an empty messages array and fail there with a 400. Refuse it
    # here as the same unsupported request, before any upstream call.
    try:
        return bool(responses_to_chat_messages(data))
    except (TypeError, ValueError):
        return False


def _chat_usage(usage):
    def number(value):
        return value if isinstance(value, int) and not isinstance(
            value, bool) and value >= 0 else 0
    usage = usage if isinstance(usage, dict) else {}
    translated = {"input_tokens": number(usage.get("prompt_tokens")),
                  "output_tokens": number(usage.get("completion_tokens")),
                  "total_tokens": number(usage.get("total_tokens"))}
    # OpenAI Responses reports cache and reasoning detail separately; a chat
    # upstream carries the same facts nested, so keep them nested rather than
    # dropping them (the Harness subtracts cached tokens from input_tokens).
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        translated["input_tokens_details"] = {
            "cached_tokens": number(prompt_details.get("cached_tokens"))}
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        translated["output_tokens_details"] = {
            "reasoning_tokens": number(
                completion_details.get("reasoning_tokens"))}
    return translated


def chat_to_responses_response(payload, model, *, created_at=None,
                               response_id=None):
    """Translate a non-stream chat answer into a Responses object.

    ``created_at``/``response_id`` default to a fresh ``time``/``secrets`` value per
    call; a caller that needs a reproducible object (fixtures, replay) passes both
    and gets them back unchanged.
    """
    if not isinstance(payload, dict):
        raise ValueError("malformed chat payload")
    if isinstance(payload.get("error"), (dict, str)) and payload["error"]:
        raise ValueError("upstream explicit error")
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        raise ValueError("malformed chat message")
    output = []
    text = _responses_text(message.get("content"))
    if text:
        output.append({"type": "message", "id": "msg_" + secrets.token_hex(12),
                       "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": text,
                                    "annotations": []}]})
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        arguments = function.get("arguments")
        call_id = call.get("id")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {})
        if not isinstance(call_id, str) or not call_id:
            call_id = "call_" + secrets.token_hex(12)
        output.append({"type": "function_call",
                       "id": "fc_" + secrets.token_hex(12),
                       "call_id": call_id, "name": name,
                       "arguments": arguments, "status": "completed"})
    finish_reason = choice.get("finish_reason") \
        if isinstance(choice, dict) else None
    if finish_reason == "length":
        status, incomplete = "incomplete", {"reason": "max_output_tokens"}
    elif finish_reason == "content_filter":
        status, incomplete = "incomplete", {"reason": "content_filter"}
    else:
        status, incomplete = "completed", None
    translated = {"id": response_id if response_id is not None
                  else "resp_" + secrets.token_hex(12),
                  "object": "response",
                  "created_at": int(time.time()) if created_at is None
                  else created_at,
                  "status": status, "model": model, "output": output,
                  "usage": _chat_usage(payload.get("usage"))}
    if incomplete is not None:
        translated["incomplete_details"] = incomplete
    return translated


def _responses_image_block(image_url):
    """Translate one Responses image_url into an Anthropic image block."""
    if isinstance(image_url, dict):
        image_url = image_url.get("url")
    if not isinstance(image_url, str) or not image_url:
        return None
    if image_url.startswith("data:"):
        header, separator, data = image_url.partition(",")
        if not separator:
            return None
        meta = header[len("data:"):]
        media_type, _, encoding = meta.partition(";")
        if not media_type.startswith("image/") or encoding != "base64" \
                or not data:
            return None
        return {"type": "image",
                "source": {"type": "base64", "media_type": media_type,
                           "data": data}}
    if image_url.startswith(("http://", "https://")):
        return {"type": "image",
                "source": {"type": "url", "url": image_url}}
    return None


def _responses_text_blocks(content):
    """Responses message content -> Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    blocks = []
    for part in content:
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            text = part.get("text")
            if isinstance(text, str):
                blocks.append({"type": "text", "text": text})
        elif kind == "input_image":
            block = _responses_image_block(part.get("image_url"))
            if block is not None:
                blocks.append(block)
    return blocks


def _responses_output_parts(output):
    if isinstance(output, list):
        return output
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, list):
            return content
        return [output]
    return [output]


def _responses_tool_result_blocks(output):
    """Responses function_call_output -> Anthropic tool_result blocks."""
    blocks = []
    for part in _responses_output_parts(output):
        if isinstance(part, str):
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            text = part.get("text")
            if isinstance(text, str):
                blocks.append({"type": "text", "text": text})
        elif kind == "input_image":
            block = _responses_image_block(part.get("image_url"))
            if block is not None:
                blocks.append(block)
    return blocks


def _anthropic_tool_input(arguments):
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except (ValueError, UnicodeError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def responses_to_messages_tool_choice(value):
    """Translate Responses tool_choice into the Anthropic shape."""
    if isinstance(value, str):
        if value == "auto":
            return {"type": "auto"}
        if value == "none":
            return {"type": "none"}
        if value == "required":
            return {"type": "any"}
        return None
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "function":
            name = value.get("name")
            if isinstance(name, str) and name:
                return {"type": "tool", "name": name}
        if kind in ("auto", "none", "any"):
            return {"type": kind}
    return None


def responses_requires_messages(data):
    """True when a tool result carries an image Chat cannot represent."""
    for item in data.get("input") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call_output":
            continue
        for part in _responses_output_parts(item.get("output")):
            if isinstance(part, dict) and part.get("type") == "input_image":
                return True
    return False


def responses_to_messages_request(data):
    """Translate the Responses subset onto Anthropic Messages.

    This is the image-capable branch: tool_result content may carry image
    blocks, which Chat Completions cannot represent.
    """
    system_parts = []
    messages = []

    def append_message(role, blocks):
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            last = messages[-1]
            has_tool_result = role == "user" and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in last["content"])
            if has_tool_result:
                messages.append({"role": role, "content": blocks})
            else:
                last["content"].extend(blocks)
            return
        messages.append({"role": role, "content": blocks})

    instructions = data.get("instructions")
    if isinstance(instructions, str) and instructions:
        system_parts.append(instructions)
    for item in data.get("input") or []:
        if isinstance(item, str):
            append_message("user", [{"type": "text", "text": item}])
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message" or (kind is None and "role" in item):
            role = item.get("role")
            if role in ("system", "developer"):
                text = _responses_text(item.get("content"))
                if text:
                    system_parts.append(text)
                continue
            if role not in ("user", "assistant"):
                continue
            append_message(role, _responses_text_blocks(item.get("content")))
        elif kind == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            append_message("assistant", [{
                "type": "tool_use", "id": call_id, "name": name,
                "input": _anthropic_tool_input(item.get("arguments"))}])
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            blocks = _responses_tool_result_blocks(item.get("output"))
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            append_message("user", [{
                "type": "tool_result", "tool_use_id": call_id,
                "content": blocks}])
    body = {"model": data["model"], "messages": messages,
            "max_tokens": data.get("max_output_tokens") or 32768}
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if data.get("stream") is True:
        body["stream"] = True
    for name in ("temperature", "top_p"):
        value = data.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            body[name] = value
    tools = []
    for tool in data.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        definition = {"name": name}
        if isinstance(tool.get("description"), str):
            definition["description"] = tool["description"]
        if isinstance(tool.get("parameters"), dict):
            definition["input_schema"] = tool["parameters"]
        tools.append(definition)
    if tools:
        body["tools"] = tools
    tool_choice = responses_to_messages_tool_choice(data.get("tool_choice"))
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def _messages_usage(usage):
    def number(value):
        return value if isinstance(value, int) and not isinstance(
            value, bool) and value >= 0 else 0
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = number(usage.get("input_tokens"))
    cache_read = number(usage.get("cache_read_input_tokens"))
    cache_write = number(usage.get("cache_creation_input_tokens"))
    output_tokens = number(usage.get("output_tokens"))
    total_input = input_tokens + cache_read + cache_write
    translated = {"input_tokens": total_input,
                  "output_tokens": output_tokens,
                  "total_tokens": total_input + output_tokens}
    if cache_read:
        translated["input_tokens_details"] = {"cached_tokens": cache_read}
    return translated


def messages_to_responses_response(payload, model, *, created_at=None,
                                   response_id=None):
    """Translate a non-stream Anthropic message into a Responses object.

    ``created_at``/``response_id`` behave as in
    :func:`chat_to_responses_response`: generated per call by default, used
    verbatim when the caller injects them.
    """
    if not isinstance(payload, dict):
        raise ValueError("malformed messages payload")
    if isinstance(payload.get("error"), (dict, str)) and payload["error"]:
        raise ValueError("upstream explicit error")
    output = []
    texts = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif kind == "tool_use":
            name = block.get("name")
            call_id = block.get("id")
            if not isinstance(name, str) or not name:
                continue
            arguments = block.get("input")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments or {})
            if not isinstance(call_id, str) or not call_id:
                call_id = "call_" + secrets.token_hex(12)
            output.append({"type": "function_call",
                           "id": "fc_" + secrets.token_hex(12),
                           "call_id": call_id, "name": name,
                           "arguments": arguments, "status": "completed"})
    if texts:
        output.insert(0, {"type": "message",
                          "id": "msg_" + secrets.token_hex(12),
                          "status": "completed", "role": "assistant",
                          "content": [{"type": "output_text",
                                       "text": "\n".join(texts),
                                       "annotations": []}]})
    stop_reason = payload.get("stop_reason")
    if stop_reason == "max_tokens":
        status, incomplete = "incomplete", {"reason": "max_output_tokens"}
    elif stop_reason in ("refusal", "sensitive"):
        status, incomplete = "incomplete", {"reason": "content_filter"}
    else:
        status, incomplete = "completed", None
    translated = {"id": response_id if response_id is not None
                  else "resp_" + secrets.token_hex(12),
                  "object": "response",
                  "created_at": int(time.time()) if created_at is None
                  else created_at,
                  "status": status, "model": model, "output": output,
                  "usage": _messages_usage(payload.get("usage"))}
    if incomplete is not None:
        translated["incomplete_details"] = incomplete
    return translated


def _sse_lines(response):
    """Yield stripped SSE lines from line iterators or raw byte chunks."""
    if hasattr(response, "iter_lines"):
        for raw in response.iter_lines():
            if raw is None:
                continue
            text = raw.decode("utf-8", "replace") \
                if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                line = line.strip()
                if line:
                    yield line
        return
    buffer = ""
    for raw in response:
        text = raw.decode("utf-8", "replace") \
            if isinstance(raw, bytes) else str(raw)
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if line:
                yield line
    line = buffer.strip()
    if line:
        yield line


def messages_stream_events(response, model, response_id, created_at):
    """Yield (event_name, payload) translating Anthropic SSE to Responses."""
    yield "response.created", {
        "type": "response.created",
        "response": {"id": response_id, "object": "response",
                     "created_at": created_at,
                     "status": "in_progress", "model": model,
                     "output": []}}
    blocks = {}
    output_items = {}
    next_index = 0
    usage = {}
    stop_reason = None
    saw_message_stop = False

    def merge_usage(update):
        if not isinstance(update, dict):
            return
        for key, value in update.items():
            if isinstance(value, dict):
                current = usage.get(key)
                if not isinstance(current, dict):
                    current = {}
                current.update(value)
                usage[key] = current
            else:
                usage[key] = value

    def create_slot(block_index, block):
        nonlocal next_index
        kind = block.get("type")
        if kind == "text":
            item = {"type": "message",
                    "id": "msg_" + secrets.token_hex(12),
                    "status": "in_progress", "role": "assistant",
                    "content": []}
            slot = {"kind": "text", "item": item,
                    "output_index": next_index, "text": "", "done": False}
            next_index += 1
            output_items[slot["output_index"]] = item
            blocks[block_index] = slot
            return ("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": slot["output_index"], "item": item})
        if kind == "tool_use":
            name = block.get("name")
            if not isinstance(name, str) or not name:
                blocks[block_index] = None
                return None
            call_id = block.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = "call_" + secrets.token_hex(12)
            item = {"type": "function_call",
                    "id": "fc_" + secrets.token_hex(12),
                    "call_id": call_id, "name": name,
                    "arguments": "", "status": "in_progress"}
            slot = {"kind": "tool", "item": item,
                    "output_index": next_index, "arguments": "",
                    "done": False}
            next_index += 1
            output_items[slot["output_index"]] = item
            blocks[block_index] = slot
            return ("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": slot["output_index"], "item": item})
        blocks[block_index] = None
        return None

    def done_event(slot):
        if slot["done"]:
            return None
        slot["done"] = True
        if slot["kind"] == "text":
            item = {"type": "message", "id": slot["item"]["id"],
                    "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text",
                                 "text": slot["text"],
                                 "annotations": []}]}
        else:
            item = {"type": "function_call", "id": slot["item"]["id"],
                    "call_id": slot["item"]["call_id"],
                    "name": slot["item"]["name"],
                    "arguments": slot["arguments"] or "{}",
                    "status": "completed"}
        output_items[slot["output_index"]] = item
        return ("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": slot["output_index"], "item": item})

    event_name = None
    for line in _sse_lines(response):
        if line.startswith("event:"):
            event_name = line[len("event:"):].strip()
            continue
        if not line.startswith("data:"):
            continue
        value = line[len("data:"):].strip()
        if value == "[DONE]":
            break
        try:
            event = json.loads(value)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type") or event_name
        event_name = None
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                merge_usage(message.get("usage"))
        elif kind == "content_block_start":
            index = event.get("index")
            block = event.get("content_block")
            if not isinstance(index, int) or not isinstance(block, dict):
                continue
            added = create_slot(index, block)
            if added is not None:
                yield added
            slot = blocks.get(index)
            if slot is None:
                continue
            if slot["kind"] == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    slot["text"] += text
                    yield "response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": slot["item"]["id"],
                        "output_index": slot["output_index"],
                        "content_index": 0, "delta": text}
            else:
                initial = block.get("input")
                if isinstance(initial, dict) and initial:
                    piece = json.dumps(initial)
                    slot["arguments"] += piece
                    yield "response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": slot["item"]["id"],
                        "output_index": slot["output_index"],
                        "delta": piece}
        elif kind == "content_block_delta":
            index = event.get("index")
            slot = blocks.get(index) if isinstance(index, int) else None
            if slot is None:
                continue
            delta = event.get("delta")
            if not isinstance(delta, dict):
                continue
            delta_type = delta.get("type")
            if delta_type == "text_delta" and slot["kind"] == "text":
                piece = delta.get("text")
                if isinstance(piece, str) and piece:
                    slot["text"] += piece
                    yield "response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "item_id": slot["item"]["id"],
                        "output_index": slot["output_index"],
                        "content_index": 0, "delta": piece}
            elif delta_type == "input_json_delta" \
                    and slot["kind"] == "tool":
                piece = delta.get("partial_json")
                if isinstance(piece, str) and piece:
                    slot["arguments"] += piece
                    yield "response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": slot["item"]["id"],
                        "output_index": slot["output_index"],
                        "delta": piece}
        elif kind == "content_block_stop":
            index = event.get("index")
            slot = blocks.get(index) if isinstance(index, int) else None
            if slot is None:
                continue
            done = done_event(slot)
            if done is not None:
                yield done
        elif kind == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and isinstance(
                    delta.get("stop_reason"), str):
                stop_reason = delta["stop_reason"]
            merge_usage(event.get("usage"))
        elif kind == "message_stop":
            saw_message_stop = True
            break
        elif kind == "error":
            yield "response.failed", {
                "type": "response.failed",
                "response": {"id": response_id, "object": "response",
                             "created_at": created_at,
                             "status": "failed", "model": model,
                             "output": [],
                             "error": {"code": "upstream_error",
                                       "message": "upstream stream failed"}}}
            return
    if not saw_message_stop:
        yield "response.failed", {
            "type": "response.failed",
            "response": {"id": response_id, "object": "response",
                         "created_at": created_at,
                         "status": "failed", "model": model,
                         "output": [],
                         "error": {"code": "upstream_eof_without_message_stop",
                                   "message": "upstream stream ended "
                                              "without message_stop"}}}
        return
    for index in sorted(blocks):
        slot = blocks[index]
        if slot is None:
            continue
        done = done_event(slot)
        if done is not None:
            yield done
    if stop_reason == "max_tokens":
        status, incomplete = "incomplete", {"reason": "max_output_tokens"}
    elif stop_reason in ("refusal", "sensitive"):
        status, incomplete = "incomplete", {"reason": "content_filter"}
    else:
        status, incomplete = "completed", None
    ordered = [output_items[index] for index in sorted(output_items)]
    event_name = "response.incomplete" if incomplete else "response.completed"
    payload = {"type": event_name,
               "response": {"id": response_id, "object": "response",
                            "created_at": created_at, "status": status,
                            "model": model, "output": ordered,
                            "usage": _messages_usage(usage)}}
    if incomplete is not None:
        payload["response"]["incomplete_details"] = incomplete
    yield event_name, payload


def responses_stream_events(response, model, response_id, created_at):
    """Yield ``(event_name, payload)`` translating chat SSE into Responses.

    A stream that ends without ``[DONE]`` yields ``response.failed`` instead of a
    fabricated completion; a ``length``/``content_filter`` finish reason yields
    ``response.incomplete``; function calls keep their arrival order in
    ``output_index`` and are finalized even when their arguments are empty.
    """
    yield "response.created", {
        "type": "response.created",
        "response": {"id": response_id, "object": "response",
                     "created_at": created_at,
                     "status": "in_progress", "model": model,
                     "output": []}}
    message_id = "msg_" + secrets.token_hex(12)
    text = ""
    message_added = False
    message_index = None
    next_index = 0
    tool_calls = {}
    output_items = {}
    usage = {}
    done_seen = False
    finish_reason = None

    def message_item():
        return {"type": "message", "id": message_id,
                "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": text,
                             "annotations": []}]}

    def add_message():
        nonlocal message_added, message_index, next_index
        if message_added:
            return None
        message_added = True
        message_index = next_index
        next_index += 1
        return ("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": message_index,
            "item": {"type": "message", "id": message_id,
                     "status": "in_progress", "role": "assistant",
                     "content": []}})

    def add_tool(slot):
        nonlocal next_index
        if slot["added"]:
            return None
        slot["added"] = True
        slot["output_index"] = next_index
        next_index += 1
        return ("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": slot["output_index"],
            "item": {"type": "function_call", "id": slot["id"],
                     "call_id": slot["call_id"] or slot["id"],
                     "name": slot["name"], "arguments": "",
                     "status": "in_progress"}})

    for line in _sse_lines(response):
        if not line.startswith("data:"):
            continue
        value = line[len("data:"):].strip()
        if value == "[DONE]":
            done_seen = True
            break
        try:
            chunk = json.loads(value)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(chunk, dict):
            continue
        if isinstance(chunk.get("error"), dict):
            yield "response.failed", {
                "type": "response.failed",
                "response": {"id": response_id, "object": "response",
                             "created_at": created_at,
                             "status": "failed", "model": model,
                             "output": [],
                             "error": {"code": "upstream_error",
                                       "message": "upstream stream failed"}}}
            return
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, dict):
            usage = chunk_usage
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            choice_finish = choice.get("finish_reason")
            if isinstance(choice_finish, str):
                finish_reason = choice_finish
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            piece = _responses_text(delta.get("content"))
            if piece:
                event = add_message()
                if event is not None:
                    yield event
                text += piece
                yield "response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": message_id, "output_index": message_index,
                    "content_index": 0, "delta": piece}
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                index = call.get("index", 0)
                if not isinstance(index, int) or isinstance(index, bool) \
                        or index < 0:
                    index = 0
                slot = tool_calls.setdefault(index, {
                    "id": "fc_" + secrets.token_hex(12),
                    "call_id": "", "name": "", "arguments": "",
                    "sent": 0, "added": False, "output_index": None})
                if isinstance(call.get("id"), str) and call["id"]:
                    slot["call_id"] = call["id"]
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                if isinstance(function.get("name"), str) \
                        and function["name"]:
                    slot["name"] = function["name"]
                arguments = function.get("arguments")
                if not isinstance(arguments, str) or not arguments:
                    continue
                slot["arguments"] += arguments
                if not slot["name"]:
                    continue
                event = add_tool(slot)
                if event is not None:
                    yield event
                pending = slot["arguments"][slot["sent"]:]
                slot["sent"] = len(slot["arguments"])
                if pending:
                    yield "response.function_call_arguments.delta", {
                        "type": "response.function_call_arguments.delta",
                        "item_id": slot["id"],
                        "output_index": slot["output_index"],
                        "delta": pending}
    if not done_seen:
        yield "response.failed", {
            "type": "response.failed",
            "response": {"id": response_id, "object": "response",
                         "created_at": created_at,
                         "status": "failed", "model": model,
                         "output": [],
                         "error": {"code": "upstream_eof_without_done",
                                   "message": "upstream stream ended "
                                              "without [DONE]"}}}
        return
    if message_added:
        item = message_item()
        output_items[message_index] = item
        yield "response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": message_index, "item": item}
    for index in sorted(tool_calls):
        slot = tool_calls[index]
        if not slot["name"]:
            continue
        event = add_tool(slot)
        if event is not None:
            yield event
        item = {"type": "function_call", "id": slot["id"],
                "call_id": slot["call_id"] or slot["id"],
                "name": slot["name"], "arguments": slot["arguments"],
                "status": "completed"}
        output_items[slot["output_index"]] = item
        yield "response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": slot["output_index"], "item": item}
    if finish_reason == "length":
        status, incomplete = "incomplete", {"reason": "max_output_tokens"}
    elif finish_reason == "content_filter":
        status, incomplete = "incomplete", {"reason": "content_filter"}
    else:
        status, incomplete = "completed", None
    ordered = [output_items[index] for index in sorted(output_items)]
    event_name = "response.incomplete" if incomplete else "response.completed"
    payload = {"type": event_name,
               "response": {"id": response_id, "object": "response",
                            "created_at": created_at, "status": status,
                            "model": model, "output": ordered,
                            "usage": _chat_usage(usage)}}
    if incomplete is not None:
        payload["response"]["incomplete_details"] = incomplete
    yield event_name, payload


